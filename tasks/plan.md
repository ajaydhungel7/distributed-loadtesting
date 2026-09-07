# Distributed Load Testing Platform — Implementation Plan

## Overview

A cloud-native, distributed load testing platform on AWS. Users submit test jobs via a REST API; the platform spins up containerized k6 workers on ECS Fargate, auto-scales based on demand, streams real-time metrics to Grafana, and aggregates results in DynamoDB and S3.

---

## Architecture

```
User / CI
  │
  ▼
ALB ──► Control Plane (ECS Fargate, Python FastAPI)
  │         │
  │         ├──► DynamoDB  (job metadata, status, summary results)
  │         ├──► SQS       (job queue)
  │         └──► S3        (raw results, test scripts)
  │
SQS Queue Depth
  │
  ▼
EventBridge → Application Auto Scaling
  │
  ▼
Worker Pool (ECS Fargate, k6)
  │         │
  │         ├──► Target URL (generates HTTP load)
  │         ├──► CloudWatch Logs (EMF metrics)
  │         ├──► DynamoDB  (result writes)
  │         └──► S3        (raw result files)
  │
  ▼
CloudWatch Metrics
  ▼
Grafana Dashboard
```

### Key AWS Services

| Service | Role |
|---------|------|
| ECS Fargate | Control plane + worker containers |
| ALB | Ingress for control plane API |
| SQS | Durable job queue, triggers autoscaling |
| DynamoDB | Job metadata, status, summary results |
| S3 | Raw result storage, test scripts |
| ECR | Docker image registry |
| CloudWatch Logs/Metrics | Worker metrics via EMF format |
| Application Auto Scaling | Scale ECS worker service by SQS queue depth |
| VPC | Network isolation, private/public subnets |
| IAM | Service roles, least-privilege permissions |
| AWS CDK (TypeScript) | Infrastructure-as-Code |
| Grafana | Real-time + historical dashboards |

### Load Generator: k6
- Go binary, minimal memory footprint
- JavaScript test scripts
- Rich built-in metrics (p50/p95/p99, error rate, throughput)
- Outputs structured logs parseable by CloudWatch EMF

---

## Vertical Slices (Tasks)

Work is organized so each task delivers an end-to-end, runnable slice — not a horizontal layer.

---

### SLICE 1 — Infrastructure Foundation
**Goal**: VPC, ECR, DynamoDB, S3, SQS, IAM roles, CDK bootstrap.
Everything downstream depends on this.

**Deliverables**:
- CDK stack: VPC (2 AZs, public + private subnets, NAT Gateway)
- ECR repositories: `control-plane`, `worker`
- DynamoDB table: `load-tests` (PK: `testId`, SK: `timestamp`; GSI on `status`)
- S3 bucket: `loadtest-results-{account}-{region}` (versioning on, lifecycle to IA after 30d)
- SQS queue: `loadtest-jobs` + dead-letter queue (`loadtest-jobs-dlq`, maxReceiveCount=3)
- IAM roles: `ControlPlaneTaskRole`, `WorkerTaskRole` (least-privilege)
- Outputs: ARNs/URLs exported as CDK stack outputs

**Acceptance Criteria**:
- `cdk deploy InfraStack` succeeds without errors
- DynamoDB table, SQS queue, S3 bucket, ECR repos visible in console
- IAM roles have correct trust policies for ECS tasks

**Verification**:
```bash
cdk deploy InfraStack
aws sqs list-queues --query 'QueueUrls[?contains(@, `loadtest`)]'
aws dynamodb describe-table --table-name load-tests
```

---

### SLICE 2 — Control Plane API (Core)
**Goal**: A working REST API that accepts test jobs, stores metadata, and enqueues them.
Workers can be invoked manually; no autoscaling yet.

**Deliverables**:
- FastAPI application (`control-plane/`)
- `POST /tests` — validate config, write DynamoDB record (status=PENDING), publish to SQS
- `GET /tests/{id}` — retrieve job status + summary from DynamoDB
- `GET /tests` — list all tests (paginated, filter by status)
- `DELETE /tests/{id}` — cancel a pending or running test
- Dockerfile for control plane
- ECS task definition + Fargate service (ALB-backed)
- CDK stack: `ControlPlaneStack`

**Data Model**:
```json
{
  "testId": "uuid",
  "name": "string",
  "targetUrl": "string",
  "virtualUsers": 100,
  "duration": "5m",
  "rampUp": "30s",
  "status": "PENDING | RUNNING | COMPLETED | FAILED | CANCELLED",
  "createdAt": "ISO8601",
  "startedAt": "ISO8601",
  "completedAt": "ISO8601",
  "workerCount": 0,
  "results": { "p50": 0, "p95": 0, "p99": 0, "errorRate": 0, "throughput": 0 }
}
```

**Acceptance Criteria**:
- `POST /tests` returns `202 Accepted` with `testId`
- SQS message appears in queue with correct payload
- `GET /tests/{id}` returns full record with status=PENDING
- Service is reachable via ALB DNS name

