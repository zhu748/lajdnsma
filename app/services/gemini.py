import json
import os
from app.models.schemas import ChatCompletionRequest
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Union
import secrets
import string
import app.config.settings as settings

# 纯函数工具（零 app 依赖，从本文件拆出；历史符号名保持不变以便引用方兼容）
from app.services.gemini_payload import (
    GEMINI_MAX_THINKING_BUDGET,
    _clamp_thinking_budget,
    _guess_mime_type_from_url,
    sanitize_gemini_payload,
    sanitize_gemini_schema,
)

from app.utils.http_client import get_async_client, UPSTREAM_TIMEOUT
from app.utils.logging import log
from app.utils.model_mapping import (
    DEFAULT_PREFILL_FIX_PROMPT,
    build_prefill_fallback_instruction,
    build_prefill_user_turn,
    extract_trailing_model_prefill,
    needs_model_turn_fix,
)
from app.utils.prefill_echo import (
    PrefillEchoStreamFilter,
    build_tail_chunk,
    get_current_prefill_echo,
    set_current_prefill_echo,
)
from app.utils.sse import iter_sse_json
from app.utils.thinking import (
    apply_thinking_override,
    sanitize_thinking_for_model,
)
from app.utils.stealth import (
    build_gemini_headers,
    build_key_probe_headers,
    gen_anthropic_thinking_signature,
)


def _generate_thought_signature() -> str:
    # Previously this was the literal string `"skip_thought_signature_validator"`,
    # which was injected into the Gemini request whenever an assistant tool_call
    # was missing a thoughtSignature.  The literal is a highly-recognisable
    # proxy fingerprint (real Gemini thoughtSignatures are opaque ~200-char
    # base64-like strings).  We now generate a strong-random signature of
    # the same shape on demand; the function call preserves the legacy
    # name as a single import surface so callers don't break.
    return gen_anthropic_thinking_signature()


def generate_secure_random_string(length):
    all_characters = string.ascii_letters + string.digits
    secure_random_string = "".join(
        secrets.choice(all_characters) for _ in range(length)
    )
    return secure_random_string


def _apply_prefill_fix(contents: Optional[List[Dict[str, Any]]], model: str) -> Optional[str]:
    """对以 model turn 结尾的 contents 应用 prefill 兼容补丁，返回 prefill 原文。

    原生 Gemini 协议路径（_convert_request_data）与 OpenAI 协议路径
    （convert_messages）共享的补丁本体 —— 历史版本两处各维护一份逐分支
    复制的 ~70 行实现。语义（对齐 vertex-master）：
      1. 只把「末尾纯文本 model turn」识别为 prefill（functionCall /
         thought / 媒体结尾是真实历史，保持原样）；
      2. 前一非空轮是 user/function → 追加续写指令 user turn；
         model-only / 连续 model 等畸形历史 → 兑底改写为 JSON 数据
         内嵌的 user 指令（结构上必然合法）；
      3. 返回 prefill 原文，由调用方经 ContextVar 透传给响应侧回显剥离。

    前置条件（由调用方保证）：contents 已过 sanitize（assistant→model
    角色归一）、模型命中 needs_model_turn_fix、配置开关已检查。
    """
    _prefill_ctx = extract_trailing_model_prefill(contents)
    if _prefill_ctx is None:
        return None
    _prefill_text, _preceding_role, _prefill_index = _prefill_ctx
    _fix_prompt = (
        getattr(settings, "PREFILL_FIX_PROMPT", None) or DEFAULT_PREFILL_FIX_PROMPT
    )
    if _preceding_role in ("user", "function"):
        # 常规 prefill：保留 model turn 原文，末尾追加续写指令
        # （官方迁移指南要求的兼容形态）。
        contents.append(build_prefill_user_turn(_fix_prompt))
        log("INFO", f"已追加 user turn 修复 prefill 兼容（{model}）")
    else:
        # 畸形历史兑底：移除末尾 model turn，prefill 原文以 JSON 数据
        # 形式内嵌进 user 指令（并入既有 user 轮，或新建 user 轮 ——
        # 两种都保证结尾合法）。
        del contents[_prefill_index]
        _instruction = build_prefill_fallback_instruction(_prefill_text)
        _merged = False
        if (
            contents
            and isinstance(contents[-1], dict)
            and contents[-1].get("role") == "user"
        ):
            contents[-1].setdefault("parts", []).append({"text": _instruction})
            _merged = True
        if not _merged:
            contents.append({"role": "user", "parts": [{"text": _instruction}]})
        log(
            "INFO",
            f"检测到畸形历史（model-only/连续 model），已按 JSON 兑底改写 prefill（{model}）",
        )
    return _prefill_text


