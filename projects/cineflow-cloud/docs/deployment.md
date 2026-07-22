# 部署与容量

## 独立部署边界

本目录以后可直接作为新仓库根目录。生产服务只接收现有客户端生成的 OSS URL，不部署或导入 pyVideoTrans 的上传、字幕消除、GUI 和缓存代码。

## 推荐最小拓扑

- 控制平面：2 个 CPU 副本；
- Media Worker：首选阿里云 API 适配器，后备火山引擎适配器；
- ASR Worker：首选阿里云，后备火山引擎；
- CineFusion Worker：优先评估云厂商能力，不满足时部署 24GB GPU Worker；
- DeepSeek：默认字幕翻译和低置信度文本证据；
- Azure Speech：多角色、多语言 TTS；
- 对象存储：调用方已有 OSS 以及本服务生成的中间结果/成片；
- Redis Streams/PostgreSQL：替换当前进程内事件、队列和任务存储。

## 容量原则

`max_inflight_jobs` 是同时执行数，不是接单上限。超过该数的有效任务进入队列。

例如：

```text
Media 2 槽、ASR 2 槽、Speaker GPU 1 槽、Azure 并发足够
=> CINEFLOW_MAX_INFLIGHT_JOBS=1
=> 第 2 条任务显示 queued，第一条释放槽位后继续执行
```

提高全局并发前，必须确认最慢阶段能承受同等并发，否则只会增加 p95 和 API 限流。

## 300 秒目标

预热和容量规划用于提高 300 秒内成功率，但：

- 不因无空闲槽拒绝；
- 不因预测超过目标拒绝；
- 不在 300 秒时取消；
- 超时后继续执行并记录 `target_exceeded=true`。

## Provider 部署顺序

首选地址和后备地址对应：

```text
CINEFLOW_MEDIA_WORKER_URL                 # 阿里云优先
CINEFLOW_SECONDARY_MEDIA_WORKER_URL       # 火山引擎后备
CINEFLOW_ASR_WORKER_URL                   # 阿里云优先
CINEFLOW_SECONDARY_ASR_WORKER_URL         # 火山引擎后备
CINEFLOW_SPEAKER_WORKER_URL               # 阿里云能力或自建主 Worker
CINEFLOW_SECONDARY_SPEAKER_WORKER_URL     # 火山能力或其他后备
```

DeepSeek 与 Azure TTS 不参与上述替换顺序，按产品决策保留。

## 性能矩阵

至少测试：

- 30 秒、2 分钟、5 分钟；
- 720p、1080p、4K 输入；
- 2 人、4 人、8 人；
- 正脸对白、反打镜头、画外音、重叠说话；
- 中译英、中译日、中译西；
- 软字幕和硬字幕；
- 保留背景音与不保留背景音；
- Worker 预热、冷启动和首选端点故障切换。

记录 p50/p90/p95、队列等待、各阶段耗时、Azure 字符数、GPU 活跃秒数、`target_exceeded`、降级状态和说话人准确率。
