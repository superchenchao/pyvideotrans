# 部署与容量

## 推荐最小拓扑

- 控制平面：2 个 CPU 副本；
- Media Worker：至少 2 个预热编码槽；
- ASR Worker：主、备各至少 1 个预热槽；
- CineFusion Worker：至少 1 张 24GB GPU，按实测并发设置；
- 对象存储：原片、中间音频、TTS 片段和成片；
- Redis Streams/PostgreSQL：多副本生产环境替换当前进程内事件和任务存储。

## 容量原则

严格 SLA 不使用普通排队。`max_inflight_jobs` 必须小于或等于所有关键阶段可同时服务的最小槽位数。

例如：

```text
Media 2 槽、ASR 2 槽、Speaker GPU 1 槽、Azure 并发足够
=> CINEFLOW_MAX_INFLIGHT_JOBS=1
```

只有实测 GPU Worker 能稳定同时处理 2 条五分钟视频，才能把全局并发提高到 2。

## 性能矩阵

至少测试：

- 30 秒、2 分钟、5 分钟；
- 720p、1080p；
- 2 人、4 人、8 人；
- 正脸对白、反打镜头、画外音、重叠说话；
- 中译英、中译日、中译西；
- 软字幕和硬字幕；
- 保留背景音与不保留背景音。

记录每个阶段耗时、总耗时、Azure 字符数、GPU 活跃秒数、降级状态和说话人准确率。
