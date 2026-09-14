import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { LoadTestStack } from '../lib/load-test-stack';

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const stack = new LoadTestStack(app, 'TestStack', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  template = Template.fromStack(stack);
});

// ── VPC ───────────────────────────────────────────────────────────────────────

test('VPC is created with public and private subnets', () => {
  template.hasResource('AWS::EC2::VPC', {});
  template.resourceCountIs('AWS::EC2::Subnet', 4);
  template.resourceCountIs('AWS::EC2::NatGateway', 1);
});

// ── ECR ───────────────────────────────────────────────────────────────────────

test('ECR repos exist for all three services', () => {
  template.hasResourceProperties('AWS::ECR::Repository', { RepositoryName: 'loadtest/control-plane' });
  template.hasResourceProperties('AWS::ECR::Repository', { RepositoryName: 'loadtest/worker' });
  template.hasResourceProperties('AWS::ECR::Repository', { RepositoryName: 'loadtest/grafana' });
});

// ── DynamoDB ──────────────────────────────────────────────────────────────────

test('DynamoDB table is created with correct key schema and GSI', () => {
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    TableName: 'queue-jobs',
    KeySchema: Match.arrayWith([
      { AttributeName: 'jobId',     KeyType: 'HASH'  },
      { AttributeName: 'createdAt', KeyType: 'RANGE' },
    ]),
    BillingMode: 'PAY_PER_REQUEST',
    GlobalSecondaryIndexes: Match.arrayWith([
      Match.objectLike({
        IndexName: 'status-index',
        KeySchema: Match.arrayWith([{ AttributeName: 'status', KeyType: 'HASH' }]),
      }),
    ]),
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
  });
});

// ── SQS ───────────────────────────────────────────────────────────────────────

test('SQS queues are created (jobs, target, dlq)', () => {
  template.hasResourceProperties('AWS::SQS::Queue', { QueueName: 'loadtest-jobs' });
  template.hasResourceProperties('AWS::SQS::Queue', { QueueName: 'loadtest-target' });
  template.hasResourceProperties('AWS::SQS::Queue', { QueueName: 'loadtest-jobs-dlq' });
});

test('Job queue has DLQ redrive policy', () => {
  template.hasResourceProperties('AWS::SQS::Queue', {
    QueueName: 'loadtest-jobs',
    RedrivePolicy: Match.objectLike({ maxReceiveCount: 3 }),
  });
});

// ── S3 ────────────────────────────────────────────────────────────────────────

test('S3 results bucket is private and versioned', () => {
  template.hasResourceProperties('AWS::S3::Bucket', {
    VersioningConfiguration: { Status: 'Enabled' },
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true, BlockPublicPolicy: true,
      IgnorePublicAcls: true, RestrictPublicBuckets: true,
    },
  });
});

// ── IAM ───────────────────────────────────────────────────────────────────────

test('ControlPlaneTaskRole and WorkerTaskRole are created', () => {
  template.hasResourceProperties('AWS::IAM::Role', {
    RoleName: 'ControlPlaneTaskRole',
    AssumeRolePolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({ Principal: { Service: 'ecs-tasks.amazonaws.com' }, Action: 'sts:AssumeRole' }),
      ]),
    }),
  });
  template.hasResourceProperties('AWS::IAM::Role', {
    RoleName: 'WorkerTaskRole',
    AssumeRolePolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({ Principal: { Service: 'ecs-tasks.amazonaws.com' }, Action: 'sts:AssumeRole' }),
      ]),
    }),
  });
});

test('GitHubActionsDeployRole uses OIDC trust policy for the repo', () => {
  template.hasResourceProperties('AWS::IAM::Role', {
    RoleName: 'GitHubActionsDeployRole',
    AssumeRolePolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: 'sts:AssumeRoleWithWebIdentity',
          Condition: Match.objectLike({
            StringEquals: Match.objectLike({ 'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com' }),
            StringLike:   Match.objectLike({ 'token.actions.githubusercontent.com:sub': 'repo:ajaydhungel7*distributed-loadtesting*' }),
          }),
        }),
      ]),
    }),
  });
});

// ── ECS Cluster ───────────────────────────────────────────────────────────────

test('ECS cluster is created', () => {
  template.hasResourceProperties('AWS::ECS::Cluster', { ClusterName: 'loadtest' });
});

// ── Control Plane ─────────────────────────────────────────────────────────────

test('Control plane task definition has correct resources and image', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'control-plane',
        Essential: true,
        PortMappings: Match.arrayWith([Match.objectLike({ ContainerPort: 8000 })]),
        Environment: Match.arrayWith([
          Match.objectLike({ Name: 'TABLE_NAME',       Value: 'queue-jobs' }),
          Match.objectLike({ Name: 'TARGET_QUEUE_URL' }),
        ]),
      }),
    ]),
    Cpu: '512', Memory: '1024',
  });
});

test('Control plane ALB is internet-facing on port 80', () => {
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
    Scheme: 'internet-facing',
  });
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', { Port: 80 });
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
    Port: 8000, HealthCheckPath: '/health',
  });
});

// ── Worker ────────────────────────────────────────────────────────────────────

test('Worker task definition has correct resources and environment', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'worker',
        Essential: true,
        Environment: Match.arrayWith([
          Match.objectLike({ Name: 'TABLE_NAME',       Value: 'queue-jobs' }),
          Match.objectLike({ Name: 'TARGET_QUEUE_URL' }),
          Match.objectLike({ Name: 'RESULTS_BUCKET' }),
        ]),
      }),
    ]),
  });
});

test('Worker service starts at 0 desired count', () => {
  template.hasResourceProperties('AWS::ECS::Service', {
    DesiredCount: 0,
  });
});

// ── Autoscaling ───────────────────────────────────────────────────────────────

test('Scalable target is registered for ECS worker service', () => {
  template.hasResourceProperties('AWS::ApplicationAutoScaling::ScalableTarget', {
    ServiceNamespace: 'ecs',
    ScalableDimension: 'ecs:service:DesiredCount',
    MinCapacity: 0,
    MaxCapacity: 50,
  });
});

test('Scale-out alarm monitors target queue depth', () => {
  template.hasResourceProperties('AWS::CloudWatch::Alarm', {
    AlarmName: 'LoadTestWorkerQueueDepth',
    Namespace: 'AWS/SQS',
    MetricName: 'ApproximateNumberOfMessagesVisible',
    Dimensions: Match.arrayWith([Match.objectLike({ Name: 'QueueName' })]),
    Threshold: 1,
  });
});

test('Scale-in alarm fires when queue is empty for 5 consecutive minutes', () => {
  template.hasResourceProperties('AWS::CloudWatch::Alarm', {
    Namespace: 'AWS/SQS',
    MetricName: 'ApproximateNumberOfMessagesVisible',
    Threshold: 0,
    EvaluationPeriods: 5,
    DatapointsToAlarm: 5,
  });
});

// ── Grafana ───────────────────────────────────────────────────────────────────

test('Grafana task definition listens on port 3000', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'grafana',
        PortMappings: Match.arrayWith([Match.objectLike({ ContainerPort: 3000 })]),
      }),
    ]),
  });
});

test('Grafana admin password is in Secrets Manager', () => {
  template.hasResourceProperties('AWS::SecretsManager::Secret', {
    Name: 'loadtest/grafana-admin-password',
  });
});

test('Grafana ALB target group uses port 3000', () => {
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
    Port: 3000, HealthCheckPath: '/api/health',
  });
});
