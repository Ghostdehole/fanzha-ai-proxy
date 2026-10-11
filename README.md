# 国家反诈 AI 反向代理服务（fanzha-ai-proxy）

将反诈平台的网页接口适配为常用 OpenAI 风格接口，供个人协议研究与客户端集成使用。支持中文/英文、普通/深度问答、流式和非流式输出、语音合成、语音识别及用量看板。

这是**基础协议适配**，不是完整 OpenAI API 实现。模型名称中的 OpenAI 名称仅为兼容别名，实际请求始终发送到配置的反诈平台。上游的内容范围、模型能力和账户配额仍然适用。

## 安装与启动

需要 Python 3.9 或更新版本。建议使用独立虚拟环境：

~~~bash
git clone https://github.com/Ghostdehole/fanzha-ai-proxy.git
cd fanzha-ai-proxy
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
~~~

复制 .env.example 为 .env，填入自己的 FANZHA_ACCESS_TOKEN；有刷新令牌时同时填写 FANZHA_REFRESH_TOKEN，然后运行：

~~~bash
python main.py
~~~

默认监听 http://127.0.0.1:8088。本机客户端可以继续填写占位 API Key：sk-no-key-needed。

### 获取自己的上游凭证

在 https://xzfzznt.gaj.sh.gov.cn 登录自己的账号，通过浏览器开发者工具查看站点 Local Storage 中的 user 数据，或自己发送请求后的 Authorization 请求头，获取 accessToken 和 refreshToken。站点存储格式可能随上游版本变化；不要公开凭证，也不要提交 .env。

### 对外访问与浏览器调用

- 对外监听前，设置非空 PROXY_API_KEY，再把 HOST 改为所需地址，例如 0.0.0.0。
- 客户端的 Authorization: Bearer 值应填写 **PROXY_API_KEY**。它和上游令牌是两种不同的凭证。
- 配置代理密钥后，无密钥、错误密钥、占位密钥均返回 401；密钥不会转发给上游。
- 未配置代理密钥时，只接受回环连接和回环 Host。即使用 uvicorn main:app 从其他地址启动，也不能通过远程请求借用服务器凭证。
- 默认只允许 localhost、127.0.0.1 和 [::1] 的浏览器 Origin。网页客户端来自其他域名时，在 CORS_ORIGINS 中逐个填写可信的完整源（协议、域名及必要端口），用逗号分隔，不支持星号。
- 对外传输凭证时在前置网关配置 HTTPS。不要将密钥放入 URL。
- / 和 /health 是公开的进程存活检查，不访问上游、不表示上游账户可用。其他接口受代理鉴权保护。
- 本机默认模式可直接用浏览器访问 /usage。启用代理密钥后，看板请求也必须携带 Authorization 头，不提供 URL 密钥或免认证后门。

单进程启动是支持的部署方式。自动刷新锁只在进程内协调；不要用多个 worker 或副本共享同一套会轮换的刷新凭证。

## 配置

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| FANZHA_BASE_URL | https://xzfzznt.gaj.sh.gov.cn | 上游地址 |
| FANZHA_ACCESS_TOKEN | 空 | 服务器访问令牌 |
| FANZHA_REFRESH_TOKEN | 空 | 可选刷新令牌 |
| DEFAULT_MODEL | fanzha-ai | 缺省聊天模型 |
| HOST / PORT | 127.0.0.1 / 8088 | 监听地址与端口 |
| TRUST_ENV_PROXY | false | 是否使用系统代理环境变量 |
| PROXY_API_KEY | 空 | 独立代理密钥；对外监听必须设置 |
| CORS_ORIGINS | 空 | 额外允许的浏览器源，逗号分隔 |
| TOKEN_STATE_FILE | 空 | 可选的刷新凭证状态文件 |
| MAX_REQUEST_BODY_BYTES | 33554432 | 整个请求体上限，包含分块传输和 multipart 开销 |
| MAX_AUDIO_UPLOAD_BYTES | 26214400 | 单个 ASR 文件大小上限 |
| MAX_UPSTREAM_RESPONSE_BYTES | 8388608 | 上游单个 JSON、完整聊天流或合成音频总量上限 |
| MAX_CHAT_CHARS | 128000 | 完整聊天历史及附件 URL 的字符上限 |
| MAX_TTS_CHARS | 20000 | 语音合成输入字符上限 |
| UPSTREAM_REQUIRE_DONE | false | 是否要求上游显式发出流结束标记 |

