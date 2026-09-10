import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { InfraStack } from '../lib/infra-stack';
import { ControlPlaneStack } from '../lib/control-plane-stack';
import { WorkerStack } from '../lib/worker-stack';

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const infra = new InfraStack(app, 'TestInfraStack');
  const controlPlane = new ControlPlaneStack(app, 'TestControlPlaneStack', { infra });
  const worker = new WorkerStack(app, 'TestWorkerStack', { infra, cluster: controlPlane.cluster });
  template = Template.fromStack(worker);
});

// ── Task Definition ───────────────────────────────────────────────────────────

test('Worker task definition uses Fargate compatibility', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    RequiresCompatibilities: ['FARGATE'],
    NetworkMode: 'awsvpc',
  });
});

test('Worker task definition has correct CPU and memory', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    Cpu: '512',
    Memory: '1024',
  });
});

test('Worker task definition uses WorkerTaskRole', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    TaskRoleArn: Match.objectLike({
      'Fn::ImportValue': Match.stringLikeRegexp('WorkerTaskRoleArn'),
    }),
  });
});

test('Worker container references worker ECR image', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'worker',
        Essential: true,
      }),
    ]),
  });
});

test('Worker container receives job config via environment', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Environment: Match.arrayWith([
          Match.objectLike({ Name: 'TABLE_NAME' }),
          Match.objectLike({ Name: 'JOB_QUEUE_URL' }),
          Match.objectLike({ Name: 'RESULTS_BUCKET' }),
        ]),
      }),
    ]),
  });
});

test('Worker container logs go to CloudWatch', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        LogConfiguration: Match.objectLike({
          LogDriver: 'awslogs',
          Options: Match.objectLike({
            'awslogs-stream-prefix': 'ecs',
          }),
        }),
      }),
    ]),
  });
});

// ── CloudWatch Log Group ──────────────────────────────────────────────────────

test('CloudWatch log group is created for workers', () => {
  template.hasResourceProperties('AWS::Logs::LogGroup', {
    LogGroupName: '/loadtest/workers',
    RetentionInDays: 30,
  });
});
