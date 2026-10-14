# 国家反诈 AI 反向代理服务 (fanzha-ai-proxy)

本项目将“国家反诈AI”（官方智脑平台）的私有通信接口封装为兼容常用 OpenAI 接口格式的 RESTful API 服务。

支持流式（SSE）打字机模式与非流式响应，支持深度研判推理引擎（`xxhd`）、多语种问答、语音合成（TTS）、语音识别转文字（ASR）以及内置的账户用量与风控监控仪表盘。可通过自定义 OpenAI 接口接入 Chatbox、Cherry Studio、NextChat、LobeChat、OneAPI / New API 等主流开源生态与客户端；具体可用功能取决于客户端配置和上游能力。

> 本项目是独立的协议适配服务，并非完整 OpenAI API 实现。兼容模型名不代表调用 OpenAI 的模型；上游的内容范围、账户配额及使用规则仍然适用。接口支持范围见下文。

---

## 功能特性

- 常用接口协议适配：支持 `/v1/chat/completions`、`/v1/models`、`/v1/audio/speech`、`/v1/audio/transcriptions`。
- 多模型能力覆盖：内置标准问答、官方深度研判模式（`xxhd` 详细推演引擎）及国际英文模式。
- 语音合成与识别：支持多方言 TTS 语音合成（普通话、沪语、东北话、广西话、重庆话、英语）与 ASR 录音转文字。
- 账户与配额监控：内置独立的 HTML/CSS/JS 遥测看板，支持查看调用额度、周期限额、WAF 封禁状态与倒计时。
- 多轮上下文传递：每次请求建立独立上游会话，传入客户端携带的完整历史，避免不同账号或相同开场白之间串话。
- 长效自动续期：内置并发安全锁，服务器 Access Token 返回 401 时使用 Refresh Token 刷新并重试一次，覆盖对话、音频及配额查询；已输出的聊天内容不会自动重放。
- 网页请求适配：保留上游网页使用的 Origin、Referer、User-Agent 与渠道标头；网络可达性及风控结果仍由上游决定。
- 模块化工程架构：遵循分层设计，路由、协议适配、网络通信、音频处理彼此解耦，易于二次开发与维护。

---

## 支持模型一览

| 模型名称 (Model ID) | 适用接口 | 特性说明 |
| :--- | :--- | :--- |
| `fanzha-ai` | `/v1/chat/completions` | 中文标准问答模式，响应速度快，适用于日常风险咨询。 |
| `fanzha-ai-deep` | `/v1/chat/completions` | 中文深度研判模式，调用官方 `xxhd` 引擎进行推演和定性分析。 |
| `fanzha-ai-en` | `/v1/chat/completions` | 国际版英文标准问答模式。 |
| `fanzha-ai-en-deep` | `/v1/chat/completions` | 国际版英文深度研判模式。 |
| `tts-1` | `/v1/audio/speech` | 语音合成引擎，支持普通话、沪语等方言及英语。 |
| `tts-1-hd` | `/v1/audio/speech` | `tts-1` 的兼容别名，使用相同合成引擎，不提供额外音质档位。 |
| `whisper-1` | `/v1/audio/transcriptions` | 语音识别引擎，将音频文件转写为文本。 |
| `gpt-4o-mini` | `/v1/chat/completions` | 兼容性别名，映射至 `fanzha-ai`。 |

`tts-1`、`tts-1-hd`、`whisper-1` 和 `gpt-4o-mini` 均用于客户端接口兼容，实际请求由反诈平台处理。可通过 `GET /v1/models` 获取列表；未知模型返回 `400`。

---

## 令牌 (Token) 获取方式

服务端依赖官方平台的身份凭证进行转发鉴权。推荐使用桌面端浏览器抓取。

### 抓取步骤

