import * as cdk from 'aws-cdk-lib';
import * as appscaling from 'aws-cdk-lib/aws-applicationautoscaling';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';

export class LoadTestStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const controlPlaneTag = this.node.tryGetContext('controlPlaneTag') ?? 'latest';
    const workerTag       = this.node.tryGetContext('workerTag')       ?? 'latest';
    const grafanaTag      = this.node.tryGetContext('grafanaTag')      ?? 'latest';

    // ── VPC ───────────────────────────────────────────────────────────────────
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        { name: 'Public',  subnetType: ec2.SubnetType.PUBLIC,               cidrMask: 24 },
        { name: 'Private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,  cidrMask: 24 },
      ],
    });

    // ── ECR ───────────────────────────────────────────────────────────────────
    const controlPlaneRepo = new ecr.Repository(this, 'ControlPlaneRepo', {
      repositoryName: 'loadtest/control-plane',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    });

    const workerRepo = new ecr.Repository(this, 'WorkerRepo', {
      repositoryName: 'loadtest/worker',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    });

    const grafanaRepo = new ecr.Repository(this, 'GrafanaRepo', {
      repositoryName: 'loadtest/grafana',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [{ maxImageCount: 5 }],
    });

    // ── DynamoDB ──────────────────────────────────────────────────────────────
    const table = new dynamodb.Table(this, 'Table', {
      tableName: 'queue-jobs',
      partitionKey: { name: 'jobId',     type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'createdAt', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    table.addGlobalSecondaryIndex({
      indexName: 'status-index',
      partitionKey: { name: 'status',    type: dynamodb.AttributeType.STRING },
      sortKey:      { name: 'createdAt', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── S3 ────────────────────────────────────────────────────────────────────
    const resultsBucket = new s3.Bucket(this, 'ResultsBucket', {
      versioned: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ── SQS ───────────────────────────────────────────────────────────────────
    const dlq = new sqs.Queue(this, 'Dlq', {
      queueName: 'loadtest-jobs-dlq',
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });

    new sqs.Queue(this, 'JobQueue', {
      queueName: 'loadtest-jobs',
      visibilityTimeout: cdk.Duration.minutes(10),
      retentionPeriod: cdk.Duration.days(4),
      enforceSSL: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
    });

    // Workers consume from this queue; control plane floods it on job submit
    const targetQueue = new sqs.Queue(this, 'TargetQueue', {
      queueName: 'loadtest-target',
      visibilityTimeout: cdk.Duration.seconds(30),
      retentionPeriod: cdk.Duration.hours(1),
      enforceSSL: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    // ── IAM Task Roles ────────────────────────────────────────────────────────
    const ecsTaskPrincipal = new iam.ServicePrincipal('ecs-tasks.amazonaws.com');

    const controlPlaneTaskRole = new iam.Role(this, 'ControlPlaneTaskRole', {
      assumedBy: ecsTaskPrincipal,
    });
    controlPlaneTaskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['sqs:SendMessage', 'sqs:SendMessageBatch', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl'],
      resources: [targetQueue.queueArn],
    }));
    controlPlaneTaskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query', 'dynamodb:Scan'],
      resources: [table.tableArn, `${table.tableArn}/index/*`],
    }));

    const workerTaskRole = new iam.Role(this, 'WorkerTaskRole', {
      assumedBy: ecsTaskPrincipal,
    });
    workerTaskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:ChangeMessageVisibility', 'sqs:GetQueueAttributes'],
      resources: [targetQueue.queueArn],
    }));
    workerTaskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query'],
      resources: [table.tableArn, `${table.tableArn}/index/*`],
    }));
    workerTaskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:PutObject'],
      resources: [`${resultsBucket.bucketArn}/*`],
    }));
    workerTaskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: ['*'],
    }));

    // ── GitHub Actions OIDC ───────────────────────────────────────────────────
    const githubOidcProvider = iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
      this, 'GitHubOidcProvider',
      `arn:aws:iam::${this.account}:oidc-provider/token.actions.githubusercontent.com`,
    );
    const githubActionsRole = new iam.Role(this, 'GitHubActionsDeployRole', {
      assumedBy: new iam.WebIdentityPrincipal(githubOidcProvider.openIdConnectProviderArn, {
        StringEquals: { 'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com' },
        StringLike:   { 'token.actions.githubusercontent.com:sub': 'repo:ajaydhungel7*distributed-loadtesting*' },
      }),
    });
    githubActionsRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'ecr:GetAuthorizationToken', 'ecr:BatchCheckLayerAvailability', 'ecr:PutImage',
        'ecr:InitiateLayerUpload', 'ecr:UploadLayerPart', 'ecr:CompleteLayerUpload',
        'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer',
      ],
      resources: ['*'],
    }));
    githubActionsRole.addToPolicy(new iam.PolicyStatement({
      actions: ['sts:AssumeRole'],
      resources: [`arn:aws:iam::${this.account}:role/cdk-*`],
    }));

    // ── ECS Cluster ───────────────────────────────────────────────────────────
    const cluster = new ecs.Cluster(this, 'Cluster', {
      vpc,
      containerInsights: true,
    });

    // ── Control Plane ─────────────────────────────────────────────────────────
    const cpExecutionRole = new iam.Role(this, 'ControlPlaneExecutionRole', {
      assumedBy: ecsTaskPrincipal,
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy')],
    });
    controlPlaneRepo.grantPull(cpExecutionRole);

    const cpLogGroup = new logs.LogGroup(this, 'ControlPlaneLogGroup', {
      logGroupName: '/loadtest/control-plane',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const cpTaskDef = new ecs.FargateTaskDefinition(this, 'ControlPlaneTaskDef', {
      cpu: 512, memoryLimitMiB: 1024,
      taskRole: controlPlaneTaskRole,
      executionRole: cpExecutionRole,
    });
    cpTaskDef.addContainer('control-plane', {
      image: ecs.ContainerImage.fromEcrRepository(controlPlaneRepo, controlPlaneTag),
      essential: true,
      portMappings: [{ containerPort: 8000, protocol: ecs.Protocol.TCP }],
      environment: {
        TABLE_NAME:       'queue-jobs',
        TARGET_QUEUE_URL: `https://sqs.${this.region}.amazonaws.com/${this.account}/loadtest-target`,
        AWS_REGION:       this.region,
      },
      logging: ecs.LogDrivers.awsLogs({ logGroup: cpLogGroup, streamPrefix: 'ecs' }),
      healthCheck: {
        command: ['CMD-SHELL', 'python3 -c "import urllib.request; urllib.request.urlopen(\'http://localhost:8000/health\')" || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    const cpAlbSg = new ec2.SecurityGroup(this, 'CpAlbSg', {
      vpc, description: 'Allow HTTP from internet to control plane ALB', allowAllOutbound: true,
    });
    cpAlbSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'HTTP from internet');

    const cpServiceSg = new ec2.SecurityGroup(this, 'CpServiceSg', {
      vpc, description: 'Control plane service - ALB traffic only', allowAllOutbound: true,
    });
    cpServiceSg.addIngressRule(cpAlbSg, ec2.Port.tcp(8000), 'From ALB only');

    const cpAlb = new elbv2.ApplicationLoadBalancer(this, 'ControlPlaneAlb', {
      vpc, internetFacing: true, securityGroup: cpAlbSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });
    const cpTg = new elbv2.ApplicationTargetGroup(this, 'ControlPlaneTg', {
      vpc, port: 8000, protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: '/health', protocol: elbv2.Protocol.HTTP, healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(30), timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2, unhealthyThresholdCount: 3,
      },
    });
    cpAlb.addListener('CpHttpListener', {
      port: 80, protocol: elbv2.ApplicationProtocol.HTTP, defaultTargetGroups: [cpTg],
    });

    const cpService = new ecs.FargateService(this, 'ControlPlaneService', {
      cluster, taskDefinition: cpTaskDef, desiredCount: 1,
      securityGroups: [cpServiceSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
    });
    cpService.attachToApplicationTargetGroup(cpTg);

    // ── Worker ────────────────────────────────────────────────────────────────
    const workerExecutionRole = new iam.Role(this, 'WorkerExecutionRole', {
      assumedBy: ecsTaskPrincipal,
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy')],
    });
    workerRepo.grantPull(workerExecutionRole);

    const workerLogGroup = new logs.LogGroup(this, 'WorkerLogGroup', {
      logGroupName: '/loadtest/workers',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const workerTaskDef = new ecs.FargateTaskDefinition(this, 'WorkerTaskDef', {
      cpu: 512, memoryLimitMiB: 1024,
      taskRole: workerTaskRole,
      executionRole: workerExecutionRole,
    });
    workerTaskDef.addContainer('worker', {
      image: ecs.ContainerImage.fromEcrRepository(workerRepo, workerTag),
      essential: true,
      environment: {
        TABLE_NAME:       'queue-jobs',
        TARGET_QUEUE_URL: `https://sqs.${this.region}.amazonaws.com/${this.account}/loadtest-target`,
        RESULTS_BUCKET:   resultsBucket.bucketName,
        AWS_REGION:       this.region,
      },
      logging: ecs.LogDrivers.awsLogs({ logGroup: workerLogGroup, streamPrefix: 'ecs' }),
    });

    const workerSg = new ec2.SecurityGroup(this, 'WorkerSg', {
      vpc, description: 'Worker - outbound only', allowAllOutbound: true,
    });

    const workerService = new ecs.FargateService(this, 'WorkerService', {
      cluster, taskDefinition: workerTaskDef, desiredCount: 0,
      securityGroups: [workerSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
    });

    // ── Autoscaling ───────────────────────────────────────────────────────────
    const scalableTarget = new appscaling.ScalableTarget(this, 'WorkerScalableTarget', {
      serviceNamespace: appscaling.ServiceNamespace.ECS,
      scalableDimension: 'ecs:service:DesiredCount',
      resourceId: `service/${cluster.clusterName}/${workerService.serviceName}`,
      minCapacity: 0,
      maxCapacity: 50,
    });

    const queueDepthMetric = new cloudwatch.Metric({
      namespace: 'AWS/SQS',
      metricName: 'ApproximateNumberOfMessagesVisible',
      dimensionsMap: { QueueName: targetQueue.queueName },
      statistic: 'Maximum',
      period: cdk.Duration.minutes(1),
    });

    const scaleOutAction = new appscaling.StepScalingAction(this, 'ScaleOutAction', {
      scalingTarget: scalableTarget,
      adjustmentType: appscaling.AdjustmentType.EXACT_CAPACITY,
      metricAggregationType: appscaling.MetricAggregationType.MAXIMUM,
    });
    scaleOutAction.addAdjustment({ adjustment: 5,  lowerBound: 0,  upperBound: 5  });
    scaleOutAction.addAdjustment({ adjustment: 10, lowerBound: 5,  upperBound: 10 });
    scaleOutAction.addAdjustment({ adjustment: 20, lowerBound: 10, upperBound: 20 });
    scaleOutAction.addAdjustment({ adjustment: 50, lowerBound: 20 });

    const scaleOutAlarm = new cloudwatch.Alarm(this, 'QueueDepthAlarm', {
      alarmName: 'LoadTestWorkerQueueDepth',
      metric: queueDepthMetric,
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    scaleOutAlarm.addAlarmAction(new cloudwatch_actions.ApplicationScalingAction(scaleOutAction));

    const scaleInAction = new appscaling.StepScalingAction(this, 'ScaleInAction', {
      scalingTarget: scalableTarget,
      adjustmentType: appscaling.AdjustmentType.EXACT_CAPACITY,
      metricAggregationType: appscaling.MetricAggregationType.MAXIMUM,
    });
    scaleInAction.addAdjustment({ adjustment: 0, lowerBound: 0 });

    const scaleInAlarm = new cloudwatch.Alarm(this, 'QueueEmptyAlarm', {
      metric: queueDepthMetric,
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      evaluationPeriods: 5,
      datapointsToAlarm: 5,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    scaleInAlarm.addAlarmAction(new cloudwatch_actions.ApplicationScalingAction(scaleInAction));

    // ── Grafana ───────────────────────────────────────────────────────────────
    const grafanaPassword = new secretsmanager.Secret(this, 'GrafanaAdminPassword', {
      secretName: 'loadtest/grafana-admin-password',
      generateSecretString: { passwordLength: 32, excludeCharacters: '"@/\\' },
    });

    const grafanaExecutionRole = new iam.Role(this, 'GrafanaExecutionRole', {
      assumedBy: ecsTaskPrincipal,
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy')],
    });
    grafanaPassword.grantRead(grafanaExecutionRole);
    grafanaRepo.grantPull(grafanaExecutionRole);

    const grafanaTaskRole = new iam.Role(this, 'GrafanaTaskRole', {
      assumedBy: ecsTaskPrincipal,
    });
    grafanaTaskRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'cloudwatch:GetMetricData', 'cloudwatch:ListMetrics', 'cloudwatch:GetMetricStatistics',
        'logs:StartQuery', 'logs:StopQuery', 'logs:GetQueryResults', 'logs:DescribeLogGroups',
      ],
      resources: ['*'],
    }));

    const grafanaLogGroup = new logs.LogGroup(this, 'GrafanaLogGroup', {
      logGroupName: '/loadtest/grafana',
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const grafanaTaskDef = new ecs.FargateTaskDefinition(this, 'GrafanaTaskDef', {
      cpu: 512, memoryLimitMiB: 1024,
      taskRole: grafanaTaskRole,
      executionRole: grafanaExecutionRole,
    });
    grafanaTaskDef.addContainer('grafana', {
      image: ecs.ContainerImage.fromEcrRepository(grafanaRepo, grafanaTag),
      essential: true,
      portMappings: [{ containerPort: 3000, protocol: ecs.Protocol.TCP }],
      environment: { GF_AUTH_ANONYMOUS_ENABLED: 'false', GF_INSTALL_PLUGINS: '', AWS_REGION: this.region },
      secrets: { GF_SECURITY_ADMIN_PASSWORD: ecs.Secret.fromSecretsManager(grafanaPassword) },
      logging: ecs.LogDrivers.awsLogs({ logGroup: grafanaLogGroup, streamPrefix: 'ecs' }),
    });

    const grafanaAlbSg = new ec2.SecurityGroup(this, 'GrafanaAlbSg', {
      vpc, description: 'Allow HTTP from internet to Grafana ALB', allowAllOutbound: true,
    });
    grafanaAlbSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'HTTP from internet');

    const grafanaServiceSg = new ec2.SecurityGroup(this, 'GrafanaServiceSg', {
      vpc, description: 'Grafana service - ALB traffic only', allowAllOutbound: true,
    });
    grafanaServiceSg.addIngressRule(grafanaAlbSg, ec2.Port.tcp(3000), 'From Grafana ALB only');

    const grafanaAlb = new elbv2.ApplicationLoadBalancer(this, 'GrafanaAlb', {
      vpc, internetFacing: true, securityGroup: grafanaAlbSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });
    const grafanaTg = new elbv2.ApplicationTargetGroup(this, 'GrafanaTg', {
      vpc, port: 3000, protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: { path: '/api/health', healthyHttpCodes: '200', interval: cdk.Duration.seconds(30) },
    });
    grafanaAlb.addListener('GrafanaHttpListener', {
      port: 80, protocol: elbv2.ApplicationProtocol.HTTP, defaultTargetGroups: [grafanaTg],
    });

    const grafanaService = new ecs.FargateService(this, 'GrafanaService', {
      cluster, taskDefinition: grafanaTaskDef, desiredCount: 1,
      securityGroups: [grafanaServiceSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
    });
    grafanaService.attachToApplicationTargetGroup(grafanaTg);

    // ── Outputs ───────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'ControlPlaneUrl', {
      value: cpAlb.loadBalancerDnsName,
      description: 'Control plane API endpoint',
    });
    new cdk.CfnOutput(this, 'GrafanaUrl', {
      value: `http://${grafanaAlb.loadBalancerDnsName}`,
      description: 'Grafana dashboard URL',
    });
    new cdk.CfnOutput(this, 'TargetQueueUrl', { value: targetQueue.queueUrl });
    new cdk.CfnOutput(this, 'TableName',       { value: table.tableName });
  }
}
