from app.utils.upstream_reporting import observe_upstream
"""Gemini embedding 上游客户端（batchEmbedContents）。"""
import base64
import struct
from app.models.schemas import EmbeddingRequest, EmbeddingData, EmbeddingResponse, Usage
from app.utils.http_client import get_async_client
from app.utils.logging import log
from app.utils.stealth import build_embedding_headers

class EmbeddingClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    @observe_upstream("AI Studio")
    async def create_embeddings(self, request: EmbeddingRequest) -> EmbeddingResponse:
        model_name = request.model.removeprefix("models/")
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:batchEmbedContents"
        headers = build_embedding_headers(self.api_key)
        headers["x-goog-api-key"] = self.api_key

        if isinstance(request.input, str):
            inputs = [request.input]
        else:
            inputs = request.input

        # The Gemini API expects a list of contents, so we format it this way.
        data = {
            "requests": [
                {
                    "model": f"models/{model_name}",
                    "content": {
                        "parts": [{"text": text}]
                    }
                } for text in inputs
            ]
        }
        
        extra_log = {
            "key": "key#" + str(hash(self.api_key) & 0xFFFFFF),
            "model": model_name,
        }
        log("INFO", f"向量生成请求开始", extra=extra_log)

        client = await get_async_client()
        response = await client.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()

        response_json = response.json()

        # The response is a JSON object with an "embeddings" key.
        # Each item in the list is an object with a "values" key.
        embeddings = response_json.get("embeddings", [])
        if len(embeddings) != len(inputs):
            raise ValueError("Upstream returned an unexpected number of embeddings")
        log(
            "INFO",
            f"向量生成请求完成，返回 {len(embeddings)} 个向量",
            extra=extra_log,
        )

        embedding_data = []
        for i, item in enumerate(embeddings):
            values = item["values"]
            if request.encoding_format == "base64":
                values = base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")
            embedding_data.append(EmbeddingData(embedding=values, index=i))

        # The Gemini API does not provide token usage for embeddings.
        # We'll return a default usage object.
        usage = Usage(prompt_tokens=0, total_tokens=0)

        return EmbeddingResponse(
            object="list",
            data=embedding_data,
            model=model_name,
            usage=usage,
        )
