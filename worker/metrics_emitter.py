"""
Emits CloudWatch Embedded Metric Format (EMF) log lines.
Workers print these to stdout; ECS log driver forwards them to CloudWatch Logs.
"""
import json
import time
from typing import Optional


def build_emf_log_line(
    *,
    job_id: str,
    task_id: str,
    total_processed: Optional[int],
    avg_processing_ms: Optional[float],
) -> str:
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "QueueScaling",
                    "Dimensions": [["jobId"]],
                    "Metrics": [
                        {"Name": "totalProcessed", "Unit": "Count"},
                        {"Name": "avgProcessingMs", "Unit": "Milliseconds"},
                    ],
                }
            ],
        },
        "jobId": job_id,
        "workerTaskId": task_id,
        "totalProcessed": total_processed,
        "avgProcessingMs": avg_processing_ms,
    }
    return json.dumps(payload)


def emit(job_id: str, task_id: str, result: dict) -> None:
    line = build_emf_log_line(
        job_id=job_id,
        task_id=task_id,
        total_processed=result.get("totalProcessed"),
        avg_processing_ms=result.get("avgProcessingMs"),
    )
    print(line, flush=True)
