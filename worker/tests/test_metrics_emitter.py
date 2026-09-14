"""Tests for CloudWatch EMF metric emission."""
import json
from metrics_emitter import build_emf_log_line


def test_emf_log_line_is_valid_json():
    line = build_emf_log_line(job_id="job-1", task_id="task-1", total_processed=100, avg_processing_ms=50.0)
    assert isinstance(json.loads(line), dict)


def test_emf_has_correct_namespace():
    line = build_emf_log_line(job_id="job-1", task_id="task-1", total_processed=100, avg_processing_ms=50.0)
    parsed = json.loads(line)
    assert parsed["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "QueueScaling"


def test_emf_has_job_id_dimension():
    line = build_emf_log_line(job_id="job-1", task_id="task-1", total_processed=100, avg_processing_ms=50.0)
    parsed = json.loads(line)
    assert ["jobId"] in parsed["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert parsed["jobId"] == "job-1"


def test_emf_includes_metrics():
    line = build_emf_log_line(job_id="job-1", task_id="task-1", total_processed=500, avg_processing_ms=100.0)
    parsed = json.loads(line)
    assert parsed["totalProcessed"] == 500
    assert parsed["avgProcessingMs"] == 100.0
