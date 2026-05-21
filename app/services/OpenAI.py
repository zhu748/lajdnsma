import os
from app.models.schemas import ChatCompletionRequest
from dataclasses import dataclass
from typing import Optional
import secrets
import string
import app.config.settings as settings

from app.utils.http_client import get_async_client
from app.utils.logging import log
from app.utils.sse import iter_sse_json


def generate_secure_random_string(length):
    all_characters = string.ascii_letters + string.digits
    secure_random_string = "".join(
        secrets.choice(all_characters) for _ in range(length)
    )
    return secure_random_string


@dataclass
class GeneratedText:
    text: str
    finish_reason: Optional[str] = None


class OpenAIClient:
    AVAILABLE_MODELS = []
    EXTRA_MODELS = os.environ.get("EXTRA_MODELS", "").split(",")

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _request_to_dict(request: ChatCompletionRequest):
        if hasattr(request, "model_dump"):
            return request.model_dump(exclude_none=True)
        if isinstance(request, dict):
            return request
        return {
            key: value
            for key, value in vars(request).items()
            if not key.startswith("_") and value is not None
        }

    def filter_data_by_whitelist(data, allowed_keys):
        """
        根据白名单过滤字典。
        Args:
            data (dict): 原始的 Python 字典 (代表 JSON 对象)。
            allowed_keys (list or set): 包含允许保留的键名的列表或集合。
                                        使用集合 (set) 进行查找通常更快。
        Returns:
            dict: 只包含白名单中键的新字典。
        """
        # 使用集合(set)可以提高查找效率，特别是当白名单很大时
        allowed_keys_set = set(allowed_keys)
        # 使用字典推导式创建过滤后的新字典
        filtered_data = {
            key: value for key, value in data.items() if key in allowed_keys_set
        }
        return filtered_data

    # 真流式处理
    async def stream_chat(self, request: ChatCompletionRequest):
        whitelist = [
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "stream",
            "tools",
            "reasoning_effort",
            "top_k",
            "presence_penalty",
        ]

        request_data = self._request_to_dict(request)
        data = self.filter_data_by_whitelist(request_data, whitelist)

        if settings.search["search_mode"] and data["model"].endswith("-search"):
            log(
                "INFO",
                "开启联网搜索模式",
                extra={"key": self.api_key[:8], "model": request.model},
            )
            data.setdefault("tools", []).append({"google_search": {}})

        data["model"] = data["model"].removesuffix("-search")

        # 真流式请求处理逻辑
        extra_log = {
            "key": self.api_key[:8],
            "request_type": "stream",
            "model": request.model,
        }
        log("INFO", "流式请求开始", extra=extra_log)

        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        client = await get_async_client()
        async with client.stream(
            "POST", url, headers=headers, json=data, timeout=600
        ) as response:
            try:
                async for data in iter_sse_json(response):
                    yield data
            except Exception as e:
                log(
                    "ERROR",
                    "流式处理期间发生错误",
                    extra={
                        "key": self.api_key[:8],
                        "request_type": "stream",
                        "model": request.model,
                    },
                )
                raise e
            finally:
                log("info", "流式请求结束")
