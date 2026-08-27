import random


def should_continue_retry(current_try_num: int, max_retry_num: int, empty_response_count: int, max_empty_responses: int) -> bool:
    return current_try_num < max_retry_num and empty_response_count < max_empty_responses


def compute_inter_batch_backoff(
    failed_batches: int, *, base: float = 0.5, cap: float = 8.0
) -> float:
    """Full-jitter delay to sleep between FAILED retry batches.

    Round 4 anti-fingerprint fix: 此前 4 个重试循环（nonstream 两条、
    stream/fake-stream 各一条）在一批密钥全部失败后**立即**发射下一
    批——从上游视角这是教科书级的重试风暴（N 个请求在同一瞬间再来
    N 个），也是最容易触发谷歌风控升级的行为模式之一。
    app.utils.stealth.full_jitter_backoff 早已存在但从未被这些路径使用。

    本 helper 返回 [0, min(base * 2**failed_batches, cap)] 区间内的
    均匀随机值（AWS 推荐 full-jitter 策略）：
      第 1 次批间失败 → 0~0.5s
      第 2 次         → 0~1s
      第 3 次         → 0~2s
      第 4 次及以上    → 0~8s（封顶，避免长尾请求挂死客户端）

    与并发重试者的同步退避不同，jitter 让并发请求的重试时刻错开，
    不会形成可观测的周期性脉冲。首个批次之前不等待（failed_batches
    为 0 时调用方不应 sleep），成功路径也无需等待。
    """
    if failed_batches <= 0:
        return 0.0
    expo = min(base * (2 ** (failed_batches - 1)), cap)
    return random.uniform(0.0, expo)


def next_batch_size(current_try_num: int, max_retry_num: int, current_concurrent: int) -> int:
    return min(max_retry_num - current_try_num, current_concurrent)


def increase_concurrency(current_concurrent: int, increase_by: int, max_concurrent: int) -> int:
    """Cap concurrency at max_concurrent.

    Retained for backwards compatibility — callers that previously used
    this to grow concurrency on failure should now use
    `decrease_concurrency` instead (see below).
    """
    return min(current_concurrent + increase_by, max_concurrent)


def decrease_concurrency(current_concurrent: int, decrease_by: int, min_concurrent: int = 1) -> int:
    """Reduce concurrency after a failed batch.

    This is the corrected behaviour for "failure on upstream":
    the previous code did the opposite — it grew concurrency on failure,
    which from the upstream provider's perspective is exactly the
    request-rate doubling pattern that triggers anti-bot rate limiters
    and longer bans.  Failure should reduce load and let the upstream
    recover.
    """
    if current_concurrent <= 1:
        return 1
    return max(current_concurrent - max(decrease_by, 1), min_concurrent)


def reached_empty_response_limit(empty_response_count: int, max_empty_responses: int) -> bool:
    return empty_response_count >= max_empty_responses


def remove_completed_tasks(tasks):
    return [(key, task) for key, task in tasks if not task.done()]


def cancel_pending_tasks(tasks):
    for _, task in tasks:
        if not task.done():
            task.cancel()
