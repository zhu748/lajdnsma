from app.vertex.models import OpenAIMessage, OpenAIRequest


def build_vertex_openai_request(request):
    openai_messages = [
        OpenAIMessage(
            role=message.get("role", ""),
            content=message.get("content", ""),
        )
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
        stop=request.stop,
        presence_penalty=request.presence_penalty,
        frequency_penalty=request.frequency_penalty,
        seed=getattr(request, "seed", None),
        logprobs=getattr(request, "logprobs", None),
        response_logprobs=getattr(request, "response_logprobs", None),
        n=request.n,
    )
