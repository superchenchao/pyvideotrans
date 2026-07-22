# Security

## 凭据

- 桌面端不得保存阿里云永久 AccessKey、DeepSeek Key、Azure Key、Caca Key 或内部 Worker 密钥。
- 原视频上传只使用 Upload Worker 签发的短时效、单对象 STS 凭据。
- 服务端优先使用实例 RAM 角色、工作负载身份或云密钥管理服务；不要使用阿里云主账号 AccessKey。
- 临时 STS Secret 与 SecurityToken 只在创建或刷新会话时返回，不写入持久化 session。
- 任何出现在终端记录、Issue、测试夹具或提交中的真实密钥都应立即轮换。

## OSS

- source、subtitle-removal、generated 使用独立前缀和最小 RAM 权限。
- STS Policy 只能访问一个具体 `object_key`，不得授予整个 Bucket 管理权限。
- 上传完成前必须检查对象大小和 `x-oss-meta-sha256`；multipart ETag 不能替代完整文件 SHA-256。
- 签名 URL 的有效期应覆盖排队与处理时间，但不应无限期有效。
- 日志不得输出签名 URL 查询参数。
- 清理操作必须依据持久化任务状态；OSS 生命周期规则仅作为兜底。
- 删除原片、字幕消除结果或成片必须是显式策略，不得因普通重试误删已成功对象。

## API 与网络

- Upload、Subtitle、Media、ASR、Speaker Worker 应部署在私网或受认证网关后。
- 控制平面目前不反向代理所有 Worker；公开部署应由 API Gateway 将不同路径路由到对应服务，并统一实施用户认证和审计。
- 样例 bearer token 只是接口；生产优先使用 mTLS、工作负载身份或服务网格认证。
- 外部桌面入口需要用户认证、速率限制、文件大小限制、并发限制和审计日志。
- CORS、反向代理 body 限制和超时必须按服务分别配置；原片不经过控制平面上传。

## 字幕消除

- 本地字幕消除命令模板只能来自可信服务端配置，不能由 API 请求传入，防止命令注入。
- 本地模型容器应以非 root 用户运行，只读挂载模型，并为临时目录设置磁盘配额。
- Caca 返回 URL 是不可信外部输入；必须限制下载大小、跟随重定向策略、复制到受控 OSS 并重新 `ffprobe` 验证。
- 阿里 VideoDetext、Caca 和本地输出在发布签名 URL 前均需验证时长、视频流、宽高、FPS 和对象大小。
- 无效输出应隔离或删除，不得直接进入后续翻译流水线。

## 文件与媒体处理

- 文件名、session ID、job ID 和对象 key 必须经过规范化，拒绝路径穿越。
- 下载和 Base64 写入接口必须设置字节上限。
- FFmpeg/ffprobe 处理不可信媒体时应运行在隔离容器，限制 CPU、内存、临时磁盘、进程数和网络权限。
- 不在日志中记录音频 Base64、完整字幕敏感内容或人脸 embedding。

## Provider 数据

- DeepSeek 请求应只携带完成任务所需的字幕、术语和有限上下文。
- Azure TTS 请求只携带目标语言、音色和该句文本。
- 角色声纹、人脸 embedding 和角色库属于敏感生物特征数据，应加密存储、限制访问并提供删除策略。
- Provider 返回内容必须按 Pydantic/JSON 契约验证，不能直接拼入命令、SQL 或文件路径。

## 状态和恢复

- FileStateStore 适合单副本与持久卷；多副本必须使用共享数据库和分布式锁，避免重复提交付费任务。
- 外部 JobId、provider、对象 key、请求哈希和幂等键应持久化。
- 恢复逻辑不得重复创建已经存在的云任务；无法确定状态时应先查询 provider，而不是直接重提。
- 审计日志应记录创建、刷新 STS、中止 multipart、删除对象、提交/取消字幕任务和发布结果等高风险动作，但不记录密钥。

## 依赖与供应链

- 锁定生产依赖版本并定期扫描漏洞。
- Light-ASD/LR-ASD、pyannote、本地字幕模型和字体必须确认许可证后再打包或挂载。
- 容器镜像使用最小基础镜像、非 root 用户、只读根文件系统和固定 digest。
- GitHub Actions、容器仓库和部署账户使用最小权限。
