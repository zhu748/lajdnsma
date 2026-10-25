# 🚀 HAJIMI Gemini API Proxy

这是一个基于 FastAPI 构建的 Gemini API 代理，旨在提供一个简单、安全且可配置的方式来访问 Google 的 Gemini 模型。适用于在 Hugging Face Spaces 上部署，并支持openai api格式的工具集成。

## 📁 代码结构

```text
app/
├── main.py                 # 应用入口：lifespan 装配、中间件、异常处理
├── api/                    # HTTP 路由层（路由只做接线，业务在 handlers）
│   ├── routes.py           #   业务路由聚合器 + init_router 依赖注入
│   ├── dashboard/          #   管理面板 API（runtime / auth / stats /
│   │                       #   config_updates 注册表 / model_mapping / api_keys）
│   ├── chat_routes / protocol_routes / model_routes / embedding_routes
│   ├── *_handlers.py       #   各协议业务实现（chat / 流式 / 非流式 / 协议适配）
│   └── route_runtime.py    #   运行时依赖容器（密钥池 / 缓存 / 活跃请求）
├── services/               # 上游客户端层
│   ├── gemini.py           #   GeminiClient：请求转换 + 流式/非流式调用
│   └── gemini_payload.py   #   纯函数 payload 清洗（零 app 依赖）
├── vertex/                 # Vertex AI 模式（SA 凭证 / Express / OpenAI Direct）
│   ├── routes/chat_api.py  #   端点编排 + openai_direct.py 全链路
│   ├── api_helpers.py      #   流式引擎与 execute_gemini_call
│   ├── message_processing.py / model_names.py / credentials_manager.py ...
├── utils/                  # 工具层（密钥选择 / 错误处理 / 协议转换 / 统计 / 缓存）
└── config/                 # 配置层（settings / 持久化 / safety 模式）
tests/                      # 449 项回归测试（含源码文本契约，重构前必读
                            #   tests/test_round4/round5 的注释说明）
```

分层约定：`api` 层不直接发起上游请求，`services`/`vertex` 层不感知 HTTP，
`utils` 层为纯函数或独立状态机；面板与业务共用 `route_runtime`/`dashboard.runtime`
注入的运行时对象。

## 项目采用动态更新，随时会有一些小更新同步到主仓库且会自动构建镜像，如果反馈的bug开发者说修了但是版本号没变是正常现象，~~记得勤更新镜像哦~~

## 错误自查

遇到问题请先查看以下的 **错误自查** 文档，确保已尝试按照其上的指示进行了相应的排查与处理。

- [错误自查](./wiki/error.md)

###  使用文档

