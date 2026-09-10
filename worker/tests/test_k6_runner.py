"""Tests for k6 script generation and output parsing."""
import json
import pytest
from k6_runner import build_k6_script, parse_k6_summary


# ── Script generation ─────────────────────────────────────────────────────────

def test_script_contains_target_url():
    script = build_k6_script("https://api.example.com/users", vus=10, duration="30s")
    assert "https://api.example.com/users" in script


def test_script_contains_vu_count():
    script = build_k6_script("https://example.com", vus=50, duration="1m")
    assert "50" in script


def test_script_contains_duration():
    script = build_k6_script("https://example.com", vus=10, duration="2m")
    assert "2m" in script


def test_script_includes_ramp_up_stage_when_provided():
    script = build_k6_script("https://example.com", vus=100, duration="5m", ramp_up="30s")
    assert "stages" in script
    assert "30s" in script


def test_script_uses_simple_options_when_no_ramp_up():
    script = build_k6_script("https://example.com", vus=10, duration="30s")
    # Without ramp-up, use flat vus + duration (simpler than stages)
    assert "vus" in script
    assert "stages" not in script


def test_script_is_valid_javascript():
    script = build_k6_script("https://example.com", vus=10, duration="30s")
    # Must start with an import or export statement (valid k6 JS)
    assert "import" in script or "export" in script


# ── Output parsing ────────────────────────────────────────────────────────────

# k6 --summary-export writes values flat on the metric object (no "values" nesting).
# p50 is reported as "med"; p(99) is omitted when not explicitly requested.
K6_SUMMARY = {
    "metrics": {
        "http_req_duration": {
            "avg": 45.2,
            "med": 40.1,
            "p(95)": 120.5,
            "p(99)": 200.3,
            "min": 10.0,
            "max": 500.0,
        },
        "http_req_failed": {
            "rate": 0.012,
            "passes": 3,
            "fails": 247,
        },
        "http_reqs": {
            "count": 250,
            "rate": 8.33,
        },
        "vus": {
            "value": 0,
            "min": 0,
            "max": 10,
        },
    }
}


def test_parse_extracts_p50():
    # k6 reports median as "med"; parser maps it to p50
    result = parse_k6_summary(K6_SUMMARY)
    assert result["p50"] == pytest.approx(40.1)


def test_parse_extracts_p95():
    result = parse_k6_summary(K6_SUMMARY)
    assert result["p95"] == pytest.approx(120.5)


def test_parse_extracts_p99():
    result = parse_k6_summary(K6_SUMMARY)
    assert result["p99"] == pytest.approx(200.3)


def test_parse_extracts_error_rate():
    result = parse_k6_summary(K6_SUMMARY)
    assert result["errorRate"] == pytest.approx(0.012)


def test_parse_extracts_throughput():
    result = parse_k6_summary(K6_SUMMARY)
    assert result["throughput"] == pytest.approx(8.33)


def test_parse_handles_missing_metrics_gracefully():
    result = parse_k6_summary({"metrics": {}})
    assert result["p50"] is None
    assert result["p95"] is None
    assert result["p99"] is None
    assert result["errorRate"] is None
    assert result["throughput"] is None
