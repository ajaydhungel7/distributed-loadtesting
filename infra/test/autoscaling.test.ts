import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { InfraStack } from '../lib/infra-stack';
import { ControlPlaneStack } from '../lib/control-plane-stack';
import { WorkerStack } from '../lib/worker-stack';
import { AutoscalingStack } from '../lib/autoscaling-stack';

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const infra = new InfraStack(app, 'TestInfraStack');
  const controlPlane = new ControlPlaneStack(app, 'TestControlPlaneStack', { infra });
  const worker = new WorkerStack(app, 'TestWorkerStack', { infra, cluster: controlPlane.cluster });
  const autoscaling = new AutoscalingStack(app, 'TestAutoscalingStack', { infra, controlPlane, worker });
  template = Template.fromStack(autoscaling);
});

// ── Application Auto Scaling ──────────────────────────────────────────────────

test('Scalable target is registered for ECS worker service', () => {
  template.hasResourceProperties('AWS::ApplicationAutoScaling::ScalableTarget', {
    ServiceNamespace: 'ecs',
    ScalableDimension: 'ecs:service:DesiredCount',
    MinCapacity: 0,
    MaxCapacity: 50,
  });
});

test('Scale-out policy is step scaling based on queue depth', () => {
  template.hasResourceProperties('AWS::ApplicationAutoScaling::ScalingPolicy', {
    PolicyType: 'StepScaling',
    StepScalingPolicyConfiguration: Match.objectLike({
      AdjustmentType: 'ExactCapacity',
      StepAdjustments: Match.arrayWith([
        Match.objectLike({ ScalingAdjustment: 5 }),
        Match.objectLike({ ScalingAdjustment: 10 }),
        Match.objectLike({ ScalingAdjustment: 20 }),
        Match.objectLike({ ScalingAdjustment: 50 }),
      ]),
    }),
  });
});

test('Scale-in policy scales to zero when queue is empty', () => {
  template.hasResourceProperties('AWS::ApplicationAutoScaling::ScalingPolicy', {
    PolicyType: 'StepScaling',
    StepScalingPolicyConfiguration: Match.objectLike({
      AdjustmentType: 'ExactCapacity',
      StepAdjustments: Match.arrayWith([
        Match.objectLike({ ScalingAdjustment: 0 }),
      ]),
    }),
  });
});

// ── CloudWatch Alarms ─────────────────────────────────────────────────────────

test('Scale-out alarm monitors SQS queue depth', () => {
  template.hasResourceProperties('AWS::CloudWatch::Alarm', {
    MetricName: 'ApproximateNumberOfMessagesVisible',
    Namespace: 'AWS/SQS',
    ComparisonOperator: 'GreaterThanOrEqualToThreshold',
    Threshold: 1,
    EvaluationPeriods: 1,
  });
});

test('Scale-in alarm fires when queue is empty', () => {
  template.hasResourceProperties('AWS::CloudWatch::Alarm', {
    MetricName: 'ApproximateNumberOfMessagesVisible',
    Namespace: 'AWS/SQS',
    ComparisonOperator: 'LessThanOrEqualToThreshold',
    Threshold: 0,
  });
});
