# 安全设置配置
#
# 风控硬化说明：
# 之前默认把 5 类 HARM_CATEGORY 全部设为 BLOCK_NONE / OFF，并塞入每次请求。
# 这是高度显著的"代理特征"——真实用户极少显式把所有安全类别全 OFF，
# Google 风控可凭此直接判定为代理转发。
#
# 新策略：
#   SAFETY_MODE = "default"  -> 不显式发送 safetySettings（让 Gemini 用默认）
#   SAFETY_MODE = "permissive"  -> 仅发送 4 类 OFF，保留 CIVIC_INTEGRITY 为 BLOCK_ONLY_HIGH
#   SAFETY_MODE = "off_all"  -> 5 类全 OFF（旧代理行为，仅用于兼容测试）
#
# CIVIC_INTEGRITY 是 Google 比较敏感的一类，把它保留为非 OFF 可避免极值指纹。

import os

SAFETY_MODE = os.environ.get("SAFETY_MODE", "default").lower().strip()

# 旧 SAFETY_SETTINGS（Gemini 1.0）—— 仅在 SAFETY_MODE=off_all 时使用
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
]

# 旧 SAFETY_SETTINGS_G2（Gemini 2.0）—— 仅在 SAFETY_MODE=off_all 时使用
SAFETY_SETTINGS_G2 = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "OFF"},
]

# 新：宽松模式（permissive），保留 CIVIC_INTEGRITY 为 BLOCK_ONLY_HIGH 以避免极值指纹
SAFETY_SETTINGS_PERMISSIVE = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_ONLY_HIGH"},
]

SAFETY_SETTINGS_G2_PERMISSIVE = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_ONLY_HIGH"},
]


def get_safety_settings(is_gemini_2: bool = False):
    """根据 SAFETY_MODE 返回应当附加到请求的 safety_settings 列表。

    返回 None 表示"不要附加 safetySettings 字段"（让 Gemini 用默认配置），
    这是新默认行为，也是避免被代理特征识别的关键。
    """
    if SAFETY_MODE == "default":
        return None
    if SAFETY_MODE == "permissive":
        return SAFETY_SETTINGS_G2_PERMISSIVE if is_gemini_2 else SAFETY_SETTINGS_PERMISSIVE
    # SAFETY_MODE == "off_all" 或其他显式值 -> 旧行为
    return SAFETY_SETTINGS_G2 if is_gemini_2 else SAFETY_SETTINGS
