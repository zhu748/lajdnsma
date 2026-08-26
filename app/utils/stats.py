from datetime import datetime, timedelta
from app.utils.logging import log
import app.config.settings as settings
from collections import defaultdict, Counter, deque
import time
import threading
import queue


class ApiStatsManager:
    """API调用统计管理器，优化性能的新实现"""

    def __init__(self, enable_background=True, batch_interval=1.0):
        # 使用Counter记录API密钥和模型的调用次数
        self.api_key_counts = Counter()  # 记录每个API密钥的调用次数
        self.model_counts = Counter()  # 记录每个模型的调用次数
        self.api_model_counts = defaultdict(
            Counter
        )  # 记录每个API密钥对每个模型的调用次数

        # 记录token使用量
        self.api_key_tokens = Counter()  # 记录每个API密钥的token使用量
        self.model_tokens = Counter()  # 记录每个模型的token使用量
        self.api_model_tokens = defaultdict(
            Counter
        )  # 记录每个API密钥对每个模型的token使用量

        # 用于时间序列分析的数据结构（最近24小时，按分钟分组）
        self.time_buckets = {}  # 格式: {timestamp_minute: {"calls": count, "tokens": count}}

        # 保存与兼容格式相关的调用日志（最小化存储）
        # deque(maxlen=N) 自动淘汰最旧记录，避免 list.pop(0) 的 O(n) 复制
        self.recent_calls = deque(maxlen=100)  # 仅保存最近的少量调用，用于前端展示
        self.max_recent_calls = 100  # 最大保存的最近调用记录数

        # 当前时间分钟桶的时间戳（分钟级别）
        self.current_minute = self._get_minute_timestamp(datetime.now())

        # 清理间隔（小时）
        self.cleanup_interval = 1
        self.last_cleanup = time.time()

        # 使用线程锁而不是asyncio锁
        self._counters_lock = threading.Lock()
        self._time_series_lock = threading.Lock()
        self._recent_calls_lock = threading.Lock()

        # 后台处理相关
        self.enable_background = enable_background
        self.batch_interval = batch_interval
        self._update_queue = queue.Queue()
        self._worker_thread = None
        self._stop_event = threading.Event()

        if enable_background:
            self._start_worker()

    def _start_worker(self):
        """启动后台工作线程"""
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop, daemon=True
            )
            self._worker_thread.start()

    def shutdown(self):
        """Signal the background worker thread to stop.

        Previously `_stop_event` was never set, so the worker thread
        would spin in `time.sleep(0.01)` forever (until the process
        was killed).  Now any caller — including the FastAPI shutdown
        event in app/main.py — can ask the worker to stop cleanly.
        """
        self._stop_event.set()
        if self._worker_thread is not None:
            # Don't block forever — the worker sleeps at most 10ms per
            # iteration, so 2 seconds is plenty.
            self._worker_thread.join(timeout=2.0)

    def _worker_loop(self):
        """后台工作线程的主循环

        Perf fix: 旧实现每 10ms 只 get_nowait() 一条更新，消费速率上限
        ~100 条/秒；持续 QPS 超过该值时 _update_queue 无界增长（内存泄
        漏），同时线程以 100Hz 空转唤醒浪费 CPU。新实现：
        1. 阻塞等待第一条更新（最多等 batch_interval 秒）——空闲时零唤醒；
        2. 拿到第一条后立即排空队列中当前积压的所有更新（一次性批处理，
           消费速率只受锁开销限制，可达每秒数十万条）。
        """
        while not self._stop_event.is_set():
            try:
                # 阻塞等待第一条更新（空闲时挂起，不空转）
                try:
                    first = self._update_queue.get(timeout=self.batch_interval)
                except queue.Empty:
                    continue

                batch = [first]
                # 立即排空当前积压的整批更新
                while True:
                    try:
                        batch.append(self._update_queue.get_nowait())
                    except queue.Empty:
                        break

                self._process_batch(batch)

            except Exception as e:
                log("error", f"后台处理线程错误: {str(e)}")
                time.sleep(1)  # 发生错误时短暂休眠

    def _process_batch(self, batch):
        """处理一批更新"""
        with self._counters_lock:
            for api_key, model, tokens in batch:
                self.api_key_counts[api_key] += 1
                self.model_counts[model] += 1
                self.api_model_counts[api_key][model] += 1
                self.api_key_tokens[api_key] += tokens
                self.model_tokens[model] += tokens
                self.api_model_tokens[api_key][model] += tokens

    async def update_stats(self, api_key, model, tokens=0):
        """更新API调用统计"""
        if self.enable_background:
            # 将更新放入队列
            self._update_queue.put((api_key, model, tokens))
        else:
            # 同步更新
            with self._counters_lock:
                self.api_key_counts[api_key] += 1
                self.model_counts[model] += 1
                self.api_model_counts[api_key][model] += 1
                self.api_key_tokens[api_key] += tokens
                self.model_tokens[model] += tokens
                self.api_model_tokens[api_key][model] += tokens

        # 更新时间序列数据
        now = datetime.now()
        minute_ts = self._get_minute_timestamp(now)

        with self._time_series_lock:
            if minute_ts not in self.time_buckets:
                self.time_buckets[minute_ts] = {"calls": 0, "tokens": 0}

            self.time_buckets[minute_ts]["calls"] += 1
            self.time_buckets[minute_ts]["tokens"] += tokens
            self.current_minute = minute_ts

        # 更新最近调用记录（deque(maxlen) 自动淘汰旧记录，避免
        # list.pop(0) 的 O(n) 复制）
        with self._recent_calls_lock:
            compact_call = {
                "api_key": api_key,
                "model": model,
                "timestamp": now,
                "tokens": tokens,
            }
            self.recent_calls.append(compact_call)

        # 记录日志
        # Don't log raw key prefix (AIzaSy12 is a recognisable Gemini
        # key prefix).  Use a stable short hash instead.
        key_id = "key#" + str(hash(api_key) & 0xFFFFFF)
        log_message = f"API调用已记录: 秘钥 '{key_id}', 模型 '{model}', 令牌: {tokens if tokens is not None else 0}"
        log("info", log_message)

    async def cleanup(self):
        """清理超过24小时的时间桶数据"""
        now = datetime.now()
        day_ago_ts = self._get_minute_timestamp(now - timedelta(days=1))

        with self._time_series_lock:
            # 直接删除旧的时间桶
            for ts in list(self.time_buckets.keys()):
                if ts < day_ago_ts:
                    del self.time_buckets[ts]

        self.last_cleanup = time.time()

    async def maybe_cleanup(self, force=False):
        """根据需要清理旧数据"""
        now = time.time()
        if force or (now - self.last_cleanup > self.cleanup_interval * 3600):
            await self.cleanup()
            self.last_cleanup = now

    async def get_api_key_usage(self, api_key, model=None):
        """获取API密钥的使用统计

        Fix: 旧实现用 `self.api_model_counts[api_key][model]` 下标访问
        defaultdict —— 读操作也会插入空 Counter，导致统计字典被从未
        调用过的 key 污染（条目只增不减）。改用 .get() 链式读法，
        读路径零副作用。
        """
        with self._counters_lock:
            if model:
                return self.api_model_counts.get(api_key, {}).get(model, 0)
            return self.api_key_counts.get(api_key, 0)

    def get_calls_last_24h(self):
        """获取自上次每日重置以来的总调用次数。

        Naming: 旧名/旧注释声称"过去24小时"，但计数器随每日 15:00 的
        定时重置清零，并非滑动窗口 —— 修正注释以免误导（口径变更需
        UI 同步，此处仅澄清语义）。
        """
        with self._counters_lock:
            return sum(self.api_key_counts.values())

    def get_calls_last_hour(self, now=None):
        """获取过去一小时的总调用次数"""
        if now is None:
            now = datetime.now()

        hour_ago_ts = self._get_minute_timestamp(now - timedelta(hours=1))

        with self._time_series_lock:
            return sum(
                data["calls"]
                for ts, data in self.time_buckets.items()
                if ts >= hour_ago_ts
            )

    def get_calls_last_minute(self, now=None) -> int:
        """获取过去一分钟的总调用次数"""
        if now is None:
            now = datetime.now()

        minute_ago_ts = self._get_minute_timestamp(now - timedelta(minutes=1))

        with self._time_series_lock:
            return sum(
                data["calls"]
                for ts, data in self.time_buckets.items()
                if ts >= minute_ago_ts
            )

    def get_calls_last_minute_for_key(self, api_key: str, now=None) -> int:
        """获取过去一分钟内某个 API key 的调用次数。

        实现方式：扫描 self.recent_calls（保留最近 100 条），过滤
        timestamp 在过去 60s 内且 api_key 匹配的记录。

        这个范围足够支撑 RPM 限流决策（默认 RPM=15-30，远低于 100 的窗口
        上限）。  对大池子或超高 QPS 场景，需要切换到独立的滑动窗口
        实现，但当前实现已足够避免单 Key 短时间被打穿。
        """
        if now is None:
            now = datetime.now()
        cutoff = now - timedelta(seconds=60)

        with self._recent_calls_lock:
            return sum(
                1
                for call in self.recent_calls
                if call.get("api_key") == api_key
                and call.get("timestamp") is not None
                and call["timestamp"] >= cutoff
            )

    def get_tokens_last_24h(self):
        """获取过去24小时的总Token消耗量"""
        with self._counters_lock:
            return sum(self.api_key_tokens.values())

    def get_tokens_last_hour(self, now=None):
        """获取过去一小时的总Token消耗量"""
        if now is None:
            now = datetime.now()
        
        hour_ago_ts = self._get_minute_timestamp(now - timedelta(hours=1))
        
        with self._time_series_lock:
            return sum(
                data["tokens"]
                for ts, data in self.time_buckets.items()
                if ts >= hour_ago_ts
            )

    def get_tokens_last_minute(self, now=None):
        """获取过去一分钟的总Token消耗量"""
        if now is None:
            now = datetime.now()
        
        minute_ago_ts = self._get_minute_timestamp(now - timedelta(minutes=1))
        
        with self._time_series_lock:
            return sum(
                data["tokens"]
                for ts, data in self.time_buckets.items()
                if ts >= minute_ago_ts
            )

    def get_time_series_data(self, minutes=30, now=None):
        """获取过去N分钟的时间序列数据"""
        if now is None:
            now = datetime.now()

        calls_series = []
        tokens_series = []

        with self._time_series_lock:
            for i in range(minutes, -1, -1):
                minute_dt = now - timedelta(minutes=i)
                minute_ts = self._get_minute_timestamp(minute_dt)

                bucket = self.time_buckets.get(minute_ts, {"calls": 0, "tokens": 0})

                calls_series.append(
                    {"time": minute_dt.strftime("%H:%M"), "value": bucket["calls"]}
                )

                tokens_series.append(
                    {"time": minute_dt.strftime("%H:%M"), "value": bucket["tokens"]}
                )

        return calls_series, tokens_series

    def get_api_key_stats(self, api_keys):
        """获取API密钥的详细统计信息"""
        stats = []

        # Correctness: 与 get_api_key_usage 同型的读污染修复 —— 旧实现
        # 用下标访问 defaultdict，读操作也会插入空 Counter/0，统计字典被
        # 从未调用过的 key 污染（条目只增不减）。改用 .get() 链式读法。
        with self._counters_lock:
            for api_key in api_keys:
                api_key_id = "key#" + str(hash(api_key) & 0xFFFFFF)
                calls_24h = self.api_key_counts.get(api_key, 0)
                total_tokens = self.api_key_tokens.get(api_key, 0)

                model_stats = {}
                for model, count in self.api_model_counts.get(api_key, {}).items():
                    tokens = self.api_model_tokens.get(api_key, {}).get(model, 0)
                    model_stats[model] = {"calls": count, "tokens": tokens}

                usage_percent = (
                    (calls_24h / settings.API_KEY_DAILY_LIMIT) * 100
                    if settings.API_KEY_DAILY_LIMIT > 0
                    else 0
                )

                stats.append(
                    {
                        "api_key": api_key_id,
                        "calls_24h": calls_24h,
                        "total_tokens": total_tokens,
                        "limit": settings.API_KEY_DAILY_LIMIT,
                        "usage_percent": round(usage_percent, 2),
                        "model_stats": model_stats,
                    }
                )

        stats.sort(key=lambda x: x["usage_percent"], reverse=True)
        return stats

    async def reset(self):
        """重置所有统计数据"""
        with self._counters_lock:
            self.api_key_counts.clear()
            self.model_counts.clear()
            self.api_model_counts.clear()
            self.api_key_tokens.clear()
            self.model_tokens.clear()
            self.api_model_tokens.clear()

        with self._time_series_lock:
            self.time_buckets.clear()

        with self._recent_calls_lock:
            self.recent_calls.clear()

        self.current_minute = self._get_minute_timestamp(datetime.now())
        self.last_cleanup = time.time()

    def _get_minute_timestamp(self, dt):
        """将时间戳转换为分钟级别的时间戳（按分钟取整）"""
        return int(dt.timestamp() // 60 * 60)


# 创建全局单例实例
api_stats_manager = ApiStatsManager()


# 兼容现有代码的函数（clean_expired_stats 曾是一个 fire-and-forget 的
# asyncio.create_task 包装器，但全仓库无任何调用方，且从同步上下文调用
# 时会因无运行中的事件循环而报错——真正的定时清理由 maintenance 调度器
# 每 5 分钟直接执行 api_stats_manager.cleanup，故连同 app/utils/__init__
# 的导入一并删除）


async def update_api_call_stats(api_call_stats, endpoint=None, model=None, token=None):
    """更新API调用统计的函数 (兼容旧接口)"""
    if endpoint and model:
        await api_stats_manager.update_stats(
            endpoint, model, token if token is not None else 0
        )


async def get_api_key_usage(api_call_stats, api_key, model=None):
    """获取API密钥的调用次数 (兼容旧接口)"""
    return await api_stats_manager.get_api_key_usage(api_key, model)


def get_calls_last_minute_for_key(api_key: str) -> int:
    """获取过去一分钟内某个 API key 的调用次数 (兼容新接口)。

    同步函数，因为底层是同步的；调用方在 async 上下文里直接调用即可。
    """
    return api_stats_manager.get_calls_last_minute_for_key(api_key)


# Default Gemini free-tier RPM safety threshold.  When a key's last-minute
# call count is at or above this fraction of MAX_OUTBOUND_RPM, we should
# skip it during key selection to avoid hitting the upstream limit.
# Default 0.8 = use the key only when it's at less than 80% of the limit.
import os as _os
MAX_OUTBOUND_RPM = int(_os.environ.get("MAX_OUTBOUND_RPM", "15"))  # Gemini free-tier default
OUTBOUND_RPM_BACKOFF_FRACTION = float(_os.environ.get("OUTBOUND_RPM_BACKOFF_FRACTION", "0.8"))
