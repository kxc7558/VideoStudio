# 出片台（VideoStudio）项目记忆

面向「小白也能一键出视频」的本地客户端，封装 ComfyUI 的图生视频 / 文生视频能力。
**用户是非技术 PM**：质量优先、不怕慢、可挂后台。交流时避免技术黑话，说人话。

## AI 原生标准件（2026-09-10 装）

- **上岗证 `AGENTS.md`**：任何 AI 从这里开始（读宪法 → 读层守则 → 占班 → 干活 → 释放排班表）
- **机器档案 `project.yaml`**：怎么跑/怎么测/路径/端口的机器可读速查表
- **排班表 `data/ai-shift.json`**：动手改代码前先占班，同一时刻只一个 AI 在岗；2 小时无更新视为离岗可接管；干完释放
- **意见箱**：界面右下角 💬 按钮吐槽 → `data/feedback/*.json`；`/api/feedback`（POST 用户吐槽 / GET AI 读箱）；用户说「**处理意见箱**」→ 按 `处理意见箱.md` 执行，处理完在 `data/feedback/changelog.md` 记成长日志
- 意见箱测试：`venv/Scripts/python.exe test_feedback.py --api`（4 项全过为绿）

## 运行方式

- 双击 `run.bat`：自动拉起 ComfyUI（`D:\ComfyUI_Wan`，端口 8188）+ 出片台后端（uvicorn，端口 8000）+ 打开浏览器。
- `run.bat` 是 **GBK + CRLF 编码，且不要加 `chcp 65001`**（chcp 会把中文弄乱码）。改完务必保持编码，否则乱码。
- **run.bat 用 `curl` 轮询端口直到就绪才开浏览器**（不能用固定 `timeout`，否则电脑重启后冷启动慢、会「127.0.0.1 拒绝连接」）。引擎 8188 循环等，后端 8000 等 60 秒超时后报错。
- 前端在 `web/`（纯 HTML+JS，无构建），后端 `app.py`。
- **只跑离线批量脚本时不用开 `run.bat`**，手动起引擎更可控：`D:\ComfyUI_Wan\run_comfyui.bat`（8188），再跑脚本。
- **`--reserve-vram` 取值两个场景不同**：交互式出片用 `run_comfyui.bat` 里的 `1.0`（给系统留 1GB，浏览器/预览不卡）；**离线批量长任务建议改 `0.3`**——只给系统留 0.3GB，多出的 0.7GB 给模型，实测能扛住 124 帧不 OOM。

## 架构（文件职责）

| 文件 | 作用 |
| --- | --- |
| `app.py` | FastAPI 后端：任务表存内存，封装生成/故事/拼接/取消等接口 |
| `comfy.py` | ComfyUI API 封装：提交工作流、轮询完成、WebSocket 进度、取消 |
| `storyboard.py` | 调 DeepSeek 把故事拆成分镜（API key 在 `local_config.py`，已 gitignore） |
| `ai.py` | AI 辅助：本地 Ollama(`qwen2.5vl:7b`) 看图 + DeepSeek 写叙事提示词 |
| `web/` | 三个 tab：图生视频 / 文生视频 / 故事模式 |
| `workflows/` | ComfyUI 节点图 json（wan 的 `i2v_api.json`/`t2v_api.json` + h3 的 `h3_*_api.json`） |
| `run.bat` | 一键启动脚本 |
| `_run_batch_seg2to10.py` | **离线批量产线**：故事分段逐段高清重生成，断点续跑 + 可被桌宠控制 |
| `_pet.py` | **桌宠**：Hello Kitty 悬浮窗，双击暂停/继续、右键菜单，贴边自动隐藏 |
| `_monitor_progress.py` | 命令行进度监控：`python _monitor_progress.py [间隔秒]` |
| `_finalize_1080p.py` | 多段交叉溶解拼接 + 升 1080p + 锐化（一步到位只编码一次） |
| `_upscale_realesrgan.py` | Real-ESRGAN 本地超分：拆帧 → 逐帧放大 → 重组 |
| `archive/` | 已完成任务的留档（4 个一次性脚本 + 历史日志 + 测试帧），见 `archive/README.md` |