def _log_upstream_error_summary(response, key_id: str, request_type: str, model: str):
    """非 200 响应时记录脱敏后的上游错误摘要（stream / non-stream 共享）。

    Round 6（详细日志）：把 message/status/quota 维度的摘要记入面板
    日志 —— 此前 body 被消费后直接丢弃，排障只剩状态码。摘要失败
    静默降级（不影响原有的 raise_for_status 流程）。
    """
    try:
        from app.utils.error_handling import summarize_upstream_error

        summary = summarize_upstream_error(response)
        if summary:
            log(
                "warning",
                f"上游错误 ({request_type}, HTTP {response.status_code}): {summary}",
                extra={
                    "key": key_id,
                    "request_type": request_type,
                    "model": model,
                    "status_code": response.status_code,
                },
            )
    except Exception:
        pass


def _key_log_id(api_key: str) -> str:
    # Don't log raw key prefix (AIzaSy12 is a recognisable Gemini
    # key prefix).  Use a stable hash so the same key still shows
    # up as the same identifier in the dashboard.
    return "key#" + str(hash(api_key) & 0xFFFFFF)


@dataclass
class GeneratedText:
    text: str
    finish_reason: Optional[str] = None


class GeminiResponseWrapper:
    def __init__(self, data: Dict[Any, Any]):
        self._data = data
        self._text = self._extract_text()
        self._finish_reason = self._extract_finish_reason()
        self._prompt_token_count = self._extract_prompt_token_count()
        self._candidates_token_count = self._extract_candidates_token_count()
        self._total_token_count = self._extract_total_token_count()
        self._thoughts = self._extract_thoughts()
        self._function_call = self._extract_function_call()
        self._json_dumps = None
        self._model = "gemini"

    def _extract_thoughts(self) -> Optional[str]:
        try:
            thoughts = []
            for part in self._data["candidates"][0]["content"]["parts"]:
                if part.get("thought"):
                    thoughts.append(part.get("text", ""))
            return "".join(thoughts) if thoughts else None
        except (KeyError, IndexError):
            return None

    def _extract_text(self) -> str:
        try:
            text = ""
            for part in self._data["candidates"][0]["content"]["parts"]:
                if not part.get("thought") and "text" in part:
                    text += part["text"]
            return text
        except (KeyError, IndexError):
            return ""

    def _extract_function_call(self) -> Optional[List[Dict[str, Any]]]:
        try:
            parts = (
                self._data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [])
            )
            function_calls = []
            for part in parts:
                if not isinstance(part, dict) or "functionCall" not in part:
                    continue
                function_call = dict(part["functionCall"])
                thought_signature = part.get("thoughtSignature") or part.get(
                    "thought_signature"
                )
                if thought_signature:
                    function_call["extra_content"] = {
                        "google": {"thought_signature": thought_signature}
                    }
                function_calls.append(function_call)
            # 如果列表不为空，则返回列表；否则返回 None
            return function_calls if function_calls else None
        except (KeyError, IndexError, TypeError):
            return None

    def _extract_finish_reason(self) -> Optional[str]:
        try:
            return self._data["candidates"][0].get("finishReason")
        except (KeyError, IndexError):
            return None

    def _extract_prompt_token_count(self) -> Optional[int]:
        try:
            return self._data["usageMetadata"].get("promptTokenCount")
        except KeyError:
            return None

    def _extract_candidates_token_count(self) -> Optional[int]:
        try:
            return self._data["usageMetadata"].get("candidatesTokenCount")
        except KeyError:
            return None

    def _extract_total_token_count(self) -> Optional[int]:
        try:
            return self._data["usageMetadata"].get("totalTokenCount")
        except KeyError:
            return None

    def set_model(self, model) -> Optional[str]:
        self._model = model

    @property
    def data(self) -> Dict[Any, Any]:
        return self._data

    @property
    def text(self) -> str:
        return self._text

    @property
    def finish_reason(self) -> Optional[str]:
        return self._finish_reason

    @property
    def prompt_token_count(self) -> Optional[int]:
        return self._prompt_token_count

    @property
    def candidates_token_count(self) -> Optional[int]:
        return self._candidates_token_count

    @property
    def total_token_count(self) -> Optional[int]:
        return self._total_token_count

    @property
    def thoughts(self) -> Optional[str]:
        return self._thoughts

    @property
    def json_dumps(self) -> str:
        if self._json_dumps is None:
            self._json_dumps = json.dumps(self._data, indent=4, ensure_ascii=False)
        return self._json_dumps

    @property
    def model(self) -> str:
        return self._model

    @property
    def function_call(self) -> Optional[List[Dict[str, Any]]]:
        return self._function_call


