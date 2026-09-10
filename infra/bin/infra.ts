#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { InfraStack } from '../lib/infra-stack';
import { ControlPlaneStack } from '../lib/control-plane-stack';
import { WorkerStack } from '../lib/worker-stack';
import { AutoscalingStack } from '../lib/autoscaling-stack';
import { GrafanaStack } from '../lib/grafana-stack';

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

// Slice 1 — foundation: VPC, ECR, DynamoDB, SQS, S3, IAM
const infra = new InfraStack(app, 'InfraStack', { env });

// Slice 2 — control plane API: ECS Fargate service + ALB
const controlPlane = new ControlPlaneStack(app, 'ControlPlaneStack', { env, infra });
controlPlane.addDependency(infra);

// Slice 3 — worker service (starts at 0 tasks, scaled by autoscaling stack)
const worker = new WorkerStack(app, 'WorkerStack', { env, infra, cluster: controlPlane.cluster });
worker.addDependency(controlPlane);

// Slice 4 — autoscaling: queue-depth-triggered scale-out/in
const autoscaling = new AutoscalingStack(app, 'AutoscalingStack', { env, infra, controlPlane, worker });
autoscaling.addDependency(controlPlane);
autoscaling.addDependency(worker);

// Slice 5 — Grafana dashboard
const grafana = new GrafanaStack(app, 'GrafanaStack', { env, infra, controlPlane });
grafana.addDependency(controlPlane);
