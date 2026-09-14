"""
Emits CloudWatch Embedded Metric Format (EMF) log lines.

EMF lets CloudWatch automatically parse structured log lines into metrics
without needing to configure explicit metric filters. Workers print these
lines to stdout; the ECS log driver forwards them to CloudWatch Logs.
"""
import json
import time
from typing import Optional


def build_emf_log_line(
    *,
    job_id: str,
    worker_index: int,
    task_id: str,
    total_sent: Optional[int],
    total_failed: Optional[int],
    throughput: Optional[float],
    duration_seconds: Optional[float],
    avg_processing_ms: Optional[float],
) -> str:
    """Return a single JSON string in CloudWatch EMF format."""
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "QueueScaling",
                    "Dimensions": [["jobId", "workerIndex"]],
                    "Metrics": [
                        {"Name": "totalSent", "Unit": "Count"},
                        {"Name": "totalFailed", "Unit": "Count"},
                        {"Name": "throughput", "Unit": "Count/Second"},
                        {"Name": "durationSeconds", "Unit": "Seconds"},
                        {"Name": "avgProcessingMs", "Unit": "Milliseconds"},
                    ],
                }
            ],
        },
        # Dimensions
        "jobId": job_id,
        "workerIndex": str(worker_index),
        # Metric values
        "totalSent": total_sent,
        "totalFailed": total_failed,
        "throughput": throughput,
        "durationSeconds": duration_seconds,
        "avgProcessingMs": avg_processing_ms,
        # Pass-through for context (not a metric)
        "workerTaskId": task_id,
    }
    return json.dumps(payload)


def emit(job_id: str, worker_index: int, task_id: str, result: dict) -> None:
    """Print an EMF line to stdout — ECS log driver picks it up."""
    line = build_emf_log_line(
        job_id=job_id,
        worker_index=worker_index,
        task_id=task_id,
        total_sent=result.get("totalSent"),
        total_failed=result.get("totalFailed"),
        throughput=result.get("throughput"),
        duration_seconds=result.get("durationSeconds"),
        avg_processing_ms=result.get("avgProcessingMs"),
    )
    print(line, flush=True)
