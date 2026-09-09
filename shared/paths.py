# -*- coding: utf-8 -*-
"""路径常量（公共层）：全项目统一的目录与二进制位置。零依赖。"""
from pathlib import Path

BASE = Path(__file__).parent.parent
WORKFLOWS = BASE / "workflows"
OUTPUT = BASE / "output"
UPLOADS = BASE / "uploads"
JUBEN = BASE / "juben"
WEB = BASE / "web"
CREATIVE_PROFILES = OUTPUT / "creative_profiles.json"

# 拼接视频用的 ffmpeg（复用 ComfyUI 自带的二进制，避免再下载）
FFMPEG = r"D:\ComfyUI_Wan\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
