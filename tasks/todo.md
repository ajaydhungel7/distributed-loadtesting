# Load Testing Platform — Task Checklist

> Status: `[ ]` Not started | `[~]` In progress | `[x]` Done

---

## SLICE 1 — Infrastructure Foundation
> Checkpoint A: All AWS resources exist and are correctly configured

- [ ] Initialize CDK project (`infra/`) with TypeScript
- [ ] `InfraStack`: VPC (2 AZs, public + private subnets, NAT Gateway)
- [ ] `InfraStack`: ECR repos — `control-plane`, `worker`
- [ ] `InfraStack`: DynamoDB table `load-tests` (PK: testId, SK: timestamp; GSI on status)
- [ ] `InfraStack`: S3 bucket `loadtest-results` (versioning on, lifecycle to IA at 30d, block public access, SSE)
- [ ] `InfraStack`: SQS queue `loadtest-jobs` + DLQ `loadtest-jobs-dlq` (maxReceiveCount=3, SSE)
- [ ] `InfraStack`: IAM role `ControlPlaneTaskRole` (least-privilege)
- [ ] `InfraStack`: IAM role `WorkerTaskRole` (least-privilege)
- [ ] `cdk deploy InfraStack` succeeds in target account
- [ ] **CHECKPOINT A** — verify all resources in console

---

## SLICE 2 — Control Plane API
> Runnable API that accepts, stores, and queues test jobs

- [ ] Scaffold FastAPI app (`control-plane/app/`)
- [ ] Data models: `TestJob` Pydantic schema (validation incl. URL format)
- [ ] DynamoDB service: `put_item`, `get_item`, `query`, `update_item`
- [ ] SQS service: `send_message`
- [ ] Route: `POST /tests` → validate → DynamoDB write (PENDING) → SQS publish → return 202
- [ ] Route: `GET /tests/{id}` → DynamoDB fetch → return full record
- [ ] Route: `GET /tests` → paginated list with optional status filter
- [ ] Route: `DELETE /tests/{id}` → update status=CANCELLED, remove from SQS if visible
- [ ] Health check: `GET /health` → 200 OK
- [ ] Dockerfile for control plane (multi-stage build)
- [ ] `ControlPlaneStack`: ECS cluster, task definition, Fargate service, ALB, security groups
- [ ] Push control-plane image to ECR, deploy service
- [ ] Unit tests (`control-plane/tests/`) for routes and services
- [ ] `POST /tests` returns `{"testId":"...","status":"PENDING"}` via ALB

---

## SLICE 3 — Worker Container (k6)
> Worker reads a job, runs k6, writes results — triggered manually

- [ ] Scaffold worker app (`worker/`)
- [ ] SQS consumer: `receive_message` with visibility heartbeat (ExtendVisibilityTimeout every 2 min)
- [ ] DynamoDB updater: set status=RUNNING on job start, COMPLETED/FAILED on finish
- [ ] k6 script generator: build `.js` script from job config (targetUrl, VUs, duration, rampUp)
- [ ] k6 runner: exec k6 binary, capture JSON summary output
- [ ] Result parser: extract p50, p95, p99, errorRate, throughput from k6 JSON
- [ ] S3 writer: upload raw k6 JSON output to `s3://loadtest-results/{testId}/raw.json`
- [ ] DynamoDB result writer: update `results` field with summary stats
- [ ] CloudWatch EMF emitter: emit structured metrics every 15s during test
- [ ] Dockerfile (`worker/Dockerfile`): base `grafana/k6`, add Python, pin versions
- [ ] `WorkerStack`: ECS task definition for workers, CloudWatch log group
- [ ] Manual trigger test: run worker task → job transitions PENDING→RUNNING→COMPLETED
- [ ] Unit tests for k6 script generator and result parser
- [ ] **CHECKPOINT B** — full end-to-end manual flow verified

---

## SLICE 4 — Autoscaling
> Workers spawn and drain automatically based on SQS queue depth

