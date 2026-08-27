import random
import re
import os
import logging
import asyncio
import time
from app.utils.http_client import get_async_client
from app.utils.logging import format_log_message
from app.utils.stealth import build_key_probe_headers
import app.config.settings as settings

logger = logging.getLogger("my_logger")

# ---------------------------------------------------------------------------
# API key parsing (env-var / config-string form)
# ---------------------------------------------------------------------------
# Gemini API 密钥目前有两种已知格式：
#   经典格式（2025 之前签发）: "AIzaSy" + 33 位字符，共 39 字符
#   新版格式（2025 起 Google AI Studio 签发）: "AQ." 前缀，约 90 字符
#
# Bug fix: 旧实现只用 AIzaSy 正则提取密钥，新版 AQ. 格式密钥在
# 环境变量里会被静默丢弃——而面板"添加密钥"入口（dashboard 的
# gemini_api_keys 分支）按逗号分割、不做格式过滤，什么格式都能加。
# 两条路径行为不一致，导致"环境变量加不进去、面板才能加进去"。
# 现在环境变量解析与面板对齐：任意格式都接受，同时保留正则提取
# 兜底（兼容把整段 JSON / 自由文本粘进环境变量的旧用法）。
CLASSIC_KEY_PATTERN = r"AIzaSy[a-zA-Z0-9_-]{33}"
NEW_FORMAT_KEY_PATTERN = r"AQ\.[A-Za-z0-9_-]{20,}"
# 低于该长度的 token 一律视为噪声，不可能是一条合法密钥
# （最短的已知格式——经典 AIzaSy——也有 39 字符）。
MIN_KEY_LENGTH = 20


def _parse_api_keys(raw: str) -> list:
    """从原始配置字符串中解析出 API 密钥列表。

    解析策略（与面板"添加密钥"行为保持一致）：
      Pass 1: 按 逗号 / 换行 / 分号 / 空白 切分，去掉首尾引号与空白，
              接受任何"纯不透明 token"（仅含字母数字 . _ -，长度达标）。
              这样未来 Google 再换密钥前缀也不会重演本次 bug。
      Pass 2: 对原始串跑已知格式正则，把嵌在 JSON、URL 或自由文本里
              的密钥也提取出来（兼容旧版"整段粘贴"用法）。
    返回保序去重后的列表。
    """
    if not raw or not raw.strip():
        return []

    candidates = []

    # Pass 1: 分隔符切分（任意格式均接受）
    for token in re.split(r"[\s,;]+", raw):
        token = token.strip().strip('"').strip("'").strip()
        if len(token) < MIN_KEY_LENGTH:
            continue
        # 只接受纯不透明 token；含 JSON 标点 / URL 结构的碎片
        # （如 {"key": "AIzaSy..."} 被 split 出的片段）交给 Pass 2 兜底。
        if re.fullmatch(r"[A-Za-z0-9._-]+", token):
            candidates.append(token)

    # Pass 2: 已知格式正则提取（兼容整段 JSON / 自由文本粘贴）
    candidates.extend(re.findall(CLASSIC_KEY_PATTERN, raw))
    candidates.extend(re.findall(NEW_FORMAT_KEY_PATTERN, raw))

    # 保序去重
    seen = set()
    deduped = []
    for k in candidates:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    return deduped

# ---------------------------------------------------------------------------
# Key cooldown state
# ---------------------------------------------------------------------------

# Maps api_key -> epoch seconds when the key becomes available again.
# 429 -> 60s cooldown, 500/503 -> 5s cooldown, 401/403 -> permanent (until
# process restart or manual reset).  The "permanent" form is encoded as a
# very large timestamp so existing code can still treat it as a timestamp.
PERMANENT_BLOCK_TS = 2**62  # ~ year 15 billion

_key_cooldowns: dict[str, float] = {}
_key_cooldowns_lock = asyncio.Lock()


async def mark_key_failure(api_key: str, status_code: int) -> float:
    """Place a key on cooldown based on the failure type.

    Returns the cooldown duration (seconds).  0 means no cooldown applied
    (e.g. unrecognised status code).
    """
    if status_code in (401, 403):
        # Invalid / forbidden: permanent block until restart
        cooldown = PERMANENT_BLOCK_TS - time.time()
        async with _key_cooldowns_lock:
            _key_cooldowns[api_key] = PERMANENT_BLOCK_TS
        log_msg = format_log_message(
            "WARNING",
            f"key#{hash(api_key) & 0xFFFFFF:06x} -> permanently blocked due to {status_code}",
        )
        logger.warning(log_msg)
        return cooldown
    if status_code == 429:
        cooldown = 60.0
    elif status_code in (500, 503):
        cooldown = 5.0
    else:
        return 0.0

    available_at = time.time() + cooldown
    async with _key_cooldowns_lock:
        _key_cooldowns[api_key] = available_at
    log_msg = format_log_message(
        "WARNING",
        f"key#{hash(api_key) & 0xFFFFFF:06x} -> cooldown {cooldown}s due to {status_code}",
    )
    logger.warning(log_msg)
    return cooldown


