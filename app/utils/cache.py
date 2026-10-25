"""响应缓存：多值键 deque、按写入时间驱逐、结构化 xxhash 缓存键。"""
import heapq
import time
import json
import xxhash
import asyncio
from typing import Dict, Any, Optional, Tuple
from collections import deque
from app.utils.logging import log

# 定义缓存项的结构
CacheItem = Dict[str, Any]


# Only delivery mode (stream) is excluded: cached Gemini responses can be
# rendered as either JSON or SSE. All options that affect generation or
# response visibility must distinguish requests, including tool selection.
_OPENAI_CACHE_OPTIONS = (
    "temperature", "top_p", "top_k", "n", "max_tokens",
    "max_completion_tokens", "presence_penalty", "frequency_penalty",
    "seed", "logprobs", "top_logprobs", "response_logprobs", "stop",
    "tool_choice", "parallel_tool_calls", "thinking_budget", "enable_thinking",
    "reasoning_effort", "expose_reasoning", "source_protocol",
)


def _hash_field(hasher, name: str, value: Any) -> None:
    """Canonical JSON with length framing preserves types and field boundaries."""
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode("utf-8", errors="surrogatepass")
    hasher.update(name.encode("ascii") + b"\0")
    hasher.update(len(encoded).to_bytes(8, "big"))
    hasher.update(encoded)


def _hash_generation_params(hasher, chat_request, is_gemini: bool) -> None:
    if is_gemini:
        payload = chat_request.payload
        for name in ("generationConfig", "safetySettings", "tool_config"):
            value = getattr(payload, name, None)
            if name == "tool_config" and value is None:
                value = getattr(payload, "toolConfig", None)
            if value is not None:
                _hash_field(hasher, name, value)
        instruction = getattr(payload, "system_instruction", None)
        if instruction is None:
            instruction = getattr(payload, "systemInstruction", None)
        _hash_field(hasher, "system_instruction", instruction)
        _hash_field(hasher, "api_version", getattr(chat_request, "api_version", None))
    else:
        for name in _OPENAI_CACHE_OPTIONS:
            value = getattr(chat_request, name, None)
            if value is not None:
                _hash_field(hasher, name, value)


