# CineFusion Worker 接口

## 主动说话人命令

环境变量 `CINEFLOW_SPEAKER_ACTIVE_SPEAKER_COMMAND` 是一个可信的服务端命令模板，支持：

- `{input}`：已下载的输入视频；
- `{output}`：命令必须写入的 JSON；
- `{workdir}`：本次任务临时目录。

示例：

```text
python /models/light_asd/run.py --input {input} --output {output}
```

输出可以是数组，也可以是 `{ "tracks": [...] }`：

```json
{
  "tracks": [
    {
      "start_ms": 1200,
      "end_ms": 2850,
      "face_id": "character_001",
      "score": 0.94,
      "av_sync_confidence": 0.91
    }
  ]
}
```

`face_id` 必须是跨镜头身份聚类后的稳定 ID，不能是单帧检测框编号。推荐在主动说话人程序内部组合：

```text
SCRFD/RetinaFace → ArcFace → ByteTrack/轨迹合并 → Light-ASD
```

## 预热要求

严格 SLA 下 `/healthz` 必须同时返回：

```json
{"ok": true, "warm": true}
```

Worker 在启动阶段下载/加载模型；任务阶段禁止模型下载。没有主动说话人命令时，生产 Worker 保持 `warm=false`，控制平面不会接受多角色严格 SLA 任务。
