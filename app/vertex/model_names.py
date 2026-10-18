"""模型名前后缀解析（chat_api 与 models_api 共享）。

历史版本中 chat_api.py 与 models_api.py 各自硬编码同一组前后缀常量
（OPENAI_DIRECT_SUFFIX / PAY_PREFIX / EXPRESS_PREFIX 等）并各自实现
解析逻辑，存在漂移风险。现收敛为单一实现：

- ModelRequestFlags: 一次解析得到的全部请求级标志位与剥离后的基础模型名
- parse_model_request():  前缀/后缀判定 + 基础模型名剥离
- validate_special_variants(): -nothinking / -max 后缀的模型白名单校验
"""
from dataclasses import dataclass

OPENAI_DIRECT_SUFFIX = "-openai"
EXPERIMENTAL_MARKER = "-exp-"
PAY_PREFIX = "[PAY]"
EXPRESS_PREFIX = "[EXPRESS] "  # 带尾随空格，便于剥离

# 支持 -nothinking / -max 后缀的模型白名单
SUPPORTED_FLASH_VARIANTS = [
    "gemini-2.5-flash-preview-04-17",
    "gemini-2.5-flash-preview-05-20",
    "gemini-2.5-pro-preview-06-05",
]
SUPPORTED_FLASH_VARIANTS_STR = "' or '".join(SUPPORTED_FLASH_VARIANTS)


@dataclass
class ModelRequestFlags:
    """请求模型名的解析结果（标志位 + 剥离前后缀的基础模型名）。"""

    is_openai_direct_model: bool
    is_auto_model: bool
    is_grounded_search: bool
    is_encrypted_model: bool
    is_encrypted_full_model: bool
    is_nothinking_model: bool
    is_max_thinking_model: bool
    is_express_model_request: bool
    base_model_name: str


def parse_model_request(model: str) -> ModelRequestFlags:
    """解析请求模型名：判定全部标志位并剥离已知前缀/后缀。

    剥离顺序：先前缀（[EXPRESS] / [PAY]），再后缀（-openai 组合剥离
    / -auto / -search / -encrypt-full / -encrypt / -nothinking / -max）。
    """
    is_openai_direct_model = False
    if model.endswith(OPENAI_DIRECT_SUFFIX):
        temp_name_for_marker_check = model[: -len(OPENAI_DIRECT_SUFFIX)]
        if temp_name_for_marker_check.startswith(PAY_PREFIX):
            is_openai_direct_model = True
        elif EXPERIMENTAL_MARKER in temp_name_for_marker_check:
            is_openai_direct_model = True

    is_auto_model = model.endswith("-auto")
    is_grounded_search = model.endswith("-search")
    is_encrypted_model = model.endswith("-encrypt")
    is_encrypted_full_model = model.endswith("-encrypt-full")
    is_nothinking_model = model.endswith("-nothinking")
    is_max_thinking_model = model.endswith("-max")
    base_model_name = model

    is_express_model_request = False
    if base_model_name.startswith(EXPRESS_PREFIX):
        is_express_model_request = True
        base_model_name = base_model_name[len(EXPRESS_PREFIX):]

    if base_model_name.startswith(PAY_PREFIX):
        base_model_name = base_model_name[len(PAY_PREFIX):]

    # 后缀剥离（作用于已去前缀的名称）。
    # 注意：-encrypt-full 必须在 -encrypt 之前判定。
    if is_openai_direct_model:
        # OpenAI Direct 模型的基础名 = 原名去掉 -openai 后缀，
        # 还需剥掉其内部可能存在的 [EXPRESS] / [PAY] 前缀。
        temp_base_for_openai = model[: -len(OPENAI_DIRECT_SUFFIX)]
        if temp_base_for_openai.startswith(EXPRESS_PREFIX):
            temp_base_for_openai = temp_base_for_openai[len(EXPRESS_PREFIX):]
        if temp_base_for_openai.startswith(PAY_PREFIX):
            temp_base_for_openai = temp_base_for_openai[len(PAY_PREFIX):]
        base_model_name = temp_base_for_openai
    elif is_auto_model:
        base_model_name = base_model_name[: -len("-auto")]
    elif is_grounded_search:
        base_model_name = base_model_name[: -len("-search")]
    elif is_encrypted_full_model:
        base_model_name = base_model_name[: -len("-encrypt-full")]
    elif is_encrypted_model:
        base_model_name = base_model_name[: -len("-encrypt")]
    elif is_nothinking_model:
        base_model_name = base_model_name[: -len("-nothinking")]
    elif is_max_thinking_model:
        base_model_name = base_model_name[: -len("-max")]

    return ModelRequestFlags(
        is_openai_direct_model=is_openai_direct_model,
        is_auto_model=is_auto_model,
        is_grounded_search=is_grounded_search,
        is_encrypted_model=is_encrypted_model,
        is_encrypted_full_model=is_encrypted_full_model,
        is_nothinking_model=is_nothinking_model,
        is_max_thinking_model=is_max_thinking_model,
        is_express_model_request=is_express_model_request,
        base_model_name=base_model_name,
    )


def validate_special_variants(model: str, flags: ModelRequestFlags) -> str | None:
    """校验 -nothinking / -max 后缀的模型白名单，返回错误信息或 None。

    仅这两个后缀是排他性能力（依赖特定 preview 版本的 thinking_budget
    控制），其余后缀对所有模型开放。
    """
    if flags.is_nothinking_model and flags.base_model_name not in SUPPORTED_FLASH_VARIANTS:
        return (
            f"Model '{model}' (-nothinking) is only supported for "
            f"'{SUPPORTED_FLASH_VARIANTS_STR}'."
        )
    if flags.is_max_thinking_model and flags.base_model_name not in SUPPORTED_FLASH_VARIANTS:
        return (
            f"Model '{model}' (-max) is only supported for "
            f"'{SUPPORTED_FLASH_VARIANTS_STR}'."
        )
    return None