class GeminiClient:
    AVAILABLE_MODELS = []
    extra_models_str = os.environ.get("EXTRA_MODELS", "")
    EXTRA_MODELS = [
        model.strip() for model in extra_models_str.split(",") if model.strip()
    ]

    def __init__(self, api_key: str):
        self.api_key = api_key

    # 请求参数处理
    def _convert_request_data(
        self, request, contents, safety_settings, system_instruction
    ):
        model = request.model
        format_type = getattr(request, "format_type", None)
        if format_type and (format_type == "gemini"):
            requested_api_version = getattr(request, "api_version", None)
            api_version = (
                requested_api_version
                if requested_api_version in {"v1", "v1beta", "v1alpha"}
                else "v1alpha"
                if "think" in request.model
                else "v1beta"
            )
            if request.payload:
                # 将 Pydantic 模型转换为字典, 假设 Pydantic V2+
                data = request.payload.model_dump(exclude_none=True, by_alias=True)
                if "systemInstruction" in data:
                    data.pop("system_instruction", None)
            # # 注入搜索提示
            # if settings.search["search_mode"] and request.model and request.model.endswith("-search"):
            #     data.insert(len(data)-2,{'role': 'user', 'parts': [{'text':settings.search["search_prompt"]}]})

            # # 注入随机字符串
            # if settings.RANDOM_STRING:
            #     data.insert(1,{'role': 'user', 'parts': [{'text': generate_secure_random_string(settings.RANDOM_STRING_LENGTH)}]})
            #     data.insert(len(data)-1,{'role': 'user', 'parts': [{'text': generate_secure_random_string(settings.RANDOM_STRING_LENGTH)}]})
            #     log('INFO', "伪装消息成功")

        else:
            api_version, data = self._convert_openAI_request(
                request, contents, safety_settings, system_instruction
            )

        data = sanitize_gemini_payload(data)

        # 联网模式
        if settings.search["search_mode"] and request.model.endswith("-search"):
            log(
                "INFO",
                "开启联网搜索模式",
                # Use hash-based key id instead of prefix (which is a
                # highly-recognisable Gemini key prefix `AIzaSy12`).
                extra={"key": "key#" + str(hash(self.api_key) & 0xFFFFFF), "model": request.model},
            )

            data.setdefault("tools", []).append({"google_search": {}})
            model = request.model.removesuffix("-search")

        # --- Prefill 补丁：原生 Gemini 协议路径 ---
        # OpenAI / Claude / Responses 协议的补丁在 convert_messages 末尾；
        # 原生 Gemini 请求（format_type="gemini"）的 payload 直接 dump
        # 透传、不经 convert_messages，此前完全无补丁 —— 新世代模型上
        # 以 model turn 结尾的原生 prefill 请求同样命中 400
        # （"Requests ending with a model turn are not supported."）。
        # 必须放在 sanitize_gemini_payload（assistant→model 角色归一）
        # 之后，否则识别不到被归一化的 assistant 结尾。
        # 补丁本体见 _apply_prefill_fix（两条路径共享）。
        _prefill_echo = None
        if (
            format_type == "gemini"
            and getattr(settings, "PREFILL_FIX", True)
            and model
            and needs_model_turn_fix(
                model,
                extra_include=getattr(settings, "PREFILL_FIX_MODELS", None) or [],
                exclude=getattr(settings, "PREFILL_FIX_EXCLUDE_MODELS", None) or [],
            )
        ):
            _prefill_echo = _apply_prefill_fix(data.get("contents"), model)
        # 同一 async 任务内的请求/响应共享上下文：这里必须显式写入
        # （含 None），避免上一请求的值泄漏；OpenAI 协议路径由
        # convert_messages 在请求准备阶段写入，此分支不覆盖。
        if format_type == "gemini":
            set_current_prefill_echo(_prefill_echo)

        # 最高思考强度覆盖（见 app/utils/thinking.py）：
        # 对两条路径（Gemini 原生 payload / OpenAI 转换产物）的最终 data
        # 统一应用。此时 model 已完成 -search 后缀剥离，家族判定准确。
        # 无覆盖时（返回 None）不改动任何内容，保持请求原样透传。
        apply_thinking_override(model, data)

        # 等级制模型（3.x/4.x）防呆净化：无论 budget 来自代理合成
        # （reasoning_effort / thinking_budget 换算）还是客户端直发，
        # 4.x 携带 thinkingBudget 都会 400 —— 就近换算为 thinkingLevel。
        # 2.5 系 budget 家族不受影响；ENABLE_THINKING 关闭时不生效。
        sanitize_thinking_for_model(model, data)

        return api_version, model, data

    def _convert_openAI_request(
        self,
        request: ChatCompletionRequest,
        contents,
        safety_settings,
        system_instruction,
    ):
        config_params = {
            "temperature": request.temperature,
            "maxOutputTokens": request.max_tokens,
            "topP": request.top_p,
            "topK": request.top_k,
            "stopSequences": request.stop
            if isinstance(request.stop, list)
            else [request.stop]
            if request.stop is not None
            else None,
            "candidateCount": request.n,
        }
        thinking_config = {}
        if settings.ENABLE_THINKING:
            # Map OpenAI's `reasoning_effort` (low/medium/high) to a
            # Gemini `thinkingBudget`.  OpenAI clients (e.g. o1-style
            # prompts sent via the chat completions API) set this field
            # to control reasoning depth; previously we silently
            # ignored it, which caused high-effort requests to
            # produce low-budget reasoning.  Only honour it when the
            # caller didn't set an explicit `thinking_budget`
            # (explicit budget wins).
            if (
                request.thinking_budget is None
                and getattr(request, "reasoning_effort", None)
                and request.reasoning_effort.lower() in {"low", "medium", "high"}
            ):
                effort = request.reasoning_effort.lower()
                # Heuristic mapping: low ~ 1024, medium ~ 8192,
                # high ~ 24576 (Gemini's cap).  Real values depend on
                # the model but these are sensible defaults.
                effort_budget = {"low": 1024, "medium": 8192, "high": GEMINI_MAX_THINKING_BUDGET}[effort]
                thinking_config["thinkingBudget"] = effort_budget

            if request.thinking_budget is not None:
                thinking_config["thinkingBudget"] = _clamp_thinking_budget(
                    request.thinking_budget
                )

            if getattr(request, "expose_reasoning", getattr(request, "enable_thinking", False)):
                thinking_config["include_thoughts"] = True

        if thinking_config:
            config_params["thinkingConfig"] = thinking_config
            
        generationConfig = {k: v for k, v in config_params.items() if v is not None}

        api_version = "v1alpha" if "think" in request.model else "v1beta"

        data = {
            "contents": contents,
            "generationConfig": generationConfig,
        }
        # Only attach safetySettings when caller explicitly passes a non-empty
        # list.  Sending all-OFF safetySettings on every request is one of the
        # most obvious "this is a proxy" fingerprints (real users almost
        # never do that).  See app/config/safety.py:get_safety_settings.
        if safety_settings:
            data["safetySettings"] = safety_settings

        # --- 函数调用处理 ---
        # 1. 添加 tools (函数声明)
        function_declarations = []
        if request.tools:
            # 显式提取 Gemini API 所需的字段，避免包含 'id' 等无效字段
            function_declarations = []
            for tool in request.tools:
                if tool.get("type") == "function":
                    func_def = tool.get("function")
                    if func_def:
                        # 只包含 Gemini API 接受的字段
                        declaration = {
                            "name": func_def.get("name"),
                            "description": func_def.get("description"),
                        }
                        # 获取 parameters 并移除可能存在的 $schema 字段
                        parameters = sanitize_gemini_schema(func_def.get("parameters"))
                        if parameters is not None:
                            declaration["parameters"] = parameters

                        # 移除值为 None 的键，以保持 payload 清洁
                        declaration = {
                            k: v for k, v in declaration.items() if v is not None
                        }
                        if declaration.get("name"):  # 确保 name 存在
                            function_declarations.append(declaration)

        if function_declarations:
            data["tools"] = [{"function_declarations": function_declarations}]

        # 2. 添加 tool_config (基于 tool_choice)
        tool_config = None
        if request.tool_choice:
            choice = request.tool_choice
            mode = None
            allowed_functions = None
            if isinstance(choice, str):
                if choice == "none":
                    mode = "NONE"
                elif choice == "auto":
                    mode = "AUTO"
            elif isinstance(choice, dict) and choice.get("type") == "function":
                func_name = choice.get("function", {}).get("name")
                if func_name:
                    mode = "ANY"  # 'ANY' 模式用于强制调用特定函数
                    allowed_functions = [func_name]
            elif isinstance(choice, dict) and choice.get("type") == "function_calling_config":
                requested_mode = choice.get("mode")
                if requested_mode in {"AUTO", "ANY", "NONE"}:
                    mode = requested_mode

            # 如果成功解析出有效的 mode，构建 tool_config
            if mode:
                config: Dict[str, Union[str, List[str]]] = {"mode": mode}
                if allowed_functions:
                    config["allowed_function_names"] = allowed_functions
                # Honour the OpenAI `parallel_tool_calls` flag by
                # passing the equivalent Gemini
                # `parallel_function_calls` field.  Previously this flag
                # was silently dropped, so clients that sent
                # `parallel_tool_calls=false` would still see Gemini
                # emit parallel tool calls.
                if (
                    hasattr(request, "parallel_tool_calls")
                    and request.parallel_tool_calls is False
                ):
                    config["parallel_function_calls"] = False
                tool_config = {"function_calling_config": config}

        # 3. 添加 tool_config 到 data
        if tool_config and function_declarations:
            data["tool_config"] = tool_config

        if system_instruction:
            data["system_instruction"] = system_instruction

        return api_version, data

    # 流式请求
    async def stream_chat(self, request, contents, safety_settings, system_instruction):
        # 真流式请求处理逻辑
        extra_log = {
            "key": _key_log_id(self.api_key),
            "request_type": "stream",
            "model": request.model,
        }
        log("INFO", "流式请求开始", extra=extra_log)

        api_version, model, data = self._convert_request_data(
            request, contents, safety_settings, system_instruction
        )
        # Prefill 回显剥离（响应侧）：请求侧补丁已把 prefill 原文写入
        # ContextVar（同一请求任务内可见）。这里创建跨 chunk 状态机，
        # 过滤上游输出中重复回显的 prefill 前缀；PREFILL_FIX_STRIP_ECHO
        # 关闭时退化恒为 no-op 过滤器（prefill=""）。
        _echo_filter = PrefillEchoStreamFilter(
            get_current_prefill_echo()
            if getattr(settings, "PREFILL_FIX_STRIP_ECHO", True)
            else ""
        )
        #log("INFO", f"Request body to Google: {json.dumps(data, ensure_ascii=False)}")

        # Hardening: API key moved from URL query (?key=...) to the
        # `x-goog-api-key` header.  URL query is logged by every layer in
        # the path (Google access logs, CDN, any intermediate proxy), so
        # putting the key in the URL effectively publishes it.  The header
        # form is what the official Google SDK uses.
        url = f"https://generativelanguage.googleapis.com/{api_version}/models/{model}:streamGenerateContent?alt=sse"
        headers = build_gemini_headers(self.api_key, streaming=True)
        headers["x-goog-api-key"] = self.api_key

        client = await get_async_client()
        async with client.stream(
            "POST", url, headers=headers, json=data, timeout=UPSTREAM_TIMEOUT
        ) as response:
            try:
                # 检查响应状态码，如果不是成功，则先消费响应体再抛出异常
                if response.status_code != 200:
                    await response.aread()
                    # 非 200 时记录脱敏上游错误摘要（冷却时长已按 429
                    # retryDelay 调整，见 error_handling.schedule_key_cooldown）。
                    _log_upstream_error_summary(
                        response, _key_log_id(self.api_key), "stream", request.model
                    )
                    response.raise_for_status()

                async for data in iter_sse_json(response):
                    # 原地改写本 chunk 的普通文本 part（thought/工具/媒体
                    # 不动）；遇真实 finishReason 帧时在同一 chunk 内释放
                    # 未决暂存，不产生额外帧。
                    _echo_filter.filter_gemini_chunk(data)
                    yield GeminiResponseWrapper(data)

                # 上游流关闭却仍有未决暂存（无 finishReason 帧，如连接
                # 异常中断）的罕见场景：按真实内容原样释放，合成一个
                # 普通形态的文本 chunk 补发给下游，绝不吞输出。
                _tail_chunk = build_tail_chunk(_echo_filter.finalize_gemini())
                if _tail_chunk is not None:
                    yield GeminiResponseWrapper(_tail_chunk)

            except Exception as e:
                # 在重新抛出异常之前，确保响应体被完全读取
                if not response.is_closed:
                    await response.aread()
                raise e
            finally:
                log("info", "流式请求结束")

    # 非流式处理
    async def complete_chat(
        self, request, contents, safety_settings, system_instruction
    ):
        api_version, model, data = self._convert_request_data(
            request, contents, safety_settings, system_instruction
        )
        #log("INFO", f"Request body to Google: {json.dumps(data, ensure_ascii=False)}")

        url = f"https://generativelanguage.googleapis.com/{api_version}/models/{model}:generateContent"
        headers = build_gemini_headers(self.api_key, streaming=False)
        headers["x-goog-api-key"] = self.api_key

        client = await get_async_client()
        response = await client.post(url, headers=headers, json=data, timeout=UPSTREAM_TIMEOUT)
        if response.status_code != 200:
            # 非 200 时记录脱敏上游错误摘要（与 stream 路径对齐）
            _log_upstream_error_summary(
                response, _key_log_id(self.api_key), "non-stream", request.model
            )
        response.raise_for_status()  # 检查 HTTP 错误状态

        # Prefill 回显剥离（非流式）：一次性过滤完整响应（含最终释放），
        # 语义与流式路径完全一致；未命中补丁时为 no-op。
        if getattr(settings, "PREFILL_FIX_STRIP_ECHO", True):
            _data = response.json()
            PrefillEchoStreamFilter(get_current_prefill_echo()).filter_complete_response(_data)
            return GeminiResponseWrapper(_data)
        return GeminiResponseWrapper(response.json())

    # OpenAI 格式请求转换为 gemini 格式请求
    def convert_messages(
        self,
        messages,
        use_system_prompt=False,
        model=None,
        skip_random_string=False,
    ):
        gemini_history = []
        errors = []

        system_instruction_text = ""
        leading_system_count = 0
        system_instruction_parts = []  # 用于收集系统指令文本

        # 处理系统指令
        if use_system_prompt:
            # 遍历消息列表，查找开头的连续 system 消息
            for i, message in enumerate(messages):
                # 必须是 system 角色且内容是字符串
                if message.get("role") == "system" and isinstance(
                    message.get("content"), str
                ):
                    system_instruction_parts.append(message.get("content"))
                    leading_system_count += 1
                else:
                    break  # 遇到第一个非 system 或内容非字符串的消息就停止

        # 将收集到的系统指令合并为一个字符串
        system_instruction_text = "\n".join(system_instruction_parts)
        system_instruction = (
            {"parts": [{"text": system_instruction_text}]}
            if system_instruction_text
            else None
        )

        # 转换主要消息

        call_id_to_name = {}
        for previous_message in messages:
            if not isinstance(previous_message, dict):
                continue
            for tool_call in previous_message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function_data = tool_call.get("function", {})
                if isinstance(function_data, dict) and tool_call.get("id") and function_data.get("name"):
                    call_id_to_name[tool_call["id"]] = function_data["name"]

        for i, message in enumerate(messages):
            if use_system_prompt and i < leading_system_count:
                continue

            role = message.get("role")
            content = message.get("content")
            if role == "assistant" and message.get("tool_calls"):
                parts = []
                if isinstance(content, str) and content:
                    parts.append({"text": content})
                elif isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text" and item.get("text"):
                            parts.append({"text": item.get("text")})
                for tool_call in message.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    function_data = tool_call.get("function", {})
                    if not isinstance(function_data, dict):
                        continue
                    function_name = function_data.get("name")
                    if not function_name:
                        continue
                    arguments = function_data.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments) if arguments else {}
                        except json.JSONDecodeError:
                            arguments = {"raw_arguments": arguments}
                    function_call_part = {
                        "functionCall": {
                            "name": function_name,
                            "args": arguments if isinstance(arguments, dict) else {},
                        }
                    }
                    extra_content = tool_call.get("extra_content") or {}
                    google_extra = extra_content.get("google", {})
                    thought_signature = (
                        google_extra.get("thought_signature")
                        or google_extra.get("thoughtSignature")
                        or tool_call.get("thought_signature")
                        or tool_call.get("thoughtSignature")
                    )
                    # Previously fell back to the literal
                    # `GEMINI_DUMMY_THOUGHT_SIGNATURE = "skip_thought_signature_validator"`
                    # which is a recognizable proxy fingerprint.  We now
                    # generate a strong-random signature of realistic
                    # shape on demand when one is missing.
                    if not thought_signature:
                        thought_signature = _generate_thought_signature()
                    function_call_part["thoughtSignature"] = thought_signature
                    parts.append(function_call_part)

                if parts:
                    if gemini_history and gemini_history[-1]["role"] == "model":
                        gemini_history[-1]["parts"].extend(parts)
                    else:
                        gemini_history.append({"role": "model", "parts": parts})
                    continue

            if isinstance(content, str):
                if role == "tool":
                    role_to_use = "function"
                    tool_call_id = message.get("tool_call_id")

                    prefix = "call_"
                    function_name = message.get("name") or call_id_to_name.get(tool_call_id)
                    if function_name:
                        pass
                    elif isinstance(tool_call_id, str) and tool_call_id.startswith(prefix):
                        # The previous tool_call_id format embedded the
                        # function name as `call_{name}__{rand}`, so we
                        # used to parse it back here.  Real OpenAI
                        # tool_call_ids are opaque `call_` + 24 lowercase
                        # alphanumeric chars with no name embedded, so
                        # there is nothing to parse out.  We fall through
                        # and rely on the call_id_to_name map (populated
                        # when the assistant's tool_calls were converted
                        # earlier in this same pass) or the explicit
                        # `message.name` field.  If neither is present,
                        # we cannot safely reconstruct a Gemini
                        # functionResponse and skip this tool message.
                        continue
                    else:
                        continue
                    function_response_part = {
                        "functionResponse": {
                            "name": function_name,
                            "response": {"content": content},
                        }
                    }

                    gemini_history.append(
                        {"role": role_to_use, "parts": [function_response_part]}
                    )

                    continue
                elif role in ["user", "system", "developer"]:
                    role_to_use = "user"
                elif role == "assistant":
                    role_to_use = "model"

                else:
                    errors.append(f"Invalid role: {role}")
                    continue

                # Gemini 的一个重要规则：连续的同角色消息需要合并
                # 如果 gemini_history 已有内容，并且最后一条消息的角色和当前要添加的角色相同
                if gemini_history and gemini_history[-1]["role"] == role_to_use:
                    gemini_history[-1]["parts"].append({"text": content})
                else:
                    gemini_history.append(
                        {"role": role_to_use, "parts": [{"text": content}]}
                    )
            elif isinstance(content, list):
                parts = []
                for item in content:
                    if item.get("type") == "text":
                        parts.append({"text": item.get("text")})
                    elif item.get("type") == "image_url":
                        image_data = item.get("image_url", {}).get("url", "")
                        if image_data.startswith("data:image/"):
                            try:
                                mime_type, base64_data = (
                                    image_data.split(";")[0].split(":")[1],
                                    image_data.split(",")[1],
                                )
                                parts.append(
                                    {
                                        "inline_data": {
                                            "mime_type": mime_type,
                                            "data": base64_data,
                                        }
                                    }
                                )
                            except (IndexError, ValueError):
                                errors.append(
                                    f"Invalid data URI for image: {image_data}"
                                )
                        elif image_data.startswith(("http://", "https://")):
                            file_data = {"file_uri": image_data}
                            mime_type = _guess_mime_type_from_url(image_data)
                            if mime_type:
                                file_data["mime_type"] = mime_type
                            parts.append({"file_data": file_data})
                        else:
                            errors.append(f"Invalid image URL format for item: {item}")

                if parts:
                    if role in ["user", "system", "developer"]:
                        role_to_use = "user"
                    elif role == "assistant":
                        role_to_use = "model"
                    else:
                        errors.append(f"Invalid role: {role}")
                        continue

                    if gemini_history and gemini_history[-1]["role"] == role_to_use:
                        gemini_history[-1]["parts"].extend(parts)
                    else:
                        gemini_history.append({"role": role_to_use, "parts": parts})
        if errors:
            # Correctness: 旧实现此处 `return errors` 返回单值列表，而所有
            # 调用方都按二元组解包（contents, system_instruction）——含 1 个
            # 错误时解包抛 ValueError → 500；含恰好 2 个错误时错误字符串被
            # 当成 contents/system_instruction 原样发给上游。改为抛
            # ValueError，由调用方（request_format.prepare_request_messages）
            # 转换成 HTTP 400 返回给客户端。
            raise ValueError("; ".join(errors))

        # --- 后处理 ---

        # 注入搜索提示
        if settings.search["search_mode"] and model and model.endswith("-search"):
            gemini_history.insert(
                len(gemini_history) - 2,
                {"role": "user", "parts": [{"text": settings.search["search_prompt"]}]},
            )

        # 注入随机字符串
        if settings.RANDOM_STRING and not skip_random_string:
            gemini_history.insert(
                1,
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": generate_secure_random_string(
                                settings.RANDOM_STRING_LENGTH
                            )
                        }
                    ],
                },
            )
            gemini_history.insert(
                len(gemini_history) - 1,
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": generate_secure_random_string(
                                settings.RANDOM_STRING_LENGTH
                            )
                        }
                    ],
                },
            )
            log("INFO", "伪装消息成功")

        # --- Prefill / model-turn 结尾兼容补丁（全场景通用） ---
        # 新世代 Gemini 模型（3.5-flash-lite 与 3.6/3.7/3.8/4-argon/
        # *-latest，详见 model_mapping.needs_model_turn_fix）拒绝以
        # model turn 结尾的请求，返回 400："Requests ending with a
        # model turn are not supported."。prefill（请求以 assistant
        # 消息结尾让模型续写）是通用技巧，覆盖：回答截断后继续输出、
        # 酒馆填表/续写、结构化输出前缀强制、抑制前言等全部场景。
        #
        # 必须放在 RANDOM_STRING 注入之后执行，保证追加的 user
        # turn 位于 contents 的绝对末尾（Google 校验的是最后一个
        # 非空 turn 的角色）。
        # 配置一律 getattr 防御性读取：settings 属性可能来自旧持久
        # 化快照或测试桩，缺键时按 settings.py 的默认值（启用 +
        # 新默认指令）处理；追加文本保底非空（官方要求 nonempty）。
        # 补丁本体见 _apply_prefill_fix（两条路径共享）。
        _prefill_echo = None
        if (
            getattr(settings, "PREFILL_FIX", True)
            and model
            and gemini_history
            and needs_model_turn_fix(
                model,
                extra_include=getattr(settings, "PREFILL_FIX_MODELS", None) or [],
                exclude=getattr(settings, "PREFILL_FIX_EXCLUDE_MODELS", None) or [],
            )
        ):
            _prefill_echo = _apply_prefill_fix(gemini_history, model)
        # 显式写入（含 None）：响应侧（stream_chat/complete_chat）经
        # ContextVar 读取本值做回显剥离；同任务多请求场景防泄漏。
        set_current_prefill_echo(_prefill_echo)

        return gemini_history, system_instruction

    @staticmethod
    async def _fetch_models_raw(api_key) -> dict:
        """拉取 Gemini 原生模型列表的原始 JSON（两个静态列表方法共享）。

        Key moved from ?key=... to header to avoid leaking it into access logs.
        """
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        headers = build_key_probe_headers(api_key)
        headers["x-goog-api-key"] = api_key
        client = await get_async_client()
        response = await client.get(url, headers=headers, timeout=60)
        response.raise_for_status()
        return response.json()

    @staticmethod
    async def list_available_models(api_key) -> list:
        data = await GeminiClient._fetch_models_raw(api_key)
        models = []
        for model in data.get("models", []):
            models.append(model["name"])
            if (
                model["name"].startswith("models/gemini-2")
                and settings.search["search_mode"]
            ):
                models.append(model["name"] + "-search")
        models.extend(GeminiClient.EXTRA_MODELS)

        return models

    @staticmethod
    async def list_native_models(api_key):
        """获取原生 Gemini 模型列表（原始 JSON 形态）。"""
        return await GeminiClient._fetch_models_raw(api_key)
