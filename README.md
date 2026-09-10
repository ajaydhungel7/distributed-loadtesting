# Distributed Load Testing Platform

A distributed load testing platform on AWS. Submit a test via the control plane API, k6 workers spin up automatically on ECS Fargate, run the test, and publish metrics to Grafana via CloudWatch.

## Architecture

```
User → Control Plane API (ALB → ECS Fargate)
           │
           ├── DynamoDB  (job metadata + results)
           └── SQS       (job queue)
                │
                ├── CloudWatch Alarm (queue depth)
                │       │
                │       └── App Auto Scaling → Worker ECS Service (0 → 50 tasks)
                │
                └── Worker Tasks (k6)
                        │
                        ├── DynamoDB  (update status + write results)
                        ├── S3        (raw k6 JSON summary)
                        └── CloudWatch EMF (p50/p95/p99/throughput/errorRate)
                                │
                                └── Grafana (CloudWatch datasource)
```

**Flow:**
1. `POST /tests` → stored in DynamoDB as `PENDING`, job sent to SQS
2. CloudWatch alarm fires when queue depth ≥ 1 → autoscaler sets worker desired count to 5–50
3. Worker picks up the job, runs k6, writes results back to DynamoDB + S3 + CloudWatch
4. Test transitions `PENDING → RUNNING → COMPLETED`

## Repository Structure

```
.
├── infra/                  # AWS CDK (TypeScript) — all infrastructure
│   ├── bin/infra.ts        # App entry point — wires all stacks
│   ├── lib/
│   │   ├── infra-stack.ts          # Foundation: VPC, ECR, DynamoDB, SQS, S3, IAM
│   │   ├── control-plane-stack.ts  # FastAPI service: ECS Fargate + ALB
│   │   ├── worker-stack.ts         # k6 worker: ECS Fargate service (idle at 0)
│   │   ├── autoscaling-stack.ts    # Step scaling triggered by SQS queue depth
│   │   └── grafana-stack.ts        # Grafana on ECS Fargate + ALB
│   └── test/               # CDK assertion tests (Jest)
│
├── control-plane/          # FastAPI app (Python)
│   ├── app/
│   │   ├── main.py         # FastAPI app entry point
│   │   ├── routes/tests.py # REST endpoints
│   │   ├── models/         # Pydantic models + validation
│   │   └── services/       # DynamoDB + SQS clients
│   ├── Dockerfile
│   └── tests/              # pytest + moto
│
├── worker/                 # k6 worker (Python + k6 binary)
│   ├── main.py             # SQS polling loop + job lifecycle
│   ├── k6_runner.py        # k6 script generation + output parsing
│   ├── metrics_emitter.py  # CloudWatch EMF log emission
│   ├── Dockerfile          # Copies k6 from grafana/k6, runs as non-root
│   └── tests/              # pytest + moto
│
├── grafana/                # Custom Grafana image
│   ├── Dockerfile
│   ├── provisioning/
│   │   ├── datasources/cloudwatch.yaml   # CloudWatch datasource (uses ECS task role)
│   │   └── dashboards/dashboards.yaml    # File provider config
│   └── dashboards/loadtest.json          # Pre-built dashboard
│
└── .github/workflows/
    ├── ci.yml      # PR checks: tests + docker build (no push)
    └── deploy.yml  # main push: test → deploy InfraStack → push images → deploy app stacks
```

## CDK Stacks

Each stack is a separate file in `infra/lib/`. They are deployed in dependency order.

### `InfraStack` — Foundation
Shared resources consumed by all other stacks.

| Resource | Details |
|----------|---------|
| VPC | 2 AZs, 1 NAT gateway, public + private subnets |
| ECR | 3 repos: `loadtest/control-plane`, `loadtest/worker`, `loadtest/grafana` |
| DynamoDB | `load-tests` table — `testId` (PK), `createdAt` (SK), GSI on `status` |
| SQS | `loadtest-jobs` queue + DLQ (maxReceiveCount=3, SSE enabled) |
| S3 | Results bucket — versioned, SSE, lifecycle to IA after 30 days |
| IAM | `ControlPlaneTaskRole`, `WorkerTaskRole` (least privilege) |
| OIDC | `GitHubActionsDeployRole` — OIDC trust, no long-lived keys |

### `ControlPlaneStack` — API
FastAPI service fronted by an Application Load Balancer.

| Resource | Details |
|----------|---------|
| ECS Cluster | `loadtest` — shared with workers |
| Fargate Service | 1 task, 512 CPU / 1024 MB, private subnets |
| ALB | Internet-facing, port 80 |
| Health check | `python3 -c "urllib.request.urlopen('http://localhost:8000/health')"` |
| Logs | `/loadtest/control-plane` |

