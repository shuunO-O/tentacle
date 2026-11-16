"""模型可见的紧凑回执；不改变管理器/API 的原始结构。"""

import json

from runtime.timezone import local_datetime


def compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def task_line(task: dict) -> str:
    line = (f"  [T{task['id']}]{task['content']} | {task['status']}"
            f" | 紧急度:{task['urgency']}")
    if task.get("start_time") is not None:
        line += f" | 开始:{local_datetime(task['start_time']).isoformat()}"
    return line


def plan_text(goals: list[dict]) -> str:
    lines = [f"目标:{len(goals)}"]
    for goal in goals:
        lines.extend(["", f"[G{goal['id']}]{goal['description']} | 优先级:{goal['priority']}"])
        if goal.get("result"):
            lines.append(f"结果:{goal['result']}")
        lines.extend(task_line(task) for task in goal["tasks"])
    return "\n".join(lines)


def batch_text(result: dict) -> str:
    if result.get("status") != "ok":
        # 错误位置、已回滚步骤、未知状态等原样保留，供 scheduler 识别失败。
        return compact_json(result)
    items = result.get("results", [])
    followups = result.get("completed_followups", [])
    lines = [f"成功:{len(items)}"]
    for index, item in enumerate(items, 1):
        fields = []
        for key, value in item.items():
            if key in ("status", "index"):
                continue
            if key == "message" and value == "已接收执行，结果用 schedule_list 查看":
                continue
            prefix = {"goal_id": "G", "task_id": "T", "schedule_id": "S"}.get(key)
            rendered = value if isinstance(value, str) else compact_json(value)
            fields.append(f"{prefix}{value}" if prefix else f"{key}:{rendered}")
        lines.append(f"[{index}]" + " | ".join(fields))
    if (len(items) == 1 and len(followups) == 1
            and followups[0]["completed_task_ids"] == [items[0].get("task_id")]):
        lines = []  # 单任务完成用下方确认代替重复的成功/ID 包装。
    for followup in followups:
        lines.append("已完成:" + ",".join(f"T{task_id}" for task_id in followup["completed_task_ids"]))
        tasks = followup["remaining_tasks"]
        lines.append(f"[G{followup['goal_id']}]{followup['goal_description']} | 剩余:{len(tasks)}")
        lines.extend(task_line(task) for task in tasks)
    return "\n".join(lines)


def schedule_text(schedules: list[dict], *, detail: bool = False) -> str:
    lines = [f"定时任务:{len(schedules)}"]
    for schedule in schedules:
        trigger = schedule["trigger"]
        if isinstance(trigger, dict):
            trigger = f"{trigger['type']}:{trigger['expr']}"
        lines.extend(["", f"[S{schedule['id']}]{schedule['name']} | {trigger}"])
        if detail:
            lines.append(f"状态:{schedule['status']}")
            # 动作参数中的空串、false、0 都可能有业务含义，不能递归删空。
            for key in ("actions", "max_runtime"):
                lines.append(f"{key}:{compact_json(schedule[key])}")
            if schedule.get("on_failure"):
                lines.append(f"on_failure:{compact_json(schedule['on_failure'])}")
        if schedule.get("next_run"):
            lines.append(f"下次:{schedule['next_run']}")
        previous = [str(schedule[key]) for key in ("last_run", "last_status") if schedule.get(key)]
        if previous:
            lines.append("上次:" + " | ".join(previous))
        if schedule.get("last_error"):
            lines.append(f"错误:{schedule['last_error']}")
        if "recent_runs" in schedule:
            lines.append(f"recent_runs:{compact_json(schedule['recent_runs'])}")
    return "\n".join(lines)
