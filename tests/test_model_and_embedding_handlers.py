import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_model_handlers():
    fake_schemas = types.ModuleType("app.models.schemas")

    class ModelList:
        def __init__(self, data, object="list"):
            self.object = object
            self.data = data

    fake_schemas.ModelList = ModelList
    sys.modules["app.models.schemas"] = fake_schemas

    spec = importlib.util.spec_from_file_location(
        "model_handlers", ROOT / "app/api/model_handlers.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_embedding_handlers():
    fake_fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fake_fastapi.HTTPException = HTTPException
    sys.modules["fastapi"] = fake_fastapi

    spec = importlib.util.spec_from_file_location(
        "embedding_handlers", ROOT / "app/api/embedding_handlers.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, HTTPException


class ModelAndEmbeddingHandlersTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_build_aistudio_model_list(self):
        module = load_model_handlers()
        result = module.build_aistudio_model_list(
            ["a", "b", "c"], whitelist_models=["a", "c"], blocked_models=["b"]
        )
        self.assertEqual([item["id"] for item in result.data], ["a", "c"])

        result = module.build_aistudio_model_list(
            ["a", "b", "c"], whitelist_models=[], blocked_models=["b"]
        )
        self.assertEqual([item["id"] for item in result.data], ["a", "c"])

    async def test_embedding_and_vector_handlers(self):
        module, HTTPException = load_embedding_handlers()

        class KeyManager:
            async def get_available_key(self):
                return "key-1"

        class EmbeddingRequest:
            def __init__(self, input, model):
                self.input = input
                self.model = model

        class EmbeddingData:
            def __init__(self, embedding):
                self.embedding = embedding

        class EmbeddingClient:
            def __init__(self, api_key):
                self.api_key = api_key

            async def create_embeddings(self, request):
                return types.SimpleNamespace(data=[EmbeddingData([0.1, 0.2])])

        req = EmbeddingRequest("hello", "model-a")
        result = await module.create_embeddings_with_key(req, KeyManager(), EmbeddingClient)
        self.assertEqual(result.data[0].embedding, [0.1, 0.2])

        class Request:
            headers = {"x": "1"}

            async def json(self):
                return {"searchText": "hello", "model": "model-a"}

        vector_result = await module.handle_vector_query(
            Request(), KeyManager(), EmbeddingRequest, EmbeddingClient, lambda *a, **k: None
        )
        self.assertEqual(vector_result["items"][0]["metadata"]["embedding"], [0.1, 0.2])

        class InsertRequest:
            headers = {"x": "1"}

            async def json(self):
                return {"items": [{"text": "a"}, {"text": "b"}], "model": "model-a"}

        insert_result = await module.handle_vector_insert(
            InsertRequest(), KeyManager(), EmbeddingRequest, EmbeddingClient, lambda *a, **k: None
        )
        self.assertEqual(insert_result, {"success": True})

        class EmptyKeyManager:
            async def get_available_key(self):
                return None

        with self.assertRaises(HTTPException):
            await module.create_embeddings_with_key(req, EmptyKeyManager(), EmbeddingClient)


if __name__ == "__main__":
    unittest.main()
