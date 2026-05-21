from fastapi.responses import StreamingResponse

from app.utils.protocol_adapters import (
    claude_request_to_chat_request,
    openai_chat_to_claude_response,
    openai_chat_to_response_api,
    openai_stream_to_claude_stream,
    openai_stream_to_responses_stream,
    response_request_to_chat_request,
)


async def handle_responses_request(payload: dict, http_request, auth_dep, user_agent_dep, chat_handler):
    normalized_request = response_request_to_chat_request(payload)
    response = await chat_handler(normalized_request, http_request, auth_dep, user_agent_dep)

    if isinstance(response, StreamingResponse):
        return StreamingResponse(
            openai_stream_to_responses_stream(
                response.body_iterator, normalized_request.model
            ),
            media_type="text/event-stream",
        )

    return openai_chat_to_response_api(response)


async def handle_claude_messages_request(payload: dict, http_request, auth_dep, user_agent_dep, chat_handler):
    normalized_request = claude_request_to_chat_request(payload)
    response = await chat_handler(normalized_request, http_request, auth_dep, user_agent_dep)

    if isinstance(response, StreamingResponse):
        return StreamingResponse(
            openai_stream_to_claude_stream(
                response.body_iterator, normalized_request.model
            ),
            media_type="text/event-stream",
        )

    return openai_chat_to_claude_response(response)
