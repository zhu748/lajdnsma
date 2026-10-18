import os
import pathlib
import logging
import asyncio
import json

# ---------- 以下是基础配置信息 ----------

# 调用本项目时使用的密码
# Hardening: previously defaulted to "123" — an operator who forgot to
# set PASSWORD would silently expose the gateway to anyone who knew the
# project.  We now default to empty string, which causes
# `custom_verify_password` to reject every inbound request until an
# operator sets a real password.  This is fail-closed.
PASSWORD = os.environ.get("PASSWORD", "").strip('"')

# 网页配置密码，设置后，在网页修改配置时使用 WEB_PASSWORD 而不是上面的 PASSWORD
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", PASSWORD).strip('"')

# API密钥
GEMINI_API_KEYS = os.environ.get("GEMINI_API_KEYS", "")

# Key rotation strategy.
#   "fill"     (default, sticky) — use the same key until it hits a
#              cooldown / daily-limit / invalidation, then advance to
#              the next. Produces the lowest possible RPM-per-key
#              signature, which is closer to a real single-user client
#              and harder for Google risk control to flag as a pool.
#   "polling"  (round-robin)    — distribute requests evenly across all
#              keys (original behaviour). Higher per-key RPM dispersion;
#              useful when individual keys have very low RPM limits and
#              the operator wants to load-balance across them.
KEY_ROTATION_STRATEGY = os.environ.get(
    "KEY_ROTATION_STRATEGY", "fill"
).lower().strip()

# 假流式是否开启
# Hardening default: previously "true".  Fake-stream (non-streaming
# upstream call + chunked SSE replay to the client) has detectable
# timing patterns (fixed 1s keepalive, fixed 10-chunk split, fixed
# 50ms inter-chunk delay) that real OpenAI/Anthropic streams don't
# exhibit.  Default to false; operators who need it can re-enable
# via FAKE_STREAMING=true.
FAKE_STREAMING = os.environ.get("FAKE_STREAMING", "false").lower() in [
    "true",
    "1",
    "yes",
]

# 配置持久化存储目录
# Hardening: previously defaulted to `/hajimi/settings/` — the path
# itself contains the upstream project name.  Operators migrating from
# the old default should set STORAGE_DIR explicitly.  We now use a
# neutral `/data/settings/` default.
STORAGE_DIR = os.environ.get("STORAGE_DIR", "/data/settings/")
ENABLE_STORAGE = os.environ.get("ENABLE_STORAGE", "false").lower() in [
    "true",
    "1",
    "yes",
]

# 并发请求配置
CONCURRENT_REQUESTS = int(os.environ.get("CONCURRENT_REQUESTS", "1"))  # 默认并发请求数
INCREASE_CONCURRENT_ON_FAILURE = int(
    os.environ.get("INCREASE_CONCURRENT_ON_FAILURE", "0")
)  # 失败时增加的并发数
MAX_CONCURRENT_REQUESTS = int(
    os.environ.get("MAX_CONCURRENT_REQUESTS", "3")
)  # 最大并发请求数

# 缓存配置
CACHE_EXPIRY_TIME = int(
    os.environ.get("CACHE_EXPIRY_TIME", "21600")
)  # 默认缓存 6 小时 (21600 秒)
MAX_CACHE_ENTRIES = int(
    os.environ.get("MAX_CACHE_ENTRIES", "500")
)  # 默认最多缓存500条响应
CALCULATE_CACHE_ENTRIES = int(
    os.environ.get("CALCULATE_CACHE_ENTRIES", "6")
)  # 默认取最后 6 条消息算缓存键
PRECISE_CACHE = os.environ.get("PRECISE_CACHE", "false").lower() in [
    "true",
    "1",
    "yes",
]  # 是否取所有消息来算缓存键

# 是否启用 Vertex AI
ENABLE_VERTEX = os.environ.get("ENABLE_VERTEX", "false").lower() in ["true", "1", "yes"]
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

# 是否启用快速模式 Vertex
ENABLE_VERTEX_EXPRESS = os.environ.get("ENABLE_VERTEX_EXPRESS", "false").lower() in [
    "true",
    "1",
    "yes",
]
VERTEX_EXPRESS_API_KEY = os.environ.get("VERTEX_EXPRESS_API_KEY", "")

# 联网搜索配置
search = {
    "search_mode": os.environ.get("SEARCH_MODE", "false").lower()
    in ["true", "1", "yes"],
    "search_prompt": os.environ.get(
        "SEARCH_PROMPT", "（使用搜索工具联网搜索，需要在content中结合搜索内容）"
    ).strip('"'),
}

