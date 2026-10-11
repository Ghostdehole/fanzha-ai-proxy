# 4.3 维护记录

本次仅维护 Ghostdehole/fanzha-ai-proxy，以 71aee9c 为基线。全部回归测试使用模拟上游，无真实账号/配额调用。

| 原问题 | 修复 | 回归覆盖 |
| --- | --- | --- |
| 首句哈希导致会话/账号混用 | 每个请求独立会话，携带完整有序历史 | test_chat、test_client |
| 编辑分支、导入历史和重启后上下文不一致 | 客户端历史作为唯一来源；角色文本编码 | test_chat |
| 创建会话失败后伪造 UUID | 严格检查成功数据和会话 ID | test_client |
| 全局缓存锁阻塞其他会话、缓存无清理 | 移除隐式会话缓存和全局创建锁 | test_client |
| 续期覆盖不全、聊天使用旧令牌 | 统一请求认证处理，动态选择服务器最新令牌 | test_client、test_chat |
| 并发重复刷新、失败后刷新风暴 | 锁内双检、一次重试、5 秒失败冷却 | test_client |
| 外部账号误用服务器 Refresh Token | 显式区分服务器令牌与外部令牌 | test_client、test_security |
| 刷新后重启丢失轮换凭证 | 可选、绑定配置的原子状态文件 | test_client |
| 无独立代理鉴权、任意跨域 | 本机兼容；远程要求代理密钥、显式 Origin | test_security |
| 500/401 被包装成 200 或空回复 | 统一安全错误和明确状态；流内失败无正常 stop | test_client、test_chat |
| SSE 简单按行丢失多行帧/元数据误当文本 | 有界增量解析、UTF-8/CRLF/BOM/多行/心跳支持 | test_chat |
| 断开/取消后可能泄漏上游连接 | 预读流与响应关闭均覆盖取消路径 | test_client、test_chat |
| 类型、未知能力、模型名未验证 | 明确校验；精确模型映射；不支持参数返回 400 | test_chat、test_audio |
| 温度和 token 上限被忽略、用量伪造 | 透传支持参数；不提供虚假的 token 数量 | test_chat |
| 上传和响应无界 | 整请求/单音频/文本/上游响应限额 | test_security、test_audio、test_client、test_chat |
| 语音长句不切分、丢失失败片段 | 150 字符硬分片，所有片段严格成功 | test_audio、test_client |
| 损坏、异参数 WAV 静默拼接 | 检查 RIFF、PCM 格式与帧长度，整次失败 | test_audio |
| 格式/语速参数静默忽略 | 仅支持能力明确列出，其余返回 400 | test_audio |
| 看板超时误报正常、HTML 注入 | 状态未知、错误状态、转义、nonce CSP | test_dashboard |
| Token/提示词/异常正文泄露 | 去掉凭证预览和提示词日志，统一脱敏错误 | test_security、test_client、test_audio |
| NaN/Infinity/非法 Unicode 等边界 | 严格上游 JSON及安全错误编码 | test_client、test_chat、test_dashboard、test_security |
| 测试被 gitignore 排除、无 CI | 保留 tests/，新增测试依赖及 CI 矩阵 | 全套测试 |

## 兼容性与真实上游验证边界

- 路径、四种问答模式、gpt-4o-mini 别名、六种语音、流式/非流式、ASR 和看板保持。
- 占位 Key 只在本机未配置代理密钥时兼容；远程调用需配置 PROXY_API_KEY。
- 多轮历史改为文本角色模拟，不能声称等同原生 messages 协议。真实上游对完整历史和附件关联的接受情况仍需有权账号实测。
- 此前被静默忽略的参数现在可能返回 400；检查客户端默认输出格式、语速、工具调用和高级参数。
- TTS 保持 WAV，不新增转码；标准音色名称映射 mandarin，不代表真实相应音色。
- 上游没有可靠 token 用量，因此不再返回字符数冒充 token。
- 正常 EOF 的静默截断无法无条件识别。UPSTREAM_REQUIRE_DONE 严格模式仅在已确认上游终止协议时启用。
- 不自动重放已产生文本的聊天，也不针对普通网络失败/限流无限重试。
- 仅支持单进程管理同一套轮换凭证；未实现分布式刷新锁或多副本协调。
- 上游接口是否仍可用、实际模型质量、额度规则和音频/图片解码能力不由模拟测试证明。
