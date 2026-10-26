# 请求头专项审查

日期：2026-10-26。范围：AI Studio 手写 REST、四类协议入口转换、Embedding、模型列表与密钥探测、Vertex Express、Vertex 服务账号、Vertex OpenAI Direct 的非流式/真流式/伪流式、辅助下载，以及两个应用入口的 CORS。

结论：原实现没有做到一致的请求头模拟。认证主路径基本正确，但版本标识、SDK 附加头删除、伪流式内容协商与配额项目处理存在明确问题。本轮已修复，并用真实依赖构造请求，在 HTTP transport 入口捕获最终请求验证。测试使用合成凭证和 MockTransport；没有向 Google 发起计费调用，没有测量 TLS 指纹，也没有证明网关与浏览器或官方客户端在网络层不可区分。

## 发现与修复

| 问题 | 原行为及影响 | 修复 |
| --- | --- | --- |
| SDK 标识格式和版本不一致 | 手写池使用 `google-genai-sdk-python/...`，包含旧版 SDK、Chrome 126–132 和 curl；当前安装的 google-genai 1.52.0 实际发送 `google-genai-sdk/1.52.0 gl-python/<运行版本>` | 手写 Google REST 头从已安装包版本与当前 Python 版本生成，UA 与 `x-goog-api-client` 相同；与真实 SDK 请求逐项比较 |
| 随机浏览器身份与实际传输不匹配 | Python httpx 请求随机声明 Chrome，并带固定 AI Studio Origin/Referer 与 Client Hints；Direct 路径选择浏览器 UA 时又没有这些配套字段 | 统一为稳定的 Google SDK 兼容 REST 头，移除浏览器身份池和虚构浏览器上下文；这不是浏览器 TLS 模拟 |
| `X-Stainless-*` 并未真正删除 | 设置空字符串后，最终请求仍含空的 Lang/OS/Runtime 等头；SDK 又填入 Retry-Count 和 Read-Timeout | 使用异步 HTTP request hook，在 SDK 完成所有头合并之后移除整个前缀；无空字符串占位 |
| OpenAI 环境配置可能发往 Google | AsyncOpenAI 会读取 `OPENAI_ORG_ID` / `OPENAI_PROJECT_ID`，即使 base_url 是 Vertex | 同一出站 hook 删除 OpenAI-Organization / OpenAI-Project；保留 Google 认证与配额头 |
| 伪流式 Accept 错误 | 下游 stream=true 决定客户端默认 Accept=SSE，但上游实际 stream=false，返回 JSON | 每次实际调用决定 Accept：真流式 SSE，普通和伪流式 JSON；避免默认头串用 |
| Direct 遗漏配额项目 | 原生 SDK 会发送凭证的 quota_project_id，Direct 仅传 access token | 凭证存在该值时加入 `x-goog-user-project`；配额项目参与客户端缓存隔离 |
| SDK 跨域请求被拦 | 固定 CORS 头名单只覆盖基本鉴权，遗漏 SDK 元数据及浏览器显式启用头 | 主应用和独立 Vertex 应用均允许已配置来源请求的头；方法限制与 Cookie 凭证禁用保持，实际请求仍要求认证 |
| UA 缓存无界、身份随 token 刷新变化 | 原 `_key_to_ua` 长期保存每个 API key/token，服务重启或 token 更新后重新随机选身份 | 删除缓存和锁；软件版本身份与凭证无关；辅助版本/模型配置下载使用 httpx 自身默认 UA，不携带 Google 认证或 API 客户端头 |

Direct 客户端现在共用已有的 HTTP 连接池配置和分层超时。非流式、真流式、伪流式不再因为 Accept 不同创建重复 SDK 客户端；凭证或配额项目不同仍分别隔离。

## 修复后的头矩阵

以下是请求到达 HTTP transport 前的行为，不是抓取生产 TLS 流量的结果。`Host`、`Content-Length`、HTTP/2 伪头由传输层处理，不手工模拟。