1. 在电脑浏览器（Chrome / Edge 等）打开官方网页端：[https://xzfzznt.gaj.sh.gov.cn](https://xzfzznt.gaj.sh.gov.cn)，按页面提供的方式注册并登录自己的账号。原版使用教程记录了手机号短信或邮箱登录、自有域名邮箱的用法，以及每个账号每天限额提问 20 条；当前注册方式和实际额度请以上游页面、账户配额为准，邮箱别名不代表无限可用额度。
2. 按键盘 `F12` 打开开发者工具：
   - 方式 A（读取存储）：进入 Application（应用程序）标签页，展开 Local Storage（本地存储空间），点击站点域名，找到键名为 `user` 的项，展开 JSON 数据即可查看到 `accessToken` 和 `refreshToken`。
   - 方式 B（捕获请求）：进入 Network（网络）标签页，在网页发送一条提问。在网络记录中查看 `create_session` 或 `chat` 请求的 Request Headers（请求头），复制 `Authorization` 中 `Bearer ` 后面的字符串。
3. 将获取到的字符串填入 `.env` 文件中的 `FANZHA_ACCESS_TOKEN` 和 `FANZHA_REFRESH_TOKEN`，无需包含 `Bearer ` 前缀。

方式 B 的请求头通常只能获取 Access Token；需要自动续期时，还要从自己的登录数据中获取 Refresh Token。上游页面和存储格式可能调整。不要公开令牌、提交 `.env` 或上传包含令牌的截图。

---

## 项目结构说明

```text
fanzha-ai-proxy/
├── .env.example          # 环境变量示例，复制为 .env 后填写自己的配置
├── .gitignore            # Git 忽略文件规则
├── requirements.txt      # 运行依赖清单
├── requirements-dev.txt  # 测试依赖清单
├── pytest.ini            # 测试配置
├── config.py             # 配置读取与日志格式化模块
├── auth.py               # 代理鉴权与上游账户凭证选择
├── errors.py             # 统一 API 错误格式
├── utils.py              # WAV 音频校验拼接、文本切片
├── adapter.py            # OpenAI 协议适配、历史编码与 SSE 打包
├── client.py             # 上游 API 通信、独立会话与 Token 续期
├── main.py               # 统一应用主入口、生命周期、中间件和路由聚合
├── templates/
│   └── dashboard.html    # 独立的用量遥测看板前端模板
├── routers/
│   ├── __init__.py       # 路由聚合导出
│   ├── chat.py           # 对话补全接口与上游 SSE 解析
│   ├── audio.py          # 语音合成与识别接口路由
│   ├── models.py         # 模型列表接口路由
│   └── dashboard.py      # 配额查看与健康检查接口路由
├── tests/                # 本地模拟上游的回归测试
├── docs/
│   └── maintenance.md    # 修复记录及兼容性说明
└── .github/workflows/
    └── tests.yml         # GitHub Actions 自动测试
```

---

## 快速安装与启动

### 1. 环境准备

需要 Python 3.9 及以上版本，建议使用独立虚拟环境。

```bash
git clone https://github.com/Ghostdehole/fanzha-ai-proxy.git
cd fanzha-ai-proxy
python -m venv .venv

# Windows PowerShell：.venv\Scripts\Activate.ps1
# Windows CMD：.venv\Scripts\activate.bat
# macOS / Linux：source .venv/bin/activate
# 根据自己的系统执行上面对应的一条激活命令，然后安装依赖：
python -m pip install -r requirements.txt
```

### 2. 配置环境变量

将项目根目录的 `.env.example` 复制为 `.env`，再编辑配置。Windows 请确认文件没有被保存成 `.env.txt`。基本配置如下：

```ini
# 官方接口地址（通常保持默认）
FANZHA_BASE_URL=https://xzfzznt.gaj.sh.gov.cn

# 必填：国家反诈 AI 访问令牌 (Access Token)
FANZHA_ACCESS_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6...

# 可选：国家反诈 AI 刷新令牌 (Refresh Token，用于长效自动续期)
FANZHA_REFRESH_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6...

# 可选：从本人官方网页聊天请求取得的 X-Chat-Key；上游要求时填写
FANZHA_CHAT_KEY=

# 默认对话模型
DEFAULT_MODEL=fanzha-ai

# 服务监听配置
HOST=127.0.0.1
PORT=8088

# 独立代理密钥：仅本机使用时可留空；对外监听前必须设置
PROXY_API_KEY=

# 是否使用系统代理环境变量（不控制 TUN / VPN 路由）
TRUST_ENV_PROXY=false

# 可选：保存刷新后的令牌，避免重启后丢失轮换凭证
# TOKEN_STATE_FILE=.fanzha-token-state.json
```

### 3. 运行服务

```bash
python main.py
```

服务启动后，控制台将输出监听地址（默认 `http://127.0.0.1:8088`）。

默认本机模式下，客户端可以填写占位密钥 `sk-no-key-needed`。需要供局域网、服务器或其他设备访问时，按下一节设置代理密钥。

### 4. 远程访问与代理鉴权

在 `.env` 中设置自己的代理密钥，并修改监听地址：

```ini
HOST=0.0.0.0
PROXY_API_KEY=替换为你自己生成的随机长密钥
```

- 客户端填写服务器实际地址，例如 `http://服务器IP:8088/v1`；`0.0.0.0` 是监听地址，不是客户端访问地址。对外传输凭证时，应在前置网关配置 HTTPS。
- 客户端 API Key 填写 `PROXY_API_KEY`，请求头为 `Authorization: Bearer 你的代理密钥`。启用后，原来的占位密钥不再有效。
- `PROXY_API_KEY` 是访问本代理的密码，`FANZHA_ACCESS_TOKEN` 是上游平台的凭证，两者用途不同；代理密钥不会发送给上游。
- 未配置代理密钥时，仅接受回环连接和回环 Host；即使换用其他 Uvicorn 启动方式，也不能直接开放远程访问。
- 网页客户端来自非本机域名时，在 `CORS_ORIGINS` 中填写可信的完整源，例如 `https://chat.example.com`；多个源用逗号分隔，不支持 `*`。默认允许本机的 `localhost`、`127.0.0.1` 和 `[::1]` 浏览器源。
- `/usage` 看板同样需要鉴权。配置密钥后，直接在浏览器地址栏打开会返回 `401`，需要通过能添加 `Authorization` 请求头的方式访问，不要把密钥放入 URL。
- `/` 与 `/health` 是公开的进程存活检查，不调用上游，不能代表上游账号或额度可用。

请保持**单进程**运行：当前令牌刷新锁仅在进程内协调，不支持多个 worker 或副本共同管理同一套会轮换的刷新凭证。

---

## 接口调用与使用示例

以下示例使用默认本机模式；如果配置了 `PROXY_API_KEY`，请把所有 `sk-no-key-needed` 替换为自己的代理密钥。cURL 示例采用 Bash 语法，Windows 可在 Git Bash 或 WSL 中运行。

### 1. 访问配额与状态仪表盘

使用浏览器访问：
`http://127.0.0.1:8088/usage`

页面展示：

- 周期内剩余可用次数、已使用次数、周期总限额。
- 账户角色、配额超限状态、WAF 封禁状态及解封倒计时。
- 页面提供中英文语言切换与明亮/暗黑主题切换。

若请求头带有 `Accept: application/json`，该接口在查询成功时返回原始 JSON 遥测数据：

```bash
curl http://127.0.0.1:8088/usage \
  -H "Authorization: Bearer sk-no-key-needed" \
  -H "Accept: application/json"
```

查询超时、凭证失效或配额数据不完整时，看板显示“状态未知”并返回相应错误状态，不再把查询失败显示为正常；凭证只显示是否已配置，不展示令牌片段。

---

### 2. 文本对话 (Chat Completions)

#### cURL 调用示例（流式响应）

```bash
curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-no-key-needed" \
  -d '{
    "model": "fanzha-ai-deep",
    "messages": [
      {"role": "user", "content": "有人声称是我领导，让我先垫付资金转账到指定私人账户，这是诈骗吗？"}
    ],
    "stream": true
  }'
```

#### Python SDK 调用示例

先安装可选的 Python 客户端依赖：

```bash
python -m pip install openai
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8088/v1",
    api_key="sk-no-key-needed"
)

# 使用深度研判模式
stream = client.chat.completions.create(
    model="fanzha-ai-deep",
    messages=[
        {"role": "user", "content": "收到自称医保局的短信说账户异常需点击链接认证，该如何核实？"}
    ],
    stream=True
)

for chunk in stream:
    content = chunk.choices[0].delta.content or ""
    print(content, end="", flush=True)
print()
```

#### 多轮对话与参数支持

- 非流式调用可设置 `stream=false`。多轮对话需在每次请求中携带完整 `messages` 历史，包括先前的用户提问与助手回复。
- 每次调用使用独立上游会话，不再依赖首句哈希复用会话。`system`、`developer`、`user` 和 `assistant` 历史按角色与顺序编码为文本；这是角色模拟，不等同于上游原生支持 OpenAI 的角色层级。
- 支持 `temperature`（0～2，默认 0.1）、正整数 `max_tokens` / `max_completion_tokens`（默认 2048）和 `n=1`。两个 token 上限同时传入时必须一致；参数会传给上游，其实际执行取决于上游实现。
- 用户消息中的 `image_url` 内容块作为附件 URL 传递，并保留顺序标记，代理本身不下载图片；`detail` 仅支持 `auto`，实际图像理解和历史附件关联能力取决于上游。
- 工具调用、JSON Schema、多候选、音频聊天等未实现能力返回明确的 `400`，不会静默忽略。客户端如果默认附带这些参数，需要关闭对应功能。
- 上游没有提供可靠的 token 用量，因此非流式响应省略未知的 `usage`；设置 `stream_options.include_usage=true` 时，流中的 `usage` 为 `null`。
- 已开始输出的流若发生错误，会发送 `data: {"error": ...}`，随后发送 `[DONE]`，不会再以正常 `finish_reason=stop` 结束。此时 HTTP 状态已发送，客户端还需检查流内错误。

---

### 3. 语音合成 (Text-to-Speech)

支持以下方言与语言选项（`voice`）：

- `mandarin`（普通话，默认）
- `shanghainese`（沪语）
- `dongbei`（东北话）
- `guangxi`（广西话）
- `chongqing`（重庆话）
- `english`（英语）

```bash
curl http://127.0.0.1:8088/v1/audio/speech \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-no-key-needed" \
  -d '{
    "model": "tts-1",
    "input": "防范电信网络诈骗，不轻信、不透露、不转账。",
    "voice": "shanghainese",
    "response_format": "wav",
    "speed": 1
  }' \
  --output warning_shanghai.wav
```

- 默认且唯一输出格式为 **WAV**，仅支持 `speed=1`。MP3、PCM、其他格式或语速会返回 `400`；使用 SDK 时也请显式指定 `response_format="wav"`。
- `alloy`、`echo`、`fable`、`onyx`、`nova`、`shimmer` 六个兼容音色名均映射为 `mandarin`，不代表复刻 OpenAI 的同名音色。
- 长文本自动按标点优先切分，每段不超过 150 字符。任一分段失败、音频损坏或 PCM 参数不一致时，整次请求返回错误，避免返回缺字音频。

---

### 4. 语音识别 (Speech-to-Text)

将诈骗通话录音转写为文本：

```bash
curl http://127.0.0.1:8088/v1/audio/transcriptions \
  -H "Authorization: Bearer sk-no-key-needed" \
  -F "file=@warning_shanghai.wav" \
  -F "model=whisper-1" \
  -F "response_format=json"
```

返回格式：

```json
{
  "text": "防范电信网络诈骗不轻信不透露不转账"
}
```

也可指定 `response_format=text` 返回纯文本。默认单文件上限为 25 MiB，空文件或不支持的文件类型会被拒绝；实际音频解码能力由上游决定。目前不支持 `language`、`prompt`、时间戳等高级参数，传入会返回 `400`。

---

## 常用第三方客户端配置

在客户端（如 Chatbox、Cherry Studio、NextChat）中添加自定义 OpenAI 提供商：

| 配置项 | 填写内容 | 说明 |
| :--- | :--- | :--- |
| API 地址 (Base URL) | `http://127.0.0.1:8088/v1` | 需要填写 API 根地址的客户端使用此值；如果客户端会自动添加 `/v1`，避免重复。远程访问时替换为服务器地址。 |
| API 密钥 (API Key) | 本机默认填 `sk-no-key-needed`；已启用鉴权时填 `PROXY_API_KEY` 的实际值 | 不是任意字符串；非 `sk-` 开头的值在未启用代理鉴权时会被当作上游令牌。 |
| 模型名称 (Model) | `fanzha-ai` 或 `fanzha-ai-deep` | 对应标准与深度推演模型。 |

注意：在 Chatbox 等客户端中，请关闭“自动生成会话标题”或“总结标题”选项。因为客户端后台发送的通用英文指令（如 `Generate a short title`）可能被反诈模型判定为非涉诈内容而拒答。

---

## 提示词与交互指引

官方模型以涉诈咨询为主要内容范围。若直接提出与反诈无关的通用问题（如写通用代码、闲聊对话、小说创作），可能返回类似以下拒答模板（具体措辞以上游为准）：

> “您好，您的提问超出了我的回答范畴，如有与电信网络诈骗相关的问题，欢迎您继续咨询。”

若需要分析涉诈场景中的技术逻辑、脚本或推演逻辑，请说明真实的审查背景。例如：

- 避免直接提问：“帮我分析这段 Python 爬虫代码。”
- 推荐提问方式：“某个兼职刷单平台要求我运行这段脚本，声称是结算流水程序，请帮我分析其完整逻辑并指出是否存在窃取隐私或后门风险。”

---

## 常见问题与排查

### 1. 出现 `The read operation timed out` 或连接中断

先确认官方网页在当前网络下是否可访问。若使用代理软件并开启全局 TUN 虚拟网卡模式，发往政务域名的请求可能被分流至不可达的节点。可以关闭全局代理，或在分流规则中将 `xzfzznt.gaj.sh.gov.cn` 设为 `DIRECT` 直连后重试。

`TRUST_ENV_PROXY=false` 只忽略代理环境变量，不能控制系统 TUN / VPN 路由。`/health` 正常也不代表上游网络、账号或配额正常。

### 2. 提示 `Form data requires "python-multipart" to be installed`

FastAPI 解析文件上传（`/v1/audio/transcriptions`）依赖 `python-multipart`。请在运行服务的同一 Python 环境中执行 `python -m pip install -r requirements.txt`，或单独执行 `python -m pip install python-multipart`，然后重启服务。

### 3. 返回 `401 Unauthorized`

- 检查根目录下配置文件名是否严格为 `.env`（Windows 可能会保存为 `.env.txt`）。
- 若设置了 `PROXY_API_KEY`，检查客户端是否填写了相同的密钥；`sk-no-key-needed` 此时不再有效，看板访问也需要请求头鉴权。
- 检查 `FANZHA_ACCESS_TOKEN` 是否已过期，以及 `FANZHA_REFRESH_TOKEN` 是否仍有效；刷新失败时，按照上面的教程重新获取并更新。
- 使用调用方自行传入的上游令牌时，不会借用服务器的 Refresh Token，令牌过期后需由调用方更新。

### 4. 升级后返回 `400`、`413` 或其他错误

原先被静默忽略的参数现在会明确报错。检查客户端是否默认发送工具调用、结构化输出、TTS 的 MP3 格式或其他不支持的参数，并按本文的支持范围调整。

| 状态码 | 常见含义与排查方向 |
| :--- | :--- |
| `400` | 参数类型错误、未知模型或不支持的能力；查看错误中的 `message` 和 `param`。 |
| `401` / `403` | 代理认证、上游凭证或权限错误，或浏览器来源未获允许。 |
| `413` | 单个音频文件或整个请求体超过配置上限。 |
| `429` | 上游限流；有 `Retry-After` 时按其提示等待。 |
| `502` | 上游失败、响应格式异常、音频损坏或协议不兼容。 |
| `504` | 上游通信超时；检查网络及上游状态。 |

看板出现“状态未知”表示当前无法确认账户状态，不等同于已经封禁，也不能视为额度正常。

### 5. 登录正常，但聊天返回 `403`

先看错误中的 `message`：`This browser origin is not allowed.` 表示浏览器来源未获允许；`Remote access requires PROXY_API_KEY.` 表示远程访问尚未配置代理密钥；`Upstream access was denied.` 表示上游拒绝了请求。它们需要分别排查。

2026-10-14 的官方前端样本显示，聊天请求除 Bearer 令牌外还发送 `X-Chat-Key`。本项目通过 `.env` 中的 `FANZHA_CHAT_KEY` 配置该值，仅用于聊天请求。留空时保持旧版行为；它不替代 Access Token、Refresh Token 或代理密钥。

可从本人登录后的官方网页实际聊天请求取得该头；配套诊断工具若观察到该值，会单独保存本地 `fanzha-chat-key-时间戳.env`。将其中 `FANZHA_CHAT_KEY` 一行加入项目 `.env` 后重启服务。此配置文件含私有值，不要上传、提交或混入诊断 ZIP。

首次采集仅记录到登录、用户信息和创建会话成功；后续报告已捕获带此头的普通聊天请求，HTTP 200 并返回完整答案流。两份报告均未出现 403，因此缺少该头是已确认的协议差异，尚不能认定为全部 403 的原因。不要对 403 反复刷新令牌；还需对照同一网络下官方网页与反代的实际聊天结果。

---

## 高级配置与兼容性说明

通常只需填写基本配置。以下可选变量可在 `.env.example` 中查看：

| 变量 | 默认值 | 作用 |
| :--- | :--- | :--- |
| `FANZHA_CHAT_KEY` | 空 | 官方网页实际聊天请求中的 `X-Chat-Key`，只发给聊天接口，不内置第三方密钥。 |
| `CORS_ORIGINS` | 空 | 额外可信的浏览器源，逗号分隔；默认允许本机浏览器源。 |
| `TOKEN_STATE_FILE` | 空 | 刷新凭证持久化文件，建议 `.fanzha-token-state.json`。 |
| `MAX_REQUEST_BODY_BYTES` | `33554432`（32 MiB） | 整个请求体上限，包含 multipart 开销与分块上传。 |
| `MAX_AUDIO_UPLOAD_BYTES` | `26214400`（25 MiB） | 单个 ASR 上传文件上限。 |
| `MAX_UPSTREAM_RESPONSE_BYTES` | `8388608`（8 MiB） | 上游单个 JSON、完整聊天流或合成音频总量上限。 |
| `MAX_CHAT_CHARS` | `128000` | 完整聊天历史及附件 URL 的字符上限。 |
| `MAX_TTS_CHARS` | `20000` | 语音合成输入字符上限。 |
| `UPSTREAM_REQUIRE_DONE` | `false` | 是否要求上游显式发送流结束标记。 |

这些是本服务的资源限制，不代表上游一定接受同样大小的请求。

### 令牌续期与重启

服务器管理的凭证收到 HTTP 或业务 JSON 中的 401 时，会刷新一次并重试一次；并发请求共用刷新结果，同一套失败凭证会短暂冷却 5 秒。不会在已经输出聊天内容后重试整次请求。

默认刷新结果仅保存在内存中；需要重启后继续使用轮换后的凭证时，可设置 `TOKEN_STATE_FILE=.fanzha-token-state.json`。文件已被 Git 忽略，采用原子替换写入，在支持 POSIX 权限的平台限制为 `0600`。Windows 上应确保存放目录仅供授权账户访问。

状态文件绑定初始服务器凭证和上游地址；修改账户配置后不会误载入旧账户状态。不要在多个进程间共享此文件，也不要把路径指向 `.env` 或源码文件。

### 临时使用其他上游账户

- 未启用 `PROXY_API_KEY` 的本机模式：`Authorization` 为空或以 `sk-` 开头时使用服务器凭证；其他 Bearer Token 作为调用方的上游凭证。
- 启用 `PROXY_API_KEY` 后：`Authorization` 仅用于代理鉴权，另一个账户的访问令牌通过 `X-Upstream-Token` 请求头传入。
- 外部上游令牌不会使用服务器的 Refresh Token 自动续期，过期后由调用方更新。

### 兼容路径与流结束判断

除上文接口外，还保留 `/chat/completions`、`/models` 和 `/v1/dashboard/billing/usage` 兼容路径。最后一个返回上游配额信息，并非 OpenAI 美元计费统计。

默认接受上游在输出有效文本后正常关闭流，以兼容旧接口；这种情况下无法仅凭正常 EOF 判断内容是否被静默截断。只有确认上游会始终发送完成标记后，才应启用 `UPSTREAM_REQUIRE_DONE=true`。

已支持实测官方答案事件中的 `exec_elapsed_secs` 完成标志，包含合法的零耗时值；保留 `[DONE]` 等既有结束方式。节点状态和消息保存事件只作元数据处理，不拼入答案，也不会用其中的耗时字段提前结束聊天。

---

## 开发与测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

测试使用本地 HTTPX / ASGI 模拟，不需要真实令牌，也不消耗上游额度。覆盖会话与账户隔离、并发刷新、SSE、错误处理、输入边界、音频完整性及仪表盘等。GitHub Actions 配置了 Python 3.9、3.12、3.14 测试。

详细变更见 [维护说明](docs/maintenance.md)。模拟测试不能替代真实上游验收，当前私有接口、实际音色、图片理解和业务限制仍以上游实际响应为准。

---

## 免责声明

1. 本项目仅供网络协议逆向研究、API 代理机制学习及个人学术验证使用，严禁用于任何商业牟利、恶意滥用、批量刷量或非法用途。
2. 开发者及使用者应严格遵守《中华人民共和国网络安全法》、《中华人民共和国反电信网络诈骗法》及相关法律法规，不得利用本项目干扰政务公开系统的正常运行。
3. 本项目为独立开源研究，与公安部、国家反诈中心或上海市公安局无任何官方隶属或商业合作关系。

项目采用 GPL-3.0 许可证，详见 [LICENSE](LICENSE)。
