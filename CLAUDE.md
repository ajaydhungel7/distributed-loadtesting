# Queue-Based Autoscaling — CLAUDE.md

## What This Project Is

A distributed job processing platform that demonstrates SQS queue-driven autoscaling on AWS ECS Fargate. The control plane floods an SQS queue with messages; workers scale proportionally to drain it; everything scales back to zero when done.

## Repository Layout

```
infra/                    CDK TypeScript — single LoadTestStack
control-plane/            FastAPI (Python 3.12) — job API
worker/                   Python worker — SQS poll loop
grafana/                  Custom Grafana image — pre-built dashboard
.github/workflows/        deploy.yml — single CI+CD workflow
```

## Key Design Decisions

- **Single CDK stack** (`LoadTestStack`) — eliminates cross-stack export deadlocks. Never split back into multiple stacks.
- **desiredCount=0 in CDK** for all ECS services — CloudFormation always stabilizes. Pipeline scales up after deploy.
- **Target tracking autoscaling** on backlog-per-task (`messages / running workers`), target=500. Do not revert to step scaling.
- **Workers loop** and process multiple messages per launch. They exit after 3 consecutive empty polls (~60s idle). Do not change to one-message-per-task.
- **ECS task scale-in protection** — workers call the ECS agent endpoint to protect themselves while processing a message.
- **No hardcoded resource names** in CDK — prevents EarlyValidation conflicts on re-deploy.

## AWS Resources

| Resource | Name/ID |
|----------|---------|
| Stack | `LoadTestStack` |
| ECS Cluster | `LoadTestStack-ClusterEB0386A7-r3cahAglJNmG` |
| Worker Service | `LoadTestStack-WorkerService99815FA9-d9Q3bkIGX9Dy` |
| Grafana Service | `LoadTestStack-GrafanaServiceB3AB259D-vCtvSmUJz7Uz` |
| Control Plane Service | `LoadTestStack-ControlPlaneServiceAF0E121B-AocQ0MkqidkZ` |
| SQS Target Queue | `loadtest-target` |
| SQS DLQ | `loadtest-jobs-dlq` |
| DynamoDB Table | `queue-jobs` |
| Grafana Password | Secrets Manager: `loadtest/grafana-admin-password` |
| OIDC Role | `GitHubActionsDeployRole` |
| Region | `us-east-1` |

## Endpoints

- **Control Plane API:** `http://LoadTe-Contr-Gm99Iyav1nxr-1667948017.us-east-1.elb.amazonaws.com`
- **Grafana:** `http://LoadTe-Grafa-PBFLQERfHrwD-1646674974.us-east-1.elb.amazonaws.com`

## How to Deploy

### First time (fresh AWS account)
```bash
# 1. Bootstrap CDK (once per account/region)
cd infra && npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1

# 2. Add GitHub secret: AWS_ACCOUNT_ID

# 3. Push to main — pipeline does everything
git push origin main
```

### Normal deploy (code change)
Just push to `main`. The pipeline:
1. Runs all tests
2. Bootstraps ECR repos if they don't exist
3. Rebuilds only changed service images (git diff on `control-plane/`, `worker/`, `grafana/`)
4. Deploys `LoadTestStack` with new image tags
5. Scales control-plane and Grafana to 1

### Manual deploy (skip pipeline)
```bash
cd infra
npx cdk deploy LoadTestStack \
  --context controlPlaneTag=latest \
  --context workerTag=latest \
  --context grafanaTag=latest \
  --require-approval never
```

### If stack gets stuck
Check status:
```bash
aws cloudformation describe-stacks --stack-name LoadTestStack \
  --region us-east-1 --query 'Stacks[0].StackStatus' --output text
```

Delete if needed (last resort):
```bash
aws cloudformation delete-stack --stack-name LoadTestStack --region us-east-1
```

## How to Test

### Submit a job
```bash
curl -X POST http://LoadTe-Contr-Gm99Iyav1nxr-1667948017.us-east-1.elb.amazonaws.com/jobs \
  -H "Content-Type: application/json" \
  -d '{"name": "test", "messageCount": 1000, "processingTime": 500}'
```

### Check job status
```bash
curl http://LoadTe-Contr-Gm99Iyav1nxr-1667948017.us-east-1.elb.amazonaws.com/jobs/<jobId>
```

