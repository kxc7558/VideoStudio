# -*- coding: utf-8 -*-
"""高分辨率重新生成完整视频：1024×576, 5秒每段（81帧）。"""
import json
import time
from pathlib import Path

from ai import h3_prompt
import comfy

WORKFLOWS = Path(r"D:\VideoStudio\workflows")
OUTPUT_DIR = Path(r"D:\VideoStudio\output")
STORY_FILE = OUTPUT_DIR / "1e9d3c87cc3b.json"

# 新配置（实测 1024×576 + 113帧=7秒 会显存爆掉严重变慢，故选 81帧=5秒 档位）
WIDTH = 1024
HEIGHT = 576
LENGTH = 81  # 5秒 @ 16fps (必须是 4n+1)
STEPS = 20
SEED_BASE = 999000000  # 固定种子基数保证可复现

def load_story():
    """加载原故事板。"""
    story = json.loads(STORY_FILE.read_text(encoding="utf-8"))
    segments = []
    for ai_prompt in story.get("ai_prompts", []):
        segments.append({
            "index": ai_prompt["segment"],
            "cn": ai_prompt["original"],
            "en": ai_prompt["rewritten"],
        })
    return segments

def submit_segment(seg_index: int, prompt_cn: str, prompt_en: str) -> str:
    """提交单个分段生成任务。"""
    print(f"\n{'='*60}")
    print(f"分段 {seg_index}/10: {prompt_cn[:50]}...")
    print(f"{'='*60}")

    # 使用已有的 DeepSeek 增强提示词
    enhanced = prompt_en
    print(f"使用已增强提示词（前 200 字符）：\n{enhanced[:200]}...\n")

    # 加载 Wan t2v 工作流
    wf = json.loads((WORKFLOWS / "t2v_api.json").read_text(encoding="utf-8"))

    # 配置参数
    seed = SEED_BASE + seg_index
    half = STEPS // 2

    wf["2"]["inputs"]["text"] = enhanced
    wf["9"]["inputs"].update({"width": WIDTH, "height": HEIGHT, "length": LENGTH})
    wf["10"]["inputs"].update({"noise_seed": seed, "steps": STEPS, "start_at_step": 0, "end_at_step": half})
    wf["11"]["inputs"].update({"noise_seed": seed, "steps": STEPS, "start_at_step": half, "end_at_step": STEPS})
    wf["14"]["inputs"]["filename_prefix"] = f"video/highres_seg{seg_index:02d}"

    print(f"提交任务（seed={seed}, {WIDTH}×{HEIGHT}, {LENGTH}帧）...")
    prompt_id = comfy.submit(wf)
    print(f"任务 ID: {prompt_id}")
    return prompt_id

def main():
    if not comfy.is_ready():
        print("ComfyUI 未运行，请先启动")
        return

    segments = load_story()
    print(f"\n加载故事板：共 {len(segments)} 个分段")
    print(f"配置：{WIDTH}×{HEIGHT}, {LENGTH}帧（5秒）, {STEPS}步\n")

    task_ids = []
    for seg in segments:
        task_id = submit_segment(seg["index"], seg["cn"], seg["en"])
        task_ids.append((seg["index"], task_id))
        print(f"预计生成时间：10-15 分钟")
        time.sleep(2)  # 避免请求过快

    print(f"\n{'='*60}")
    print(f"全部 {len(task_ids)} 个任务已提交")
    print(f"ComfyUI 将按队列顺序生成")
    print(f"预计总时长：{len(task_ids) * 12} 分钟（约 {len(task_ids) * 12 // 60} 小时）")
    print(f"输出目录：{OUTPUT_DIR}")
    print(f"文件名：highres_seg01_xxxxx_.mp4 ~ highres_seg10_xxxxx_.mp4")
    print(f"{'='*60}")

    # 保存任务清单
    manifest = {
        "task_ids": task_ids,
        "config": {"width": WIDTH, "height": HEIGHT, "length": LENGTH, "steps": STEPS},
        "started": time.time(),
    }
    (OUTPUT_DIR / "highres_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"任务清单已保存：highres_manifest.json")

    return task_ids

if __name__ == "__main__":
    main()