# 随机字符串
RANDOM_STRING = os.environ.get("RANDOM_STRING", "true").lower() in ["true", "1", "yes"]
RANDOM_STRING_LENGTH = int(os.environ.get("RANDOM_STRING_LENGTH", "5"))

# ---------- Prefill / model-turn 结尾兼容补丁 ----------
# Google 自 gemini-3.5-flash-lite / 3.6-flash（2026-07-21 发布）起的
# 新世代模型拒绝"请求以 model turn 结尾"（即 prefill / 前缀续写），
# 返回 400 INVALID_ARGUMENT：
#   "Requests ending with a model turn are not supported."
# prefill 是通用技巧，覆盖但不限于：回答截断后继续输出、酒馆
# 填表/续写、结构化输出前缀强制（如预填 "{"）、抑制前言等场景。
# 开启本补丁后，命中受限模型名单（>= 3.5 世代 / 4+ / *-latest）且
# contents 以 model turn 结尾时，自动在末尾追加一条非空文本的
# user turn（官方迁移指南要求的兼容形态），语义为通用的
# "从停下的地方继续，不要重复"。旧模型（2.5 / 3.0 /
# 3.1 等）不受影响，保持原生 prefill 行为。判断实现见
# app/utils/model_mapping.py::needs_model_turn_fix。
PREFILL_FIX = os.environ.get("PREFILL_FIX", "true").lower() in ["true", "1", "yes"]
# 追加的 user turn 文本（官方要求 nonempty text）。默认英文明确版
# 指令，覆盖截断续写 / 填表 / 结构化前缀等全部 prefill 场景：
# 从停下的地方继续、不重复已有内容（避免模型重写 prefill 部分）。
# 可按需改成中文，如 "从你停下的地方继续，不要重复任何内容。"
_prefill_prompt_raw = os.environ.get(
    "PREFILL_FIX_PROMPT", "Continue from where you left off, without repeating any content."
).strip().strip('"')
PREFILL_FIX_PROMPT = (
    _prefill_prompt_raw
    if _prefill_prompt_raw
    else "Continue from where you left off, without repeating any content."
)
# 额外白名单：逗号分隔的模型名/前缀/通配符，强制对这些模型启用补丁
# （用于点名 antigravity / omni 等未纳入世代启发式的专用模型）。
PREFILL_FIX_MODELS = [
    item.strip()
    for item in os.environ.get("PREFILL_FIX_MODELS", "").split(",")
    if item.strip()
]
# 黑名单：逗号分隔的模型名/前缀/通配符，对这些模型停用补丁（优先级
# 最高，可用于"该模型其实支持 prefill"的纠偏）。
PREFILL_FIX_EXCLUDE_MODELS = [
    item.strip()
    for item in os.environ.get("PREFILL_FIX_EXCLUDE_MODELS", "").split(",")
    if item.strip()
]

# 是否全局启用思考过程
ENABLE_THINKING = os.environ.get("ENABLE_THINKING", "true").lower() in [
    "true",
    "1",
    "yes",
]

# ---------- 最高思考强度（thinking 覆盖策略，见 app/utils/thinking.py） ----------
# 开启后：忽略请求中携带的思考强度参数，对所有支持思考的模型直接使用
# 该模型家族的最高档位。默认开启，可经网页设置或 MAX_THINKING=false 关闭。
# 各家族「最高档」的取值（依据官方文档 ai.google.dev/gemini-api/docs/thinking）：
#   - Gemini 2.5 系列（仅支持数字 budget）：Pro → thinkingBudget=32768，
#     Flash / Flash-Lite → thinkingBudget=24576；
#   - Gemini 3.x / 4.x 及新世代（等级制）：thinkingLevel="high"；
#     注意 4.x 不接受 thinkingBudget（会 400），因此等级制模型绝不发送 budget；
#   - 不支持思考的模型（TTS / Embedding / Lyria / Transcribe / Gemma /
#     Omni / 深度研究等代理类）静默跳过，不附加任何思考配置。
# 优先级：按模型指定等级（MAX_THINKING_MODEL_LEVELS）> 本开关 > 请求参数。
MAX_THINKING = os.environ.get("MAX_THINKING", "true").lower() in ["true", "1", "yes"]

