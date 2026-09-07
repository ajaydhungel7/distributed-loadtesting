import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { InfraStack } from './infra-stack';
import { ControlPlaneStack } from './control-plane-stack';

interface GrafanaStackProps extends cdk.StackProps {
  infra: InfraStack;
  controlPlane: ControlPlaneStack;
}

export class GrafanaStack extends cdk.Stack {
  public readonly alb: elbv2.ApplicationLoadBalancer;

  constructor(scope: Construct, id: string, props: GrafanaStackProps) {
    super(scope, id, props);

    const { vpc, grafanaRepo } = props.infra;
    const { service: controlPlaneService } = props.controlPlane;
    const cluster = controlPlaneService.cluster;

    // ── Grafana Admin Password ────────────────────────────────────────────────
    const adminPassword = new secretsmanager.Secret(this, 'GrafanaAdminPassword', {
      secretName: 'loadtest/grafana-admin-password',
      generateSecretString: {
        passwordLength: 32,
        excludeCharacters: '"@/\\',
      },
    });

    // ── CloudWatch Log Group ──────────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, 'GrafanaLogGroup', {
      logGroupName: '/loadtest/grafana',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Execution Role ────────────────────────────────────────────────────────
    const executionRole = new iam.Role(this, 'GrafanaExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy'),
      ],
    });
    adminPassword.grantRead(executionRole);
    grafanaRepo.grantPull(executionRole);

    // ── Task Role ─────────────────────────────────────────────────────────────
    // Grafana needs CloudWatch read access to query metrics as a datasource
    const taskRole = new iam.Role(this, 'GrafanaTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'cloudwatch:GetMetricData',
        'cloudwatch:ListMetrics',
        'cloudwatch:GetMetricStatistics',
        'logs:StartQuery',
        'logs:StopQuery',
        'logs:GetQueryResults',
        'logs:DescribeLogGroups',
      ],
      resources: ['*'],
    }));

    // ── Task Definition ───────────────────────────────────────────────────────
    const taskDef = new ecs.FargateTaskDefinition(this, 'GrafanaTaskDef', {
      cpu: 512,
      memoryLimitMiB: 1024,
      taskRole,
      executionRole,
    });

    taskDef.addContainer('grafana', {
      // Custom image baked with datasource + dashboard provisioning files
      image: ecs.ContainerImage.fromEcrRepository(grafanaRepo, 'latest'),
      essential: true,
      portMappings: [{ containerPort: 3000, protocol: ecs.Protocol.TCP }],
      environment: {
        GF_AUTH_ANONYMOUS_ENABLED: 'false',
        GF_INSTALL_PLUGINS: '',
        AWS_REGION: this.region,
      },
      secrets: {
        GF_SECURITY_ADMIN_PASSWORD: ecs.Secret.fromSecretsManager(adminPassword),
      },
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: 'ecs',
      }),
    });

    // ── Security Groups ───────────────────────────────────────────────────────
    const albSg = new ec2.SecurityGroup(this, 'GrafanaAlbSg', {
      vpc,
      description: 'Allow HTTP from internet to Grafana ALB',
      allowAllOutbound: true,
    });
    albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'HTTP from internet');

    const serviceSg = new ec2.SecurityGroup(this, 'GrafanaServiceSg', {
      vpc,
      description: 'Grafana ECS service — allow traffic from ALB only',
      allowAllOutbound: true,
    });
    serviceSg.addIngressRule(albSg, ec2.Port.tcp(3000), 'From Grafana ALB only');

    // ── ALB ───────────────────────────────────────────────────────────────────
    this.alb = new elbv2.ApplicationLoadBalancer(this, 'GrafanaAlb', {
      vpc,
      internetFacing: true,
      securityGroup: albSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });

    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'GrafanaTg', {
      vpc,
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: '/api/health',
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(30),
      },
    });

    this.alb.addListener('GrafanaHttpListener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultTargetGroups: [targetGroup],
    });

    // ── Fargate Service ───────────────────────────────────────────────────────
    const service = new ecs.FargateService(this, 'GrafanaService', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      securityGroups: [serviceSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
    });

    service.attachToApplicationTargetGroup(targetGroup);

    // ── Stack Outputs ─────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'GrafanaUrl', {
      value: `http://${this.alb.loadBalancerDnsName}`,
      description: 'Grafana dashboard URL',
    });
  }
}
