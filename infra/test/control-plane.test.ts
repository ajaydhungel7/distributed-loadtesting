import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { InfraStack } from '../lib/infra-stack';
import { ControlPlaneStack } from '../lib/control-plane-stack';

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const infra = new InfraStack(app, 'TestInfraStack');
  const controlPlane = new ControlPlaneStack(app, 'TestControlPlaneStack', { infra });
  template = Template.fromStack(controlPlane);
});

// ── ECS Cluster ───────────────────────────────────────────────────────────────

test('ECS cluster is created', () => {
  template.hasResource('AWS::ECS::Cluster', {});
});

// ── Task Definition ───────────────────────────────────────────────────────────

test('Task definition uses Fargate compatibility', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    RequiresCompatibilities: ['FARGATE'],
    NetworkMode: 'awsvpc',
  });
});

test('Task definition has correct CPU and memory', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    Cpu: '512',
    Memory: '1024',
  });
});

test('Task definition uses ControlPlaneTaskRole', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    TaskRoleArn: Match.objectLike({
      'Fn::ImportValue': Match.stringLikeRegexp('ControlPlaneTaskRoleArn'),
    }),
  });
});

test('Container definition references control-plane ECR image', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'control-plane',
        Essential: true,
        PortMappings: Match.arrayWith([
          Match.objectLike({ ContainerPort: 8000, Protocol: 'tcp' }),
        ]),
      }),
    ]),
  });
});

test('Container logs go to CloudWatch', () => {
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

test('CloudWatch log group is created for control plane', () => {
  template.hasResourceProperties('AWS::Logs::LogGroup', {
    LogGroupName: '/loadtest/control-plane',
    RetentionInDays: 30,
  });
});

// ── ECS Service ───────────────────────────────────────────────────────────────

test('Fargate service is created', () => {
  template.hasResourceProperties('AWS::ECS::Service', {
    LaunchType: 'FARGATE',
    DesiredCount: 1,
  });
});

test('Fargate service is in private subnets', () => {
  template.hasResourceProperties('AWS::ECS::Service', {
    NetworkConfiguration: Match.objectLike({
      AwsvpcConfiguration: Match.objectLike({
        AssignPublicIp: 'DISABLED',
      }),
    }),
  });
});

// ── Load Balancer ─────────────────────────────────────────────────────────────

test('Application Load Balancer is created', () => {
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
    Type: 'application',
    Scheme: 'internet-facing',
  });
});

test('ALB listener is on port 80', () => {
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
    Port: 80,
    Protocol: 'HTTP',
  });
});

test('ALB target group uses HTTP on port 8000', () => {
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
    Port: 8000,
    Protocol: 'HTTP',
    TargetType: 'ip',
  });
});

test('ALB target group has health check on /health', () => {
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
    HealthCheckPath: '/health',
    HealthCheckProtocol: 'HTTP',
  });
});
