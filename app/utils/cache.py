import time
import json
import xxhash
import asyncio
from typing import Dict, Any, Optional, Tuple
import logging
from collections import deque
from app.utils.logging import log

logger = logging.getLogger("my_logger")
import heapq

# 定义缓存项的结构
CacheItem = Dict[str, Any]


def _hash_generation_params(h, chat_request, is_gemini: bool) -> None:
    """Round 6（缓存正确性）：把采样/生成参数纳入缓存键。

    旧键只哈希 model + tools + 最近 N 条消息 —— temperature /
    max_tokens / top_p / stop 等参数不同但消息相同的两个请求共享
    缓存键，第二个请求会拿到第一个请求在**不同采样参数**下的答案
    （CALCULATE_CACHE_ENTRIES=6 截断时更易碰撞）。现在把标量生成
    参数规范化后增量哈希：None/缺省不写入（保持与旧键兼容的"默认
    请求"路径），非默认值以「参数名:值」形式写入。
    """
    # OpenAI 格式的标量采样参数
    _OPENAI_SCALARS = (
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "logprobs",
        "top_logprobs",
    )
    # Gemini 原生 payload 里除 generationConfig 外基本不含采样参数
    _GEMINI_CONFIG_KEYS = (
        "temperature",
        "topP",
        "topK",
        "maxOutputTokens",
        "stopSequences",
        "presencePenalty",
        "frequencyPenalty",
        "seed",
        "thinkingConfig",
        "responseMimeType",
        "responseModalities",
        "candidateCount",
    )
    try:
        if is_gemini:
            payload = getattr(chat_request, "payload", None)
            config = getattr(payload, "generationConfig", None)
            if isinstance(config, dict):
                for k in _GEMINI_CONFIG_KEYS:
                    v = config.get(k)
                    if v is None or v == "" or v == []:
                        continue
                    h.update(f"gp:{k}=".encode("utf-8"))
                    h.update(
                        json.dumps(v, sort_keys=True, default=str).encode(
                            "utf-8", errors="surrogateescape"
                        )
                    )
        else:
            for name in _OPENAI_SCALARS:
                v = getattr(chat_request, name, None)
                if v is None or v == "":
                    continue
                h.update(f"op:{name}=".encode("utf-8"))
                h.update(str(v).encode("utf-8", errors="surrogateescape"))
            stop = getattr(chat_request, "stop", None)
            if stop:
                h.update(b"op:stop=")
                h.update(
                    json.dumps(stop, sort_keys=True, default=str).encode(
                        "utf-8", errors="surrogateescape"
                    )
                )
    except Exception:
        # 哈希失败时宁可退化为旧键（碰撞事小，崩溃事大）
        pass


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
        self.max_entries = max_entries  # 总条目数限制
        self.cur_cache_num = 0  # 当前条目数
        self.lock = asyncio.Lock()  # Added lock
        # Round 6（性能）：精确 LRU 驱逐堆。(created_at, seq, cache_key)
        # 元组的最小堆；seq 单调递增用于同秒内排序与条目身份验证。
        # get_and_remove / clean_expired 移除的条目会留下惰性堆节点，
        # 驱逐时通过比对 deque 内条目的 seq 跳过（详见 _evict_oldest）。
        self._evict_heap: list = []
        self._seq: int = 0

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
            log(
                "info",
                f"缓存容量驱逐：移除 {removed} 个最旧条目"
                f"（跳过 {skipped_stale} 个失效堆节点），当前 {self.cur_cache_num}/{self.max_entries}",
            )
        return removed

    async def clean_expired(self):
        """清理所有缓存项中已过期的项。"""
        now = time.time()
        keys_to_remove = []
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

                if cleaned_count > 0:
                    log(
                        "info", f"清理键 {key[:8]}... 的过期缓存项 {cleaned_count} 个。"
                    )
                    total_cleaned += cleaned_count

                if not valid_items:
                    keys_to_remove.append(key)  # 标记此键以便稍后删除
                    # 在持有锁时直接删除键
                    if key in self.cache:
                        del self.cache[key]
                        log("info", f"缓存键 {key[:8]}... 的所有项均已过期，移除该键。")
                elif cleaned_count > 0:
                    # 替换为只包含有效项的 deque
                    self.cache[key] = valid_items

            # 统一更新缓存计数
            if total_cleaned > 0:
                self.cur_cache_num = max(0, self.cur_cache_num - total_cleaned)

    async def clean_if_needed(self):
        """如果缓存总条目数超过限制，清理全局最旧的项目。

        Round 6：驱逐逻辑迁移到 _evict_oldest（堆式精确 LRU），本方法
        只做阈值判断与调用，全部在锁内完成但成本均摊 O(logN)。
        """

        async with self.lock:
            if self.cur_cache_num <= self.max_entries:
                return

            # 计算目标大小和需要移除的数量
            target_size = max(self.max_entries - 10, 10)
            if self.cur_cache_num <= target_size:
                return

            items_to_remove_count = self.cur_cache_num - target_size
            self._evict_oldest(items_to_remove_count)


