import asyncio

from app.api.nonstream_status_handlers import handle_nonstream_task_status
from app.utils.retry_state import remove_completed_tasks


async def run_nonstream_batch_until_success(
    *,
    tasks,
    tasks_map,
    chat_request,
    response_cache_manager,
    cache_key: str,
    is_gemini: bool,
    empty_response_count: int,
    wait_timeout: float | None = None,
    serialize_json: bool = False,
):
    """Run a keyed nonstream task batch until success, exhaustion, or keepalive tick.

    Returns a dict with:
    - status: "success" | "pending" | "exhausted"
    - response: final response when success, else None
    - empty_response_count: updated empty response count
    - tasks: remaining pending keyed tasks
    """
    while tasks:
        wait_kwargs = {"return_when": asyncio.FIRST_COMPLETED}
        if wait_timeout is not None:
            wait_kwargs["timeout"] = wait_timeout

        done, _ = await asyncio.wait([task for _, task in tasks], **wait_kwargs)
        if not done:
            return {
                "status": "pending",
                "response": None,
                "empty_response_count": empty_response_count,
                "tasks": tasks,
            }

        for task in done:
            api_key = tasks_map[task]
            status, response, empty_response_count = await handle_nonstream_task_status(
                task=task,
                api_key=api_key,
                chat_request=chat_request,
                response_cache_manager=response_cache_manager,
                cache_key=cache_key,
                is_gemini=is_gemini,
                empty_response_count=empty_response_count,
                serialize_json=serialize_json,
            )
            if status == "success":
                return {
                    "status": "success",
                    "response": response,
                    "empty_response_count": empty_response_count,
                    "tasks": tasks,
                }

            tasks = remove_completed_tasks(tasks)

    return {
        "status": "exhausted",
        "response": None,
        "empty_response_count": empty_response_count,
        "tasks": tasks,
    }
