import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';

import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { InfraStack } from './infra-stack';

interface ControlPlaneStackProps extends cdk.StackProps {
  infra: InfraStack;
}

export class ControlPlaneStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;
  public readonly service: ecs.FargateService;
  public readonly alb: elbv2.ApplicationLoadBalancer;

  constructor(scope: Construct, id: string, props: ControlPlaneStackProps) {
    super(scope, id, props);

    const { vpc, controlPlaneRepo } = props.infra;

    // ── ECS Cluster ───────────────────────────────────────────────────────────
    this.cluster = new ecs.Cluster(this, 'LoadTestCluster', {
      vpc,
      clusterName: 'loadtest',
      containerInsights: true,
    });

    // ── CloudWatch Log Group ──────────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, 'ControlPlaneLogGroup', {
      logGroupName: '/loadtest/control-plane',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Task Definition ───────────────────────────────────────────────────────
    // Execution role — allows ECS agent to pull image from ECR and write logs
    const executionRole = new iam.Role(this, 'ControlPlaneExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy'),
      ],
    });
    controlPlaneRepo.grantPull(executionRole);

    const taskDef = new ecs.FargateTaskDefinition(this, 'ControlPlaneTaskDef', {
      cpu: 512,
      memoryLimitMiB: 1024,
      taskRole: iam.Role.fromRoleArn(
        this,
        'ImportedControlPlaneTaskRole',
        cdk.Fn.importValue('ControlPlaneTaskRoleArn'),
      ),
      executionRole,
    });

    const imageTag = this.node.tryGetContext('imageTag') ?? 'latest';

    taskDef.addContainer('control-plane', {
      image: ecs.ContainerImage.fromEcrRepository(controlPlaneRepo, imageTag),
      essential: true,
      portMappings: [{ containerPort: 8000, protocol: ecs.Protocol.TCP }],
      environment: {
        TABLE_NAME: 'queue-jobs',
        TARGET_QUEUE_URL: `https://sqs.${this.region}.amazonaws.com/${this.account}/loadtest-target`,
        AWS_REGION: this.region,
      },
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: 'ecs',
      }),
      healthCheck: {
        command: ['CMD-SHELL', 'python3 -c "import urllib.request; urllib.request.urlopen(\'http://localhost:8000/health\')" || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // ── Security Groups ───────────────────────────────────────────────────────
    const albSg = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc,
      description: 'Allow HTTP from internet to ALB',
      allowAllOutbound: true,
    });
    albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'HTTP from internet');

    const serviceSg = new ec2.SecurityGroup(this, 'ControlPlaneServiceSg', {
      vpc,
      description: 'Control plane ECS service - allow traffic from ALB only',
      allowAllOutbound: true,
    });
    serviceSg.addIngressRule(albSg, ec2.Port.tcp(8000), 'From ALB only');

    // ── ALB ───────────────────────────────────────────────────────────────────
    this.alb = new elbv2.ApplicationLoadBalancer(this, 'ControlPlaneAlb', {
      vpc,
      internetFacing: true,
      securityGroup: albSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });

    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'ControlPlaneTg', {
      vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: '/health',
        protocol: elbv2.Protocol.HTTP,
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
    });

    this.alb.addListener('HttpListener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultTargetGroups: [targetGroup],
    });

    // ── Fargate Service ───────────────────────────────────────────────────────
    this.service = new ecs.FargateService(this, 'ControlPlaneService', {
      cluster: this.cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      securityGroups: [serviceSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
    });

    this.service.attachToApplicationTargetGroup(targetGroup);

    // ── Stack Outputs ─────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'AlbDnsName', {
      value: this.alb.loadBalancerDnsName,
      description: 'Control plane API endpoint',
      exportName: 'ControlPlaneAlbDnsName',
    });

    new cdk.CfnOutput(this, 'EcsClusterName', {
      value: this.cluster.clusterName,
      exportName: 'EcsClusterName',
    });
  }
}