大小限制为本服务的资源保护配置，不代表上游接受同样大小的请求；超过上游实际限制仍会返回错误。

### 刷新凭证

服务器管理的令牌在收到 HTTP/JSON 401 时刷新一次并重试一次，聊天、语音、识别和配额查询使用同一逻辑。并发请求共用一次刷新；同一套失败凭证短暂冷却 5 秒，避免排队反复刷新。不会在已输出聊天内容后重放请求。

默认刷新结果只保存在内存中。需要重启后继续使用轮换后的凭证时，可设置：

~~~dotenv
TOKEN_STATE_FILE=.fanzha-token-state.json
~~~

该文件在 .gitignore 中排除，采用原子替换写入，在支持 POSIX 权限的平台限制为 0600。Windows 上还应确保存放目录的账户访问权限合适。状态绑定初始服务器凭证和上游地址；修改配置后不会误载入旧账户状态。不要在进程间共享该文件；不要将路径指向 .env 或仓库源码。

### 使用不同的上游账户

未启用 PROXY_API_KEY 的本机旧模式继续支持把自己的上游 Bearer Token 放入 Authorization；空值及 sk- 开头的占位符使用服务器凭证。

启用代理密钥后，Authorization 仅用于代理鉴权。需要指定另一个上游账号时，用 X-Upstream-Token 传入该账号的访问令牌。此类外部令牌**不会使用服务器的 Refresh Token**，过期后由调用方更新。

## 接口与模型

| 接口 | 模型 / 功能 |
| --- | --- |
| POST /v1/chat/completions | fanzha-ai、fanzha-ai-deep、fanzha-ai-en、fanzha-ai-en-deep |
| POST /chat/completions | 上述聊天接口的兼容路径 |
| GET /v1/models、GET /models | 可用模型列表 |
| POST /v1/audio/speech | tts-1，tts-1-hd 为同一引擎的别名 |
| POST /v1/audio/transcriptions | whisper-1 为上游 ASR 的兼容别名 |
| GET /usage | 配额 HTML 看板或 JSON |
| GET /v1/dashboard/billing/usage | 配额接口兼容路径；不是美元计费统计 |
| GET /、GET /health | 进程存活检查 |

gpt-4o-mini 是 fanzha-ai 的聊天别名，不调用 GPT-4o mini。tts-1-hd 不提供额外音质档位。未知模型会返回 400，避免无意切换模式。

### 聊天

~~~bash
curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-no-key-needed" \
  -d '{"model":"fanzha-ai-deep","messages":[{"role":"user","content":"自称领导的人要求向私人账户垫付资金，该如何核实？"}],"stream":true}'
~~~

启用代理密钥时，将上例占位密钥替换为自己的 PROXY_API_KEY。

- 支持 stream 布尔值、temperature（0 到 2）、正整数 max_tokens / max_completion_tokens（两者同时出现时必须一致）和 n=1。
- 默认 temperature=0.1、max_tokens=2048；显式提供的值会传递给上游，上游是否遵从取决于其实现。
- 普通单条用户文本原样传递；多轮请求每次创建独立上游会话，将完整 system/developer/user/assistant 历史按角色和顺序编码为文本。客户端必须在每次调用携带完整历史。
- 不再根据相同开场白复用会话，因此不同账号、相同开场白、编辑分支、重启和历史导入不会依赖错误的服务端历史缓存。
- 私有接口只有文本字段，角色层级通过文本模拟，不能保证等同于原生 OpenAI 角色语义，也不能覆盖上游自己的规则。
- 用户 image_url 内容块作为附件 URL 传入，并在历史中标记顺序；不会在代理端下载图片。实际图像理解、格式与历史附件关联能力取决于上游；detail 仅接受 auto。
- 工具调用、JSON schema、音频聊天、多候选、非默认惩罚等未实现能力返回 400。常见 SDK 中性默认参数可以接受；未知字段不静默丢弃。
- 不把字符数伪装成 token 计数：非流式响应省略未知 usage；stream_options.include_usage=true 时，流中的 usage 为 null，不生成虚假的累计用量。
- 未收到有效文本时返回错误，不再生成 Notice 占位回答。
- 上游 HTTP/JSON 错误在开始下游流之前检查。已输出内容后发生错误会发出 data: {"error": ...}，随后 [DONE]，不会再发正常 finish_reason=stop。此时已发送的 HTTP 200 无法更改，调用方应检查流内 error。
- 支持多行 SSE、心跳、跨网络块的 UTF-8 与 CR/LF、完成标记和响应限额。默认兼容“有文本后正常 EOF”的旧上游；因此无法仅凭正常 EOF 判断其是否静默截断。确认上游提供完成标记后可启用 UPSTREAM_REQUIRE_DONE=true。

