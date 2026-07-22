# OSS 上传 Worker

`cineflow.upload_worker` 与 `cineflow.upload_client` 共同提供新项目自己的原视频上传能力：

- 服务端签发最小权限、短时效 STS 凭据；
- 客户端直接上传到 OSS，不经过控制平面转发大文件；
- 分片并发上传；
- 本地检查点和断点续传；
- STS 过期后刷新凭据并继续同一个对象；
- 服务端保存 multipart upload ID；
- 上传完成后检查对象大小、Content-Type 和 SHA-256 元数据；
- 支持中止未完成分片、删除残留对象和定期清理旧会话。

上传 Worker 不需要把原视频落到自己的磁盘，因此普通电脑的上行带宽仍是上传速度的主要限制，但本地 CPU、GPU 和内存不会成为处理瓶颈。

## 安全模型

桌面端永远不保存阿里云永久 AccessKey。流程为：

```text
桌面端
  ↓ 创建上传会话
Upload Worker
  ↓ AssumeRole，策略只允许一个 object_key
短期 STS 凭据
  ↓
桌面端使用 oss2 直接分片上传到 OSS
  ↓
Upload Worker HEAD 校验对象并返回签名下载 URL
```

STS 策略限定到单个对象，并只授予上传、列举分片、中止分片以及必要的对象读取权限。服务端的 RAM 角色应再通过 Bucket Policy 和 RAM Policy 限制到 `CINEFLOW_UPLOAD_OSS_SOURCE_PREFIX`。

## 服务端接口

### 创建会话

```http
POST /v1/uploads/sessions
```

```json
{
  "filename": "episode-01.mp4",
  "size_bytes": 188743680,
  "content_type": "video/mp4",
  "sha256": "64位十六进制摘要",
  "project_id": "series-a"
}
```

返回：

- 固定 `session_id`；
- 固定 `object_key`；
- OSS endpoint 和 bucket；
- 短期 STS 凭据及过期时间；
- 内部规范 URL。

服务端持久化时不会保存临时 AccessKey Secret 或 SecurityToken。

### 刷新 STS

```http
POST /v1/uploads/{session_id}/credentials
```

对象 key 不变，因此凭据过期后可以继续同一个 multipart upload。

### 登记 multipart upload ID

```http
POST /v1/uploads/{session_id}/multipart
```

```json
{"upload_id":"..."}
```

服务端保存 upload ID，用于中止遗留分片和故障清理。

### 完成并验证

```http
POST /v1/uploads/{session_id}/complete
```

```json
{
  "size_bytes": 188743680,
  "sha256": "64位十六进制摘要"
}
```

Worker 使用 OSS HEAD 验证：

- 实际字节数；
- `x-oss-meta-sha256`；
- Content-Type；
- ETag；
- 对象是否可生成签名 URL。

SHA-256 由客户端对本地完整文件计算，并随 multipart 对象元数据提交。服务端无法仅靠 ETag 判断分片对象的完整文件哈希，因此缺少或不匹配 SHA-256 元数据时会拒绝完成。

### 中止与清理

```http
DELETE /v1/uploads/{session_id}?delete_object=true
POST /v1/uploads/cleanup?older_than_seconds=86400&delete_completed=false
```

默认清理未完成、失败和已中止的旧会话，不删除已完成原片。只有显式设置 `delete_completed=true` 才会删除已完成对象。

## 桌面端命令

安装阿里云依赖：

```bash
python -m pip install -e '.[aliyun]'
```

首次上传：

```bash
cineflow-upload "E:/video/episode-01.mp4" \
  --worker http://127.0.0.1:8094 \
  --project-id series-a \
  --part-size-mib 8 \
  --threads 4
```

检查点默认写到当前目录的 `.cineflow-upload/`。网络中断或程序关闭后，使用同一命令再次执行即可恢复已完成的分片。

中止并清理：

```bash
cineflow-upload "E:/video/episode-01.mp4" \
  --worker http://127.0.0.1:8094 \
  --abort
```

## 服务端启动

```bash
python -m pip install -e '.[aliyun]'
uvicorn cineflow.upload_worker:app --host 0.0.0.0 --port 8094
```

Docker Compose：

```bash
docker compose --profile ingest up -d upload-aliyun
```

## 关键配置

```text
CINEFLOW_UPLOAD_ALIYUN_REGION=cn-beijing
CINEFLOW_UPLOAD_ALIYUN_ROLE_ARN=acs:ram::ACCOUNT_ID:role/CineFlowUploader
CINEFLOW_UPLOAD_ALIYUN_ACCESS_KEY_ID=...
CINEFLOW_UPLOAD_ALIYUN_ACCESS_KEY_SECRET=...

CINEFLOW_UPLOAD_OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
CINEFLOW_UPLOAD_OSS_BUCKET=your-private-bucket
CINEFLOW_UPLOAD_OSS_SOURCE_PREFIX=cineflow/sources
CINEFLOW_UPLOAD_STATE_DIR=/var/lib/cineflow/uploads
CINEFLOW_UPLOAD_BEARER_TOKEN=...
```

生产环境优先使用实例 RAM 角色或密钥管理服务注入服务端凭据，不使用主账号 AccessKey。上传 Worker 应位于私网入口后，并由网关鉴权和限流。

## 当前边界

- 当前持久层是原子 JSON 文件，适合单副本或共享持久卷；多副本应替换为 Redis/PostgreSQL。
- 客户端检查点包含 upload ID 和分片 ETag，但不包含任何永久云密钥。
- 上传耗时不计入“5 分钟视频尽量 5 分钟内处理”的服务端处理目标。
- 真实 OSS 账号上线前必须测试跨网断开、STS 过期刷新、重复完成、中止分片和大文件校验。
