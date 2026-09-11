# -*- coding: utf-8 -*-
"""豆包视频 Provider（业务层）：普通视频走豆包 Seedance（会员额度），无审查走本地 Wan。

链路：出片台 → doubao-relay(:8788, OpenAI 兼容) → UI 自动化 → 豆包网页 → Seedance。
额度意识：豆包每日有限额度，抽卡/预览等低成本动作不用它，只在正片出片时用。
"""
import httpx

# doubao-relay 网关（d:\\doubao-relay，npm start 启动）
RELAY = "http://127.0.0.1:8788"
RELAY_KEY = "local-dev-key-change-me"


def relay_ready() -> bool:
    """doubao-relay 网关是否在线。"""
    try:
        r = httpx.get(f"{RELAY}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def generate_video(prompt: str, duration: int = 5, ratio: str = "9:16", timeout_s: int = 600) -> dict:
    """豆包 Seedance 出片（UI 模式：自动点确认卡）。

    返回 {"ok": bool, "video_url": str|None, "msg": str}。
    UI 模式一次约 1~5 分钟（含豆包排队/生成/确认卡交互）。
    """
    try:
        r = httpx.post(
            f"{RELAY}/v1/videos/ui",
            headers={"Authorization": f"Bearer {RELAY_KEY}"},
            json={"prompt": prompt, "duration": duration, "ratio": ratio},
            timeout=timeout_s,
        )
        r.raise_for_status()
        data = r.json()
        videos = [v for v in data.get("videos", []) if v.startswith("http") and ".mp4" in v.lower() or "video/tos" in v]
        # 取第一条真实视频流（douyin v3/vdl 域名的是成片）
        real = [v for v in videos if "douyin.com" in v or "vdl.doubao.com" in v] or videos
        return {"ok": bool(real), "video_url": real[0] if real else None, "msg": data.get("message", "")}
    except httpx.HTTPStatusError as e:
        try:
            msg = e.response.json().get("error", {}).get("message", "")
        except Exception:
            msg = str(e)
        return {"ok": False, "video_url": None, "msg": msg}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "video_url": None, "msg": str(e)}


def download_video(url: str, dest) -> bool:
    """下载豆包产出的视频到本地。"""
    try:
        req = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=120, follow_redirects=True)
        req.raise_for_status()
        with open(dest, "wb") as f:
            f.write(req.content)
        return True
    except Exception:
        return False
