
## 对外接口清单（提供给 service 层）

| 名称 | 所在文件 | 作用 |
|------|---------|------|
| tasks / _lock | tasks_store.py | 任务内存表与锁 |
| update(task_id, **kw) | tasks_store.py | 更新任务（跨进程读改写安全） |
| task_or_none(task_id) | tasks_store.py | 取任务（内存→磁盘回退） |
| write_meta / meta_dict | tasks_store.py | 档案落盘 |
| reconcile_stale_tasks() | tasks_store.py | 重启清理僵尸任务 |
| list_from_disk() | tasks_store.py | 读全部磁盘档案 |
| cancelled(task_id) | tasks_store.py | 查取消标记 |
| profiles() / save_profiles(data) / clean_profiles(data) | profiles_store.py | 制片资料读写 |
| add_feedback(text, page, screenshot) / list_feedback() / get_feedback(id) / update_status(id, status, reply) | feedback_store.py | 意见箱存取（每条一个 json，落盘 data/feedback/） |
