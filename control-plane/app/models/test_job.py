import ipaddress
import re
import socket
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, HttpUrl, model_validator


# Valid duration format: number followed by s, m, or h (e.g. 30s, 5m, 1h)
_DURATION_RE = re.compile(r"^\d+[smh]$")

# RFC-1918 + link-local + loopback ranges that must never be load-tested
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local (IMDS)
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 ULA
]


def _is_private_host(host: str) -> bool:
    """Return True if `host` resolves to a private/reserved IP address."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:
            addr = ipaddress.ip_address(socket.gethostbyname(host))
        except (socket.gaierror, ValueError):
            return False
    return any(addr in net for net in _PRIVATE_NETWORKS)


class TestStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TestResults(BaseModel):
    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    errorRate: Optional[float] = None
    throughput: Optional[float] = None


class CreateTestRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    targetUrl: str
    virtualUsers: int = Field(..., ge=1, le=10000)
    duration: str = Field(..., description="e.g. 30s, 5m, 1h")
    rampUp: Optional[str] = Field(None, description="e.g. 30s")

    @field_validator("targetUrl")
    @classmethod
    def validate_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("targetUrl must be a valid http or https URL")
        return v

    @field_validator("duration", "rampUp", mode="before")
    @classmethod
    def validate_duration(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _DURATION_RE.match(v):
            raise ValueError("Duration must match pattern: <number><s|m|h> (e.g. 30s, 5m, 1h)")
        return v

    # SSRF check is enforced at the route level (returns HTTP 400, not 422)


class TestJob(BaseModel):
    testId: str
    name: str
    targetUrl: str
    virtualUsers: int
    duration: str
    rampUp: Optional[str] = None
    status: TestStatus = TestStatus.PENDING
    createdAt: str
    startedAt: Optional[str] = None
    completedAt: Optional[str] = None
    workerCount: int = 0
    results: TestResults = TestResults()


class TestListResponse(BaseModel):
    items: list[TestJob]
    count: int
