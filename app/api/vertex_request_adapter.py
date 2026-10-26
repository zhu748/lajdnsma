"""ChatCompletionRequest → Vertex OpenAIRequest 的请求映射。"""
from app.vertex.models import OpenAIMessage, OpenAIRequest


def build_vertex_openai_request(request):
    openai_messages = [
        OpenAIMessage(**message)
        for message in request.messages
    ]

    return OpenAIRequest(
        model=request.model,
        messages=openai_messages,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        top_p=request.top_p,
        top_k=request.top_k,
        stream=request.stream,
        stop=[request.stop] if isinstance(request.stop, str) else request.stop,
        presence_penalty=request.presence_penalty,
        frequency_penalty=request.frequency_penalty,
        seed=getattr(request, "seed", None),
        logprobs=getattr(request, "logprobs", None),
        response_logprobs=getattr(request, "response_logprobs", None),
        n=request.n,
        **{
            key: getattr(request, key) for key in (
                "tools", "tool_choice", "parallel_tool_calls", "response_format",
                "reasoning_effort", "thinking_budget", "expose_reasoning",
            ) if getattr(request, key, None) is not None
        },
    )