**Verification**:
```bash
curl -X POST https://{alb-dns}/tests -d '{"name":"smoke","targetUrl":"http://example.com","virtualUsers":10,"duration":"30s"}'
# Returns {"testId":"...","status":"PENDING"}
curl https://{alb-dns}/tests/{id}
# Returns full record
```

---

### SLICE 3 — Worker Container (k6)
**Goal**: A Docker container that reads a test job from SQS, runs k6, and writes results.
No autoscaling yet — worker is triggered manually.

**Deliverables**:
- Worker application (`worker/`)
- On start: pull SQS message, update DynamoDB status=RUNNING
- Generate k6 script from job config (target URL, VUs, duration, ramp-up)
- Execute k6 binary; stream JSON output
- Parse k6 output: extract p50/p95/p99 latencies, error rate, throughput
- Write summary to DynamoDB, raw JSON to S3
- Update DynamoDB status=COMPLETED (or FAILED)
- Delete SQS message on success; leave for DLQ on repeated failure
- Dockerfile (`worker/Dockerfile` — base: `grafana/k6`, add Python shim)
- ECS task definition for workers (CDK: `WorkerStack`)
- CloudWatch log group `/loadtest/workers`

**Acceptance Criteria**:
- Running worker task manually processes a PENDING job end-to-end
- `GET /tests/{id}` returns status=COMPLETED with p50/p95/p99/errorRate/throughput populated
- Raw results file visible in S3
- CloudWatch Logs show structured k6 output

**Verification**:
```bash
# Manually trigger worker task
aws ecs run-task --cluster loadtest --task-definition worker --launch-type FARGATE ...
# Poll status
curl https://{alb-dns}/tests/{id}
# {"status":"COMPLETED","results":{"p50":42,"p95":120,"p99":200,"errorRate":0.01,"throughput":95.3}}
aws s3 ls s3://loadtest-results-.../results/{testId}/
```

---

### SLICE 4 — Autoscaling (Queue-Depth Triggered)
**Goal**: Workers scale in/out automatically based on SQS queue depth.
Zero manual task invocations needed after this slice.

**Deliverables**:
- Application Auto Scaling target: ECS worker service
- Scale-out policy: `ApproximateNumberOfMessagesVisible > 0` → scale to `min(queue_depth * 2, 50)`
- Scale-in policy: queue empty for 5 minutes → scale to 0
- Step-scaling configuration (CDK: `AutoscalingStack`)
- CloudWatch alarm: `LoadTestWorkerQueueDepth`
- Integration test: submit 3 jobs, confirm workers auto-spawn, process all jobs, scale to 0

**Scaling Table**:
| Queue Messages | Target Workers |
|---------------|----------------|
| 0 | 0 |
| 1–5 | 5 |
| 6–10 | 10 |
| 11–20 | 20 |
| 21+ | 50 (max) |

**Acceptance Criteria**:
- After submitting a test, worker task spawns within 90 seconds
- Multiple jobs run concurrently (one job → one worker task)
- After all jobs complete, workers scale to 0 within 5 minutes
- No failed/orphaned tasks

**Verification**:
```bash
# Submit 3 jobs in quick succession
for i in 1 2 3; do curl -X POST https://{alb}/tests -d '{...}'; done
# Observe in ECS console: runningCount goes from 0 → 3 → 0
aws ecs describe-services --cluster loadtest --services worker --query 'services[0].runningCount'
```

---

### SLICE 5 — Real-Time Metrics & Grafana Dashboard
**Goal**: Live metrics visible in Grafana while a test is running.

**Deliverables**:
- Workers emit CloudWatch EMF metrics (embedded in log lines):
  - `LoadTest/ResponseTime` (p50, p95, p99) — every 15s
  - `LoadTest/Throughput` (req/sec)
  - `LoadTest/ErrorRate` (%)
  - `LoadTest/ActiveVUs`
  - Dimensions: `testId`, `workerTaskId`
- CloudWatch metric filters (CDK) to parse EMF logs into named metrics
- Grafana provisioned with CloudWatch datasource
- Pre-built Grafana dashboard (`grafana/dashboards/loadtest.json`):
  - Real-time response time graph (p50/p95/p99)
  - Throughput graph (req/sec)
  - Error rate graph (%)
  - Active VU count
  - Test status panel (RUNNING / COMPLETED)
  - Recent tests table (via DynamoDB API backend)
- Grafana deployed on ECS Fargate (CDK: `GrafanaStack`) or EC2

**Acceptance Criteria**:
- Within 30 seconds of job start, metrics appear in Grafana
- p50/p95/p99 panels update every 15 seconds during test
- Completed test's metrics remain queryable after test ends

**Verification**:
```bash
# Submit test → open Grafana dashboard → select testId → see live metrics populate
```

---

### SLICE 6 — Security Hardening & Production Readiness
**Goal**: Platform is safe to run against external targets; no over-permissive IAM; secrets in Secrets Manager.

