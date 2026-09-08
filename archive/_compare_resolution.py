# -*- coding: utf-8 -*-
"""对照测试：832×480 vs 1024×576，同种子同提示词，评估质量差异。"""
import json
import time
from pathlib import Path

from ai import h3_prompt
import comfy

WORKFLOWS = Path(r"D:\VideoStudio\workflows")
OUTPUT_DIR = Path(r"D:\VideoStudio\output")

# 固定种子保证可比性
SEED = 999888777
# 简化测试提示词（3秒 = 49帧）
TEST_PROMPT = "薄雾弥漫的清晨，一位中年人沿着田埂缓步走来，晨雾轻轻浮动。"

def submit_test(width: int, height: int, length: int, label: str) -> str:
    """提交一次 Wan 生成测试。"""
    print(f"\n{'='*60}")
    print(f"测试: {label} ({width}×{height}, {length}帧)")
    print(f"{'='*60}")

    # DeepSeek 增强提示词
    print("生成 DeepSeek 细节增强提示词...")
    enhanced = h3_prompt(TEST_PROMPT, "t2v", length / 16.0)
    if not enhanced:
        print("DeepSeek 调用失败，使用原始提示词")
        enhanced = TEST_PROMPT
    print(f"增强后提示词（前 200 字符）：\n{enhanced[:200]}...\n")

    # 加载工作流
    wf = json.loads((WORKFLOWS / "t2v_api.json").read_text(encoding="utf-8"))

    # 关键参数：Wan 2.2 t2v 默认 20 步，10/10 分割
    steps = 20
    half = steps // 2

    wf["2"]["inputs"]["text"] = enhanced
    wf["9"]["inputs"].update({"width": width, "height": height, "length": length})
    wf["10"]["inputs"].update({"noise_seed": SEED, "steps": steps, "start_at_step": 0, "end_at_step": half})
    wf["11"]["inputs"].update({"noise_seed": SEED, "steps": steps, "start_at_step": half, "end_at_step": steps})
    wf["14"]["inputs"]["filename_prefix"] = f"video/compare_{label}"

    print(f"提交任务（seed={SEED}, steps={steps}）...")
    prompt_id = comfy.submit(wf)
    print(f"任务 ID: {prompt_id}")
    return prompt_id

def main():
    if not comfy.is_ready():
        print("ComfyUI 未运行，请先启动")
        return

    # 测试 1: 默认分辨率 832×480, 3秒 = 49帧
    id1 = submit_test(832, 480, 49, "832x480")
    print(f"预计生成时间：5-8 分钟\n")

    # 测试 2: 高分辨率 1024×576, 3秒 = 49帧
    id2 = submit_test(1024, 576, 49, "1024x576")
    print(f"预计生成时间：8-12 分钟\n")

    print(f"\n{'='*60}")
    print("两个任务已提交，ComfyUI 将按队列顺序生成")
    print(f"输出目录：{OUTPUT_DIR}")
    print(f"文件名：compare_832x480_xxxxx_.mp4 和 compare_1024x576_xxxxx_.mp4")
    print(f"{'='*60}")

    return [id1, id2]

if __name__ == "__main__":
    main()
