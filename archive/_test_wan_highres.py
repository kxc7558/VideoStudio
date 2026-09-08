# -*- coding: utf-8 -*-
"""测试 Wan 2.2 高分辨率生成（1024×576）+ DeepSeek 细节增强提示词。"""
import json
import time
from pathlib import Path

from ai import h3_prompt
import comfy

WORKFLOWS = Path(r"D:\VideoStudio\workflows")
OUTPUT_DIR = Path(r"D:\VideoStudio\output")

# 测试提示词：第一段故事（薄雾清晨）
TEST_PROMPT = "薄雾弥漫的清晨，光东县南乡的荒野上，远处一位中年人廖某沿着田埂小径缓步走来，晨雾在他身后轻轻浮动。"

def test_wan_highres():
    print("生成 DeepSeek 细节增强提示词...")
    enhanced = h3_prompt(TEST_PROMPT, "t2v", 5.0)
    if not enhanced:
        print("DeepSeek 调用失败，使用原始提示词")
        enhanced = TEST_PROMPT

    print(f"\n增强后提示词（前 300 字符）：\n{enhanced[:300]}...\n")

    # 加载 Wan t2v 工作流模板
    wf = json.loads((WORKFLOWS / "t2v_api.json").read_text(encoding="utf-8"))

    # 修改为高分辨率 1024×576，5秒（129帧）
    seed = int(time.time())
    steps = 50  # Wan 默认 50 步
    half = steps // 2

    wf["2"]["inputs"]["text"] = enhanced
    wf["9"]["inputs"].update({"width": 1024, "height": 576, "length": 129})
    wf["10"]["inputs"].update({"noise_seed": seed, "steps": steps, "start_at_step": 0, "end_at_step": half})
    wf["11"]["inputs"].update({"noise_seed": seed, "steps": steps, "start_at_step": half, "end_at_step": steps})
    wf["14"]["inputs"]["filename_prefix"] = "video/wan_highres_test"

    # 提交任务
    print("提交 Wan 1024×576 高分辨率任务...")
    prompt_id = comfy.submit(wf)
    print(f"任务 ID: {prompt_id}")
    print(f"预计生成时间：10-15 分钟")
    print(f"输出目录：{OUTPUT_DIR}")

    return prompt_id

if __name__ == "__main__":
    if not comfy.is_ready():
        print("ComfyUI 未运行，请先启动")
    else:
        test_wan_highres()