**Deliverables**:
- All secrets (Grafana admin password, etc.) in AWS Secrets Manager
- No hardcoded credentials anywhere
- Worker task role: only `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `s3:PutObject`, `logs:*`
- Control plane task role: only `sqs:SendMessage`, `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:Query`, `dynamodb:Scan`
- SQS queue has server-side encryption (SSE)
- S3 bucket blocks all public access; server-side encryption enabled
- DynamoDB encryption at rest enabled
- VPC: workers in private subnets (no direct internet exposure)
- Rate limiting on control plane API (ALB WAF or middleware)
- Input validation: reject targetUrls that resolve to private IP ranges (SSRF prevention)
- CloudTrail enabled for API calls

**Acceptance Criteria**:
- IAM Access Analyzer reports no external access on task roles
- Attempting to target `http://169.254.169.254` (IMDS) returns 400 error
- All data at rest is encrypted
- Secrets are retrieved from Secrets Manager, not env vars

**Verification**:
```bash
# SSRF test
curl -X POST https://{alb}/tests -d '{"targetUrl":"http://169.254.169.254/latest/meta-data/",...}'
# → 400 Bad Request: "targetUrl resolves to a private or reserved IP range"
aws iam get-role-policy --role-name WorkerTaskRole  # verify limited actions only
```

---

### SLICE 7 — CI/CD Pipeline & Developer Experience
**Goal**: Push code → tests run → Docker images build → deploy automatically.

**Deliverables**:
- GitHub Actions workflows:
  - `ci.yml`: lint, unit tests, Docker build on every PR
  - `deploy.yml`: build + push to ECR + `cdk deploy` on merge to `main`
- `docker-compose.yml` for local development (control plane + localstack + dynamodb-local)
- `Makefile` with common commands: `make dev`, `make test`, `make deploy`
- `README.md` with quickstart, architecture diagram, API reference

**Acceptance Criteria**:
- `make dev` brings up local environment in < 2 minutes
- PR CI passes (lint + unit tests + docker build)
- Merge to main triggers automatic deploy to AWS

---

## Checkpoints

### Checkpoint A — Infrastructure Up (after Slice 1)
Manual verification that all AWS resources exist and are correctly configured. No application code yet.

### Checkpoint B — End-to-End Manual Flow (after Slice 3)
A complete test job can be submitted, processed by a manually-triggered worker, and results retrieved. No autoscaling — this confirms the core data flow works before adding complexity.

### Checkpoint C — Fully Automated (after Slice 4)
Submit a job; platform handles everything. No manual steps. Core product is functional.

### Checkpoint D — Observable + Hardened (after Slice 6)
Platform is production-ready: real-time metrics, security controls, encrypted storage.

---

## Directory Structure

```
/
├── infra/                    # CDK stacks (TypeScript)
│   ├── bin/app.ts
│   ├── lib/
│   │   ├── infra-stack.ts    # VPC, DynamoDB, SQS, S3, ECR, IAM
│   │   ├── control-plane-stack.ts
│   │   ├── worker-stack.ts
│   │   ├── autoscaling-stack.ts
│   │   └── grafana-stack.ts
│   ├── cdk.json
│   └── package.json
│
├── control-plane/            # FastAPI application
│   ├── app/
│   │   ├── main.py
│   │   ├── routes/
│   │   │   └── tests.py
│   │   ├── models/
│   │   │   └── test_job.py
│   │   └── services/
│   │       ├── dynamodb.py
│   │       └── sqs.py
│   ├── Dockerfile
│   ├── requirements.txt
│   └── tests/
│
├── worker/                   # k6 worker shim
│   ├── main.py               # SQS poll, job dispatch, result write
│   ├── k6_runner.py          # k6 script generation + execution
│   ├── metrics_emitter.py    # CloudWatch EMF output
│   ├── Dockerfile
│   ├── requirements.txt
│   └── tests/
│
├── grafana/
│   └── dashboards/
│       └── loadtest.json
│
├── .github/
│   └── workflows/
│       ├── ci.yml
│       └── deploy.yml
│
├── Makefile
└── README.md
```

---

## Risk Register

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| k6 output format changes | Low | Pin k6 version in Dockerfile; unit test parser |
| SQS message visibility timeout expires (long tests) | Medium | Worker sends SQS heartbeat (ExtendVisibilityTimeout) every 2 min |
| Workers scale up too aggressively (cost) | Medium | Hard max=50 tasks; budget alarm in CloudWatch |
| SSRF via targetUrl | High | Validate URL resolves to public IP before running test |
| Fargate cold start delays test start | Low | Accept 30-60s warm-up; document in SLA |
| CloudWatch metric ingestion lag | Low | Add 30s buffer when correlating real-time metrics |

---

## Dependencies

- Slice 1 must complete before any other slice
- Slices 2 and 3 can be developed in parallel after Slice 1
- Slice 4 requires Slices 2 + 3 complete
- Slice 5 requires Slices 2 + 3 complete (can parallel with 4)
- Slice 6 can begin in parallel with Slices 4 + 5
- Slice 7 can begin any time after Slice 2

```
Slice 1 ──► Slice 2 ──┬──► Slice 4 ──► Checkpoint C
                      │
           Slice 3 ──┘
                      │
                      └──► Slice 5
                      │
                      └──► Slice 6
```