官方模型仍以反诈内容为范围。客户端后台自动生成标题等通用任务可能被上游拒答；可关闭客户端的自动标题功能。

### 语音合成

~~~bash
curl http://127.0.0.1:8088/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"不轻信，不透露，不转账。","voice":"shanghainese","response_format":"wav"}' \
  --output warning.wav
~~~

保留 mandarin、shanghainese、dongbei、guangxi、chongqing、english 六个方言/语言选项。兼容名称 alloy、echo、fable、onyx、nova、shimmer 均明确映射为 mandarin，不复刻同名 OpenAI 音色。

默认且唯一输出格式为 WAV，只支持 speed=1。MP3、PCM、其他格式、语速及未知参数返回 400，不再偷偷返回另一种格式。文本按标点优先切分，任何单段不超过 150 字符；任一片段失败、缺失、损坏或 PCM 参数不一致都会让整个请求失败，不返回缺字音频。

### 语音识别

~~~bash
curl http://127.0.0.1:8088/v1/audio/transcriptions \
  -F "file=@warning.wav" \
  -F "model=whisper-1" \
  -F "response_format=json"
~~~

支持 json 和 text 两种返回格式。校验声明的文件类型和扩展名、拒绝空文件，按配置限制上传大小并分块读取；实际音频编解码支持由上游决定。language、prompt、时间戳和其他高级参数目前不支持，返回 400。

### 配额看板

~~~bash
curl http://127.0.0.1:8088/usage -H "Accept: application/json"
~~~

浏览器的 text/html 或通配 Accept 可查看 HTML；明确的 JSON 请求优先于同权重通配符。保留明暗主题、中英文切换、刷新和原始 JSON 展示。

有效遥测应包含 data.remaining、used、limit 数字，以及 banned、exceeded 布尔值；role 和 ban_ttl 可省略。超时、凭证错误、不完整数据等显示“状态未知”，并返回相应错误 HTTP 状态，不把缺失数据解释为正常。动态 HTML 已转义，凭证只显示“已配置/未配置”，响应禁止缓存。

## 错误与排查

错误采用 {"error":{"message":"...","type":"...","param":null,"code":...}} 格式。常见状态：

| 状态 | 含义 |
| --- | --- |
| 400 | 参数类型错误、未知模型或不支持的能力 |
| 401 / 403 | 代理认证失败、上游凭证/权限错误，或不允许的来源 |
| 413 | 上传文件或请求体超过配置限额 |
| 429 | 上游限流；可用时保留 Retry-After |
| 502 | 上游失败、无效响应、坏音频或协议不兼容 |
| 504 | 上游通信超时 |

遇到连接超时，先检查网络分流和上游网站本身是否可达。TRUST_ENV_PROXY=false 只忽略代理环境变量，不能控制系统 TUN/VPN 的路由。/health 正常并不证明上游可用。

刷新返回缺失令牌、创建会话失败或上游返回 HTML 时，不再随机生成会话 ID 或伪装为成功。日志不记录提示词内容、凭证片段或上游原始异常正文。

## 开发与测试

~~~bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
~~~

测试使用本地 HTTPX/ASGI 模拟，不需要真实令牌、不消耗上游配额。覆盖鉴权隔离、并发刷新、错误码、聊天历史、SSE、取消释放、输入边界、音频完整性和仪表盘安全。CI 配置运行 Python 3.9、3.12、3.14。

详见 [维护说明](docs/maintenance.md)。模拟测试不能证明当前私有接口、实际音色、图像理解或业务限制与上游线上实现完全一致。

## 使用范围与许可

本项目为独立研究项目，与上游平台无官方隶属关系。仅使用自己有权访问的账号与额度；不要将凭证、个人信息或服务暴露给未授权调用方。

采用 GPL-3.0 许可证，见 [LICENSE](LICENSE)。
