"""PVP 模式（Round 8）：指定单个 key 持续重试直到出结果。

语义（用户策略）：
  正常模式下密钥池按 fill/polling 策略轮换，冷却（429/401/403）、
  RPM 阈值、日额度任一命中都会让出当前 key。PVP 模式则把请求"钉"
  在指定的那一个 key 上：上述三重校验全部不构成换 key 的理由，失败
  后带退避用原 key 重试，直到拿到有效输出或达到 PVP_MAX_RETRIES
  次数上限（防止无限重试，UI 可配置）。

  两个安全阀（防止无意义烧请求）：
  1. 密钥已死（最近一次失败为 401/403）→ 立即终止并给出明确日志。
     重试一个已失效的 key 永远不可能成功；重新运行"密钥检测"或重启
     进程即可复位（检测完成时会清空失败类别记录）。
  2. PVP_MAX_RETRIES 次数上限（默认 50，最少 1）。

  429（配额耗尽）不终止 PVP —— 这正是 PVP 的核心场景：等该 key 的
  分钟级配额恢复后立刻继续。为避免在必然 429 的窗口里空烧请求，
  批间退避会参考该 key 的冷却剩余时间（即上游 retryDelay，由
  mark_key_failure 写入冷却表），在 SSE 安全上限（8s，与批间退避
  cap 一致）内抬高等待；空响应上限（MAX_EMPTY_RESPONSES）与批间
  full-jitter 退避等既有防风暴机制全部照常生效。

本模块是可选增强层：所有接入点（api_key_selection / api_key /
retry_state / 重试循环）都以"延迟导入 + 异常回退"方式使用本模块，
本模块缺位时全部路径保持 Round 7 原行为，绝不 fail-closed。

选择器（settings.PVP_KEY）支持四种写法（见 resolve_pvp_key）：
  1. 完整密钥（保存时自动脱敏为尾片段，避免明文落盘）
  2. "#3" 或 "3"          —— 密钥池序号（0 起，跨重启稳定）
  3. "key#1a2b3c"         —— 面板/日志中的哈希标识（仅当前进程内稳定）
  4. 密钥尾片段（>=4 位）  —— 与池内唯一子串匹配的密钥（跨重启稳定）
"""
from typing import Optional

import app.config.settings as settings
from app.utils.logging import log

# PVP 模式下批间退避的抬高上限（秒）。与 compute_inter_batch_backoff
# 的 cap 对齐：SSE/keepalive 路径的静默窗口不能因 PVP 而显著变长。
PVP_BACKOFF_CAP_S = 8.0

# PVP_MAX_RETRIES 的缺省与解析兜底值
DEFAULT_PVP_MAX_RETRIES = 50

# 尾片段匹配的最短长度：低于该值误匹配概率过高
MIN_FRAGMENT_LEN = 4

# 最近一次成功解析出的钉住 key（进程内）。供 elevate_backoff 读取
# 冷却剩余时间，避免把重试预算砸在必然 429 的窗口里。单事件循环
# 内并发请求全部钉在同一个 key 上，共享该状态是安全的。
_last_resolved_key: Optional[str] = None

# warn-once 去重：同一个选择器只告警一次，防止每批重试刷屏日志面板
# （Round 5 已有过日志洪水治理，这里从源头避免）。
_warned_selectors: set = set()

# 最近一次成功钉住并已写日志的 key：仅在钉住对象变化时打一条 info
# （确认激活/切换，又不随每批重试刷屏）。
_last_logged_pin: Optional[str] = None


def _key_id(api_key: str) -> str:
    return f"key#{hash(api_key) & 0xFFFFFF:06x}"


def _pvp_key_selector() -> str:
    return str(getattr(settings, "PVP_KEY", "") or "").strip()


def is_pvp_enabled() -> bool:
    """PVP 模式是否实际生效：开关打开 且 选择器非空。"""
    return bool(getattr(settings, "PVP_MODE", False)) and bool(_pvp_key_selector())


