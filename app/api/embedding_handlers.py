"""embedding 与 vector 的业务逻辑实现。"""
from fastapi import HTTPException


async def _vector_body(request):
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="请求正文必须是有效的 JSON 对象") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求正文必须是 JSON 对象")
    if not isinstance(body.get("model"), str) or not body["model"].strip():
        raise HTTPException(status_code=400, detail="缺少有效的 model 参数")
    return body


async def create_embeddings_with_key(request, key_manager, embedding_client_cls):
    api_key = await key_manager.get_available_key()
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: No available API keys",
        )

    client = embedding_client_cls(api_key)
    return await client.create_embeddings(request)


async def handle_vector_query(request, key_manager, embedding_request_cls, embedding_client_cls, log):
    log("INFO", f"收到向量查询请求")
    body = await _vector_body(request)
    search_text = body.get("searchText")
    model = body.get("model")

    if not isinstance(search_text, str) or not search_text.strip():
        raise HTTPException(status_code=400, detail="searchText 必须是非空字符串")
    api_key = await key_manager.get_available_key()
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="没有可用的 API 密钥",
        )

    client = embedding_client_cls(api_key)
    embedding_request = embedding_request_cls(input=search_text, model=model)
    embedding_response = await client.create_embeddings(embedding_request)

    return {
        "hashes": [],
        "metadata": [],
        "items": [
            {"text": "", "score": 0, "metadata": {"embedding": data.embedding}}
            for data in embedding_response.data
        ],
        "similarities": [0] * len(embedding_response.data),
    }


async def handle_vector_insert(request, key_manager, embedding_request_cls, embedding_client_cls, log):
    log("INFO", f"收到向量写入请求")
    body = await _vector_body(request)
    items = body.get("items", [])
    model = body.get("model")

    # Hardening: previously no upper bound on the number of items.
    # An attacker could submit 10 000 items × 10 KB each = 100 MB
    # request body, with each item triggering a separate upstream
    # embedding call.  We cap to a safe maximum to prevent both
    # memory exhaustion and upstream-quota abuse.
    MAX_VECTOR_INSERT_ITEMS = 100
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_VECTOR_INSERT_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"items 必须包含 1 至 {MAX_VECTOR_INSERT_ITEMS} 个对象",
        )
    if any(not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip() for item in items):
        raise HTTPException(status_code=400, detail="每个写入对象都必须包含非空的 text 字符串")

    api_key = await key_manager.get_available_key()
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="没有可用的 API 密钥",
        )

    texts = [item.get("text") for item in items]
    client = embedding_client_cls(api_key)
    embedding_request = embedding_request_cls(input=texts, model=model)
    await client.create_embeddings(embedding_request)
    return {"success": True}
