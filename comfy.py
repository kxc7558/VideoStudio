# -*- coding: utf-8 -*-
"""ComfyUI API 封装：把"传图/输文字 → 出视频"变成几个简单函数。"""
import asyncio
import json
import threading
import time
from pathlib import Path

import httpx

COMFY = "http://127.0.0.1:8188"
COMFY_WS = "ws://127.0.0.1:8188"
CLIENT = "video-studio"  # 固定 client_id，便于在 ComfyUI 队列里区分


class ComfyError(Exception):
    """ComfyUI 交互异常。"""


def is_ready() -> bool:
    """ComfyUI 是否启动并可响应。"""
    try:
        r = httpx.get(COMFY + "/system_stats", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def list_unet() -> list:
    """返回已安装的 unet GGUF 模型文件名列表。"""
    try:
        r = httpx.get(COMFY + "/object_info/UnetLoaderGGUF", timeout=15)
        r.raise_for_status()
        info = r.json().get("UnetLoaderGGUF", {})
        unet_name = info.get("input", {}).get("required", {}).get("unet_name", {})
        # unet_name 结构形如 [[候选文件名...], 控件配置] —— 第一项是候选列表
        return list(unet_name[0]) if unet_name else []
    except Exception:
        return []


def t2v_ready() -> bool:
    """文生视频的两个 GGUF 模型是否都已就位。"""
    names = list_unet()
    return any("t2v_high_noise" in n for n in names) and any("t2v_low_noise" in n for n in names)


def h3_ready() -> bool:
    """MiniMax H3 是否就绪：DiT GGUF + safetensors 文本编码器都已就位且节点可加载。"""
    if not is_ready():
        return False
    try:
        r = httpx.get(COMFY + "/object_info/H3ModelLoaderAny", timeout=15)
        r.raise_for_status()
        info = r.json().get("H3ModelLoaderAny", {})
        model_name = info.get("input", {}).get("required", {}).get("model_name", {})
        names = list(model_name[0]) if model_name else []
        if not any("MiniMax-H3" in n for n in names):
            return False
        r2 = httpx.get(COMFY + "/object_info/CLIPLoader", timeout=15)
        r2.raise_for_status()
        clip_info = r2.json().get("CLIPLoader", {})
        clip_name = clip_info.get("input", {}).get("required", {}).get("clip_name", {})
        clips = list(clip_name[0]) if clip_name else []
        return any("minimax_h3" in n.lower() for n in clips)
    except Exception:
        return False


def upload_image(path: Path) -> str:
    """把本地图片上传到 ComfyUI 的 input 目录，返回 ComfyUI 侧文件名。"""
    with open(path, "rb") as f:
        r = httpx.post(
            COMFY + "/upload/image",
            files={"image": (path.name, f, "image/png")},
            data={"overwrite": "true"},
            timeout=120,
        )
    r.raise_for_status()
    return r.json()["name"]


def submit(workflow: dict) -> str:
    """提交工作流，返回 prompt_id。"""
    r = httpx.post(COMFY + "/prompt", json={"prompt": workflow, "client_id": CLIENT}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "prompt_id" not in data:
        raise ComfyError(f"提交失败：{data}")
    return data["prompt_id"]


def _get(path: str, **kw):
    r = httpx.get(COMFY + path, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def wait_done(prompt_id: str, timeout: int = 3600, poll: float = 2.0, should_cancel=None):
    """轮询直到任务完成。返回 (成功?, history 条目)。should_cancel 返回真值则提前退出。"""
    start = time.time()
    while time.time() - start < timeout:
        if should_cancel and should_cancel():
            return False, {"error": "cancelled"}
        try:
            h = _get(f"/history/{prompt_id}")
        except Exception:
            time.sleep(poll)
            continue
        entry = h.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                return False, entry
            if status.get("completed"):
                return True, entry
        time.sleep(poll)
    return False, {"error": "timeout"}


VIDEO_EXT = (".mp4", ".webm", ".avi", ".mov", ".mkv")


def find_video(entry: dict):
    """从 history 条目里找出第一个视频输出信息。

    SaveVideo 节点把视频放在 ``images`` 键（历史遗留），
    而某些节点用 ``videos`` 键，两者都按扩展名过滤后返回。
    """
    for _node_id, out in entry.get("outputs", {}).items():
        for key in ("videos", "images"):
            for v in out.get(key, []):
                if str(v.get("filename", "")).lower().endswith(VIDEO_EXT):
                    return v
    return None


def download_video(video: dict, dest: Path) -> Path:
    """把 ComfyUI 生成的视频下载到本地 dest。"""
    r = httpx.get(
        COMFY + "/view",
        params={
            "filename": video["filename"],
            "subfolder": video.get("subfolder", ""),
            "type": video.get("type", "output"),
        },
        timeout=600,
    )
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


# ---------------------------------------------------------------------------
# 实时进度 + 队列位置（供出片台显示进度条 / 排队第几位）
# ---------------------------------------------------------------------------

_progress = {}          # prompt_id -> 0~1 的采样进度
_progress_lock = threading.Lock()
_ws_started = False


def _ws_thread():
    """后台线程：连 ComfyUI 的 WebSocket，收 progress 消息更新采样进度。"""
    asyncio.run(_ws_listen())


async def _ws_listen():
    from websockets.asyncio.client import connect
    while True:
        try:
            async with connect(COMFY_WS + "/ws?clientId=" + CLIENT) as ws:
                async for msg in ws:
                    try:
                        data = json.loads(msg if isinstance(msg, str) else msg.decode())
                    except Exception:
                        continue
                    if data.get("type") != "progress":
                        continue
                    d = data.get("data") or {}
                    pid = d.get("prompt_id")
                    if not pid:
                        continue
                    m = d.get("max") or 0
                    v = d.get("value") or 0
                    with _progress_lock:
                        _progress[pid] = (v / m) if m else 0.0
        except Exception:
            pass
        await asyncio.sleep(3)  # 连接断开后稍等再重连


def start_progress_listener():
    """启动进度监听后台线程（幂等）。ComfyUI 未启动时线程会自动反复重连。"""
    global _ws_started
    if _ws_started:
        return
    _ws_started = True
    threading.Thread(target=_ws_thread, daemon=True).start()


def get_progress(prompt_id: str) -> float:
    """返回 0~1 的采样进度；还没开始采样则返回 0。"""
    with _progress_lock:
        return _progress.get(prompt_id, 0.0)


def queue_status(prompt_id: str):
    """返回 (queue_state, position)。

    queue_state：running(正在算) / pending(排队中) / done(已完成或已不在队列)；
    position：pending 时是排队第几位（1 起），其余为 0。
    """
    try:
        q = _get("/queue")
    except Exception:
        return "done", 0
    running = [it[1] for it in q.get("queue_running", [])]
    pending = [it[1] for it in q.get("queue_pending", [])]
    if prompt_id in running:
        return "running", 0
    if prompt_id in pending:
        return "pending", pending.index(prompt_id) + 1
    return "done", 0


def cancel(prompt_id: str) -> bool:
    """取消一个任务：排队中则移出队列，运行中则中断采样。"""
    try:
        qs, _ = queue_status(prompt_id)
        if qs == "pending":
            httpx.post(COMFY + "/queue", json={"delete": [prompt_id]}, timeout=10)
        elif qs == "running":
            httpx.post(COMFY + "/interrupt", timeout=10)
        return True
    except Exception:
        return False
