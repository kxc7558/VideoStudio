# 出片台（VideoStudio）项目记忆

面向「小白也能一键出视频」的本地客户端，封装 ComfyUI 的图生视频 / 文生视频能力。
**用户是非技术 PM**：质量优先、不怕慢、可挂后台。交流时避免技术黑话，说人话。

## 运行方式

- 双击 `run.bat`：自动拉起 ComfyUI（`D:\ComfyUI_Wan`，端口 8188）+ 出片台后端（uvicorn，端口 8000）+ 打开浏览器。
- `run.bat` 是 **GBK + CRLF 编码，且不要加 `chcp 65001`**（chcp 会把中文弄乱码）。改完务必保持编码，否则乱码。
- **run.bat 用 `curl` 轮询端口直到就绪才开浏览器**（不能用固定 `timeout`，否则电脑重启后冷启动慢、会「127.0.0.1 拒绝连接」）。引擎 8188 循环等，后端 8000 等 60 秒超时后报错。
- 前端在 `web/`（纯 HTML+JS，无构建），后端 `app.py`。

## 架构（文件职责）

| 文件 | 作用 |
| --- | --- |
| `app.py` | FastAPI 后端：任务表存内存，封装生成/故事/拼接/取消等接口 |
| `comfy.py` | ComfyUI API 封装：提交工作流、轮询完成、WebSocket 进度、取消 |
| `storyboard.py` | 调 DeepSeek 把故事拆成分镜（API key 在 `local_config.py`，已 gitignore） |
| `web/` | 三个 tab：图生视频 / 文生视频 / 故事模式 |
| `workflows/` | ComfyUI 节点图 json（wan 的 `i2v_api.json`/`t2v_api.json` + h3 的 `h3_*_api.json`） |
| `run.bat` | 一键启动脚本 |

## 模型（两个，可切换）

- **Wan 2.2**（默认）：双专家 DiT，各约 9.5GB。i2v 用 LightX2V **固定 4 步**（蒸馏锁死，改步数画面会坏）；t2v 双专家对半切步数（8/16/20/30）。
- **MiniMax H3**：单模型（FL2VA），同时支持 t2v 和 i2v（首帧）。帧网格 17k+5 @ 24fps，时长帧数 56/73/124 ≈ 2.3/3.0/5.2 秒（**不是** Wan 的 4n+1 规则）。
- 模型文件在 `E:\ComfyUI_models\`，通过 ComfyUI 的 `extra_model_paths.yaml` 挂载。H3 用 GGUF 量化版 DiT + safetensors 文本编码器（`minimax_h3`）+ video VAE。
- 详见 memory：`minimax-h3-deployment`、`wan22-i2v-comfyui-deployment`。

## 长视频（首尾帧）

- 自动接续：`/api/generate` 加 `segments`(1~6)，`_run_long_task` 逐段生成，段间「抽尾帧→下一段首帧」接续，最后 `concat_videos` 拼一条。尾帧抽取 `extract_last_frame`（ffmpeg `-sseof -0.2 -frames:v 1`）。
- 故事模式「连续成片」：`/api/story-long` 收多行 `prompts`，第一段 t2v、后续段 i2v 用上一段尾帧接续。
- 指定头尾帧（**仅 H3**）：`MiniMaxH3ImageToVideo` 节点有可选 `first_frame`/`last_frame`（都是 IMAGE）。前端 i2v 加「尾帧」上传框（仅 H3 显示），`_build_h3_workflow` 动态加 `LoadImage`(node 13)+`last_frame`。**Wan 不支持尾帧**。

## 关键踩坑（非显而易见，改代码前必读）

- **SaveVideo 输出在 history 的 `images` 键**（不是 `videos`），带 `animated:[true]`；找视频要同时查 `videos` 和 `images` 两键并按扩展名过滤（`comfy.py:find_video`）。
- **GGUF 模型列表**用 `/object_info/UnetLoaderGGUF` 的 `input.required.unet_name[0]`（`/models/unet` 是 404）。H3 同理用 `/object_info/H3ModelLoaderAny`。
- **storyboard 的 SYSTEM 提示词含 JSON 花括号**，`.format()` 会当占位符抛 KeyError → 用 `%` 格式化。
- **`wait_done` timeout 默认 1800s 不够**（T2V 实测 33min），已调 3600s。
- **`/api/video/{id}` 和 `/api/tasks` 要回退磁盘** `output/*.mp4`（重启后内存 tasks 清空，但文件还在）。
- **websockets 17 新 API**：`from websockets.asyncio.client import connect`（旧 `websockets.connect` 已废弃）。
- **采样进度只能靠 WebSocket**，`/history` 不含 step 进度；搬运模型进显存那几分钟进度停在 0%（前端显示「加载模型…」）。
- **API key 在 `local_config.py`**（已 gitignore），不在 `storyboard.py` 里。换 key 只改 `local_config.py`；模板见 `local_config.example.py`。
- **不要给本地密钥文件起名叫 `secrets.py`**：会遮蔽 Python 标准库的 `secrets` 模块，导致 FastAPI 启动时 `ImportError: cannot import name 'token_hex'`（后端起不来、页面一直「拒绝连接」）。
- 取消任务：`comfy.cancel(prompt_id)` — 排队中 `POST /queue {"delete":[id]}`，运行中 `POST /interrupt`。

## 依赖

见 `requirements.txt`（已按本机 venv 实测版本锁定）。装依赖：`venv/Scripts/python.exe -m pip install -r requirements.txt`。

## 下一步方向

一致性：人物「自动画角色」（需文生图模型，本机 checkpoints/loras 目前空）、场景参考图、配音（后期）。
