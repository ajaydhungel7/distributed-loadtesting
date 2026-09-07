import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { InfraStack } from '../lib/infra-stack';
import { ControlPlaneStack } from '../lib/control-plane-stack';
import { GrafanaStack } from '../lib/grafana-stack';

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const infra = new InfraStack(app, 'TestInfraStack');
  const controlPlane = new ControlPlaneStack(app, 'TestControlPlaneStack', { infra });
  const grafana = new GrafanaStack(app, 'TestGrafanaStack', { infra, controlPlane });
  template = Template.fromStack(grafana);
});

// ── Task Definition ───────────────────────────────────────────────────────────

test('Grafana task definition uses Fargate', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    RequiresCompatibilities: ['FARGATE'],
    NetworkMode: 'awsvpc',
  });
});

test('Grafana container listens on port 3000', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'grafana',
        PortMappings: Match.arrayWith([
          Match.objectLike({ ContainerPort: 3000 }),
        ]),
      }),
    ]),
  });
});

test('Grafana container uses image from ECR', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Name: 'grafana',
        // Image is an ECR URI (Fn::Join with account/region references)
        Image: Match.objectLike({ 'Fn::Join': Match.anyValue() }),
      }),
    ]),
  });
});

// ── Secrets Manager ───────────────────────────────────────────────────────────

test('Grafana admin password is stored in Secrets Manager', () => {
  template.hasResourceProperties('AWS::SecretsManager::Secret', {
    Name: 'loadtest/grafana-admin-password',
    GenerateSecretString: Match.objectLike({
      PasswordLength: 32,
      ExcludeCharacters: '"@/\\',
    }),
  });
});

test('Grafana container reads admin password from Secrets Manager', () => {
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: Match.arrayWith([
      Match.objectLike({
        Secrets: Match.arrayWith([
          Match.objectLike({ Name: 'GF_SECURITY_ADMIN_PASSWORD' }),
        ]),
      }),
    ]),
  });
});

// ── Load Balancer ─────────────────────────────────────────────────────────────

test('Grafana ALB listener is on port 80', () => {
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
    Port: 80,
    Protocol: 'HTTP',
  });
});

test('Grafana target group uses port 3000', () => {
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
    Port: 3000,
    Protocol: 'HTTP',
    TargetType: 'ip',
  });
});

// ── CloudWatch Log Group ──────────────────────────────────────────────────────

test('CloudWatch log group is created for Grafana', () => {
  template.hasResourceProperties('AWS::Logs::LogGroup', {
    LogGroupName: '/loadtest/grafana',
    RetentionInDays: 14,
  });
});
