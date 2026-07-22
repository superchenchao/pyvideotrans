from __future__ import annotations

import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TARGET = PROJECT / "cineflow" / "subtitle_worker.py"
REPORT = PROJECT / "patch-report.txt"


def find_line(lines: list[str], value: str, start: int = 0) -> int:
    for index in range(start, len(lines)):
        if lines[index].strip() == value:
            return index
    raise RuntimeError(f"line not found: {value}")


def patch() -> list[str]:
    lines = TARGET.read_text(encoding="utf-8").splitlines()
    changes: list[str] = []

    old_condition = 'if not has_existing_submission or provider.name == "local":'
    if any(line.strip() == old_condition for line in lines):
        start = find_line(lines, "submission = self._submission_from_job(job)")
        end = find_line(lines, old_condition, start)
        indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
        replacement = [
            f"{indent}submission = self._submission_from_job(job)",
            f"{indent}output_exists = bool(",
            f"{indent}    submission.output_object_key",
            f"{indent}    and await asyncio.to_thread(",
            f"{indent}        self.store.object_exists,",
            f"{indent}        submission.output_object_key,",
            f"{indent}    )",
            f"{indent})",
            f'{indent}if provider.name == "local":',
            f"{indent}    has_existing_submission = output_exists and submission.completed",
            f"{indent}else:",
            f"{indent}    has_existing_submission = bool(",
            f"{indent}        submission.external_job_id",
            f"{indent}        or submission.completed",
            f"{indent}        or output_exists",
            f"{indent}    )",
            f"{indent}if not has_existing_submission:",
        ]
        lines[start : end + 1] = replacement
        changes.append("recovery")

    cancel_section = find_line(lines, "async def cancel(")
    cleanup_section = find_line(lines, "async def cleanup(", cancel_section)
    if not any(
        line.strip() == "task = self.tasks.get(job_id)"
        for line in lines[cancel_section:cleanup_section]
    ):
        load_at = find_line(lines, "job = await self._load(job_id)", cancel_section)
        indent = lines[load_at][: len(lines[load_at]) - len(lines[load_at].lstrip())]
        insertion = [
            f"{indent}task = self.tasks.get(job_id)",
            f"{indent}if task is not None and task is not asyncio.current_task() and not task.done():",
            f"{indent}    task.cancel()",
            f"{indent}    await asyncio.gather(task, return_exceptions=True)",
        ]
        lines[load_at + 1 : load_at + 1] = insertion
        changes.append("cancel-task")
        cleanup_section += len(insertion)

    old_delete = "delete_output = delete_successful_outputs or job.state not in {"
    if any(line.strip() == old_delete for line in lines[cleanup_section:]):
        delete_start = find_line(lines, old_delete, cleanup_section)
        try_at = find_line(lines, "try:", delete_start)
        indent = lines[delete_start][: len(lines[delete_start]) - len(lines[delete_start].lstrip())]
        replacement = [
            f"{indent}if job.state in {{",
            f"{indent}    SubtitleJobState.SUCCEEDED,",
            f"{indent}    SubtitleJobState.DEGRADED,",
            f"{indent}}} and not delete_successful_outputs:",
            f"{indent}    continue",
        ]
        lines[delete_start:try_at] = replacement
        for index in range(delete_start, len(lines)):
            if lines[index].strip() == "delete_output=delete_output,":
                call_indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
                lines[index] = f"{call_indent}delete_output=True,"
                break
        else:
            raise RuntimeError("cleanup delete_output call not found")
        changes.append("cleanup")

    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changes


def main() -> int:
    try:
        changes = patch()
        REPORT.write_text("changes=" + ",".join(changes) + "\n", encoding="utf-8")
        return 0
    except Exception:
        REPORT.write_text(traceback.format_exc(), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
