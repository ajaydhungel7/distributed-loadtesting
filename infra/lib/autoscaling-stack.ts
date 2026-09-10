import * as cdk from 'aws-cdk-lib';
import * as appscaling from 'aws-cdk-lib/aws-applicationautoscaling';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Construct } from 'constructs';
import { InfraStack } from './infra-stack';
import { ControlPlaneStack } from './control-plane-stack';
import { WorkerStack } from './worker-stack';

interface AutoscalingStackProps extends cdk.StackProps {
  infra: InfraStack;
  controlPlane: ControlPlaneStack;
  worker: WorkerStack;
}

export class AutoscalingStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AutoscalingStackProps) {
    super(scope, id, props);

    const { jobQueue } = props.infra;
    const { service } = props.worker;

    // ── Scalable Target ───────────────────────────────────────────────────────
    // Registers the worker ECS service as something App Auto Scaling can control.
    // Workers scale between 0 (no cost at idle) and 50 tasks maximum.
    const scalableTarget = new appscaling.ScalableTarget(this, 'WorkerScalableTarget', {
      serviceNamespace: appscaling.ServiceNamespace.ECS,
      scalableDimension: 'ecs:service:DesiredCount',
      resourceId: `service/${service.cluster.clusterName}/${service.serviceName}`,
      minCapacity: 0,
      maxCapacity: 50,
    });

    // ── CloudWatch Metric ─────────────────────────────────────────────────────
    const queueDepthMetric = new cloudwatch.Metric({
      namespace: 'AWS/SQS',
      metricName: 'ApproximateNumberOfMessagesVisible',
      dimensionsMap: { QueueName: jobQueue.queueName },
      statistic: 'Maximum',
      period: cdk.Duration.minutes(1),
    });

    // ── Scale-Out Action + Alarm ──────────────────────────────────────────────
    // When the queue has messages, set worker count based on depth.
    //
    // Queue messages  │  Workers
    // ────────────────┼──────────
    //  1 – 5          │  5
    //  6 – 10         │  10
    // 11 – 20         │  20
    // 21+             │  50 (max)
    const scaleOutAction = new appscaling.StepScalingAction(this, 'WorkerScaleOutAction', {
      scalingTarget: scalableTarget,
      adjustmentType: appscaling.AdjustmentType.EXACT_CAPACITY,
      metricAggregationType: appscaling.MetricAggregationType.MAXIMUM,
    });
    scaleOutAction.addAdjustment({ adjustment: 5, lowerBound: 0, upperBound: 5 });
    scaleOutAction.addAdjustment({ adjustment: 10, lowerBound: 5, upperBound: 10 });
    scaleOutAction.addAdjustment({ adjustment: 20, lowerBound: 10, upperBound: 20 });
    scaleOutAction.addAdjustment({ adjustment: 50, lowerBound: 20 });

    const scaleOutAlarm = new cloudwatch.Alarm(this, 'QueueDepthAlarm', {
      alarmName: 'LoadTestWorkerQueueDepth',
      metric: queueDepthMetric,
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'Jobs are waiting in the load test queue - scale out workers',
    });
    scaleOutAlarm.addAlarmAction(
      new cloudwatch_actions.ApplicationScalingAction(scaleOutAction),
    );

    // ── Scale-In Action + Alarm ───────────────────────────────────────────────
    // When the queue has been empty for 5 consecutive minutes, scale to 0.
    const scaleInAction = new appscaling.StepScalingAction(this, 'WorkerScaleInAction', {
      scalingTarget: scalableTarget,
      adjustmentType: appscaling.AdjustmentType.EXACT_CAPACITY,
      metricAggregationType: appscaling.MetricAggregationType.MAXIMUM,
    });
    scaleInAction.addAdjustment({ adjustment: 0, upperBound: 0 });

    const scaleInAlarm = new cloudwatch.Alarm(this, 'QueueEmptyAlarm', {
      metric: queueDepthMetric,
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 5,
      datapointsToAlarm: 5,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'SQS job queue has been empty for 5 minutes - scale workers to 0',
    });
    scaleInAlarm.addAlarmAction(
      new cloudwatch_actions.ApplicationScalingAction(scaleInAction),
    );
  }
}