| 上游路径 | 认证 | Accept / Content-Type | 客户端与其他头 |
| --- | --- | --- | --- |
| AI Studio generateContent | `x-goog-api-key`，密钥不放查询参数 | `application/json` / `application/json` | 统一 Google SDK 版本头；gzip/deflate |
| AI Studio streamGenerateContent | 同上 | `text/event-stream` / `application/json`；URL 含 `alt=sse` | 同上 |
| Embedding batchEmbedContents | 同上 | JSON / JSON | 同上 |
| 模型列表、启动及面板密钥探测 GET | 同上 | JSON / 不发 Content-Type | 同上；不继承其他请求的认证 |
| Vertex Express 原生 SDK | `x-goog-api-key` | 保留 SDK 默认内容协商；流式由 action 与 `alt=sse` 指定 | 保留真实 SDK 的版本头，不额外注入浏览器字段 |
| Vertex 服务账号原生 SDK | `Authorization: Bearer <token>` | 同上 | SDK 自动发送凭证中的 `x-goog-user-project`（若存在） |
| Vertex OpenAI Direct JSON / 伪流式 | Bearer Token | JSON / JSON | 统一 Google REST 头，保留配额项目，移除 Stainless/OpenAI 账户头 |
| Vertex OpenAI Direct 真流式 | Bearer Token | SSE / JSON | 同上；与 JSON 请求复用客户端 |
| 远程版本、模型配置文件 | 无 Google API 认证 | 调用方指定或 httpx 默认 | httpx 默认 UA；无 Google API 客户端头 |

OpenAI Chat、Responses、Anthropic Messages 的入口身份只用于网关入口鉴权和协议选择。代码不复制整套入站请求头给 Google；上游身份按实际后端构造。`anthropic-version`、客户端 Cookie、客户端 Origin 不属于 Gemini 的上游鉴权头。

## 可复现验证

```bash
uv run pytest -q tests/test_upstream_headers.py
uv run pytest -q
```

新增 9 组隔离进程契约测试，避免已有测试中的假模块污染真实 SDK：

- 四种协议转换后的 Gemini JSON/SSE、两把密钥轮换、Embedding、模型列表、密钥探测，共 22 个 REST 请求；验证认证不串用、无 URL 密钥、无重复头、GET 无 Content-Type。
- 实际项目客户端工厂创建的 Vertex Express 与服务账号 SDK，分别验证 JSON/SSE、认证、配额项目及版本标识。
- 实际 OpenAI SDK 的 JSON、真流式、伪流式及客户端复用；检查动态重试/超时头已被移除、OpenAI 环境账户字段未外泄、Accept 与上游 body.stream 一致。
- 主应用与独立 Vertex 应用的预检请求；已配置来源成功，未配置来源拒绝，无凭证的实际请求仍返回 401。

原有请求头单元测试也已改为确定性断言。旧测试随机抽到 SDK UA 时会跳过浏览器分支，也没有检查 OpenAI SDK 最后再次增加的头，因此不能证明最终请求正确。

本轮全量验证：605 项 Python 测试、119 项子测试通过，耗时 47.75 秒；`git diff --check` 通过。未修改前端源码或重新生成前端构建产物。以上为请求头专项阶段的验证记录；后续完整交付与更新结果见总审查报告及 Git 提交历史。

## 能力边界与依据

- 本轮实现的是服务端 Google REST 请求的一致性。更改 UA、Client Hints 或启用 HTTP/2，不能据此声称复制了 Chrome 的 TLS 握手、HTTP/2 SETTINGS、请求顺序或连接行为。
- 没有生产代理链路抓包，也未检查代理出口会不会重写请求头。真实服务账号权限、配额项目权限、模型区域和密钥限制仍需部署环境验证。
- SDK 升级后要重跑契约测试；若 Google 修改版本头规则，测试会暴露手写 REST 与 SDK 的差异。包版本取自当前安装环境，不声称是上游最新版本。
- [Gemini API 鉴权与 SSE](https://ai.google.dev/api)：核对 `x-goog-api-key` 和流式端点语义。
- [Vertex OpenAI 兼容鉴权](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/migrate/openai/auth-and-credentials)：核对 access token 路径。
- [Google Cloud 配额项目](https://docs.cloud.google.com/docs/quotas/set-quota-project)：核对 `x-goog-user-project`；本项目仅透传凭证已有值，不猜测或替换配额项目。
- 本地安装的 `google-genai==1.52.0` `_api_client.append_library_version_headers`、`openai==1.76.0` `_base_client._build_headers` 与 `_client.AsyncOpenAI.default_headers`：核对真实 SDK 版本标识、动态附加头和环境变量行为。