# 按模型指定思考等级（优先级高于 MAX_THINKING 全局开关）。
# JSON 映射表：{"模型名或前缀": 值}，值支持：
#   - 等级字符串："minimal" / "low" / "medium" / "high"
#     （对 2.5 系列自动换算为等价 budget：low≈1024 / medium≈8192 /
#       high=24576，Pro 为 32768；minimal→Flash 关闭 / Pro 128）
#   - 数字：2.5 系列直接作为 thinkingBudget（0=关闭，-1=动态思考）；
#     3.x/4.x 等级制模型会按区间就近换算成等级（1-2048→low、
#     2049-10240→medium、>10240→high），避免 4.x 出现 budget 400 错误
#   - "off"：不附加思考配置，走模型默认（2.5 Flash 系列为 budget=0）
# 匹配规则：先精确匹配模型名，未命中再做最长前缀匹配
# （如 "gemini-3.8-flash" 可覆盖所有 3.8 flash 变体）。
try:
    _mt_levels_raw = json.loads(
        os.environ.get("MAX_THINKING_MODEL_LEVELS", "{}") or "{}"
    )
    if not isinstance(_mt_levels_raw, dict):
        _mt_levels_raw = {}
    MAX_THINKING_MODEL_LEVELS = {
        str(k).strip(): v for k, v in _mt_levels_raw.items() if str(k).strip()
    }
except json.JSONDecodeError:
    MAX_THINKING_MODEL_LEVELS = {}

# 空响应重试次数限制
MAX_EMPTY_RESPONSES = int(
    os.environ.get("MAX_EMPTY_RESPONSES", "5")
)  # 默认最多允许5次空响应

# ---------- PVP 模式（Round 8：指定 key 持续重试） ----------
# 开启后不再按 fill/polling 轮换密钥池，而是把所有请求钉在 PVP_KEY
# 指定的那一个 key 上持续重试，直到出结果或达到 PVP_MAX_RETRIES
# 次数上限。冷却 / RPM / 日额度都不构成换 key 理由；唯二安全阀是
# "key 已死（401/403）立即终止" 与 "最大重试次数"。实现见
# app/utils/pvp.py。
PVP_MODE = os.environ.get("PVP_MODE", "false").lower() in ["true", "1", "yes"]
# 钉住的 key 选择器，支持：完整密钥 / "#0"（池内序号）/
# "key#1a2b3c"（面板哈希标识）/ 密钥尾片段（>=4 位）。
# 完整密钥在保存/回显时会被自动脱敏为尾片段，不落盘明文。
# 内联 sanitize 规则（权威实现在 pvp.sanitize_pvp_selector，此处不能
# 导入它——会形成 settings ↔ pvp 循环依赖）：长度 >= 20 的纯 token
# 视为完整密钥，只保留尾 6 位，保证 ENABLE_STORAGE 落盘的
# settings.json 永远不含 PVP 明文密钥（与 GEMINI_API_KEYS 被排除
# 出持久化的安全决策一致）。
_raw_pvp_key = os.environ.get("PVP_KEY", "").strip()
PVP_KEY = (
    _raw_pvp_key
    if len(_raw_pvp_key) < 20 or "#" in _raw_pvp_key
    else _raw_pvp_key[-6:]
)
# PVP 模式下单个请求的最大重试次数（防止无限重试）
PVP_MAX_RETRIES = int(os.environ.get("PVP_MAX_RETRIES", "50"))

# ---------- 以下是其他配置信息 ----------

# 访问限制
MAX_RETRY_NUM = int(os.environ.get("MAX_RETRY_NUM", "15"))  # 请求时的最大总轮询 key 数
MAX_REQUESTS_PER_MINUTE = int(os.environ.get("MAX_REQUESTS_PER_MINUTE", "30"))
MAX_REQUESTS_PER_DAY_PER_IP = int(os.environ.get("MAX_REQUESTS_PER_DAY_PER_IP", "600"))

# API密钥使用限制
API_KEY_DAILY_LIMIT = int(
    os.environ.get("API_KEY_DAILY_LIMIT", "100")
)  # 默认每个API密钥每24小时可使用100次

# 模型屏蔽黑名单，格式应为逗号分隔的模型名称集合
BLOCKED_MODELS = {
    model.strip()
    for model in os.environ.get("BLOCKED_MODELS", "").split(",")
    if model.strip()
}

# 公益站模式
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "false").lower() in ["true", "1", "yes"]
# 前端地址
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")

# 模型屏蔽白名单
WHITELIST_MODELS = {
    x.strip() for x in os.environ.get("WHITELIST_MODELS", "").split(",") if x.strip()
}

