from fastapi import HTTPException


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
    log("INFO", f"Received vector query request with headers: {request.headers}")
    body = await request.json()
    search_text = body.get("searchText")
    model = body.get("model")

    api_key = await key_manager.get_available_key()
    if not all([search_text, model, api_key]):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Missing required parameters or no available API keys",
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
    log("INFO", f"Received vector insert request with headers: {request.headers}")
    body = await request.json()
    items = body.get("items", [])
    model = body.get("model")

    api_key = await key_manager.get_available_key()
    if not all([items, model, api_key]):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Missing required parameters or no available API keys",
        )

    texts = [item.get("text") for item in items]
    client = embedding_client_cls(api_key)
    embedding_request = embedding_request_cls(input=texts, model=model)
    await client.create_embeddings(embedding_request)
    return {"success": True}
