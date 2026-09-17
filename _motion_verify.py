# -*- coding: utf-8 -*-
"""运动验证（真实长度）：56 帧（2.3s，动作能演完）对比「单参考人物」与「无参考」。

带引擎崩溃自动拉起（上一版没这层，ComfyUI 一崩就空轮询干等）。
结果：帧间差 + 本地视觉模型判读「有没有真动作」。
"""
import base64
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "director" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image, ImageChops, ImageStat

import comfy
import httpx
import ai
from service.generation import generate_h3_shot
from director import ensure_comfy          # 复用管线的「崩了自动拉起」

FF = r"D:\ComfyUI_Wan\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
VID = Path(r"D:\ComfyUI_Wan\output\video")
FRAMES = 56          # 2.3 秒：够演一个"转身/迈步"
STEPS = 20

MOTION_PROMPT = (
    "integrated_multimodal_description: Live-action cinematic anime style. [Shot 1] "
    "A woman with long crimson hair and small black horns stands in a vast ancient stone chamber lit by a "
    "glowing purple summoning circle carved into the floor. She slowly turns her whole body toward the viewer, "
    "takes two clear steps forward across the flagstones, then raises one hand and sweeps her long hair back, "
    "the sheer silk draped on her shoulders sliding down and fluttering behind her. Her expression shifts from "
    "wary to confident. The camera tracks her from the side with medium amplitude at moderate speed, then arcs "
    "around to face her as she stops, dust motes drifting through the purple light.\n\n"
    "overall_soundscape: Low echoing hum of the magic circle, distinct footsteps on stone, faint wind.\n\n"
    "non_diegetic_music: Dark ambient drone with sparse low piano notes, steady build."
)


def motion_score(video: Path) -> float:
    d = Path(tempfile.mkdtemp())
    subprocess.run([FF, "-y", "-i", str(video), "-vf", "fps=6", "-frames:v", "12", str(d / "f%02d.png")],
                   capture_output=True, timeout=120)
    fs = sorted(d.glob("f*.png"))
    if len(fs) < 2:
        return 0.0
    diffs = []
    for a, b in zip(fs, fs[1:]):
        ia, ib = Image.open(a).convert("L"), Image.open(b).convert("L")
        diffs.append(ImageStat.Stat(ImageChops.difference(ia, ib)).mean[0])
    return sum(diffs) / len(diffs)


def ask_vlm(video: Path, times=(0.1, 0.5, 0.85)) -> str:
    d = Path(tempfile.mkdtemp())
    imgs = []
    for i, t in enumerate(times):
        p = d / f"f{i}.png"
        subprocess.run([FF, "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(p)],
                       capture_output=True, timeout=60)
        if p.exists():
            imgs.append(base64.b64encode(p.read_bytes()).decode())
    if len(imgs) < 2:
        return "(抽帧失败)"
    ask = ("这是同一段视频的三帧（前/中/末）。客观回答：1) 人物有没有明显的位移或姿态变化"
           "（走动、转身、抬手之类），还是基本站在原地只是头发飘动？2) 像不像活人在动？两三句直说。")
    try:
        r = httpx.post(ai.OLLAMA_GEN, json={"model": ai.UNCENSORED_MODEL, "prompt": ask,
                                            "images": imgs, "stream": False, "think": False,
                                            "options": {"num_predict": 300, "num_ctx": 8192}}, timeout=900)
        return r.json().get("response", "").strip()[:300]
    except Exception as e:  # noqa: BLE001
        return f"(判读失败 {type(e).__name__})"


def run_case(tag: str, refs: list) -> None:
    if not ensure_comfy():
        print(f"[{tag}] 引擎起不来，跳过")
        return
    t0 = time.time()
    try:
        ok, _ = generate_h3_shot(task_id=f"mo_{tag}", prompt=MOTION_PROMPT, seed=7770,
                                 width=640, height=832, length=FRAMES, steps=STEPS, nsfw=True,
                                 ref_image_names=refs or None, timeout=5400)
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] 异常 {type(e).__name__}: {str(e)[:80]}")
        return
    dt = time.time() - t0
    out = VID / f"mo_{tag}_00001_.mp4"
    if not (ok and out.exists()):
        print(f"[{tag}] 失败（{dt/60:.1f} 分钟，多为引擎崩溃）")
        return
    print(f"[{tag}] 完成 {dt/60:.1f} 分钟 | 帧间差 {motion_score(out):.2f}")
    print(f"      qwen: {ask_vlm(out)}")


def main() -> None:
    refs = {}
    p = ROOT / "output/_assets_juqing/characters/莉莉丝_08.png"
    if p.exists():
        refs["莉莉丝"] = comfy.upload_image(p)
    print("已上传参考图:", list(refs))

    print("\n=== D：单参考人物（莉莉丝）===")
    run_case("D_1ref", [refs["莉莉丝"]] if "莉莉丝" in refs else [])
    print("\n=== E：无参考 ===")
    run_case("E_0ref", [])


if __name__ == "__main__":
    main()
