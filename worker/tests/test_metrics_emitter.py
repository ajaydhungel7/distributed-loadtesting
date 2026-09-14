"""Tests for CloudWatch EMF metric emission."""
import json
from metrics_emitter import build_emf_log_line


def test_emf_log_line_is_valid_json():
    line = build_emf_log_line(
        job_id="job-123",
        worker_index=0,
        task_id="task-abc",
        total_sent=1000,
        total_failed=2,
        throughput=500.0,
        duration_seconds=2.0,
        avg_processing_ms=100.0,
    )
    assert isinstance(json.loads(line), dict)


def test_emf_log_line_has_correct_namespace():
    line = build_emf_log_line(
        job_id="job-123", worker_index=0, task_id="task-abc",
        total_sent=1000, total_failed=0, throughput=500.0,
        duration_seconds=2.0, avg_processing_ms=None,
    )
    parsed = json.loads(line)
    namespace = parsed["_aws"]["CloudWatchMetrics"][0]["Namespace"]
    assert namespace == "QueueScaling"


def test_emf_log_line_includes_dimensions():
    line = build_emf_log_line(
        job_id="job-123", worker_index=2, task_id="task-abc",
        total_sent=500, total_failed=0, throughput=250.0,
        duration_seconds=2.0, avg_processing_ms=None,
    )
    parsed = json.loads(line)
    dimensions = parsed["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert ["jobId", "workerIndex"] in dimensions
    assert parsed["jobId"] == "job-123"
    assert parsed["workerIndex"] == "2"


def test_emf_log_line_includes_all_metrics():
    line = build_emf_log_line(
        job_id="job-123", worker_index=0, task_id="task-abc",
        total_sent=1000, total_failed=5, throughput=500.0,
        duration_seconds=2.0, avg_processing_ms=100.0,
    )
    parsed = json.loads(line)
    assert parsed["totalSent"] == 1000
    assert parsed["totalFailed"] == 5
    assert parsed["throughput"] == 500.0
    assert parsed["durationSeconds"] == 2.0
    assert parsed["avgProcessingMs"] == 100.0
