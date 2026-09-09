
## 对外接口清单（本层提供给他层调用的函数）

| 名称 | 所在文件 | 作用 |
|------|---------|------|
| BASE/OUTPUT/UPLOADS/WORKFLOWS/JUBEN/WEB/CREATIVE_PROFILES/FFMPEG | paths.py | 路径常量 |
| video_duration(path) | ffmpeg_tools.py | 读视频时长（秒） |
| hard_concat(paths, out) | ffmpeg_tools.py | 硬切拼接（兜底） |
| concat_videos(paths, out, transition) | ffmpeg_tools.py | 叠化拼接成片 |
| extract_last_frame(video, png) | ffmpeg_tools.py | 抽尾帧 |
| comfy.py 引擎客户端 / ai.py 本地模型 / storyboard.py 拆镜 | 同名文件 | 外部服务适配器（人人可用） |
