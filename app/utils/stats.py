from datetime import datetime, timedelta
from app.utils.logging import log
import app.config.settings as settings
from collections import defaultdict, Counter, deque
import time
import threading
import queue

# Round 5: per-key RPM 滑动窗口参数。
_RPM_WINDOW_SECONDS = 60          # 与上游 RPM 语义对齐的滑动窗口长度
_RPM_WINDOW_MAX_ENTRIES = 120     # 每 key 保留的时间戳上限（RPM > 120 已远超任何配额）
_MAX_TRACKED_KEYS = 4096         # 防御性上限：追踪的 key 数量上限


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

        # Round 5（反风控 P0）: per-key RPM 滑动窗口。
        # 旧实现把全部调用塞进一个全局 recent_calls deque(maxlen=100)
        # 再线性扫描计数——总调用量一旦超过 100 条/分钟（多 key 并发
        # 场景轻松达到），60s 窗口内的记录先被全局 maxlen 淘汰，
        # get_calls_last_minute_for_key 恒返回 0，
        # select_valid_api_keys 的 RPM 退避完全失效（负载越高越失效），
        # 热 key 持续被打穿上游 429。改为每 key 独立的时间戳 deque：
        # 写入时顺手裁剪 60s 前的过期项，读取时同样裁剪后取 len。
        # 内存有界：每 key ≤ _RPM_WINDOW_MAX_ENTRIES 个 float，
        # 追踪的 key 数 ≤ _MAX_TRACKED_KEYS（真实密钥池远小于此）。
        self._key_rpm_windows: dict = defaultdict(
            lambda: deque(maxlen=_RPM_WINDOW_MAX_ENTRIES)
        )
        self._rpm_lock = threading.Lock()

        # 用于时间序列分析的数据结构（最近24小时，按分钟分组）
        self.time_buckets = {}  # 格式: {timestamp_minute: {"calls": count, "tokens": count}}

        # 当前时间分钟桶的时间戳（分钟级别）
        self.current_minute = self._get_minute_timestamp(datetime.now())

        # 清理间隔（小时）
        self.cleanup_interval = 1
        self.last_cleanup = time.time()

        # 使用线程锁而不是asyncio锁
        self._counters_lock = threading.Lock()
        self._time_series_lock = threading.Lock()

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
        """更新API调用统计

        Round 5: 删除了逐笔 "API调用已记录" info 日志——它以每请求一条
        的频率刷入 LogManager 的有界缓冲，中等 QPS 下几秒内就把冷却
        触发、429、密钥失败等真正需要运维看到的 warning/error 全部挤出
        日志面板（可观测性反退化）。每笔调用已有 request start/success
        等带 key/model 上下文的日志，无需重复。
        """
        now = datetime.now()
        now_ts = now.timestamp()

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
        minute_ts = self._get_minute_timestamp(now)

        with self._time_series_lock:
            if minute_ts not in self.time_buckets:
                self.time_buckets[minute_ts] = {"calls": 0, "tokens": 0}

            self.time_buckets[minute_ts]["calls"] += 1
            self.time_buckets[minute_ts]["tokens"] += tokens
            self.current_minute = minute_ts

        # 更新 per-key RPM 滑动窗口（写入时顺手裁剪过期项，均摊 O(过期数)）
        with self._rpm_lock:
            if (
                api_key not in self._key_rpm_windows
                and len(self._key_rpm_windows) >= _MAX_TRACKED_KEYS
            ):
                pass  # 防御性上限：密钥池异常庞大时放弃追踪新 key
            else:
                window = self._key_rpm_windows[api_key]
                window.append(now_ts)
                cutoff = now_ts - _RPM_WINDOW_SECONDS
                while window and window[0] < cutoff:
                    window.popleft()

    def record_outbound_attempt(self, api_key: str, model: str = "") -> None:
        """Round 6: 在上游请求**发射时**记录 RPM 窗口时间戳（而非完成时）。

        旧实现只在成功完成后（finalize_gemini_response / 空响应分支）
        调用 update_stats，在途、429/500 失败、超时的请求全部不计入
        RPM 窗口 —— 并发越高、响应越慢（thinking 模型 30s+），低估越
        严重，可以在窗口计数未满时持续超发，退避永远慢一拍。

        本方法只写 RPM 窗口（同步、轻量），在三个发射点
        （nonstream_completion / fake_stream_handlers /
        native_stream_handlers 创建 GeminiClient 处）调用；
        update_stats 仍在完成时调用（它同时维护计数器/时间序列/token）。
        完成路径的窗口写入保留 —— 双写对滑动窗口是幂等的（多计一次
        会让退避略保守，这正是高负载下需要的方向；反之漏计会让退避
        失效）。
        """
        now_ts = time.time()
        with self._rpm_lock:
            if (
                api_key not in self._key_rpm_windows
                and len(self._key_rpm_windows) >= _MAX_TRACKED_KEYS
            ):
                return  # 防御性上限：密钥池异常庞大时放弃追踪新 key
            window = self._key_rpm_windows[api_key]
            window.append(now_ts)
            cutoff = now_ts - _RPM_WINDOW_SECONDS
            while window and window[0] < cutoff:
                window.popleft()

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

        Round 5: 改读 per-key 滑动窗口（_key_rpm_windows），不再依赖全局
        recent_calls——旧实现在总调用量 > 100/min 时窗口记录先被 maxlen
        淘汰，per-key 计数恒为 0，RPM 退避失效（详见 __init__ 注释）。
        读取时同样从左侧裁剪过期时间戳，之后的 len() 即为窗口内计数。
        """
        cutoff = (
            (time.time() if now is None else now.timestamp())
            - _RPM_WINDOW_SECONDS
        )

        with self._rpm_lock:
            window = self._key_rpm_windows.get(api_key)
            if not window:
                return 0
            while window and window[0] < cutoff:
                window.popleft()
            return len(window)

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

        with self._rpm_lock:
            self._key_rpm_windows.clear()

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


def record_outbound_attempt(api_key: str, model: str = "") -> None:
    """在上游请求发射时记录 RPM 窗口（同步轻量，详见方法 docstring）。"""
    api_stats_manager.record_outbound_attempt(api_key, model)


# Default Gemini free-tier RPM safety threshold.  When a key's last-minute
# call count is at or above this fraction of MAX_OUTBOUND_RPM, we should
# skip it during key selection to avoid hitting the upstream limit.
# Default 0.8 = use the key only when it's at less than 80% of the limit.
import os as _os
MAX_OUTBOUND_RPM = int(_os.environ.get("MAX_OUTBOUND_RPM", "15"))  # Gemini free-tier default
OUTBOUND_RPM_BACKOFF_FRACTION = float(_os.environ.get("OUTBOUND_RPM_BACKOFF_FRACTION", "0.8"))
