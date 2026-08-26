def should_continue_retry(current_try_num: int, max_retry_num: int, empty_response_count: int, max_empty_responses: int) -> bool:
    return current_try_num < max_retry_num and empty_response_count < max_empty_responses


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
