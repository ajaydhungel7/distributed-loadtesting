"""Tests for CloudWatch EMF metric emission."""
import json
import pytest
from metrics_emitter import build_emf_log_line


def test_emf_log_line_is_valid_json():
    line = build_emf_log_line(
        test_id="test-123",
        task_id="task-abc",
        p50=40.0,
        p95=120.0,
        p99=200.0,
        error_rate=0.01,
        throughput=95.3,
        active_vus=10,
    )
    parsed = json.loads(line)
    assert isinstance(parsed, dict)


def test_emf_log_line_contains_aws_metadata():
    line = build_emf_log_line(
        test_id="test-123", task_id="task-abc",
        p50=40.0, p95=120.0, p99=200.0,
        error_rate=0.01, throughput=95.3, active_vus=10,
    )
    parsed = json.loads(line)
    assert "_aws" in parsed
    assert "CloudWatchMetrics" in parsed["_aws"]


def test_emf_log_line_has_correct_namespace():
    line = build_emf_log_line(
        test_id="test-123", task_id="task-abc",
        p50=40.0, p95=120.0, p99=200.0,
        error_rate=0.01, throughput=95.3, active_vus=10,
    )
    parsed = json.loads(line)
    namespace = parsed["_aws"]["CloudWatchMetrics"][0]["Namespace"]
    assert namespace == "LoadTest"


def test_emf_log_line_includes_dimensions():
    line = build_emf_log_line(
        test_id="test-123", task_id="task-abc",
        p50=40.0, p95=120.0, p99=200.0,
        error_rate=0.01, throughput=95.3, active_vus=10,
    )
    parsed = json.loads(line)
    dimensions = parsed["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert ["testId", "workerTaskId"] in dimensions


def test_emf_log_line_includes_all_metrics():
    line = build_emf_log_line(
        test_id="test-123", task_id="task-abc",
        p50=40.0, p95=120.0, p99=200.0,
        error_rate=0.01, throughput=95.3, active_vus=10,
    )
    parsed = json.loads(line)
    assert parsed["p50"] == 40.0
    assert parsed["p95"] == 120.0
    assert parsed["p99"] == 200.0
    assert parsed["errorRate"] == 0.01
    assert parsed["throughput"] == 95.3
    assert parsed["activeVUs"] == 10


def test_emf_log_line_includes_dimension_values():
    line = build_emf_log_line(
        test_id="test-123", task_id="task-abc",
        p50=40.0, p95=120.0, p99=200.0,
        error_rate=0.01, throughput=95.3, active_vus=10,
    )
    parsed = json.loads(line)
    assert parsed["testId"] == "test-123"
    assert parsed["workerTaskId"] == "task-abc"