def generate_cache_key(
    chat_request, last_n_messages: int = 65536, is_gemini=False
) -> str:
    """
    根据模型名称和最后 N 条消息生成请求的唯一缓存键。
    Args:
        chat_request: 包含模型和消息列表的请求对象 (符合OpenAI格式)。
        last_n_messages: 需要包含在缓存键计算中的最后消息的数量。
    Returns:
        一个代表该请求的唯一缓存键字符串 (xxhash64哈希值)。
    """
    h = xxhash.xxh64()

    # 1. 哈希模型名称
    h.update(chat_request.model.encode("utf-8", errors="surrogateescape"))

    # 1.2 Round 6: 哈希生成/采样参数（temperature/max_tokens/top_p/...），
    # 防止"同消息不同参数"的请求共享缓存键导致串答（详见函数 docstring）。
    _hash_generation_params(h, chat_request, is_gemini=is_gemini)

    # 1.5 哈希工具定义。
    # Correctness: tools 不属于消息历史，旧的键计算只哈希模型 + 最近 N
    # 条消息 —— 同模型、同最近 6 条消息但 tools 不同的两个请求会共享
    # 缓存键，第二个请求可能拿到第一个请求（不同工具配置）的缓存答案
    # （默认 CALCULATE_CACHE_ENTRIES=6 截断时尤为严重）。规范化序列化后
    # 全量哈希，确保不同工具集的请求不会串扰。
    tools = None
    if is_gemini:
        payload = getattr(chat_request, "payload", None)
        tools = getattr(payload, "tools", None)
    else:
        tools = getattr(chat_request, "tools", None)
    if tools:
        try:
            h.update(b"tools:")
            h.update(
                json.dumps(
                    tools, sort_keys=True, ensure_ascii=False, default=str
                ).encode("utf-8", errors="surrogateescape")
            )
        except (TypeError, ValueError):
            h.update(b"tools:unserializable")

    if last_n_messages <= 0:
        # 如果不考虑消息，直接返回基于模型的哈希
        return h.hexdigest()

    messages_processed = 0

    # 2. 增量哈希最后 N 条消息 (从后往前)
    if is_gemini:
        # log('INFO', f"开启增量哈希gemini格式内容")
        for content_item in reversed(chat_request.payload.contents):
            if messages_processed >= last_n_messages:
                break
            role = content_item.get("role")
            if role is not None and isinstance(role, str):
                h.update(b"role:")
                h.update(role.encode("utf-8", errors="surrogateescape"))
            # log('INFO', f"哈希gemini格式角色{role}")
            parts = content_item.get("parts", [])
            if not isinstance(parts, list):
                parts = []
            for part in parts:
                text_content = part.get("text")
                if text_content is not None and isinstance(text_content, str):
                    h.update(b"text:")
                    h.update(text_content.encode("utf-8", errors="surrogateescape"))
                    # log('INFO', f"哈希gemini格式文本内容{text_content}")

                inline_data_obj = part.get("inline_data")
                if inline_data_obj is not None and isinstance(inline_data_obj, dict):
                    h.update(b"inline_data:")
                    data_payload = inline_data_obj.get("data", "")
                    # Hardening: previously only hashed the first 32
                    # characters of the base64 payload — see the
                    # image_url branch above for the collision risk.
                    # We now hash the full payload.
                    if isinstance(data_payload, str):
                        h.update(
                            data_payload.encode("utf-8", errors="surrogateescape")
                        )

                file_data_obj = part.get("file_data")
                if file_data_obj is not None and isinstance(file_data_obj, dict):
                    h.update(b"file_data:")
                    file_uri = file_data_obj.get("file_uri", "")
                    if isinstance(file_uri, str):
                        h.update(b"file_uri:")
                        h.update(file_uri.encode("utf-8", errors="surrogateescape"))
            messages_processed += 1

    else:
        for msg in reversed(chat_request.messages):
            if messages_processed >= last_n_messages:
                break

            # 哈希角色
            h.update(b"role:")
            h.update(msg.get("role", "").encode("utf-8", errors="surrogateescape"))

            # 哈希内容
            content = msg.get("content")
            if isinstance(content, str):
                h.update(b"text:")
                h.update(content.encode("utf-8", errors="surrogateescape"))
            elif isinstance(content, list):
                # 处理图文混合内容
                for item in content:
                    item_type = item.get("type") if hasattr(item, "get") else None
                    if item_type == "text":
                        text = item.get("text", "") if hasattr(item, "get") else ""
                        h.update(b"text:")
                        h.update(text.encode("utf-8", errors="surrogateescape"))
                    elif item_type == "image_url":
                        image_url = (
                            item.get("image_url", {}) if hasattr(item, "get") else {}
                        )
                        image_data = (
                            image_url.get("url", "")
                            if hasattr(image_url, "get")
                            else ""
                        )

                        h.update(b"image_url:")  # 加入类型标识符
                        if image_data.startswith("data:image/"):
                            # Hardening: previously only hashed the first
                            # 32 characters of the base64 payload.  Two
                            # different images with the same MIME type
                            # (e.g. "data:image/png;base64,") share
                            # the first 32 chars verbatim — so the
                            # cache key collided and the second image
                            # would get the first image's cached
                            # response.  We now hash the entire base64
                            # payload, which gives each image a unique
                            # cache key (at the cost of hashing ~MB of
                            # data per request — acceptable because
                            # hashlib.sha256 is fast).
                            h.update(
                                image_data.encode(
                                    "utf-8", errors="surrogateescape"
                                )
                            )
                        else:
                            h.update(
                                image_data.encode("utf-8", errors="surrogateescape")
                            )

            messages_processed += 1
    return h.hexdigest()
