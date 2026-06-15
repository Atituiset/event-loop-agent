from opencode_agent.core.models import ScanTask


def test_scan_task_duration():
    task = ScanTask(file_path="a.c", task_id="t1", report_file="a.md", log_file="a.log")
    task.start_time = 10.0
    task.end_time = 12.5
    assert task.duration == 2.5