def get_pvp_max_retries() -> int:
    """PVP 模式下的单请求最大重试次数（最少 1，解析失败回退默认值）。"""
    try:
        value = int(getattr(settings, "PVP_MAX_RETRIES", DEFAULT_PVP_MAX_RETRIES))
    except (TypeError, ValueError):
        return DEFAULT_PVP_MAX_RETRIES
    return max(1, value)


def sanitize_pvp_selector(raw: str) -> str:
    """把用户输入的 PVP 选择器规范化为可持久化、可回显的安全形态。

    规则：短选择器（<20 字符，或含 '#' 的序号/哈希写法）原样保留；
    长的纯 token（很可能是完整密钥）一律只保留尾部 6 位片段——
      * settings.json（ENABLE_STORAGE 开启时）永不落盘明文密钥，
        与 EXCLUDED_SETTINGS 排除 GEMINI_API_KEYS 的安全决策一致；
      * 尾片段是跨重启稳定的标识（不受密钥池顺序影响）；
      * GET 配置回显复用同一函数，完整密钥不会经由 API 泄露。
    """
    selector = str(raw or "").strip()
    if not selector:
        return ""
    if len(selector) < 20 or "#" in selector:
        return selector
    # 长 token：视为完整密钥，脱敏为尾片段
    return selector[-6:]


def _resolve_by_hash(pool: list, suffix: str) -> Optional[str]:
    """按 "key#xxxx" 哈希标识匹配。

    兼容两种既有格式：stats 面板用十进制（"key#" + str(hash & 0xFFFFFF)），
    日志用十六进制（f"key#{...:06x}"）。纯数字后缀优先按十进制（面板
    形态）匹配，未命中再按十六进制；含字母的后缀只按十六进制。
    """
    suffix = suffix.strip().lower()
    candidates = []
    if suffix.isdigit():
        candidates.append(int(suffix, 10))
    try:
        hex_value = int(suffix, 16)
        if hex_value not in candidates:
            candidates.append(hex_value)
    except ValueError:
        pass
    for target in candidates:
        for api_key in pool:
            if hash(api_key) & 0xFFFFFF == target:
                return api_key
    return None


def resolve_pvp_key(key_manager, *, force: bool = False) -> Optional[str]:
    """把 PVP_KEY 选择器解析为密钥池中的真实密钥。

    force=True 供配置接口在 PVP_MODE 尚未开启时也能预校验选择器
    （先存 key 再开模式的操作顺序）。默认（False）下 PVP 未启用直接
    返回 None。

    返回 None 表示"PVP 未启用 / 选择器无法解析"，调用方应回退到
    正常轮换路径（绝不因选择器失效而 fail-closed）。解析结果会记入
    模块状态供 elevate_backoff 使用；解析失败时清除并 warn-once。
    """
    global _last_resolved_key

    if not force and not is_pvp_enabled():
        return None

    selector = _pvp_key_selector()
    pool = list(getattr(key_manager, "api_keys", None) or [])

    resolved: Optional[str] = None
    reason = ""

    if not pool:
        reason = "密钥池为空"
    elif selector in pool:
        # 1. 完整密钥精确匹配（最优先）
        resolved = selector
    elif selector.startswith("#") and selector[1:].isdigit():
        # 2. "#N" 密钥池序号
        idx = int(selector[1:])
        if 0 <= idx < len(pool):
            resolved = pool[idx]
        else:
            reason = f"序号 {idx} 超出密钥池范围（0..{len(pool) - 1}）"
    elif selector.isdigit():
        # 2b. 纯数字同样按序号解释（完整密钥不可能是纯数字，
        #     且精确匹配已在上面先行处理）
        idx = int(selector)
        if 0 <= idx < len(pool):
            resolved = pool[idx]
        else:
            reason = f"序号 {idx} 超出密钥池范围（0..{len(pool) - 1}）"
    elif selector.startswith("key#") and len(selector) > 4:
        # 3. 哈希标识（面板下拉/日志复制）
        resolved = _resolve_by_hash(pool, selector[4:].strip().lower())
        if resolved is None:
            reason = "哈希标识在当前进程密钥池中不存在（进程重启后哈希会变化）"
    elif len(selector) >= MIN_FRAGMENT_LEN:
        # 4. 子串/尾片段匹配 —— 唯一命中才生效
        matches = [k for k in pool if selector in k]
        if len(matches) == 1:
            resolved = matches[0]
        elif len(matches) > 1:
            reason = f"片段匹配到 {len(matches)} 个密钥，需要更长的片段"
        else:
            reason = "片段未匹配到任何密钥"
    else:
        reason = f"片段过短（至少 {MIN_FRAGMENT_LEN} 位）"

    if resolved is not None:
        _last_resolved_key = resolved
        _warned_selectors.discard(selector)
        if not force:
            # force=True 是配置接口的预校验（PVP 可能尚未开启），
            # 激活日志由 dashboard 自己的保存反馈承担，此处不打。
            _log_pinned_once(resolved)
        return resolved

    _last_resolved_key = None
    if selector not in _warned_selectors:
        _warned_selectors.add(selector)
        log(
            "warning",
            f"PVP 模式：选择器未匹配到密钥（{reason}），"
            "回退到正常轮换策略，匹配后将自动钉住",
            extra={"request_type": "pvp"},
        )
    return None


