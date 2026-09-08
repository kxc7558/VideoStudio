# -*- coding: utf-8 -*-
"""续跑故事长视频（自愈版）：已完成的段自动跳过，只补缺失的段，最后交叉溶解拼接。

崩溃根因：RTX 4070 Laptop 8GB 显存跑 H3 124 帧贴边，偶发 CUDA OOM 直接崩掉 ComfyUI。
对策：
  1. ComfyUI 用 --reserve-vram 0.3（比原来的 1.0 多留出 ~0.7GB 给模型）；
  2. 本脚本自带看门狗：发现 ComfyUI 死了就自动重启、等就绪后继续；
  3. 幂等：已有 sN.mp4 的段直接跳过，可反复重跑，崩了重来也不浪费。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import ai
import comfy
from app import _build_workflow, concat_videos, extract_last_frame, OUTPUT

TASK = "1e9d3c87cc3b"
MODEL = "h3"
WIDTH, HEIGHT = 832, 480
LENGTH = 124
SECONDS = LENGTH / 24.0
TOTAL = 24

COMFY_DIR = Path(r"D:\ComfyUI_Wan")
COMFY_PY = COMFY_DIR / "venv" / "Scripts" / "python.exe"
COMFY_LOG = Path(r"D:\VideoStudio\_comfy.log")

# 第 11~24 镜的原始剧情目标（REST[k] 对应第 11+k 镜 = 索引 10+k）
REST = [
    "数日之后，光东县县城上空阴云密布，瘟疫悄然蔓延，街道冷清、行人稀少，几个路人捂着口鼻行色匆匆，沿街商铺紧闭，一片萧条。",
    "县城里有人病倒在路旁，家人惊慌失措地上前搀扶，远处传来低低的哭泣声，空气中弥漫着惶恐不安。",
    "廖某的村子依然平静，他独自站在村口的田埂上，望着远处县城上空的灰暗天色，眉头紧锁，为村民的安危深深担忧。",
    "深夜，廖某在自家屋中躺在床上辗转反侧，昏黄的烛火摇曳不定，他渐渐合上眼，沉入了梦乡。",
    "梦中，廖某恍惚看见自家门口影影绰绰地站满了人，上百个身穿旧衣的朦胧身影静静伫立在夜色里，无声无息。",
    "其中一人从人群中缓缓走上前来，身形半透明、面容和善，停在廖某面前，恳切地开口说话，双手比划着嘱托。",
    "廖某在梦中听罢，神情郑重，连连点头应允；那上百个身影齐齐拱手躬身向他致谢，随后渐渐隐没在夜雾之中。",
    "廖某猛地惊醒，翻身坐起，额头渗出冷汗，他望着摇曳的烛火，回味着梦中嘱托，眼神渐渐变得坚定。",
    "次日天明，廖某请来村中工匠，在院子里糊制了十来面纸做的战旗，又用锡纸包裹木条，做成了一百把明晃晃的木刀，整整齐齐摆满院落。",
    "傍晚，廖某把纸旗和锡纸木刀搬到义冢前堆成一堆，点燃火把俯身引燃，火焰腾空而起，纸旗木刀在烈焰中翻卷燃烧、化作灰烬与青烟。",
    "几天后的一个夜晚，村民们在睡梦中被一阵喧嚣惊醒，村外荒野上突然传来刀兵相击、人马嘶喊的嘈杂声，村民们披衣下床，走到窗边惊恐地张望。",
    "村外的夜色里，无数半透明的人影手持纸旗和锡纸木刀，与一团团翻涌的黑雾——疫鬼——激烈厮杀，刀光交错、火星四溅，喊杀声震天。",
    "战斗持续到天将破晓，疫鬼的黑雾渐渐溃散退去，那些半透明人影也一个个消散在晨光里，村外渐渐归于沉寂，只剩满地被践踏的草叶。",
    "清晨，朝阳升起，金色的阳光洒进村庄，村口炊烟袅袅，村民们纷纷走出家门，在义冢前焚香祭拜、面露感激；这场瘟疫里，这个村子没有一个人染上疫病。",
]

meta = json.loads((OUTPUT / f"{TASK}.json").read_text(encoding="utf-8"))
SEED = meta["seed"]
STEPS = meta["steps"]


def log(msg: str):
    print(msg, flush=True)


def comfy_alive() -> bool:
    try:
        return comfy.is_ready()
    except Exception:
        return False


def restart_comfy() -> bool:
    """启动/重启 ComfyUI，并等它可接受请求。"""
    log("重启 ComfyUI（--reserve-vram 0.3）…")
    with open(COMFY_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== 重启于 epoch {int(time.time())} =====\n")
    subprocess.Popen(
        [str(COMFY_PY), "main.py", "--lowvram", "--reserve-vram", "0.3",
         "--listen", "127.0.0.1", "--port", "8188"],
        cwd=str(COMFY_DIR),
        stdout=open(COMFY_LOG, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    for _ in range(360):  # 最多等 12 分钟
        time.sleep(2)
        if comfy_alive():
            log("ComfyUI 已就绪")
            return True
    log("ComfyUI 重启超时")
    return False


def wait_until_done(pid: str, timeout: int = 7200):
    """轮询直到完成；ComfyUI 死掉则快速返回（不再傻等 2 小时）。"""
    start = time.time()
    fail = 0
    while time.time() - start < timeout:
        if not comfy_alive():
            return False, {"error": "comfy_died"}
        try:
            h = comfy._get(f"/history/{pid}")
            fail = 0
        except Exception:
            fail += 1
            if fail > 10:  # 连续约 20 秒连不上 → 判定 ComfyUI 死了
                return False, {"error": "comfy_died"}
            time.sleep(2)
            continue
        entry = h.get(pid)
        if entry:
            st = entry.get("status", {})
            if st.get("status_str") == "error":
                return False, entry
            if st.get("completed"):
                return True, entry
        time.sleep(2)
    return False, {"error": "timeout"}


def generate_segment(idx: int, image_name: str, prompt: str):
    """生成第 idx 段（带重试 + ComfyUI 死了自动重启）。返回 mp4 路径或 None。"""
    seg_id = f"{TASK}_s{idx}"
    wf = _build_workflow(MODEL, "i2v", image_name, prompt, SEED + idx,
                         WIDTH, HEIGHT, LENGTH, seg_id, STEPS)
    for attempt in range(6):
        if not comfy_alive() and not restart_comfy():
            return None
        try:
            pid = comfy.submit(wf)
        except Exception as e:  # noqa: BLE001
            log(f"  第 {idx + 1} 段提交失败：{e}")
            time.sleep(5)
            continue
        ok, history = wait_until_done(pid)
        if not ok and history.get("error") == "timeout":
            # 超时兜底：可能其实已经跑完，直接再查一次历史
            try:
                h = comfy._get(f"/history/{pid}")
                entry = h.get(pid)
                if entry and entry.get("status", {}).get("completed"):
                    ok, history = True, entry
            except Exception:
                pass
        if ok:
            video = comfy.find_video(history)
            if video:
                dest = OUTPUT / f"{seg_id}.mp4"
                try:
                    comfy.download_video(video, dest)
                    return dest
                except Exception as e:  # noqa: BLE001
                    log(f"  第 {idx + 1} 段下载失败：{e}")
        log(f"  第 {idx + 1} 段第 {attempt + 1} 次失败（可能显存崩了，准备重启重试）")
        time.sleep(10)
    return None


def main():
    have = sorted(i for i in range(TOTAL) if (OUTPUT / f"{TASK}_s{i}.mp4").exists())
    missing = [i for i in range(TOTAL) if i not in set(have)]
    log(f"已有 {len(have)} 段（{have}），缺失 {len(missing)} 段（{missing}）")

    for idx in missing:
        log(f"== 第 {idx + 1}/24 段 ==")
        # 上一段一定已存在（按顺序生成），抽其尾帧做本段首帧
        prev_path = OUTPUT / f"{TASK}_s{idx - 1}.mp4"
        frame = OUTPUT / f"{TASK}_resume_last.png"
        extract_last_frame(prev_path, frame)

        original = REST[idx - 10]
        prev_desc = ai.describe_image(frame)
        bridged = ai.bridge_next_prompt(prev_desc, original, "h3", SECONDS) if prev_desc else ""
        if not bridged:
            bridged = ai.h3_prompt(original, "i2v", SECONDS)
        prompt = bridged or original
        if bridged:
            log("  提示词已按上一段结尾改写")

        # 上传首帧要连引擎，先确保 ComfyUI 活着
        if not comfy_alive() and not restart_comfy():
            log("无法启动 ComfyUI，中止")
            sys.exit(1)
        image_name = comfy.upload_image(frame)
        frame.unlink(missing_ok=True)

        dest = generate_segment(idx, image_name, prompt)
        if dest is None:
            log(f"第 {idx + 1} 段连续失败，中止（可重跑本脚本继续）")
            sys.exit(1)
        log(f"第 {idx + 1}/24 段完成")

    # 拼接全部 24 段（交叉溶解）
    all_segs = [OUTPUT / f"{TASK}_s{i}.mp4" for i in range(TOTAL)]
    log("拼接 24 段（交叉溶解）…")
    dest = OUTPUT / f"{TASK}.mp4"
    concat_videos(all_segs, dest)
    meta["state"] = "done"
    meta["msg"] = "完成"
    meta["video"] = dest.name
    (OUTPUT / f"{TASK}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    log(f"全部完成：{dest}")


if __name__ == "__main__":
    main()