class ResponseCacheManager:
    """管理API响应缓存的类，一个键可以对应多个缓存项（使用deque）"""

    def __init__(
        self,
        expiry_time: int,
        max_entries: int,
        cache_dict: Dict[str, deque[CacheItem]] = None,
    ):
        """
        初始化缓存管理器。

        Args:
            expiry_time (int): 缓存项的过期时间（秒）。
            max_entries (int): 缓存中允许的最大总条目数。
            cache_dict (Dict[str, deque[CacheItem]], optional): 初始缓存字典。默认为 None。
        """
        self.cache: Dict[str, deque[CacheItem]] = (
            cache_dict if cache_dict is not None else {}
        )
        self.expiry_time = expiry_time
        self.max_entries = max(0, max_entries)  # 总条目数限制
        self.cur_cache_num = sum(len(items) for items in self.cache.values())
        self.lock = asyncio.Lock()  # Added lock
        # Round 6（性能）：精确 LRU 驱逐堆。(created_at, seq, cache_key)
        # 元组的最小堆；seq 单调递增用于同秒内排序与条目身份验证。
        # get_and_remove / clean_expired 移除的条目会留下惰性堆节点，
        # 驱逐时通过比对 deque 内条目的 seq 跳过（详见 _evict_oldest）。
        self._evict_heap: list = []
        self._seq: int = 0
        # 外部传入的已有缓存同样需要计数和驱逐索引。
        for key, items in self.cache.items():
            for item in items:
                self._seq += 1
                item["seq"] = self._seq
                self._evict_heap.append((item.get("created_at", 0), self._seq, key))
        heapq.heapify(self._evict_heap)

    def _compact_heap_if_needed(self):
        """消费/过期留下的惰性节点不能随累计请求量无限增长。须持锁。"""
        if not self.cur_cache_num:
            self._evict_heap.clear()
        elif len(self._evict_heap) > max(64, 2 * self.cur_cache_num):
            self._evict_heap = [
                (item.get("created_at", 0), item["seq"], key)
                for key, items in self.cache.items()
                for item in items
            ]
            heapq.heapify(self._evict_heap)

    async def get_and_remove(self, cache_key: str) -> Tuple[Optional[Any], bool]:
        """获取并删除指定键的第一个有效缓存项。"""
        now = time.time()
        async with self.lock:
            if cache_key in self.cache:
                cache_deque = self.cache[cache_key]

                # 查找第一个有效项并收集过期项
                valid_item_to_remove = None
                response_to_return = None
                new_deque = deque()
                items_removed_count = 0

                for item in cache_deque:
                    if now < item.get("expiry_time", 0):
                        if valid_item_to_remove is None:  # 找到第一个有效项
                            valid_item_to_remove = item
                            response_to_return = item.get("response", None)
                            items_removed_count += 1  # 计数此项为移除

                        else:
                            new_deque.append(item)  # 保留后续有效项
                    else:
                        items_removed_count += 1  # 计数过期项为移除

                # 更新缓存状态
                if items_removed_count > 0:
                    self.cur_cache_num = max(
                        0, self.cur_cache_num - items_removed_count
                    )
                    if not new_deque:
                        # 如果所有项都被移除（过期或我们取的那个）
                        del self.cache[cache_key]
                    else:
                        self.cache[cache_key] = new_deque
                    self._compact_heap_if_needed()

                if valid_item_to_remove:
                    return response_to_return, True  # 返回找到的有效项

            # 如果键不存在或未找到有效项
            return None, False

    async def store(self, cache_key: str, response: Any):
        """存储响应到缓存（追加到键对应的deque）"""
        now = time.time()
        new_item: CacheItem = {
            "response": response,
            "expiry_time": now + self.expiry_time,
            "created_at": now,
        }

        needs_cleaning = False
        async with self.lock:
            if cache_key not in self.cache:
                self.cache[cache_key] = deque()

            self.cache[cache_key].append(new_item)  # 追加到deque末尾
            self.cur_cache_num += 1
            # Round 6: 记录精确驱逐堆节点 + 条目身份 seq。
            self._seq += 1
            new_item["seq"] = self._seq
            heapq.heappush(self._evict_heap, (now, self._seq, cache_key))
            needs_cleaning = self.cur_cache_num > self.max_entries

        if needs_cleaning:
            # 在锁外调用清理，避免长时间持有锁
            await self.clean_if_needed()

    def _evict_oldest(self, count: int) -> int:
        """Round 6（性能）：从堆顶弹出全局最旧的 count 个有效条目。

        旧实现（clean_if_needed）每次全量收集所有条目（O(N)）+
        heapq.nsmallest + 每项 deque.remove（O(K)）+ 每项一条日志，
        且全程持有 asyncio 锁 —— 缓存持续满载时每次 store 都触发一遍，
        所有请求的缓存读写被挂起，是热路径上的性能悬崖。

        新实现：堆顶就是全局最旧，弹出后通过 seq 在对应 deque 里定位
        条目（deque 通常只有 1-2 项，扫描成本恒定）；get/clean 已移除
        的条目在堆里留下惰性节点，靠 seq 验证跳过。总计均摊
        O(count·logN)，且日志合并为单行。

        调用方必须已持有 self.lock。
        """
        removed = 0
        skipped_stale = 0
        while removed < count and self._evict_heap:
            created_at, seq, key = heapq.heappop(self._evict_heap)
            cache_deque = self.cache.get(key)
            if not cache_deque:
                skipped_stale += 1
                continue
            # 在 deque 里找对应 seq 的条目（deque 短，恒定成本）
            found_idx = None
            for idx, item in enumerate(cache_deque):
                if item.get("seq") == seq:
                    found_idx = idx
                    break
            if found_idx is None:
                skipped_stale += 1  # 已被 get/clean 移除的惰性堆节点
                continue
            del cache_deque[found_idx]
            removed += 1
            if not cache_deque:
                self.cache.pop(key, None)
        if removed > 0:
            self.cur_cache_num = max(0, self.cur_cache_num - removed)
            self._compact_heap_if_needed()
            log(
                "info",
                f"缓存容量驱逐：移除 {removed} 个最旧条目"
                f"（跳过 {skipped_stale} 个失效堆节点），当前 {self.cur_cache_num}/{self.max_entries}",
            )
        return removed

    async def clean_expired(self):
        """清理所有缓存项中已过期的项。"""
        now = time.time()
        total_cleaned = 0
        async with self.lock:
            # 迭代 cache 的副本以允许在循环中安全地修改 cache
            for key, cache_deque in list(self.cache.items()):
                original_len = len(cache_deque)
                # 创建一个新的 deque，只包含未过期的项
                valid_items = deque(
                    item for item in cache_deque if now < item.get("expiry_time", 0)
                )
                cleaned_count = original_len - len(valid_items)

                total_cleaned += cleaned_count

                if not valid_items:
                    del self.cache[key]
                elif cleaned_count > 0:
                    # 替换为只包含有效项的 deque
                    self.cache[key] = valid_items

            # 统一更新缓存计数
            if total_cleaned > 0:
                self.cur_cache_num = max(0, self.cur_cache_num - total_cleaned)
            self._compact_heap_if_needed()
        if total_cleaned:
            log("info", f"清理过期缓存 {total_cleaned} 项。")

    async def clean_if_needed(self):
        """如果缓存总条目数超过限制，清理全局最旧的项目。

        Round 6：驱逐逻辑迁移到 _evict_oldest（堆式精确 LRU），本方法
        只做阈值判断与调用，全部在锁内完成但成本均摊 O(logN)。
        """

        async with self.lock:
            if self.cur_cache_num <= self.max_entries:
                return

            # 计算目标大小和需要移除的数量
            # 小容量也必须遵守上限；旧的 floor=10 让 0..9 完全失效。
            target_size = min(max(0, self.max_entries), max(self.max_entries - 10, 10))
            if self.cur_cache_num <= target_size:
                return

            items_to_remove_count = self.cur_cache_num - target_size
            self._evict_oldest(items_to_remove_count)


def generate_cache_key(
    chat_request, last_n_messages: int = 65536, is_gemini=False
) -> str:
    """Hash request options and recent full messages, retaining system prompts.

    Full message objects include tool IDs, function arguments, image MIME types,
    camelCase native parts and future content types. Length framing prevents
    user text from colliding with structural markers between messages.
    """
    hasher = xxhash.xxh64()
    _hash_field(hasher, "protocol", "gemini" if is_gemini else "openai")
    _hash_field(hasher, "model", chat_request.model)
    _hash_generation_params(hasher, chat_request, is_gemini)
    payload = chat_request.payload if is_gemini else chat_request
    _hash_field(hasher, "tools", getattr(payload, "tools", None))

    if last_n_messages <= 0:
        return hasher.hexdigest()

    messages = payload.contents if is_gemini else chat_request.messages
    start = max(0, len(messages) - last_n_messages)
    if not is_gemini:
        # System/developer instructions continue to affect all later turns,
        # even when the operator limits the number of conversation messages.
        for message in messages[:start]:
            if isinstance(message, dict) and message.get("role") in ("system", "developer"):
                _hash_field(hasher, "instruction", message)
    for message in messages[start:]:
        _hash_field(hasher, "message", message)
    return hasher.hexdigest()
