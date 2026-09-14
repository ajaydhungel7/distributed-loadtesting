from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobResults(BaseModel):
    totalSent: Optional[int] = None
    totalFailed: Optional[int] = None
    durationSeconds: Optional[float] = None
    throughput: Optional[float] = None        # messages/sec
    avgProcessingMs: Optional[float] = None   # avg simulated processing time per message


class CreateJobRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    messageCount: int = Field(..., ge=1, le=1_000_000)
    processingTime: int = Field(default=0, ge=0, le=60000, description="Simulated processing time per message in ms")


class JobRecord(BaseModel):
    jobId: str
    name: str
    messageCount: int
    processingTime: int
    status: JobStatus = JobStatus.PENDING
    createdAt: str
    startedAt: Optional[str] = None
    completedAt: Optional[str] = None
    workerCount: int = 0
    completedWorkers: int = 0
    results: JobResults = JobResults()


class JobListResponse(BaseModel):
    items: list[JobRecord]
    count: int
