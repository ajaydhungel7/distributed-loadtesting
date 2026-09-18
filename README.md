# Queue-Based Autoscaling

A distributed job processing platform on AWS that demonstrates queue-driven autoscaling. Submit a job via the control plane API — workers automatically scale up on ECS Fargate based on SQS queue depth, process all messages, then scale back to zero.

## Architecture

```
User → Control Plane API (ALB → ECS Fargate)
           │
           └── SQS: loadtest-target (job messages)
                │
                ├── CloudWatch: ApproximateNumberOfMessagesVisible
                │       │
                │       └── Application Auto Scaling (target tracking)
                │               backlog per task = messages / running workers
                │               target: 500 msgs/worker → scales 0 to 100
                │
                └── Worker Tasks (ECS Fargate, 0 at idle)
                        │
                        ├── SQS long-poll (20s) → process → delete
                        ├── DynamoDB atomic counter (processedCount)
                        ├── ECS task scale-in protection (mid-message safety)
                        └── Exit after 3 consecutive empty polls (~60s idle)

DynamoDB: job metadata + progress tracking
S3:       results storage
Grafana:  CloudWatch datasource — queue depth, workers, throughput, lag
```

**Flow:**
1. `POST /jobs` → stores job in DynamoDB, floods `loadtest-target` queue with N messages
2. CloudWatch alarm fires when queue depth ≥ 1 → bootstrap worker starts
3. Target tracking policy scales workers to `ceil(messages / 500)`
4. Each worker loops: poll → protect → process → unprotect → repeat
5. Workers exit after 3 empty polls; scale-in cooldown (5 min) brings desired to 0
6. Last worker to finish atomically marks job `COMPLETED`

## Repository Structure

```
.
├── infra/                        # AWS CDK (TypeScript) — single stack
│   ├── bin/infra.ts              # App entry point
│   ├── lib/load-test-stack.ts    # All resources in one stack (no cross-stack deps)
│   └── test/load-test-stack.test.ts
│
├── control-plane/                # FastAPI (Python)
│   ├── app/
│   │   ├── main.py
│   │   ├── routes/tests.py       # POST /jobs, GET /jobs/:id
│   │   ├── models/test_job.py    # CreateJobRequest, JobRecord
│   │   └── services/             # DynamoDB + SQS clients
│   ├── Dockerfile
│   └── tests/
│
├── worker/                       # Python worker
│   ├── main.py                   # SQS polling loop + job lifecycle
│   ├── metrics_emitter.py        # CloudWatch EMF
│   ├── Dockerfile
│   └── tests/
│
├── grafana/                      # Custom Grafana image
│   ├── Dockerfile
│   ├── provisioning/
│   │   ├── datasources/cloudwatch.yaml
│   │   └── dashboards/dashboards.yaml
│   └── dashboards/loadtest.json  # Pre-built system dashboard
│
└── .github/workflows/
    └── deploy.yml                # CI + deploy in one workflow
```

## Infrastructure (Single CDK Stack)

Everything lives in `LoadTestStack` — one stack eliminates cross-stack export deadlocks and simplifies deploys.

| Resource | Details |
|----------|---------|
| VPC | 2 AZs, 1 NAT gateway, public + private subnets |
| ECR | 3 repos: `loadtest/control-plane`, `loadtest/worker`, `loadtest/grafana` |
| DynamoDB | `queue-jobs` table — `jobId` (PK), `createdAt` (SK) |
| SQS | `loadtest-target` queue + `loadtest-jobs-dlq` (maxReceiveCount=3) |
| S3 | Results bucket — versioned, private |
| ECS | Single cluster, 3 Fargate services (CP + worker + Grafana) |
| Autoscaling | Target tracking: 500 msgs/worker, scale-out cooldown 60s, scale-in 300s |
| Grafana | Admin password in Secrets Manager (`loadtest/grafana-admin-password`) |
| OIDC | `GitHubActionsDeployRole` — no long-lived AWS keys |

## API

Base URL: the `ControlPlaneUrl` output from `LoadTestStack`.

