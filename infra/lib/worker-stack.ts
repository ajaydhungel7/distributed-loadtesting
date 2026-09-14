import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { InfraStack } from './infra-stack';

interface WorkerStackProps extends cdk.StackProps {
  infra: InfraStack;
  cluster: ecs.ICluster;
}

export class WorkerStack extends cdk.Stack {
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  public readonly service: ecs.FargateService;

  constructor(scope: Construct, id: string, props: WorkerStackProps) {
    super(scope, id, props);

    const { workerRepo, table, jobQueue, resultsBucket, vpc } = props.infra;
    // Target queue URL constructed from fixed queue name to avoid cross-stack export
    const targetQueueUrl = `https://sqs.${this.region}.amazonaws.com/${this.account}/loadtest-target`;
    const { cluster } = props;

    // ── CloudWatch Log Group ──────────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, 'WorkerLogGroup', {
      logGroupName: '/loadtest/workers',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Execution Role ────────────────────────────────────────────────────────
    // Allows ECS agent to pull from ECR and write to CloudWatch Logs
    const executionRole = new iam.Role(this, 'WorkerExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy'),
      ],
    });
    workerRepo.grantPull(executionRole);

    // ── Task Definition ───────────────────────────────────────────────────────
    // Workers are launched on-demand (not as a persistent service) — the
    // autoscaling stack will run-task with this definition when scaling out.
    this.taskDefinition = new ecs.FargateTaskDefinition(this, 'WorkerTaskDef', {
      cpu: 512,
      memoryLimitMiB: 1024,
      taskRole: iam.Role.fromRoleArn(
        this,
        'ImportedWorkerTaskRole',
        cdk.Fn.importValue('WorkerTaskRoleArn'),
      ),
      executionRole,
    });

    const imageTag = this.node.tryGetContext('imageTag') ?? 'latest';

    this.taskDefinition.addContainer('worker', {
      image: ecs.ContainerImage.fromEcrRepository(workerRepo, imageTag),
      essential: true,
      environment: {
        TABLE_NAME: 'queue-jobs',
        TARGET_QUEUE_URL: targetQueueUrl,
        RESULTS_BUCKET: resultsBucket.bucketName,
        AWS_REGION: this.region,
      },
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: 'ecs',
      }),
    });

    // ── Fargate Service (idle at 0, scaled by AutoscalingStack) ──────────────
    const workerSg = new ec2.SecurityGroup(this, 'WorkerServiceSg', {
      vpc,
      description: 'Worker ECS service - allow outbound only',
      allowAllOutbound: true,
    });

    this.service = new ecs.FargateService(this, 'WorkerService', {
      cluster,
      taskDefinition: this.taskDefinition,
      desiredCount: 0,
      securityGroups: [workerSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
    });

    // ── Stack Outputs ─────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'WorkerTaskDefArn', {
      value: this.taskDefinition.taskDefinitionArn,
      exportName: 'WorkerTaskDefArn',
    });
  }
}
