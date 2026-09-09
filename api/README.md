
## 对外接口清单（HTTP 端点，routers.py）

| 端点 | 作用 |
|------|------|
| GET / | 出片台页面 |
| GET /api/health | 三服务健康 + uncensored_ready |
| GET/POST /api/creative-profiles | 制片资料读写 |
| GET /api/tasks | 全部任务列表 |
| POST /api/generate | 单段生成（i2v/t2v，支持 nsfw/style） |
| POST /api/storyboard | 故事拆分镜（nsfw 走本地） |
| POST /api/story-long | 多镜连续成片 |
| POST /api/concat | 拼接已有片段 |
| GET /api/status/{id} | 任务状态（磁盘回退） |
| POST /api/cancel/{id} | 取消任务 |
| GET /api/video/{id} · /api/segment-video/{id}/{i} · /api/shot-preview/{id}/{i} | 成片/单镜/预览媒体 |
| POST /api/oneclick | 一键成片入口（idea 或 juben 剧本） |
| GET /api/juben | 剧本文件列表 |
| POST /api/review/storyboard | 分镜审查（意见改写→qwen3.8 重写→放行） |
| POST /api/review/resample · fixcheck · approve · recard | 重抽/检查修复/通过/重跑抽卡 |
| POST /api/review/shot_next · shot_resample · shot_finish | 逐镜审查三动作 |
