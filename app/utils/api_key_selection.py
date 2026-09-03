from typing import List, Optional

import app.config.settings as settings
from app.utils.logging import log
from app.utils.stats import (
    get_api_key_usage,
    get_calls_last_minute_for_key,
    MAX_OUTBOUND_RPM,
    OUTBOUND_RPM_BACKOFF_FRACTION,
)
from app.utils.api_key import is_key_cooled_down


def _try_import_pvp():
    """延迟加载 PVP 模块（缺位/不可导入时返回 None，行为回退默认轮换）。

    为什么不顶层导入：本模块会被多个假环境测试（round6/round7）以
    最小依赖集加载，顶层 import 会引入对 app.utils.pvp 的硬依赖。
    PVP 是可选增强（Round 8），选 key 路径必须在其缺位时保持原行为。
    """
    try:
        import app.utils.pvp as pvp

        return pvp
    except Exception:
        return None


def effective_max_retries() -> int:
    """单个请求的重试预算：PVP 模式用 PVP_MAX_RETRIES，否则全局上限。

    PVP 把整批重试钉在同一个 key 上（每批 1 个），总尝试次数由
    PVP_MAX_RETRIES 单独控制，避免复用面向多 key 轮换的
    MAX_RETRY_NUM（PVP 下它语义变成"重试多少次"而非"轮几个 key"，
    默认值也可能过大/过小）。
    """
    pvp = _try_import_pvp()
    if pvp is not None and pvp.is_pvp_enabled():
        return pvp.get_pvp_max_retries()
    return getattr(settings, "MAX_RETRY_NUM", 15)


def _key_hash(api_key: str) -> str:
    return f"key#{hash(api_key) & 0xFFFFFF:06x}"


