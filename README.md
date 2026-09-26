# 🎬 出片台 VideoStudio

面向非技术创作者的一键出视频工具：本地运行，把「一个想法」变成「一段成片」。

基于 ComfyUI 的图生视频 / 文生视频能力做了完整封装，普通人不需要懂 ComfyUI 的节点图，在网页界面上选图、写一句话、点按钮，视频就出来了。

<p align="center">
  <img src="assets/demo_frame.png" alt="出片台生成效果示例" width="560"/>
</p>

<p align="center"><em>▲ 由出片台生成的视频片段（本地 8GB 显存，Wan 2.2 模型，约 25 分钟出片）</em></p>

## 解决什么问题

- **ComfyUI 太专业**：节点图、模型文件、采样参数……普通人根本看不懂
- **AI 出片没有「工程」**：一张图生一段视频很容易，但角色一致性、多段拼接、字幕烧录、批量产出这些「最后一公里」没人管
- 出片台就是把这一段补齐：**人只做「选择」和「下指令」**，其余全部自动完成

## 三种出片模式

| 模式 | 输入 | 适合 |
|---|---|---|
| 图生视频 | 一张图 + 一句描述 | 有素材，想让它动 |
| 文生视频 | 一句话 | 从零开始 |
| 故事模式 | 一段故事 | 自动拆成分镜，逐镜生成，拼成一条连续成片 |

## 核心能力

- **角色一致性**：四种角色锚定方式——自动抽卡、角色库选卡、按描述生成、上传图；定妆卡存入本地资料库，跨片复用
- **长视频自动接续**：首段文生、后续段用上段尾帧做首帧，最多 6 段连生成，自动拼接
- **本地批量产线**：长任务走离线脚本，断点续跑，夜间挂上就能跑一整晚，早上起来出片
- **桌宠监控**：右下角悬浮小猫咪，双击暂停/继续、右键菜单管理，贴边自动收窄
- **双模型切换**：Wan 2.2（双专家，4 步蒸馏采样，默认）/ MiniMax H3（FL2VA，文生 + 图生 + 参考图锚定），前端一键切换，无需改配置

## 技术架构

```
web/（纯 HTML+JS 前端，无构建）
  ↓ FastAPI
api/ → service/ → db/   （分层调用，铁律）
  ↓
comfy.py（ComfyUI 引擎封装：提交工作流 / WebSocket 进度 / 取消）
  ↓
ComfyUI 8188（Wan 2.2 / MiniMax H3 / 角色卡文生图）
```

- **工作流原生优先**：凡 ComfyUI 节点能做的，一律做成 `workflows/*.json`，Python 只做提交、下载、编排
- **本地嵌入模型**：Ollama qwen2.5vl:7b 看图，DeepSeek 写叙事提示词
- **零依赖分层**：api → service → db → shared，各层职责清晰

## 快速开始

```bash
# 前置：ComfyUI 装在 D:\ComfyUI_Wan（端口 8188）
# Ollama 本机运行 qwen2.5vl:7b

# 复制密钥模板
cp local_config.example.py local_config.py
# 填入 DeepSeek API Key

# 一键启动（自动拉起 ComfyUI + 后端 + 浏览器）
run.bat
# 或手动访问 http://127.0.0.1:8000/
```

**离线批量跑（不用开 run.bat）**：

```bash
D:\ComfyUI_Wan\run_comfyui.bat   # 引擎
python _run_batch_seg2to10.py     # 批量产线
python _monitor_progress.py 30    # 命令行看进度
```

## 目录结构

```
web/                  前端（HTML+JS，无构建）
api/  service/  db/   分层业务代码
workflows/            ComfyUI 节点图（Wan i2v/t2v、H3、角色卡、Qwen 三视图等）
_archive/             已完成任务留档 + 4 个一次性脚本
_pet.py               桌宠（Hello Kitty 悬浮窗）
_run_batch_seg2to10.py  离线批量产线
_monitor_progress.py    命令行进度监控
_finalize_1080p.py      多段拼接 + 1080p 升分辨率
_upscale_realesrgan.py  Real-ESRGAN 本地超分
```

## 实测环境

- 8GB 显存，Wan 2.2 i2v 单段约 25 分钟（124 帧，4 步 LightX2V）
- MiniMax H3 t2v/i2v 单段约 25 分钟（124 帧，20 步 euler+beta）
- 长片（10 段高清重跑）约 55 分钟/段，挂后台跑整晚

## License

MIT