### Create a job
```bash
POST /jobs
Content-Type: application/json

{
  "name": "my-test",
  "messageCount": 1000,
  "processingTime": 500
}
```
- `messageCount` — number of messages to flood into the queue (1–1,000,000)
- `processingTime` — simulated processing time per message in ms (default 0)

Response:
```json
{ "jobId": "01M2TVK22DSS3PFYD1N43Z0PTB", "status": "RUNNING" }
```

### Get job status
```bash
GET /jobs/{jobId}
```

```json
{
  "jobId": "01M2TVK22DSS3PFYD1N43Z0PTB",
  "name": "my-test",
  "status": "COMPLETED",
  "messageCount": 1000,
  "processedCount": 1000,
  "processingTime": 500,
  "createdAt": "2026-09-18T17:38:00Z",
  "completedAt": "2026-09-18T17:43:12Z",
  "results": {
    "totalProcessed": 1000,
    "avgProcessingMs": 500.0
  }
}
```

**Statuses:** `RUNNING → COMPLETED | FAILED`

### Health check
```bash
GET /health
→ { "status": "ok" }
```

## Autoscaling Behaviour

Workers scale using **target tracking on backlog-per-task**:

```
backlog per task = ApproximateNumberOfMessagesVisible / RunningTaskCount
target           = 500
```

| Messages | Workers |
|----------|---------|
| 500 | 1 |
| 1,000 | 2 |
| 5,000 | 10 |
| 50,000 | 100 (max) |

- **Scale-out cooldown:** 60s — reacts fast to new jobs
- **Scale-in cooldown:** 300s — conservative, avoids thrashing
- **Bootstrap alarm:** bumps desired 0→1 on first message so Container Insights starts publishing `RunningTaskCount`
- **Task scale-in protection:** workers self-report safe-to-stop via ECS agent endpoint

## Grafana Dashboard

URL: the `GrafanaUrl` output from `LoadTestStack`.
Login: `admin` / password from Secrets Manager (`loadtest/grafana-admin-password`).

Panels:
- **At a Glance** — queue depth, running workers, DLQ depth, consumer lag (stat tiles)
- **Queue** — target queue depth + in-flight messages; oldest message age
- **Workers** — running task count; published vs processed message rate
- **Health** — DLQ depth over time; jobs queue depth

## CI/CD Pipeline

Single workflow: `.github/workflows/deploy.yml`

**On pull request:** runs tests only (CDK Jest + Python pytest for all services).

**On push to main:**
1. Tests
2. AWS OIDC auth (4h session — no stored keys)
3. **Bootstrap** — if ECR repos don't exist yet, deploy CDK first to create them
4. **Detect changes** — git diff to find which of `control-plane/`, `worker/`, `grafana/` changed; if ECR is empty, rebuild all
5. **Build & push** images for changed services (tagged with commit SHA + `latest`)
6. **CDK deploy** `LoadTestStack` with per-service image tags via context
7. **Scale up** — set control-plane and Grafana to `desiredCount=1` (worker stays at 0, managed by autoscaling)

CDK keeps all ECS services at `desiredCount=0` so CloudFormation always stabilizes successfully. The pipeline scales up after confirming images exist in ECR.

## Local Development

### Prerequisites
- Node.js 20+, Python 3.12+
- AWS CLI configured
- CDK CLI: `npm install -g aws-cdk`

### Run tests locally
```bash
# CDK
cd infra && npm ci && npm test

# Control plane
cd control-plane && pip install -r requirements-dev.txt && pytest tests/ -v

# Worker
cd worker && pip install -r requirements-dev.txt && pytest tests/ -v
```

### Infrastructure diff
```bash
cd infra && npx cdk diff
```

## First-Time Setup

### 1. Bootstrap CDK
```bash
cd infra
npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
```

### 2. GitHub secret
Add `AWS_ACCOUNT_ID` to your repository secrets.

### 3. Push to main
The pipeline handles everything: creates ECR repos, builds images, deploys the full stack, scales up services.

### 4. Get endpoints
```bash
aws cloudformation describe-stacks --stack-name LoadTestStack --region us-east-1 \
  --query 'Stacks[0].Outputs[*].{Key:OutputKey,Value:OutputValue}' --output table
```
