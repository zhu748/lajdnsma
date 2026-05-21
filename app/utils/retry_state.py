def should_continue_retry(current_try_num: int, max_retry_num: int, empty_response_count: int, max_empty_responses: int) -> bool:
    return current_try_num < max_retry_num and empty_response_count < max_empty_responses


def next_batch_size(current_try_num: int, max_retry_num: int, current_concurrent: int) -> int:
    return min(max_retry_num - current_try_num, current_concurrent)


def increase_concurrency(current_concurrent: int, increase_by: int, max_concurrent: int) -> int:
    return min(current_concurrent + increase_by, max_concurrent)


def reached_empty_response_limit(empty_response_count: int, max_empty_responses: int) -> bool:
    return empty_response_count >= max_empty_responses


def remove_completed_tasks(tasks):
    return [(key, task) for key, task in tasks if not task.done()]
