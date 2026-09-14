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
    totalProcessed: Optional[int] = None
    avgProcessingMs: Optional[float] = None


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
    processedCount: int = 0
    results: JobResults = JobResults()


class JobListResponse(BaseModel):
    items: list[JobRecord]
    count: int