async def is_key_cooled_down(api_key: str) -> bool:
    """Return True if the key is currently on cooldown."""
    async with _key_cooldowns_lock:
        available_at = _key_cooldowns.get(api_key)
    if not available_at:
        return False
    if time.time() < available_at:
        return True
    # Cooldown expired: clear it so we don't grow the dict forever.
    async with _key_cooldowns_lock:
        if _key_cooldowns.get(api_key) == available_at:
            _key_cooldowns.pop(api_key, None)
    return False


async def clear_key_cooldown(api_key: str) -> None:
    async with _key_cooldowns_lock:
        _key_cooldowns.pop(api_key, None)


async def clear_all_cooldowns() -> None:
    async with _key_cooldowns_lock:
        _key_cooldowns.clear()


class APIKeyManager:
    def __init__(self):
        # 收集所有密钥来源：主变量 + 编号变量 GEMINI_API_KEYS_1..98
        # Fix: 旧实现在第一个空序号就 break——配置了 _1,_2,_4 时 _4 会被
        # 静默忽略。改为遍历全部检查，空序号跳过。
        raw_sources = [settings.GEMINI_API_KEYS]
        for i in range(1, 99):
            if keys := os.environ.get(f"GEMINI_API_KEYS_{i}", ""):
                raw_sources.append(keys)

        # 解析（支持 AIzaSy 经典格式与 AQ. 新版格式，详见 _parse_api_keys）
        parsed = []
        for raw in raw_sources:
            parsed.extend(_parse_api_keys(raw))

        # Dedupe while preserving order.  The previous code accepted
        # duplicates silently, which inflated effective concurrency for a
        # subset of keys and broke RPM fairness.
        seen = set()
        deduped = []
        for k in parsed:
            if k not in seen:
                seen.add(k)
                deduped.append(k)
        self.api_keys = deduped

        # 可观测性：配置了密钥内容却一个都没解析出来时，给出显式告警。
        # 旧实现在此场景下静默得到 0 个密钥，请求时才报"没有配置任何
        # API 密钥"，排查方向会被带偏（这次的"环境变量识别不了"问题
        # 正是这种情况——AQ. 新格式密钥被旧正则静默吞掉）。
        if not self.api_keys:
            configured = [s for s in raw_sources if s and s.strip()]
            if configured:
                log_msg = format_log_message(
                    "WARNING",
                    "GEMINI_API_KEYS 已配置内容但未能解析出任何密钥！"
                    "请检查密钥格式（支持 AIzaSy... 经典格式与 AQ.... 新格式，"
                    "多个密钥用英文逗号分隔）",
                )
                logger.warning(log_msg)

        self.key_stack = []  # 初始化密钥栈
        self._reset_key_stack()  # 初始化时创建随机密钥栈
        self.lock = asyncio.Lock()  # Added lock
        # Cleanup: 这里曾有一个 BackgroundScheduler 实例被创建并启动，
        # 但从未注册任何任务（全项目无 key_manager.scheduler 引用），
        # 也从未被 shutdown——纯粹占用一个线程且在进程关闭时泄漏。已删除。

    @property
    def strategy(self) -> str:
        """Active key rotation strategy.

        Reads from settings so an operator can flip it at runtime via
        the dashboard endpoint (which calls save_settings + reloads).
        """
        return getattr(settings, "KEY_ROTATION_STRATEGY", "fill").lower().strip()

    def _reset_key_stack(self):
        """创建并随机化密钥栈.

        在 fill 模式下：仍然随机化一次起始顺序，但使用 peek (不 pop) 让同一个
        key 被反复使用直到它进入冷却 / 超出日额度。

        在 polling 模式下：每次栈空都重新随机化（旧行为）。
        """
        shuffled_keys = self.api_keys[:]  # 创建 api_keys 的副本以避免直接修改原列表
        random.shuffle(shuffled_keys)
        self.key_stack = shuffled_keys

    async def get_available_key(self):
        """从栈顶获取一个未冷却的密钥，若栈空则重新生成。

        Fill 模式（默认，推荐用于反风控）:
            1. 始终返回当前栈顶 key，直到该 key:
               - 进入冷却（429/401/403/500/503）
               - 当日累计调用数 ≥ API_KEY_DAILY_LIMIT
            2. 满足上述任一条件时 pop 该 key，转到下一个
            3. 栈空时（所有 key 都不可用）才重新随机化并再次循环

            这种 "粘住一个 key 直到不可用" 的策略产生的 per-key RPM
            模式更接近单一真实用户行为，Google 风控更难据此判定为
            "key 轮换池"。

        Polling 模式（旧行为，可配置切换）:
            1. 维护一个随机排序的栈
            2. 每次调用从栈顶取出一个未冷却的 key 返回
            3. 栈空时重新随机化
        """
        if self.strategy == "polling":
            return await self._get_available_key_polling()
        return await self._get_available_key_fill()

    async def _get_available_key_polling(self):
        """Polling (round-robin) mode: original behaviour."""
        async with self.lock:
            # 如果栈为空，重新生成
            if not self.key_stack:
                self._reset_key_stack()

            # Pop until we find a non-cooled-down key, OR the stack is empty.
            tried = set()
            while self.key_stack:
                candidate = self.key_stack.pop()
                if candidate in tried:
                    # Don't loop forever; push back and bail out
                    self.key_stack.append(candidate)
                    break
                tried.add(candidate)
                if await is_key_cooled_down(candidate):
                    continue
                return candidate

            # 如果没有可用的API密钥，记录错误
            if not self.api_keys:
                log_msg = format_log_message("ERROR", "没有配置任何 API 密钥！")
                logger.error(log_msg)
            log_msg = format_log_message("ERROR", "没有可用的API密钥（或全部冷却中）！")
            logger.error(log_msg)
            return None

    async def _get_available_key_fill(self):
        """Fill (sticky) mode: keep returning the same key until it's unusable.

        This is the anti-fingerprint-preferred strategy: real single-user
        clients don't round-robin API keys, they keep using the same one
        until they hit a quota.  A key pool that visibly round-robins
        every request is the textbook signature of a multi-key proxy.
        """
        async with self.lock:
            # Defensive: if the underlying key list changed (operator added
            # new keys via dashboard), reset the stack.
            known = set(self.api_keys)
            stack_set = set(self.key_stack)
            if stack_set and not stack_set.issubset(known):
                # Stack contains a key no longer in the pool — rebuild.
                self._reset_key_stack()
            elif not self.key_stack and self.api_keys:
                self._reset_key_stack()

            # Walk the stack from the top, popping any key that's on
            # cooldown or over daily limit.  The first "good" key is
            # returned WITHOUT being popped, so the next call returns
            # the same key again (sticky behaviour).
            tried = set()
            while self.key_stack:
                candidate = self.key_stack[-1]  # peek, don't pop

                if candidate in tried:
                    # We've cycled through every key on the stack and
                    # none of them is usable right now.  Bail out so
                    # the caller can surface the no-keys-available error
                    # instead of looping forever.
                    break
                tried.add(candidate)

                # Cooldown check (429/401/403/500/503).
                if await is_key_cooled_down(candidate):
                    # Pop this key off — it's unusable right now.  When
                    # cooldown expires, we'll pick it up on the next
                    # stack reset.
                    self.key_stack.pop()
                    continue

                # Daily-limit check (deferred import to avoid circular
                # import with stats.py).
                try:
                    from app.utils.stats import get_api_key_usage
                    usage = await get_api_key_usage(settings.api_call_stats, candidate)
                except Exception:
                    usage = 0
                if usage >= settings.API_KEY_DAILY_LIMIT:
                    # Key exhausted its daily quota.  Pop and try next.
                    #
                    # Bug fix: this branch previously called log(...) which
                    # was never imported in this module — the NameError
                    # replaced the intended "advance to next key" flow, so
                    # the sticky key stayed on top of the stack and EVERY
                    # subsequent get_available_key() crashed again (all
                    # chat/embedding requests 500 until the daily counter
                    # reset).  Uses the module's own logger style instead.
                    log_msg = format_log_message(
                        "INFO",
                        f"key#{hash(candidate) & 0xFFFFFF:06x} reached daily limit "
                        f"({usage}/{settings.API_KEY_DAILY_LIMIT}), advancing to next key (fill mode)",
                    )
                    logger.info(log_msg)
                    self.key_stack.pop()
                    continue

                # This key is good — return it WITHOUT popping so the
                # next call gets the same key (sticky behaviour).
                return candidate

            # All keys either on cooldown or over daily limit.
            if not self.api_keys:
                log_msg = format_log_message("ERROR", "没有配置任何 API 密钥！")
                logger.error(log_msg)
            log_msg = format_log_message(
                "ERROR", "没有可用的API密钥（全部冷却中或达日额度上限）！"
            )
            logger.error(log_msg)
            return None

    def show_all_keys(self):
        log_msg = format_log_message(
            "INFO",
            f"当前可用API key个数: {len(self.api_keys)} "
            f"(rotation strategy: {self.strategy})",
        )
        logger.info(log_msg)
        for i, api_key in enumerate(self.api_keys):
            # Don't log raw key prefix (AIzaSy12 is a recognisable Gemini
            # key prefix).  Use an index + a stable short hash.
            log_msg = format_log_message(
                "INFO", f"API Key#{i}: hash={hash(api_key) & 0xFFFFFF:06x}"
            )
            logger.info(log_msg)


async def test_api_key(api_key: str) -> bool:
    """
    测试 API 密钥是否有效。

    Round 4: 探测超时从 60s 降到 15s —— 这只是一个轻量 GET
    /v1beta/models，正常网络下秒级返回；60s 意味着网络黑洞时每个
    失效密钥都要占用启动路径整整一分钟（若首批 10 个 key 全部
    黑洞，服务要 10 分钟后才能就绪）。15s 已远超真实 P99，同时
    把最坏情况的启动阻塞缩短到原来的 1/4。
    """
    try:
        # Key moved from ?key=... to header to avoid leaking it into
        # upstream access logs / any intermediate proxy's access logs.
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        headers = build_key_probe_headers(api_key)
        headers["x-goog-api-key"] = api_key
        client = await get_async_client()
        response = await client.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return True
    except Exception:
        return False