# Claude/Anthropic 兼容模式下，如果客户端传入 claude-* 模型名，可映射到该 Gemini 模型。
# 为空时自动选择当前可用模型列表中的第一个模型。
CLAUDE_DEFAULT_MODEL = os.environ.get("CLAUDE_DEFAULT_MODEL", "").strip()
try:
    CLAUDE_MODEL_ALIASES = json.loads(os.environ.get("CLAUDE_MODEL_ALIASES", "{}"))
    if not isinstance(CLAUDE_MODEL_ALIASES, dict):
        CLAUDE_MODEL_ALIASES = {}
except json.JSONDecodeError:
    CLAUDE_MODEL_ALIASES = {}

# OpenAI Responses/Codex 兼容模式下，如果客户端传入 gpt-/o*/codex-* 等模型名，可映射到该 Gemini 模型。
# 为空时自动选择当前可用模型列表中的第一个模型。
RESPONSES_DEFAULT_MODEL = os.environ.get("RESPONSES_DEFAULT_MODEL", "").strip()
try:
    RESPONSES_MODEL_ALIASES = json.loads(os.environ.get("RESPONSES_MODEL_ALIASES", "{}"))
    if not isinstance(RESPONSES_MODEL_ALIASES, dict):
        RESPONSES_MODEL_ALIASES = {}
except json.JSONDecodeError:
    RESPONSES_MODEL_ALIASES = {}
RESPONSES_STATELESS_COMPAT = os.environ.get(
    "RESPONSES_STATELESS_COMPAT", "true"
).lower() in ["true", "1", "yes"]

# 白名单User-Agent
WHITELIST_USER_AGENT = {
    x.strip().lower()
    for x in os.environ.get("WHITELIST_USER_AGENT", "").split(",")
    if x.strip()
}

# 跨域配置
# 允许的源列表，逗号分隔，例如 "http://localhost:3000,https://example.com"
ALLOWED_ORIGINS_STR = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [
    origin.strip() for origin in ALLOWED_ORIGINS_STR.split(",") if origin.strip()
]

# ---------- 运行时全局信息，无需修改 ----------

# 基础目录设置
BASE_DIR = pathlib.Path(__file__).parent.parent

# 失效的API密钥
INVALID_API_KEYS = os.environ.get("INVALID_API_KEYS", "")

version = {"local_version": "0.0.0", "remote_version": "0.0.0", "has_update": False}

# API调用统计
# 这个对象保留为空结构以保持向后兼容性
# 实际统计数据已迁移到 app/utils/stats.py 中的 ApiStatsManager 类
api_call_stats = {
    "calls": []  # 兼容旧版代码结构
}

# 用于保护 api_call_stats 并发访问的锁
stats_lock = asyncio.Lock()

# 日志配置
# Fix: 旧写法 `logging.getLogger("uvicorn").disabled = True` 会连 uvicorn
# 的错误/启动日志一起杀掉，服务没起来时排障无从看起。现在只静默
# access log（高频且价值低），保留 error 级别的 uvicorn 日志；同时
# 可通过 DISABLE_UVICORN_LOG=1 恢复旧行为。
if os.environ.get("DISABLE_UVICORN_LOG", "1").lower() != "false":
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)


# ---------- 以下配置信息已废弃 ----------

# 假流式请求的空内容返回间隔（秒）
FAKE_STREAMING_INTERVAL = float(os.environ.get("FAKE_STREAMING_INTERVAL", "1"))
# 假流式响应的每个块大小
FAKE_STREAMING_CHUNK_SIZE = int(os.environ.get("FAKE_STREAMING_CHUNK_SIZE", "10"))
# 假流式响应的每个块之间的延迟（秒）
FAKE_STREAMING_DELAY_PER_CHUNK = float(
    os.environ.get("FAKE_STREAMING_DELAY_PER_CHUNK", "0.1")
)

# 非流式请求TCP保活配置
# Hardening default: previously true.  Sending empty-content delta
# keepalive chunks at fixed 5s intervals is a non-standard OpenAI SSE
# pattern that real clients don't emit.
NONSTREAM_KEEPALIVE_ENABLED = os.environ.get(
    "NONSTREAM_KEEPALIVE_ENABLED", "false"
).lower() in ["true", "1", "yes"]
NONSTREAM_KEEPALIVE_INTERVAL = float(
    os.environ.get("NONSTREAM_KEEPALIVE_INTERVAL", "5.0")
)
