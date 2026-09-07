import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { InfraStack } from '../lib/infra-stack';

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const stack = new InfraStack(app, 'TestInfraStack');
  template = Template.fromStack(stack);
});

// ── VPC ──────────────────────────────────────────────────────────────────────

test('VPC is created', () => {
  template.hasResource('AWS::EC2::VPC', {});
});

test('VPC has 2 public and 2 private subnets', () => {
  template.resourceCountIs('AWS::EC2::Subnet', 4);
});

test('NAT Gateway is created', () => {
  template.resourceCountIs('AWS::EC2::NatGateway', 1);
});

// ── ECR ──────────────────────────────────────────────────────────────────────

test('ECR repo for control-plane exists', () => {
  template.hasResourceProperties('AWS::ECR::Repository', {
    RepositoryName: 'loadtest/control-plane',
  });
});

test('ECR repo for worker exists', () => {
  template.hasResourceProperties('AWS::ECR::Repository', {
    RepositoryName: 'loadtest/worker',
  });
});

test('ECR repo for grafana exists', () => {
  template.hasResourceProperties('AWS::ECR::Repository', {
    RepositoryName: 'loadtest/grafana',
  });
});

// ── DynamoDB ─────────────────────────────────────────────────────────────────

test('DynamoDB table is created with correct key schema', () => {
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    TableName: 'load-tests',
    KeySchema: Match.arrayWith([
      { AttributeName: 'testId', KeyType: 'HASH' },
      { AttributeName: 'createdAt', KeyType: 'RANGE' },
    ]),
    BillingMode: 'PAY_PER_REQUEST',
  });
});

test('DynamoDB table has GSI on status', () => {
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    GlobalSecondaryIndexes: Match.arrayWith([
      Match.objectLike({
        IndexName: 'status-index',
        KeySchema: Match.arrayWith([
          { AttributeName: 'status', KeyType: 'HASH' },
        ]),
      }),
    ]),
  });
});

test('DynamoDB table has point-in-time recovery enabled', () => {
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
  });
});

// ── S3 ───────────────────────────────────────────────────────────────────────

test('S3 results bucket exists with versioning enabled', () => {
  template.hasResourceProperties('AWS::S3::Bucket', {
    VersioningConfiguration: { Status: 'Enabled' },
  });
});

test('S3 bucket blocks all public access', () => {
  template.hasResourceProperties('AWS::S3::Bucket', {
    PublicAccessBlockConfiguration: {
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    },
  });
});

test('S3 bucket has server-side encryption', () => {
  template.hasResourceProperties('AWS::S3::Bucket', {
    BucketEncryption: {
      ServerSideEncryptionConfiguration: Match.arrayWith([
        Match.objectLike({
          ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' },
        }),
      ]),
    },
  });
});

test('S3 bucket has lifecycle rule to transition to IA after 30 days', () => {
  template.hasResourceProperties('AWS::S3::Bucket', {
    LifecycleConfiguration: {
      Rules: Match.arrayWith([
        Match.objectLike({
          Status: 'Enabled',
          Transitions: Match.arrayWith([
            Match.objectLike({
              StorageClass: 'STANDARD_IA',
              TransitionInDays: 30,
            }),
          ]),
        }),
      ]),
    },
  });
});

// ── SQS ──────────────────────────────────────────────────────────────────────

test('SQS job queue is created with SSE', () => {
  template.hasResourceProperties('AWS::SQS::Queue', {
    QueueName: 'loadtest-jobs',
    SqsManagedSseEnabled: true,
  });
});

test('SQS DLQ is created', () => {
  template.hasResourceProperties('AWS::SQS::Queue', {
    QueueName: 'loadtest-jobs-dlq',
  });
});

test('SQS queue has redrive policy pointing to DLQ with maxReceiveCount=3', () => {
  template.hasResourceProperties('AWS::SQS::Queue', {
    QueueName: 'loadtest-jobs',
    RedrivePolicy: Match.objectLike({
      maxReceiveCount: 3,
    }),
  });
});

// ── IAM ──────────────────────────────────────────────────────────────────────

test('ControlPlaneTaskRole is created with ECS trust policy', () => {
  template.hasResourceProperties('AWS::IAM::Role', {
    RoleName: 'ControlPlaneTaskRole',
    AssumeRolePolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Principal: { Service: 'ecs-tasks.amazonaws.com' },
          Action: 'sts:AssumeRole',
        }),
      ]),
    }),
  });
});

test('WorkerTaskRole is created with ECS trust policy', () => {
  template.hasResourceProperties('AWS::IAM::Role', {
    RoleName: 'WorkerTaskRole',
    AssumeRolePolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Principal: { Service: 'ecs-tasks.amazonaws.com' },
          Action: 'sts:AssumeRole',
        }),
      ]),
    }),
  });
});

// ── GitHub Actions OIDC ───────────────────────────────────────────────────────

test('GitHub OIDC provider is created', () => {
  template.hasResourceProperties('Custom::AWSCDKOpenIdConnectProvider', {
    Url: 'https://token.actions.githubusercontent.com',
    ClientIDList: Match.arrayWith(['sts.amazonaws.com']),
  });
});

test('GitHubActionsDeployRole is created with OIDC trust policy', () => {
  template.hasResourceProperties('AWS::IAM::Role', {
    RoleName: 'GitHubActionsDeployRole',
    AssumeRolePolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Principal: Match.objectLike({
            Federated: Match.anyValue(),
          }),
          Action: 'sts:AssumeRoleWithWebIdentity',
          Condition: Match.objectLike({
            StringEquals: Match.objectLike({
              'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
            }),
            StringLike: Match.objectLike({
              'token.actions.githubusercontent.com:sub': 'repo:ajaydhungel7/distributed-loadtesting:*',
            }),
          }),
        }),
      ]),
    }),
  });
});

test('GitHubActionsDeployRole can push to ECR', () => {
  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Sid: 'ECRPush',
          Action: Match.arrayWith(['ecr:GetAuthorizationToken', 'ecr:PutImage']),
        }),
      ]),
    }),
  });
});

test('GitHubActionsDeployRole can assume CDK bootstrap roles', () => {
  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: 'sts:AssumeRole',
          Sid: 'CDKBootstrapRoles',
        }),
      ]),
    }),
  });
});