def _log_pinned_once(api_key: str) -> None:
    """钉住对象变化时打一条 info（激活/切换可观测，但不随批刷屏）。"""
    global _last_logged_pin
    if _last_logged_pin == api_key:
        return
    _last_logged_pin = api_key
    log(
        "info",
        f"PVP 模式已激活：钉住 {_key_id(api_key)} 持续重试"
        f"（最大 {get_pvp_max_retries()} 次）",
        extra={"key": _key_id(api_key), "request_type": "pvp"},
    )


def is_pvp_key_dead(api_key: str) -> bool:
    """钉住的 key 是否已失效（最近一次失败为 401/403）。

    依赖 error_handling._key_failure_kinds 的同步失败类别记录
    （Round 7 机制）。记录缺失时不视为死亡——PVP 的策略是"持续
    重试直到出结果"，只有确凿的"密钥已死"信号才短路。
    """
    try:
        from app.utils.error_handling import get_key_failure_kind

        return get_key_failure_kind(api_key) == "dead"
    except Exception:
        return False


def log_dead_key_abort(api_key: str) -> None:
    """钉住 key 死亡时的显式告警（每次请求只会在终止前打一次）。"""
    log(
        "warning",
        f"PVP 模式：钉住的 {_key_id(api_key)} 已失效（401/403），"
        "继续重试不可能成功，提前终止。若密钥已被修复，"
        "请在面板重新运行密钥检测或重启进程以复位",
        extra={"key": _key_id(api_key), "request_type": "pvp"},
    )


async def elevate_backoff(base: float) -> float:
    """PVP 批间退避增强：尊重上游 429 的 retryDelay（封顶 8s）。

    钉住的 key 若正处于冷却（429 时 mark_key_failure 已按上游
    retryDelay 写入冷却表），把 full-jitter 退避抬高到冷却剩余时间
    （封顶 PVP_BACKOFF_CAP_S）。非 PVP / 未冷却 / 依赖缺失 / 任何
    异常 → 原样返回 base（fail-open，绝不比旧行为更差）。
    """
    if base <= 0 or not is_pvp_enabled():
        return base
    api_key = _last_resolved_key
    if not api_key:
        return base
    try:
        from app.utils.api_key import peek_key_cooldown_remaining

        remaining = await peek_key_cooldown_remaining(api_key)
    except Exception:
        return base
    if remaining <= 0:
        return base
    # 语义：等待 = max(full-jitter base, 冷却剩余)，封顶 CAP；
    # base 超过 CAP 时保持 base（不比既有退避更短）。
    return max(base, min(remaining, PVP_BACKOFF_CAP_S))
