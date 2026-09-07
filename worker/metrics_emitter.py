"""
Emits CloudWatch Embedded Metric Format (EMF) log lines.

EMF lets CloudWatch automatically parse structured log lines into metrics
without needing to configure explicit metric filters. Workers print these
lines to stdout; the ECS log driver forwards them to CloudWatch Logs.

Spec: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html
"""
import json
import time
from typing import Optional


def build_emf_log_line(
    *,
    test_id: str,
    task_id: str,
    p50: Optional[float],
    p95: Optional[float],
    p99: Optional[float],
    error_rate: Optional[float],
    throughput: Optional[float],
    active_vus: int,
) -> str:
    """Return a single JSON string in CloudWatch EMF format."""
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "LoadTest",
                    "Dimensions": [["testId", "workerTaskId"]],
                    "Metrics": [
                        {"Name": "p50", "Unit": "Milliseconds"},
                        {"Name": "p95", "Unit": "Milliseconds"},
                        {"Name": "p99", "Unit": "Milliseconds"},
                        {"Name": "errorRate", "Unit": "None"},
                        {"Name": "throughput", "Unit": "Count/Second"},
                        {"Name": "activeVUs", "Unit": "Count"},
                    ],
                }
            ],
        },
        # Dimension values
        "testId": test_id,
        "workerTaskId": task_id,
        # Metric values
        "p50": p50,
        "p95": p95,
        "p99": p99,
        "errorRate": error_rate,
        "throughput": throughput,
        "activeVUs": active_vus,
    }
    return json.dumps(payload)


def emit(test_id: str, task_id: str, metrics: dict) -> None:
    """Print an EMF line to stdout — ECS log driver picks it up."""
    line = build_emf_log_line(
        test_id=test_id,
        task_id=task_id,
        p50=metrics.get("p50"),
        p95=metrics.get("p95"),
        p99=metrics.get("p99"),
        error_rate=metrics.get("errorRate"),
        throughput=metrics.get("throughput"),
        active_vus=metrics.get("activeVUs", 0),
    )
    print(line, flush=True)