### Watch workers scale
```bash
CLUSTER="LoadTestStack-ClusterEB0386A7-r3cahAglJNmG"
SERVICE="LoadTestStack-WorkerService99815FA9-d9Q3bkIGX9Dy"
aws ecs describe-services --cluster $CLUSTER --services $SERVICE --region us-east-1 \
  --query 'services[0].{Desired:desiredCount,Running:runningCount}' --output table
```

### Check queue depth
```bash
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/544234170512/loadtest-target \
  --attribute-names ApproximateNumberOfMessages --region us-east-1
```

### Run unit tests locally
```bash
cd infra && npm test
cd control-plane && pytest tests/ -v
cd worker && pytest tests/ -v
```

## How the Autoscaling Works

1. `POST /jobs` floods `loadtest-target` with `messageCount` messages
2. CloudWatch alarm (`LoadTestWorkerQueueDepth`) fires when queue ≥ 1 message
3. Step action bumps worker desired count 0→1 (bootstrap — needed so Container Insights starts publishing `RunningTaskCount`)
4. Target tracking policy takes over: `backlog_per_task = messages / running_workers`, target=500
   - 500 msgs → 1 worker
   - 5000 msgs → 10 workers
   - 50000 msgs → 100 workers (max)
5. Workers process messages in a loop, atomically incrementing `processedCount` in DynamoDB
6. Last worker to process the final message marks job `COMPLETED`
7. Workers exit after 3 empty polls; scale-in cooldown (5 min) sets desired to 0

## IAM Roles

### GitHubActionsDeployRole
- OIDC trust: `repo:ajaydhungel7*distributed-loadtesting*`
- Session duration: 4 hours (long CDK deploys)
- Permissions: ECR push, CDK deploy (`sts:AssumeRole cdk-*`), ECS list/update services

### GrafanaTaskRole
- `cloudwatch:GetMetricData`, `ListMetrics`, `GetMetricStatistics`
- `ec2:DescribeRegions` — Grafana region picker
- `oam:ListSinks` — Grafana cross-account check
- `logs:StartQuery`, `StopQuery`, `GetQueryResults`, `DescribeLogGroups`

### WorkerTaskRole
- SQS: receive, delete, get queue attributes on `loadtest-target`
- DynamoDB: get, put, update on `queue-jobs`
- S3: put on results bucket
- CloudWatch: `PutMetricData`

## Grafana

- URL: `http://LoadTe-Grafa-PBFLQERfHrwD-1646674974.us-east-1.elb.amazonaws.com`
- Login: `admin` / fetch password with:
  ```bash
  aws secretsmanager get-secret-value --secret-id loadtest/grafana-admin-password \
    --region us-east-1 --query SecretString --output text
  ```
- Dashboard auto-provisioned from `grafana/dashboards/loadtest.json`
- Datasource: CloudWatch via ECS task role (no access keys)
- `allowUiUpdates: true` — you can edit panels directly in the UI

## Worker Lifecycle Detail

```python
# Pseudocode
while True:
    message = poll_sqs(wait=20s)
    if message is None:
        empty_polls += 1
        if empty_polls >= 3:
            exit(0)   # scale-in will handle desired count
        continue
    empty_polls = 0
    set_scale_in_protection(True)   # ECS won't kill us mid-message
    try:
        process(message)
        increment_ddb_counter()
        if last_message:
            mark_job_completed()
        delete_sqs_message()
    finally:
        set_scale_in_protection(False)
```

## Common Issues

| Problem | Cause | Fix |
|---------|-------|-----|
| Stack stuck in `CREATE_IN_PROGRESS` | ECS service trying to pull image before ECR has it | `desiredCount=0` in CDK prevents this — don't change it |
| Workers not scaling | Container Insights hasn't published `RunningTaskCount` yet | Bootstrap alarm handles this — wait 1-2 minutes |
| Grafana shows no data | IAM permissions or datasource region | Check task role has `ec2:DescribeRegions` + `oam:ListSinks` |
| `UPDATE_ROLLBACK_FAILED` | Resource name conflict or failed ECS stabilization | Check for hardcoded names; `desiredCount=0` prevents stabilization failures |
| ExpiredToken during deploy | OIDC session too short | Role has 4h max session; workflow uses `role-duration-seconds: 14400` |
