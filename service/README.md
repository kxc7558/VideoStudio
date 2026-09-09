
## 对外接口清单（提供给 api 层）

| 名称 | 所在文件 | 作用 |
|------|---------|------|
| NSFW_LORA/ANIME_LORA/ENHANCER_LORA/MODEL_VARIANT | workflows.py | LoRA 配置与模型变体开关 |
| RESOLUTIONS/DURATIONS/DURATIONS_H3/MODELS/MAX_STEPS/DEFAULT_PROMPT | workflows.py | 生成业务常量 |
| _build_workflow / _build_wan_workflow / _build_h3_workflow | workflows.py | 工作流构建（含 LoRA 链注入） |
| _run_task / _run_long_task | generation.py | 单段/长视频生成执行 |
| _generate_single / _finish_all_shots | generation.py | 单镜生成 / 全镜拼片 |
| _run_oneclick_task / _oc_stage2 / _generate_shot_previews / _pick_best_character_card | oneclick.py | 一键成片状态机与抽卡 |
| _rebuild_and_concat | review.py | 审查后重拼 |