async def select_valid_api_keys(
    key_manager,
    batch_num: int,
    request_type: str,
    model: str,
    preferred_keys: Optional[List[str]] = None,
) -> List[str]:
    """选择当前批次可用的 API keys。

    Hardening vs. old behaviour:
    1. 跳过被冷却的 key（Round 7 起：仅 429 配额耗尽与 401/403 密钥失效）
    2. 跳过 RPM 即将达到上限的 key（基于过去 60s 计数）
    3. 不再在所有 key 都达日额度时强行重置栈取一个出来用——
       这等于绕过自设的日额度上限，会触发上游 RPM 限制。
       现在返回空列表，让调用方走"所有 key 都不可用"路径。

    Round 7（key 亲和重试）: 新增 preferred_keys 参数 —— 上一批因
    **非配额耗尽**（500/503/网络错误/空响应）失败的 key 列表。这些
    key 会被优先重新选中（经过冷却/RPM/日额度三重校验后），实现
    "只有配额耗尽才换 key，其他情况一律用原 key 重试"：
      * preferred 中有 key 可用 → 本批全部由 preferred 组成
        （不额外轮换其他 key，不白白消耗它们的 RPM）；
      * preferred 全部不可用（如期间被并发请求打成 429）→
        回落到正常选择路径，行为与不传 preferred 一致。
    fill（粘滞）模式下 preferred 通常就是栈顶 key，行为天然一致；
    polling 轮换模式下本机制是同 key 重试的唯一保证（否则每次
    选 key 都会 pop 到下一个）。

    语义说明（fill 模式）: get_available_key() 在 fill（粘性）模式下
    恒返回同一栈顶 key，本函数的 checked_keys 去重会让循环在第二个
    迭代即 break——即 fill 模式下每批恒为 1 个 key。这是有意的反指纹
    设计（"粘住一个 key 直到不可用"），并非 bug：同一请求并发打到同一
    key 多次只会白白消耗该 key 的 RPM 配额。需要多 key 并发竞速时请
    切换 polling 轮换策略。

    Round 6 bug fix（fill 模式 RPM 死锁）: 此前粘滞 key 因 RPM 达到
    退避阈值被 `continue` 跳过后，下一轮 `get_available_key()` 仍然
    返回**同一个**栈顶 key（fill 模式 peek 不 pop），checked_keys 去重
    立即 break → valid_keys 为空 → 客户端直接收到"所有密钥失败"，
    而池里其余健康 key 一个都没被尝试；日志还误导性地记录
    "all available keys were tried"。修复：RPM 跳过时调用
    advance_sticky_key() 把该 key 弹出粘滞位（全局粘性转移到下一个
    key），让本轮循环能继续评估后续 key。被弹出的 key 会在栈空重置
    时自然回归——这与"冷却/日额度导致的轮换"语义完全一致。

    Round 8（PVP 模式）: 运维在面板开启 PVP 并指定 key 后，本函数
    在亲和/轮换之前直接返回 [钉住 key]，冷却/RPM/日额度三重校验
    全部跳过（这正是 PVP 的语义：钉住一个 key 持续重试直到出结果）。
    唯二例外：钉住 key 已死（401/403）→ 返回空列表提前终止；选择器
    无法解析 → 回落到正常轮换（warn-once，不 fail-closed）。
    """
    # ------------------------------------------------------------------
    # Round 8: PVP 模式 —— 钉住指定 key，优先级高于一切轮换/亲和逻辑。
    # ------------------------------------------------------------------
    pvp = _try_import_pvp()
    if pvp is not None and pvp.is_pvp_enabled():
        pinned = pvp.resolve_pvp_key(key_manager)
        if pinned is not None:
            if pvp.is_pvp_key_dead(pinned):
                pvp.log_dead_key_abort(pinned)
                return []
            return [pinned]
        # 解析失败：resolve_pvp_key 内部已 warn-once，此处回落到
        # 亲和 + 轮换的既有路径，绝不因 PVP 配置问题拒绝服务。

    valid_keys: List[str] = []
    # 本轮已入选 valid_keys 的 key（亲和 + 轮换共用，防重复入选）
    selected_keys = set()
    # 轮换循环的环检测：仅记录 get_available_key 返回过的 key。
    # 注意不能与亲和阶段共用 —— 亲和阶段被跳过的 key（如刚被打成
    # 429）若混入环检测集，正常轮换循环一碰到它就会误判"已试完全
    # 部 key"而提前 break，池内其余健康 key 全部被无视。
    rotation_tried = set()

    rpm_threshold = max(1, int(MAX_OUTBOUND_RPM * OUTBOUND_RPM_BACKOFF_FRACTION))

    # ------------------------------------------------------------------
    # Round 7: key 亲和阶段 —— 优先重用上一批非配额耗尽失败的 key。
    # ------------------------------------------------------------------
    if preferred_keys:
        for api_key in preferred_keys[:batch_num]:
            if api_key in selected_keys:
                continue

            # 三重校验与正常路径完全一致：期间被并发请求冷却（429）、
            # RPM 达阈值（分钟级配额耗尽）、日额度耗尽 → 都属于
            # "配额耗尽"，放弃亲和、正常轮换。
            if await is_key_cooled_down(api_key):
                continue
            try:
                last_minute = get_calls_last_minute_for_key(api_key)
            except Exception:
                last_minute = 0
            if last_minute >= rpm_threshold:
                log(
                    "info",
                    f"{_key_hash(api_key)} at {last_minute}/{MAX_OUTBOUND_RPM} RPM, "
                    "affinity retry yields to RPM limit",
                    extra={
                        "key": "key#" + str(hash(api_key) & 0xFFFFFF),
                        "request_type": request_type,
                        "model": model,
                    },
                )
                continue
            usage = await get_api_key_usage(settings.api_call_stats, api_key)
            if usage >= settings.API_KEY_DAILY_LIMIT:
                continue
            valid_keys.append(api_key)
            selected_keys.add(api_key)

        if valid_keys:
            log(
                "info",
                "key affinity retry: reusing "
                + ", ".join(_key_hash(k) for k in valid_keys)
                + " (transient failure, quota not exhausted)",
                extra={"request_type": request_type, "model": model},
            )
            # 整批都由 preferred 组成：不额外轮换其他 key。
            return valid_keys
        # preferred 全部不可用 → 回落到正常选择路径（下方）。

    while len(valid_keys) < batch_num:
        api_key = await key_manager.get_available_key()
        if not api_key:
            break

        if api_key in rotation_tried:
            # get_available_key 返回了本轮已评估过的 key → 已绕池一
            # 周，不再死循环（polling 模式下栈空重洗；fill 模式下栈顶
            # 不变）。
            break

        rotation_tried.add(api_key)

        # 亲和阶段已入选的 key 不重复入选（例如 fill 模式下轮换栈顶
        # 就是 preferred key 本身）。
        if api_key in selected_keys:
            continue

        # Skip keys on cooldown (Round 7 起：429 配额耗尽与 401/403 密钥失效)。
        if await is_key_cooled_down(api_key):
            # 冷却中的 key 在 fill 模式下会被 get_available_key 内部
            # 自行 pop（见 _get_available_key_fill），此处 continue 后
            # 下一轮拿到的是新栈顶，无需额外处理。
            continue

        # Skip keys whose last-minute call count is at or above the RPM
        # safety threshold (avoid hitting upstream RPM cap).
        try:
            last_minute = get_calls_last_minute_for_key(api_key)
        except Exception:
            last_minute = 0
        if last_minute >= rpm_threshold:
            log(
                "info",
                f"key#{hash(api_key) & 0xFFFFFF:06x} at {last_minute}/{MAX_OUTBOUND_RPM} RPM in last 60s, skipping",
                extra={
                    "key": "key#" + str(hash(api_key) & 0xFFFFFF),
                    "request_type": request_type,
                    "model": model,
                },
            )
            # Round 6 fix: 让出粘滞位，使本轮能尝试后续健康 key。
            # （polling 模式下 get_available_key 本身就是 pop 语义，
            #  advance 是 no-op。）
            advance = getattr(key_manager, "advance_sticky_key", None)
            if advance is not None:
                await advance()
            continue

        usage = await get_api_key_usage(settings.api_call_stats, api_key)
        if usage < settings.API_KEY_DAILY_LIMIT:
            valid_keys.append(api_key)
            continue

        log(
            "warning",
            f"key#{hash(api_key) & 0xFFFFFF:06x} exceeded daily limit ({usage}/{settings.API_KEY_DAILY_LIMIT})",
            extra={
                "key": "key#" + str(hash(api_key) & 0xFFFFFF),
                "request_type": request_type,
                "model": model,
            },
        )
        # 日额度已满的 key 同样让出粘滞位（fill 模式下 get_available_key
        # 的下一次调用也会 pop 它，这里显式 advance 保证本轮立即生效）。
        advance = getattr(key_manager, "advance_sticky_key", None)
        if advance is not None:
            await advance()

    if not valid_keys:
        log(
            "warning",
            "No API keys available (all on cooldown, RPM-limited, or daily-limited)",
            extra={"request_type": request_type, "model": model},
        )

    return valid_keys
