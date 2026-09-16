---
name: director
description: 短剧导演全流程（本地无审查栈）：剧本 → 故事圣经 → 分镜 → H3 提示词 → 逐镜出片 → 拼接成片。当用户要「做短剧」「把剧本做成视频」「全流程出片」「小说转短剧」「分镜出片」时使用。默认走本地无审查链路（qwen3.8 编剧 + MiniMax H3 无审查出片），内容不出本机。
version: 1.0.0
license: MIT
---

# 导演 Skill（短剧全流程 · 本地无审查栈）

一句话：**给一个剧本文件，出一条短剧成片**。编剧、分镜、提示词、出片、拼接全部本地完成，内容不经过任何云端。

## 什么时候用

- 「把 juben 里的剧本做成视频」「做一部短剧」「全流程出片」
- 已有小说/剧本，要转成短剧视频
- 要按某个题材风格（悬疑/甜宠/追逐/内心独白…）出片

**不适用**：单段文生视频/图生视频（那用出片台界面的 i2v/t2v tab）；纯文字创作不出片。

## 本地栈（三层，全本地）

| 角色 | 用什么 | 说明 |
|---|---|---|
| 编剧大脑 | Ollama `qwen3.8-uncensored-fast` | 写故事圣经、拆分镜、写 H3 提示词；无审查、内容不出本机 |
| 出片引擎 | ComfyUI + MiniMax H3（无审查） | 未剪枝 Q4_K_M 底模 + `NaughtyTimes_v3_rank64_unpruned` LoRA（`use_lora=True`） |
| 拼接/字幕 | ffmpeg（`shared/ffmpeg_tools.py`） | 叠化拼接、抽尾帧、烧字幕 |

## 五个阶段

```
剧本 → ① 故事圣经 → ② 分镜表 → ③ H3 提示词 → ④ 逐镜出片 → ⑤ 拼接成片
```

1. **故事圣经**：从剧本提取「不可违背的事实」——人物（外貌/身份/服装）、场景、关键道具、世界规则。之后每一次模型调用都带上它，防人物漂移。
2. **分镜表**：按麦基三幕分配镜头数（25% / 50% / 25%），每镜 ≤ 5.2 秒。每镜带：景别、运镜、情绪温度、剧情作用。
3. **H3 提示词**：按 MiniMax H3 官方三段式（`integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music`）写英文提示词。格式细则见 `h3-prompt-writing` skill。
4. **逐镜出片**：每镜提交 H3 无审查工作流；同一场景的连续镜头用「上一镜尾帧 → 下一镜首帧」接续保连贯。
5. **拼接成片**：叠化拼接 + 可选烧中文字幕。

## 题材镜头语言（references/genres/）

每个题材是一份「镜头语言包」，含默认运镜、剪辑风格、构图禁忌。**按剧本内容自动匹配**，注入分镜提示词：

| 题材 | 默认运镜 | 核心 |
|---|---|---|
| [悬疑](references/genres/suspense.md) | 慢推 | 控制「观众比角色知道得多还是少」 |
| [甜宠](references/genres/sweet.md) | 环绕 | 拍两个人之间的距离变化，反应特写给足 |
| [追逐动作](references/genres/chase-action.md) | 手持/跟拍 | 空间关系要清楚，剪切点踩动作 |
| [内心独白](references/genres/inner-monologue.md) | 固定/微推 | 用画外空间和停顿外化心理 |
| [对白覆盖](references/genres/dialogue-coverage.md) | 正反打 | 谁在主导这场对话，镜头给谁 |
| [服装叙事](references/genres/costume.md) | — | 服装变化即人物弧光 |
| [编剧骨架](references/genres/screenwriter.md) | — | 麦基三幕 + 声音指纹 + 预算分配 |

> 题材包借自开源项目 [wind-comic](https://github.com/ChrisChen667788/wind-comic)（MIT），已按本地 H3 链路适配。

## 用法

```bash
# 第一步：出资产（人物卡 + 场景卡）——管线会停在这里等你挑
venv/Scripts/python.exe .claude/skills/director/scripts/director.py \
    --script juben/juqing.txt --shots 12 --assets-only

# 第二步：挑完把选中的报进来，才开跑视频
venv/Scripts/python.exe .claude/skills/director/scripts/director.py \
    --script juben/juqing.txt --shots 12 --pick "莉莉丝_01.png,召唤石室_01.png"

# 只出分镜表不烧显卡（先审剧本）
... --plan-only

# 从第 6 镜续跑（断点续跑，已出的镜自动跳过）
... --start 6

# 指定题材（不指定则按剧本自动匹配）
... --genre suspense
```

**参数默认值**：640×832 竖屏、每镜 124 帧（5.2s @24fps）、20 步 euler+beta、无审查开启。

## 资产先行（硬规则，代码强制）

**出片前必须先出资产、由人选定形象**——参考图一旦定错，整片十几小时算力全废。

- 不带 `--pick` 跑到出片阶段时，**管线拒绝继续**，只出资产并列出候选清单。
- `--pick` 传选中的文件名（人物 + 场景），写入 `assets.json` 的 `selected` 字段；后续重跑自动复用它，不再重复烧显卡出图。
- 每个资产出**多个候选**（人物默认 2 张，可 `--char-candidates 4`）方便对比。

### 出图引擎（按内容分级，自动选）

| provider | 用什么 | 适用 | 成本 |
|---|---|---|---|
| **codex**（普通剧情默认） | Codex 内置 `image_gen` | 普通剧情 | 走 ChatGPT 订阅，不花 API 钱 |
| **cloud** | 豆包 Seedream | 普通剧情备选 | 豆包每日额度 |
| **chatgpt** | ChatGPT 网页版（Playwright 驱动） | 普通剧情备选 | 订阅额度；需有头 Chrome 走代理 |
| **local** | NoobAI-XL（本地 SDXL） | **无审查内容必用** | 免费（占本机显卡） |

成人内容**不能走云端**（会被平台拦），管线在 `--no-nsfw` 未指定时自动选 `local`。

## 硬规则

- **帧数只能取 H3 网格值**：17k+5 → 56 / 73 / 124 / 192 …（不是 4n+1）。填错会报错或画面异常。
- **内容不上云**：编剧/分镜/提示词全走本地 qwen3.8；只有用户明确要求才切云端。
- **剧本文件读而不显**：脚本读字节直接送本地模型，不打印内容、不落日志。
- **断点续跑**：每镜出片后立即落盘，`--start N` 可续；已存在的段自动跳过，绝不重算。
- **长任务挂后台**：一部 12 镜短片约 12×60 分钟 ≈ 12 小时（8GB 显存 + Ref2VA）。用 `run_in_background` 跑，别在前台等。
- **抗崩**：H3 跑大帧数偶发 CUDA OOM 会让 ComfyUI **整进程消失**；`ensure_comfy()` 会自动拉起并续跑，单镜失败会换种子重试。