- [ ] `AutoscalingStack`: Application Auto Scaling target for ECS worker service
- [ ] Scale-out step policy: SQS `ApproximateNumberOfMessagesVisible` triggers worker launch
- [ ] Scale-in policy: queue empty 5 min → scale to 0
- [ ] CloudWatch alarm: `LoadTestWorkerQueueDepth`
- [ ] Max cap: 50 worker tasks
- [ ] Integration test: submit 3 jobs → observe workers auto-spawn → all complete → scale to 0
- [ ] Verify no orphaned tasks or stuck RUNNING jobs after queue empties
- [ ] **CHECKPOINT C** — fully automated, no manual steps

---

## SLICE 5 — Real-Time Metrics & Grafana Dashboard
> Live metrics visible in Grafana within 30s of test start

- [ ] CloudWatch EMF metric schema defined and implemented in worker
  - `LoadTest/ResponseTime` (dimensions: testId, workerTaskId, percentile)
  - `LoadTest/Throughput` (req/sec)
  - `LoadTest/ErrorRate` (%)
  - `LoadTest/ActiveVUs`
- [ ] CloudWatch metric filters (CDK) to parse EMF logs → named metrics
- [ ] `GrafanaStack`: Grafana on ECS Fargate (or EC2), admin password in Secrets Manager
- [ ] Grafana datasource: CloudWatch configured and provisioned
- [ ] Grafana dashboard `grafana/dashboards/loadtest.json`:
  - [ ] Real-time response time graph (p50/p95/p99)
  - [ ] Throughput graph (req/sec)
  - [ ] Error rate graph (%)
  - [ ] Active VU count panel
  - [ ] Test status panel
  - [ ] Recent tests table
- [ ] Dashboard auto-provisioned via Grafana provisioning config
- [ ] Metrics appear within 30s of test start
- [ ] **CHECKPOINT D (partial)** — observable platform

---

## SLICE 6 — Security Hardening
> Production-safe: encrypted, least-privilege, SSRF-protected

- [ ] All secrets in AWS Secrets Manager (Grafana password, any API keys)
- [ ] SSRF protection: validate `targetUrl` does not resolve to RFC-1918 / link-local ranges
- [ ] Confirm WorkerTaskRole permissions are minimal (no wildcard actions)
- [ ] Confirm ControlPlaneTaskRole permissions are minimal
- [ ] DynamoDB encryption at rest (default AWS-owned key or CMK)
- [ ] SQS SSE enabled (already done in Slice 1 — verify)
- [ ] S3 block public access + SSE (already done in Slice 1 — verify)
- [ ] CloudTrail enabled for the account (if not already)
- [ ] Rate limiting on `POST /tests` (middleware or ALB WAF rule: 20 req/min per IP)
- [ ] Input validation: VU count ≤ 10000, duration ≤ 1h, name length ≤ 100 chars
- [ ] Test SSRF endpoint → confirm 400 response
- [ ] IAM Access Analyzer scan — confirm no unintended external access
- [ ] **CHECKPOINT D (complete)** — hardened and production-ready

---

## SLICE 7 — CI/CD & Developer Experience
> Push code → test → build → deploy automatically

- [ ] `Makefile` targets: `dev`, `test`, `build`, `deploy`, `destroy`
- [ ] `docker-compose.yml`: control-plane + localstack + dynamodb-local for local dev
- [ ] GitHub Actions `ci.yml`: lint (ruff/eslint), unit tests, docker build on PRs
- [ ] GitHub Actions `deploy.yml`: ECR push + `cdk deploy` on merge to `main`
- [ ] GitHub secrets configured: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`
- [ ] `README.md`: quickstart, architecture diagram (ASCII), API reference, environment variables
- [ ] `make dev` brings up local environment in < 2 minutes
- [ ] PR CI green end-to-end

---

## Completed Milestones

- [ ] **Checkpoint A** — Infrastructure up, all AWS resources verified
- [ ] **Checkpoint B** — End-to-end manual flow (submit → worker processes → results returned)
- [ ] **Checkpoint C** — Fully automated (submit → autoscale → process → results → scale-down)
- [ ] **Checkpoint D** — Observable + Hardened (Grafana live metrics, security controls)
