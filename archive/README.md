# archive/ — 已完成任务的留档

这里放的都是**已经跑完、使命完成**的东西。留着是为了能回头查「当时怎么做的」，不是日常要用的。

## scripts/（根目录这 4 个 `_` 开头的脚本）

| 文件 | 当时的用途 | 现状 |
| --- | --- | --- |
| `_resume_story.py` | 24 段故事长视频续跑（自带看门狗，ComfyUI 崩了自动重启） | 已出片 `1e9d3c87cc3b.mp4` |
| `_regenerate_highres.py` | 高清重生成第一版 | 已被 `_run_batch_seg2to10.py` 取代 |
| `_compare_resolution.py` | 832×480 vs 1024×576 画质对照测试 | 测完定了 1024×576，日志在 `logs/comfy_compare.log` |
| `_test_wan_highres.py` | Wan 2.2 高分辨率生成测试 | 测完可用，日志在 `logs/comfy_highres.log` |

## logs/

超分和生成过程的完整日志。`upscale_resume.log`（1.4M）和 `upscale.log`（810K）是 4K 超分逐帧处理的记录，排查画质问题时才需要翻。

## test_upscale/

Real-ESRGAN 的单帧测试图（原图 vs 4 倍放大对比）。

---

**注意**：`scripts/` 里的脚本依赖根目录的 `comfy.py`、`ai.py`。如果哪天要复跑，不能直接 `python archive/xxx.py`（会 import 失败），把它们拷回根目录再跑。