> 根目录 `_` 开头的是**手搓的离线产线脚本**，不经过 `app.py`，直接调 `comfy.py` 往 ComfyUI 提交。
> Web 端（app.py）适合交互式出片；批量长任务是离线脚本更稳（不怕浏览器关掉）。

## 模型（两个，可切换）

- **Wan 2.2**（默认）：双专家 DiT，各约 9.5GB。i2v 用 LightX2V **固定 4 步**（蒸馏锁死，改步数画面会坏）；t2v 双专家对半切步数（8/16/20/30）。
- **无审查 LoRA（仅 nsfw 链路挂）**：`loras/Wan2.2_LightX2V_{high,low}_n54vv.safetensors`（各 1.24GB，来源 `rzgar/Wan2.2_LightX2V_4Step_Uncensored`，sha256 已验）。后端 `_apply_wan_nsfw_lora` 在 `use_lora=True` 时给高/低噪声专家各注入一个 `LoraLoaderModelOnly` 节点（id "200"/"201" 字符串），插在 `UnetLoaderGGUF → LoraLoaderModelOnly → ModelSamplingSD3 → KSamplerAdvanced` 之间，强度 1.0。**i2v 与 t2v 都支持**：t2v 挂 LoRA 时步数锁 4、cfg=1（蒸馏 LoRA 配方，原版 20 步会过采样糊掉）。⚠️ **i2v 和 t2v 的 ModelSamplingSD3 节点号不同**（i2v 是 8/9，t2v 是 7/8），注入时要分别传——t2v 硬套 i2v 节点号会 KeyError。这两个 LoRA 融合了 LightX2V 蒸馏，所以和 4 步采样配方协同。
- **MiniMax H3**：单模型（FL2VA），同时支持 t2v 和 i2v（首帧）。帧网格 17k+5 @ 24fps，时长帧数 56/73/124 ≈ 2.3/3.0/5.2 秒（**不是** Wan 的 4n+1 规则）。采样配方 **euler + beta + 20 步**（2026-09-11 A/B 实测胜出：人物一致性比 res_multistep+simple 稳，无配饰漂移；与 Multishot 官方产线配方、drbaph 社区参考一致）。
- **H3 无审查链路（2026-09-11 已通）**：LoRA `loras/NaughtyTimes_v3_rank64_unpruned.safetensors`（1.23GB，sha256 已验，SexGod1979/NaughtyTimes-MiniMax-H3，rank64）+ 未剪枝底模 `unet/minimax_h3_fl2va-Q4_K_M.gguf`（18.78GB，leejet 转档）。作者说明 LoRA 按**未剪枝**底模训练（含 adaln_proj 张量），挂剪枝底模效果大打折扣——所以 nsfw 时 `_build_h3_workflow(use_lora=True)` 会把 DiT 换成未剪枝版并注入 `LoraLoaderModelOnly`（节点 "300"，插在 H3ModelLoaderAny → BasicGuider 之间，强度 1.0）。⚠️ **leejet 这份 GGUF 原文件头部 KV 是空的**（没有 general.architecture），ComfyUI-GGUF 拒载；本机已修复（补了 `architecture=wan` 等 3 个 KV，脚本 `_downloads/_repair_gguf_layout.py` 留档）。无审查 H3 跳过官方全年龄提示词格式改写。
- 模型文件在 `E:\ComfyUI_models\`，通过 ComfyUI 的 `extra_model_paths.yaml` 挂载。H3 用 GGUF 量化版 DiT + safetensors 文本编码器（`minimax_h3`）+ video VAE。
- **角色卡文生图**：`checkpoints/NoobAI-XL-v1.0.safetensors`（SDXL 系动漫模型，D 盘 ComfyUI models），工作流 `workflows/anchor_t2i_api.json`（832×1216、28 步、euler_ancestral、cfg 5）。描述生成角色时补质量词 + 负向防真人词。
- 详见 memory：`minimax-h3-deployment`、`wan22-i2v-comfyui-deployment`。

## 角色三来源（2026-09-11 上线）

一键成片的角色不指定时自动抽卡；也可四选一（前端「👤 角色」下拉）：
1. **自动抽卡**（默认）：4 候选 t2v 迷你视频 → 视觉模型选最清晰 → 抽尾帧当锚
2. **角色库选**：`output/_character_cards/` 下的定妆图（描述生成/上传的都存这里），下拉选 + 预览
3. **按描述生成**：一句话 → NoobAI 文生图 4 候选 → 点选 → 自动存角色库（复用）
4. **上传图**：任意 png/jpg/webp → 存角色库

- 后端：`service/character.py`（文生图候选/存库）+ `_resolve_character_anchor`（三来源统一解析，外部图跳过抽卡直接当锚）；API 五端点 `/api/character/{generate,candidates,file,save,upload}`；`/api/oneclick` 收 `character_image`（文件名，预检存在性防路径穿越）。
- 锚统一写任务 meta：`character_card`（`ext:文件名` = 外部锚 / 段 id = 抽卡锚）+ `card_desc`（视觉模型描述，逐镜前置保一致）。
- 角色库登记在 `data/creative_profiles.json` 的 characters（白名单字段含 `image`）。

## 长视频（首尾帧）

- 自动接续：`/api/generate` 加 `segments`(1~6)，`_run_long_task` 逐段生成，段间「抽尾帧→下一段首帧」接续，最后 `concat_videos` 拼一条。尾帧抽取 `extract_last_frame`（ffmpeg `-sseof -0.2 -frames:v 1`）。
- 故事模式「连续成片」：`/api/story-long` 收多行 `prompts`，第一段 t2v、后续段 i2v 用上一段尾帧接续。
- 指定头尾帧（**仅 H3**）：`MiniMaxH3ImageToVideo` 节点有可选 `first_frame`/`last_frame`（都是 IMAGE）。前端 i2v 加「尾帧」上传框（仅 H3 显示），`_build_h3_workflow` 动态加 `LoadImage`(node 13)+`last_frame`。**Wan 不支持尾帧**。

## AI 辅助剧情接续（`ai.py`）

- 本地视觉：Ollama `qwen2.5vl:7b`（`/api/generate` 收 base64 图 + `images` 字段），`ai.describe_image()` 看图说一句话中文。
- 云端文本：DeepSeek 写叙事（key 同 `storyboard.py`，在 `local_config.py`）。
- 两个应用（都是「尽力而为」，失败回退原提示词，不拖垮主流程）：
  - **指定头尾帧**（`_run_task`）：先 `describe_image` 首尾两帧 → `transition_prompt` 写过渡提示词 → 再生成。
  - **故事模式连续成片**（`_run_long_task` 的 `bridge=True`）：每段完成后 `describe_image` 实际尾帧 → `bridge_next_prompt` 重写下一段提示词。
- 重写的提示词记在任务 `ai_prompts` 字段（落盘 + 任务卡片展示「原/新」对比）；段间尾帧 PNG 用后即删（`frame_png.unlink`）。
- **H3 官方提示词格式**（`ai._H3_RULES` + `ai.h3_prompt`）：H3 生成前把普通提示词按官方配方改写——`integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music` 三段，i2v/fl2v 带首尾帧对齐指令。**仅 H3 生效**，Wan 保持简短英文；UI 仍显示简短提示词，改写发生在后台生成时（尽力而为，失败回退原提示词）。
- **细节强化条款**（`ai._DETAIL_RULES`，⚠️ **未提交、未实测验证**）：治「画面糊、动态模糊、细节少」。H3 与 Wan 两条路都追加这段。原理是视频模型只会画你写出来的东西——不点名纹理和光影它就给一片平滑色块；动作写太猛，低步数采样跟不上就糊成一团。条款内容：
  - 必须点名至少 2 处可见材质纹理（织纹/毛发/木石/锈迹/纸纤维）
  - 必须写光源方向与光质（侧逆光/顶光/晨光，柔光或硬光）+ 高光与阴影落点
  - 加 1~2 个细微动作（衣角轻摆、火星飘升）让画面有活气
  - **动态要慢**：镜头一律 `with small amplitude at slow speed`，禁快摇/剧烈晃动
  - **禁词**：`motion blur`、`fast motion`、`rapid`、`frantic`、`chaotic`
  - 画质词写在风格词后面：`sharp focus, fine detail, crisp texture, shallow depth of field`

## 无审查出片区（`nsfw` 链路，2026-09-08）

面向「AI 成人短剧」需求：**模型**用 Wan2.2 + 无审查 LoRA，**文字 AI 全部本地化**（不碰云端 DeepSeek，避免外部过滤）。

- **前端**：第 4 个 tab「🔞 无审查」（`panel-nsfw`）。内含：首帧图上传（可选，有图 i2v / 无图 t2v，模型强制 Wan）、「🔞 AI 剧情接续也用本地模型」开关、无审查故事区（本地拆剧本 + 本地连续成片）。
- **后端参数**：`/api/generate` `/api/story-long` `/api/storyboard` 都收 `nsfw` 表单参数（"1" 开启）。`nsfw=1` 时：模型强制 `wan`（传了 h3 也回退）；`_run_task`/`_run_long_task` 收 `nsfw` → workflow `use_lora=True`（i2v 注入 LoRA）+ AI 辅助 `local=True`。
- **本地文本模型**：`ai.UNCENSORED_MODEL = qwen3.8-uncensored-fast:latest`（Ollama，27.3B Q4_K_M，14GB+，带 vision 能力）。`ai.uncensored_text()` 裸调 `/api/generate`（timeout 600s，num_predict 2048）。`ai._writer(local)` 是分派器：local 且模型在线 → 本地，否则 → DeepSeek。
- **拆剧本**：`storyboard.split_story(..., local=True)` 走 `_uncensored_json()`（本地 `/api/generate`）。⚠️ 本地模型会输出**大段元话语/思考再给 JSON**，`_extract_json` 已加固：先剥 `<think>` 段，再逐个 `{` 候选起点尝试 `json.loads`。实测拆 2 镜头约 3-4 分钟（27B 慢，可接受但别指望实时）。
- **剧情接续/过渡**：`ai.bridge_next_prompt` / `ai.transition_prompt` 带 `local=False` 参数，nsfw 时传 True。
- **速度注意**：27B 本地模型写提示词比 DeepSeek 慢一个量级；仅无审查链路使用，普通出片仍走 DeepSeek。

## 离线批量产线：小猫故事片高清重生成

故事 `1e9d3c87cc3b`（**共 10 段**，提示词重写记录存在 `output/1e9d3c87cc3b.json` 的 `ai_prompts` 字段）。

**三代成品**（都在 `output/`）：

| 版本 | 规格 | 文件 |
| --- | --- | --- |
| 初版 | 24 段交叉溶解，升 1080p | `1e9d3c87cc3b.mp4` → `_1080p.mp4`（130MB） |
| 4K 超分 | Real-ESRGAN 逐帧 4 倍，3328×1920 | `1e9d3c87cc3b_esrgan_4k.mp4`（200MB，1分43秒） |
| **高清重生成**（进行中） | 1024×576 / 81 帧 / 20 步 / 24fps | `highres_seg01~03.mp4`，拼完叫 `final_video_highres.mp4` |

第三代是**重新生成**而不是超分——原生分辨率出片，比后处理放大干净。

### 参数怎么定的

- 8/30 那批先试 `length=113`（1024×576），**显存吃不住**，`highres_manifest.json` 里还留着这个旧值。
- 9/3 降到 **81 帧**（脚本常量 `WIDTH, HEIGHT, LENGTH, STEPS = 1024, 576, 81, 20`），已跑通的 seg01~03 都是这个规格。
- 种子 `SEED_BASE = 999000000`，按段递增。**以脚本常量为准，manifest 里的 113 是历史残留。**

### 断点续跑机制（重要）

`_run_batch_seg2to10.py` 幂等：启动时扫描 `output/` 和 `ComfyUI_Wan/output/video/`，**已有 `highres_segNN.mp4` 的段直接跳过**，只补空缺段，绝不重复算。

`output/_control.txt` 是唯一控制通道，三个状态：

| 内容 | 行为 |
| --- | --- |
| 空 / `RUN` | 正常递交下一段 |
| `PAUSE` | 当前段算完就停手，原地等「继续」（保住已算完的段） |
| `STOP` | 立即退出；已提交的当前段也会发 `/interrupt` 中断 |

**续跑前必须检查 `_control.txt`**——它现在是 `STOP`，不改成 `RUN`（或清空）脚本一启动就退出。

### 桌宠（`_pet.py` + `启动桌宠.bat`）

**v5 · 纯形象 + 一键拉起产线**（2026-09-04）。v4 的问题是：窗口太大（花+文字占地方）、且「继续」只写 `_control.txt` 不拉进程——重启后引擎和批量脚本都死了，没人读文件，继续形同虚设。v5 针对性改：

- **纯形象窗口 150×150**：只留 3D 橘猫 + 右下角一个状态灯（绿=运行/黄=暂停/红=停止或引擎未起/灰=待命/金=完成）。进度与详情全进**悬停气泡**（鼠标停在猫上弹出，移开即隐）。
- **「继续」真拉产线**：双击或右键「恢复产线」→ 写 RUN → 引擎(8188)没起就 `Popen` 拉起 `ComfyUI_Wan/main.py`（`--lowvram --reserve-vram 0.3`，DETACHED）→ 轮询 `/system_stats` 等就绪（最多 6 分钟）→ 批量脚本没跑就拉起 `_run_batch_seg2to10.py`。**进程 DETACHED 独立运行，桌宠退出不影响产线**，所以重启后「双击桌宠 → 双击猫」就能全量恢复。
- **双击语义**：产线没跑 → 完整拉起；正在跑 → 切暂停；已暂停 → 继续。右键菜单：恢复产线 / 暂停 / 结束 / 退出桌宠（不影响产线）。
- **关键实现细节**（改代码必读）：
  - 进程存活检测 `_alive()` 用 `OpenProcess + GetExitCodeProcess == 259`（`os.kill(pid,0)` 对已退出的 Windows 进程会误判）。
  - 拉起 flags = `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`（0x8|0x200|0x8000000），否则引擎会弹黑框、或随桌宠一起死。
  - pid 落盘到 `output/_engine.pid` / `_batch.pid`，重启后旧 pid 读出来 `_alive` 返回 False → 判定「未在跑」，安全。
  - 引擎 stdout 重定向到 `output/_engine.log`（含 tqdm 的 s/it，桌宠从这里解析实时采样进度）。
  - 拉起顺序必须**先引擎后批量**：`_run_batch_seg2to10.py` 的 `main()` 开头 `if not comfy.is_ready(): return`，引擎没起它直接退。

**启动方式只有一种：用户双击 `启动桌宠.bat`**。AI/脚本会话里启动的进程活不过会话结束（工作台 job 清理），别浪费时间试。bat 的查杀用 CIM + `Stop-Process`（wmic 在 Win11 24H2+ 已移除），且查的是 `pythonw.exe` 不是 `python.exe`。改 bat 必须 GBK+CRLF。

## 关键踩坑（非显而易见，改代码前必读）

- **⚠️ 分层迁移回归（2026-09-11 修）**：迁移把单文件拆进 service/api 时，`_update`/`tasks`/`_lock`/`_cancelled`/`concat_videos`/`extract_last_frame`/`FFMPEG`/`_profiles` 等名字只 import 了模块没绑进文件命名空间——**所有真实生成路径一跑即 NameError**。已全部补显式 import（generation/oneclick/review/routers 四个文件）。教训：**拆文件后必须对每个新文件做「未定义名字静态扫描」+ 真实引擎端到端回归**，光 import 不报错不代表能跑。
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
- **8GB 显存（4070 Laptop）跑 H3 124 帧会偶发 CUDA OOM 直接崩掉 ComfyUI**，不是报错退出、是整个进程没了。对策：`--reserve-vram 0.3`（比默认 1.0 多留 0.7GB 给模型）+ 脚本自带看门狗检测进程存活自动重启。
- **超分/拆帧类脚本的中间产物极大**：`upscale_work/frames_in+frames_out` 一次就是 **7.9GB**（2482 帧 PNG）。跑完确认成品无误后立刻删，别留着。
- **`archive/` 里的脚本不能直接跑**：它们 `import comfy` / `import ai`，而 Python 的 `sys.path[0]` 是脚本所在目录，移到子目录后 import 会失败。要复跑先拷回根目录。
- **`ComfyUI_Wan/comfy/ldm/modules/attention.py` 有一处本地改动**（未提交）：`comfy_kitchen.int8_attention_is_available()` 改成 `getattr(comfy_kitchen, 'int8_attention_is_available', lambda: False)()`。是为了兼容没装 `comfy_kitchen` 的环境不崩，**别还原**，升级 ComfyUI 时要注意别被覆盖。
- **ComfyUI 出片在 `ComfyUI_Wan/output/video/`**，文件名会被自动加序号后缀（`highres_seg03_00001_.mp4`），批量脚本负责下载回 `VideoStudio/output/` 并去掉后缀。
- **HF 大文件下载代理不稳**：FlClash 偶发隧道断（`curl` 返回 000/18），尤其 1GB+ 文件。必须用**循环断点续传**脚本（`-C -` + `--retry` + sha256 校验），否则一次 curl 下不完。代理端口 `127.0.0.1:7890`，直连 HF 会被墙。

## 依赖

见 `requirements.txt`（已按本机 venv 实测版本锁定）。装依赖：`venv/Scripts/python.exe -m pip install -r requirements.txt`。

## 当前断点（2026-09-04 交接时）

1. **高清重生成停在第 3 段**——`highres_seg01/02/03` 已出（1024×576 / 81 帧），**还差 seg04~seg10 共 7 段**。`output/_control.txt` 现在是 `STOP`（9/3 17:14 手动停的，不是跑完）。续跑步骤：改 `_control.txt` 为 `RUN` → 起 ComfyUI（8188）→ `python -u _run_batch_seg2to10.py` → 可选开桌宠。
2. **`ai.py` + `app.py` 有未提交改动**（就是上面的 `_DETAIL_RULES`），改完没实测过画质是否真有提升。
3. **`ComfyUI_Wan` 未提交改动**：`attention.py`（兼容修复，别还原）+ `temp_torch/`（2.75GB 的 torch 安装包残留，已装进 venv，可删）。
4. ComfyUI 的 `custom_nodes/` 已装：GGUF、H3-Multishot、VideoHelperSuite。

## 下一步方向

- **无审查 LoRA 链路（已完成，2026-09-08）**：两个 LoRA 文件已下载并 sha256 校验通过；i2v 冒烟测试通过（320×320/17帧/54s 出片，LoRA 节点 200/201 注入有效）。⚠️ 踩坑：**注入节点 id 必须用字符串**（"200"/"201"），ComfyUI prompt 验证按字符串键查节点，用 int 键会 400 KeyError。待办：真实尺度内容实测（出片质量/身体还原度）。
- **H3 无审查链路（已完成，2026-09-11）**：nsfw 不再强制回退 Wan，前端无审查面板「高级」里有模型下拉（Wan/H3）。冒烟测试通过（512×512/56帧/20步 euler+beta，约 5.5 分钟含首次加载 18.8GB 模型）。A/B 实测：euler+beta 14/20 步人物一致性都优于 res_multistep+simple 20 步（后者 2.1s 处凭空出现眼镜、发长漂移）。
- 近期：跑完高清 seg04~seg10，拼 `final_video_highres.mp4`，和 4K 超分版放一起对比看哪种更耐看。
- 中期：一致性——人物「自动画角色」（需文生图模型，本机 checkpoints/loras 目前空）、场景参考图、配音（后期）。
