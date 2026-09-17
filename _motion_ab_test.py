# -*- coding: utf-8 -*-
"""运动增强对照实验：测（参考图数量 × 提示词运动强度）对成片运动量的影响。

固定 22 帧快测（网格 17k+5），每组 1 条；用帧间差 + 本地视觉模型判读"像不像活人在动"。
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image, ImageChops, ImageStat

import comfy
import httpx
import ai
from service.generation import generate_h3_shot

FF = r"D:\ComfyUI_Wan\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
VID = Path(r"D:\ComfyUI_Wan\output\video")
FRAMES = 22          # 快测用（网格值 17k+5）
STEPS = 20

# 运动增强提示词（明确要求可见动作 + 明确镜头运动），与原有"极慢几乎不动"相反
MOTION_PROMPT = (
    "integrated_multimodal_description: Live-action cinematic anime style. [Shot 1] "
    "A woman with long crimson hair stands in a vast ancient stone chamber lit by a glowing "
    "purple summoning circle carved into the floor. She turns her head slowly toward the viewer, "
    "raises one hand and sweeps her hair back from her face, her long hair swinging with the motion, "
    "the sheer silk draped over her shoulders slipping and fluttering. "
    "The camera pushes in with medium amplitude at moderate speed, then arcs slowly around her to reveal "
    "the stone pillars behind her, dust motes drifting through the purple light. "
    "Her expression shifts from wary to confident; she takes one clear step forward, then another.\n\n"
    "overall_soundscape: Low echoing hum of the magic circle, soft footsteps on stone, faint wind.\n\n"
    "non_diegetic_music: Dark ambient drone with sparse low piano notes, slow steady build."
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


def ask_vlm(video: Path) -> str:
    d = Path(tempfile.mkdtemp())
    imgs = []
    for i, t in enumerate((0.1, 0.5, 0.85)):
        p = d / f"f{i}.png"
        subprocess.run([FF, "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(p)],
                       capture_output=True, timeout=60)
        if p.exists():
            import base64
            imgs.append(base64.b64encode(p.read_bytes()).decode())
    if len(imgs) < 2:
        return "(抽帧失败)"
    ask = ("这是同一段视频的三帧（前/中/末）。一句话回答：主体有没有明显的位置移动或姿态变化，"
           "还是基本静止只是轻微形变？")
    try:
        r = httpx.post(ai.OLLAMA_GEN, json={"model": ai.UNCENSORED_MODEL, "prompt": ask,
                                            "images": imgs, "stream": False, "think": False,
                                            "options": {"num_predict": 150, "num_ctx": 8192}}, timeout=900)
        return r.json().get("response", "").strip()[:200]
    except Exception as e:  # noqa: BLE001
        return f"(判读失败 {type(e).__name__})"


def run_case(tag: str, refs: list, prompt: str) -> None:
    t0 = time.time()
    try:
        ok, _ = generate_h3_shot(task_id=f"motion_{tag}", prompt=prompt, seed=5150,
                                 width=640, height=832, length=FRAMES, steps=STEPS, nsfw=True,
                                 ref_image_names=refs or None, timeout=3600)
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] 异常 {type(e).__name__}"); return
    dt = time.time() - t0
    out = VID / f"motion_{tag}_00001_.mp4"
    if not (ok and out.exists()):
        print(f"[{tag}] 失败（{dt:.0f}s）"); return
    m = motion_score(out)
    print(f"[{tag}] 完成 {dt/60:.1f} 分钟 | 帧间差 {m:.2f}")
    print(f"      qwen 判读: {ask_vlm(out)}")


def main() -> None:
    # 上传参考图
    ref_names = {}
    for name, sub in (("莉莉丝_08", "characters"), ("爱丽丝_10", "characters"),
                      ("主人_04", "characters"), ("古代石室_04", "scenes"), ("召唤法阵中央_04", "scenes")):
        p = Path("output/_assets_juqing") / sub / f"{name}.png"
        if p.exists():
            ref_names[name] = comfy.upload_image(p)
    print("参考图:", list(ref_names))

    # 取镜 1 的原始提示词（基线）
    old = json.loads(Path("output/_director_juqing/h3_prompts.json").read_text(encoding="utf-8"))[0]

    cases = [
        ("A_5ref_原提示", [ref_names[k] for k in ("莉莉丝_08", "爱丽丝_10", "主人_04", "古代石室_04", "召唤法阵中央_04") if k in ref_names], old),
        ("B_2ref_运动提示", [ref_names[k] for k in ("莉莉丝_08", "古代石室_04") if k in ref_names], MOTION_PROMPT),
        ("C_0ref_运动提示", [], MOTION_PROMPT),
    ]
    for tag, refs, pr in cases:
        print(f"\n=== {tag}（{len(refs)} 张参考图）===")
        run_case(tag, refs, pr)


if __name__ == "__main__":
    main()