### `WorkerStack` — k6 Workers
ECS Fargate service that starts at 0 tasks and is driven by the autoscaler.

| Resource | Details |
|----------|---------|
| Fargate Service | desiredCount: 0 at idle, private subnets |
| Task | 512 CPU / 1024 MB, k6 binary + Python worker |
| Logs | `/loadtest/workers` |

### `AutoscalingStack` — Queue-Driven Scaling
Step scaling on the worker service based on SQS `ApproximateNumberOfMessagesVisible`.

| Queue depth | Worker count |
|-------------|-------------|
| 1–5 | 5 |
| 6–10 | 10 |
| 11–20 | 20 |
| 21+ | 50 (max) |
| 0 for 5 min | 0 (scale to zero) |

### `GrafanaStack` — Dashboards
Grafana running on ECS Fargate with a provisioned CloudWatch datasource and pre-built dashboard.

| Resource | Details |
|----------|---------|
| Fargate Service | 1 task, 512 CPU / 1024 MB |
| ALB | Internet-facing, port 80 → 3000 |
| Auth | Admin password in Secrets Manager (`loadtest/grafana-admin-password`) |
| Datasource | CloudWatch via ECS task role (no access keys) |

## Control Plane API

Base URL: ALB DNS from `ControlPlaneStack` outputs.

### Create a test
```bash
POST /tests
Content-Type: application/json

{
  "name": "my test",
  "targetUrl": "https://example.com",
  "virtualUsers": 50,
  "duration": "30s",
  "rampUp": "10s"
}
```

Response `202`:
```json
{ "testId": "01M26M8P...", "status": "PENDING" }
```

### Get a test
```bash
GET /tests/{testId}
```

Response:
```json
{
  "testId": "01M26M8P...",
  "status": "COMPLETED",
  "results": {
    "p50": 3.0,
    "p95": 128.6,
    "p99": null,
    "throughput": 7.9,
    "errorRate": 0.0
  },
  "startedAt": "2026-09-10T21:45:05Z",
  "completedAt": "2026-09-10T21:45:56Z"
}
```

### List tests
```bash
GET /tests
GET /tests?status=RUNNING
```

### Cancel a test
```bash
DELETE /tests/{testId}
```

**Test statuses:** `PENDING → RUNNING → COMPLETED | FAILED | CANCELLED`

**Metric fields:**
- `p50` — median response time (ms)
- `p95` — 95th percentile response time (ms)
- `p99` — 99th percentile response time (ms)
- `throughput` — requests per second
- `errorRate` — fraction of failed requests (0.0–1.0)

## CI/CD Pipeline

Two workflows, both in `.github/workflows/`.

### `ci.yml` — Pull Request checks
Runs on every PR to `main`. Four jobs in parallel:

- **CDK Tests** — Jest assertion tests against synthesized CloudFormation templates
- **Control Plane Tests** — pytest + moto (mocked AWS)
- **Worker Tests** — pytest + moto
- **Docker Build** — builds all 3 images, no push

### `deploy.yml` — Deploy on merge to main
Runs on push to `main`. Two sequential jobs:

**Tests** (same as CI)

**Deploy to AWS:**
1. Authenticate via OIDC (`GitHubActionsDeployRole`) — no access keys stored
2. `cdk deploy InfraStack` — creates ECR repos and shared resources first
3. Build and push `control-plane`, `worker`, `grafana` images to ECR (tagged with commit SHA + `latest`)
4. `cdk deploy ControlPlaneStack WorkerStack AutoscalingStack GrafanaStack`

Deploy order matters: InfraStack must exist before images are pushed (ECR repos), and images must exist in ECR before ECS services are deployed.

## Local Development

### Prerequisites
- Node.js 22+, Python 3.12+
- AWS CLI configured (`aws configure`)
- CDK CLI: `npm install -g aws-cdk`

### CDK (Infrastructure)
```bash
cd infra
npm install
npm test           # run CDK assertion tests
npx cdk diff       # preview changes
npx cdk deploy InfraStack
```

### Control Plane
```bash
cd control-plane
pip install -r requirements-dev.txt
pytest tests/ -v
```

### Worker
```bash
cd worker
pip install -r requirements-dev.txt
pytest tests/ -v
```

## First-Time Bootstrap

CDK requires a one-time bootstrap per AWS account/region:

```bash
cd infra
npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
```

Then add your AWS account ID as a GitHub secret (`AWS_ACCOUNT_ID`) and push to `main`. The pipeline handles everything from there.
