"""
Generates k6 JavaScript test scripts from job config and parses k6 JSON summary output.
"""
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def build_k6_script(target_url: str, vus: int, duration: str, ramp_up: Optional[str] = None) -> str:
    """Return a k6 JavaScript script as a string."""
    if ramp_up:
        # Ramp up to full VUs over ramp_up period, hold for remaining duration, then ramp down
        options = f"""export const options = {{
  stages: [
    {{ duration: '{ramp_up}', target: {vus} }},
    {{ duration: '{duration}', target: {vus} }},
    {{ duration: '10s', target: 0 }},
  ],
}};"""
    else:
        options = f"""export const options = {{
  vus: {vus},
  duration: '{duration}',
}};"""

    return f"""import http from 'k6/http';
import {{ sleep, check }} from 'k6';

{options}

export default function () {{
  const res = http.get('{target_url}');
  check(res, {{
    'status 2xx': (r) => r.status >= 200 && r.status < 300,
  }});
  sleep(1);
}}
"""


def run_k6(script: str) -> dict:
    """
    Write script to a temp file, run k6, return parsed JSON summary.
    Raises subprocess.CalledProcessError on non-zero exit.
    """
    with tempfile.NamedTemporaryFile(suffix=".js", mode="w", delete=False) as f:
        f.write(script)
        script_path = f.name

    summary_path = Path(script_path).with_suffix(".json")

    subprocess.run(
        [
            "k6", "run",
            "--summary-export", str(summary_path),
            "--quiet",
            script_path,
        ],
        check=True,
    )

    with open(summary_path) as f:
        return json.load(f)


def parse_k6_summary(summary: dict) -> dict:
    """Extract key metrics from a k6 JSON summary."""
    metrics = summary.get("metrics", {})

    def _get(metric_name: str, *keys: str):
        m = metrics.get(metric_name, {})
        values = m.get("values", {})
        for key in keys:
            if key in values:
                return values[key]
        return None

    return {
        "p50": _get("http_req_duration", "p(50)"),
        "p95": _get("http_req_duration", "p(95)"),
        "p99": _get("http_req_duration", "p(99)"),
        "errorRate": _get("http_req_failed", "rate"),
        "throughput": _get("http_reqs", "rate"),
    }