- [Claw Cloud部署的使用文档（免费，手机电脑均可使用）](./wiki/claw.md) 感谢[@IDeposit](https://github.com/IDeposit)编写

- [huggingface 部署的使用文档（免费，手机电脑均可使用）](./wiki/huggingface2.md)

- [termux部署的使用文档（手机使用）](./wiki/Termux.md) 感谢 [@天命不又](https://github.com/tmby) 编写

- [Render 部署的使用文档 (免费，手机电脑均可使用，但可能需要绑信用卡)](./wiki/render.md) 感谢 [@LSR](https://github.com/lesser0) 编写

- [windows 本地部署的使用文档](./wiki/windows.md)

- [vertex模式的使用文档](./wiki/vertex.md)

- ~~[zeabur部署的使用文档(需付费)](./wiki/zeabur.md) 感谢**墨舞ink**编写~~（已过时且暂时无人更新，欢迎提交pull requests）

###  更新日志
* v1.0.9
   * 修复 Vertex 流式错误路径崩溃、fill 轮换策略日额度崩溃等正确性问题
   * 限流改为按 IP 独立计数（此前单个用户可耗尽全站每分钟配额）
   * 消除事件循环阻塞的 OAuth 刷新、批量请求孤儿任务与内存泄漏
   * 密钥轮换/凭证轮换加固，前端控制台修复轮询泄漏与凭证回填问题
* v1.0.2
   * 修复 400 错误

* v1.0.1
   * 新增`清除失效密钥`功能
   * 新增`输出有效秘钥`功能

## ✨ 主要功能：

### 🔑 API 密钥轮询和管理

### 📑 模型列表接口

### 💬 聊天补全接口：

*   提供 `/v1/chat/completions` 接口，支持流式和非流式响应，支持函数调用，与 OpenAI API 格式兼容。
*   支持的输入内容: 文本、文件、图像
*   自动将 OpenAI 格式的请求转换为 Gemini 格式。

### 🔒 密码保护（可选）：

*   通过 `PASSWORD` 环境变量设置密码。
*   提供默认密码 `"123"`。

### 🧩 服务兼容

*   提供的接口与 OpenAI API 格式兼容,便于接入各种服务

### ⚙️ 功能配置

* 方式 1 : 通过网页前端进行配置
* 方式 2 : 根据 [配置文档](./app/config/settings.py) 中的注释说明，修改对应的变量

## ⚠️ 注意事项：

*   **强烈建议在生产环境中设置 `PASSWORD` 环境变量，并使用强密码。**
*   根据你的使用情况调整速率限制相关的环境变量。
*   确保你的 Gemini API 密钥具有足够的配额。

## 💡 特色功能：

### ⚔️ PVP 模式（指定 key 持续重试）

*   **作用：** 在面板「功能设置」开启后，所有请求钉在指定的一个 key 上持续重试，直到输出结果。冷却/RPM/日额度都不构成换 key 理由，适合"就想用某个 key 出结果"的场景。
*   **指定 key 写法：** 完整密钥 / `#0`（池内序号）/ `key#1a2b3c`（密钥状态页的哈希标识，面板可下拉选择）/ 密钥尾片段（≥4 位）。完整密钥会自动脱敏存储，不会落盘明文。
*   **最大重试次数：** `PVP_MAX_RETRIES`（默认 50）防止无限重试；钉住的 key 已失效（401/403）时自动提前终止，429 则按上游指示的 retryDelay 退避后继续。

### 🎭 假流式传输

*   **作用：** 解决部分网络环境下客户端通过非流式请求 Gemini 时可能遇到的断连问题。**默认开启**。

*   **原理简述：** 当客户端请求流式响应时，本代理会每隔一段时间向客户端发出一个空信息以维持连接，同时在后台向 Gemini 发起一个完整的、非流式的请求。等 Gemini 返回完整响应后，再一次性将响应发回给客户端。

*   **注意：** 如果想使用真的流式请求，请**关闭**该功能

### ⚡ 并发与缓存

*   **作用：** 允许您为用户的单次提问同时向 Gemini 发送多个请求，并将额外的成功响应缓存起来，用于后续重新生成回复。

*   **注意：** 此功能**默认关闭** 。只有当您将并发数设置为 2 或以上时，缓存才会生效。缓存匹配要求提问的上下文与被缓存的问题**完全一致**（包括标点符号）。此外，该模式目前仅支持非流式及假流式传输
    
    **Q: 新版本增加的并发缓存功能会增加 gemini 配额的使用量吗？**
   
    **A: 不会**。因为默认情况下该功能是关闭的。只有当你主动将并发数 `CONCURRENT_REQUESTS` 设置为大于 1 的数值时，才会实际发起并发请求，这才会消耗更多配额。
   
    **Q: 如何使用并发缓存功能？**
   
    **A:** 修改并发请求数，使其等于你想在一次用户提问中同时向 Gemini 发送的请求数量（例如设置为 `3`）。
    
    这样设置后，如果一次并发请求中收到了多个成功的响应，除了第一个返回给用户外，其他的就会被缓存起来。

### 🎭 伪装信息

*   **作用：** 在发送给 Gemini 的消息中添加一段随机生成的、无意义的字符串，用于"伪装"请求，可能有助于防止被识别为自动化程序。**默认开启**。

*   **注意：** 如果使用非 SillyTavern 的其余客户端 (例如 cherryStudio )，请**关闭**该功能

### 🌐 联网模式

*   **作用：** 让 Gemini 模型能够利用搜索工具进行联网搜索，以回答需要最新信息或超出其知识库范围的问题。

*   **如何使用：**

    在客户端请求时，选择模型名称带有 `-search` 后缀的模型（例如 `gemini-2.5-pro-search`，具体可用模型请通过 `/v1/models` 接口查询）。

### 🚦 速率限制和防滥用：

*   通过环境变量自定义限制：
    *   `MAX_REQUESTS_PER_MINUTE`：每分钟最大请求数（默认 30）。
    *   `MAX_REQUESTS_PER_DAY_PER_IP`：每天每个 IP 最大请求数（默认 600）。
*   超过速率限制时返回 429 错误。

## 📡 API 接口说明

### 模型列表接口

- **GET** `/v1/models` - 获取可用模型列表
- **GET** `/models` - 获取可用模型列表（兼容旧版本）

### 聊天补全接口

- **POST** `/v1/chat/completions` - 聊天补全接口，支持流式和非流式响应
- **POST** `/chat/completions` - 聊天补全接口（兼容旧版本）

### Vertex 模式接口

- **GET** `/vertex/models` - 获取 Vertex 模型列表
- **POST** `/vertex/chat/completions` - Vertex 聊天补全接口

### AI Studio 模式接口

- **GET** `/aistudio/models` - 获取 AI Studio 模型列表
- **POST** `/aistudio/chat/completions` - AI Studio 聊天补全接口

## ⚙️ 配置选项

项目支持多种配置方式，可以通过环境变量进行配置：

### 基础配置

- `PASSWORD` - API访问密码，默认为"123"
- `WEB_PASSWORD` - 网页配置密码，默认与PASSWORD相同
- `GEMINI_API_KEYS` - Gemini API密钥，多个密钥用逗号分隔（同时支持 `AIzaSy…` 经典格式与 `AQ.…` 新格式密钥）
- `FAKE_STREAMING` - 是否启用假流式传输，默认为true
- `STORAGE_DIR` - 配置持久化存储目录
- `ENABLE_STORAGE` - 是否启用配置持久化存储，默认为false

### 并发与缓存配置

- `CONCURRENT_REQUESTS` - 并发请求数，默认为1
- `INCREASE_CONCURRENT_ON_FAILURE` - 失败时增加的并发数，默认为0
- `MAX_CONCURRENT_REQUESTS` - 最大并发请求数，默认为3
- `CACHE_EXPIRY_TIME` - 缓存过期时间（秒），默认为21600（6小时）
- `MAX_CACHE_ENTRIES` - 最大缓存条目数，默认为500
- `CALCULATE_CACHE_ENTRIES` - 计算缓存键时使用的消息数量，默认为6
- `PRECISE_CACHE` - 是否使用精确缓存，默认为false

### Vertex AI 配置

- `ENABLE_VERTEX` - 是否启用Vertex AI，默认为false
- `GOOGLE_CREDENTIALS_JSON` - Google凭证JSON
- `ENABLE_VERTEX_EXPRESS` - 是否启用Vertex Express模式，默认为false
- `VERTEX_EXPRESS_API_KEY` - Vertex Express API密钥

### 联网搜索配置

- `SEARCH_MODE` - 是否启用联网搜索模式，默认为false
- `SEARCH_PROMPT` - 联网搜索提示语

### 思考配置

- `ENABLE_THINKING` - 思考功能总开关，默认为true。关闭后代理完全不干预任何思考参数（也不做等级换算）
- `MAX_THINKING` - 最高思考强度，默认为true。开启后忽略请求携带的思考强度参数，对所有支持思考的模型直接使用其最高档位：2.5 Pro → budget 32768，2.5 Flash 系 → budget 24576，3.x / 4.x → thinkingLevel=high；不支持思考的模型（TTS / Embedding / Lyria 等）自动跳过
- `MAX_THINKING_MODEL_LEVELS` - 按模型指定思考等级（JSON 映射，优先级高于 `MAX_THINKING`），如 `{"gemini-3.8-flash": "low", "gemini-2.5-pro": 16384}`。值支持 `minimal` / `low` / `medium` / `high` 等级、`off`（关闭，2.5 Pro 退化为最低 128）、`dynamic`（2.5 系动态思考）或数字 budget；模型名支持精确匹配与最长前缀匹配

以上三项均可在网页控制台「功能配置」中直接开关/编辑；等级制模型（3.x/4.x）上出现的 budget 会自动就近换算为等级，避免 4.x 报 400 错误。

### 预填充兼容配置

- `PREFILL_FIX` - 预填充（prefill）兼容补丁开关，默认为true。新世代模型（3.5-flash-lite 与 3.6/3.7/3.8/4-argon/-latest）拒绝以 model turn（assistant 消息）结尾的请求（返回 400），开启后自动修复兼容；旧模型（2.5/3.0/3.1/3.5-flash 等规则生效前发布的模型）不受影响，保持原生 prefill 行为。修复策略（对齐社区实现 vertex-master 的成熟语义）：
  - 末尾是**纯文本** model turn 且前一轮是 user/function → 末尾追加一条续写指令的 user turn（官方迁移指南要求的兼容形态，保留 prefill 原文角色）；
  - **model-only / 连续 model turn** 等畸形历史（追加指令也修不好的结构）→ 兜底改写：移除末尾 model turn，prefill 原文以 JSON 数据形式内嵌进 user 指令（明确告知是已输出文本、不是待执行的指令，防提示注入）；
  - 末尾 model turn 含 **functionCall / thought / 媒体** part 的是真实历史（如待回复的工具调用），保持原样让上游按官方语义校验，不做猜测改写
- `PREFILL_FIX_PROMPT` - 追加的 user turn 文本（官方要求非空），默认为 "Continue the immediately preceding assistant response. Output only its continuation; do not repeat, explain, or restart it."（锚定"紧邻的上一条 assistant 回复"，避免被误解为续写用户消息）
- `PREFILL_FIX_STRIP_ECHO` - 响应侧回显剥离，默认为true。追加续写指令后，模型有相当概率先把 prefill 原文复述一遍再续写（客户端拼接后内容重复）；开启后在 Gemini API / Vertex 的全部流式与非流式路径上，用跨 chunk 状态机精确剥离回显前缀——仅在输出仍与前缀逐字匹配期间暂存、失配立即放行（正常输出首 chunk 即释放，零额外延迟），thought/工具/媒体 part 不受影响，流中途断开时未决内容按真实输出原样释放、绝不吞内容。行为异常时可关闭本开关回退
- `PREFILL_FIX_MODELS` - 额外白名单，逗号分隔的模型名/前缀/通配符，强制对这些模型启用补丁
- `PREFILL_FIX_EXCLUDE_MODELS` - 黑名单，逗号分隔，对这些模型停用补丁（优先级最高，可用于"该模型其实支持 prefill"的纠偏）

补丁覆盖全部三条链路：OpenAI/Claude/Responses 协议转换、原生 Gemini 协议透传、Vertex 模式（真流式/伪流式/非流式均含回显剥离）。

### 安全配置

- `RANDOM_STRING` - 是否启用随机字符串伪装，默认为true
- `RANDOM_STRING_LENGTH` - 随机字符串长度，默认为5
- `MAX_EMPTY_RESPONSES` - 最大空响应重试次数，默认为5

### 速率限制配置

- `MAX_RETRY_NUM` - 请求时的最大总轮询key数，默认为15
- `MAX_REQUESTS_PER_MINUTE` - 每分钟最大请求数，默认为30
- `MAX_REQUESTS_PER_DAY_PER_IP` - 每天每个IP最大请求数，默认为600
- `API_KEY_DAILY_LIMIT` - 每个API密钥每天使用限制，默认为100

### 模型过滤配置

- `BLOCKED_MODELS` - 屏蔽的模型列表，用逗号分隔
- `WHITELIST_MODELS` - 白名单模型列表，用逗号分隔
- `WHITELIST_USER_AGENT` - 白名单User-Agent列表，用逗号分隔

### 其他配置

- `PUBLIC_MODE` - 公益站模式，默认为false
- `DASHBOARD_URL` - 前端地址
- `ALLOWED_ORIGINS` - 允许的跨域源列表，用逗号分隔

## 🚀 部署方式

### Hugging Face Spaces 部署

1. Fork本项目到你的GitHub账户
2. 在GitHub Actions中构建Docker镜像
3. 在Hugging Face Spaces中创建新的Docker空间
4. 配置环境变量（PASSWORD和GEMINI_API_KEYS为必需）
5. 部署并访问前端界面

### Claw Cloud 部署

1. 注册Claw Cloud账户
2. 登录Claw Cloud控制台
3. 进入APP Launchpad并创建新应用
4. 使用镜像地址 `ghcr.io/zhu748/lajdnsma:latest`（本仓库 GitHub Actions 自动构建）
5. 配置环境变量
6. 部署应用并获取访问地址

### Vertex AI 部署

1. 在Google Cloud Platform中创建服务账户
2. 获取服务账户的JSON凭证文件
3. 在项目中启用Vertex AI API
4. 配置环境变量 `ENABLE_VERTEX=true` 和 `GOOGLE_CREDENTIALS_JSON`
5. 部署应用

### 本地部署

1. 克隆项目代码
2. 安装依赖：`pip install -r requirements.txt`
3. 配置环境变量
4. 运行应用：`uvicorn app.main:app --host 0.0.0.0 --port 7860`

## 运行日志与请求结果

控制台和管理面板的业务日志使用中文，普通网关与 Vertex 日志会合并显示。每次 API 请求结束后记录一条结果摘要，包含请求编号、模型、状态、耗时、缓存来源、输入/输出/思考/总 token、返回与思考文本字数、工具调用次数及结束原因。流式请求在实际结束后汇总，失败和客户端中断也会记录。

token 用量取自响应中的 usage；未提供的明细显示“未提供”。输出 token 已包含思考 token，不能再次相加。缓存命中的摘要描述缓存响应的用量，可通过“来源：缓存”区分。

在面板日志中展开“查看返回与思考摘要”可查看正文与已返回思考文本的预览。默认各保留前 200 字，设置环境变量 `LOG_RESPONSE_PREVIEW_CHARS=0` 可关闭内容预览、只保留统计；上限为 4096 字。请求要求隐藏思考时，日志也不记录隐藏的思考正文。日志不会打印完整请求头、访问令牌或凭证 JSON，常见密钥会自动脱敏。

## 📚 更多文档

- [配置文档](./app/config/settings.py) - 详细的配置选项说明
- [错误自查](./wiki/error.md) - 常见问题和解决方案
- [Hugging Face部署指南](./wiki/huggingface2.md) - 详细的Hugging Face部署教程
- [Claw Cloud部署指南](./wiki/claw.md) - 详细的Claw Cloud部署教程
- [Vertex模式指南](./wiki/vertex.md) - Vertex AI配置和使用指南
