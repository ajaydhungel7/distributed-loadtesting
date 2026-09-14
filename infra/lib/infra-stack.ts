import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export class InfraStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly controlPlaneRepo: ecr.Repository;
  public readonly workerRepo: ecr.Repository;
  public readonly grafanaRepo: ecr.Repository;
  public readonly table: dynamodb.Table;
  public readonly resultsBucket: s3.Bucket;
  public readonly jobQueue: sqs.Queue;
  public readonly targetQueue: sqs.Queue;
  public readonly dlq: sqs.Queue;
  public readonly controlPlaneTaskRole: iam.Role;
  public readonly workerTaskRole: iam.Role;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── VPC ──────────────────────────────────────────────────────────────────
    this.vpc = new ec2.Vpc(this, 'LoadTestVpc', {
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        { name: 'Public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: 'Private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
      ],
    });

    // ── ECR ──────────────────────────────────────────────────────────────────
    this.controlPlaneRepo = new ecr.Repository(this, 'ControlPlaneRepo', {
      repositoryName: 'loadtest/control-plane',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    });

    this.workerRepo = new ecr.Repository(this, 'WorkerRepo', {
      repositoryName: 'loadtest/worker',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [{ maxImageCount: 10 }],
    });

    this.grafanaRepo = new ecr.Repository(this, 'GrafanaRepo', {
      repositoryName: 'loadtest/grafana',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [{ maxImageCount: 5 }],
    });

    // ── DynamoDB ─────────────────────────────────────────────────────────────
    this.table = new dynamodb.Table(this, 'LoadTestsTable', {
      tableName: 'load-tests',
      partitionKey: { name: 'jobId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.table.addGlobalSecondaryIndex({
      indexName: 'status-index',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── S3 ───────────────────────────────────────────────────────────────────
    this.resultsBucket = new s3.Bucket(this, 'ResultsBucket', {
      versioned: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [
        {
          enabled: true,
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(30),
            },
          ],
        },
      ],
    });

    // ── SQS ──────────────────────────────────────────────────────────────────
    this.dlq = new sqs.Queue(this, 'JobDlq', {
      queueName: 'loadtest-jobs-dlq',
      retentionPeriod: cdk.Duration.days(14),
      enforceSSL: true,
    });

    this.jobQueue = new sqs.Queue(this, 'JobQueue', {
      queueName: 'loadtest-jobs',
      visibilityTimeout: cdk.Duration.minutes(10),
      retentionPeriod: cdk.Duration.days(4),
      enforceSSL: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      deadLetterQueue: {
        queue: this.dlq,
        maxReceiveCount: 3,
      },
    });

    // Target queue — workers flood this with messages to showcase autoscaling
    this.targetQueue = new sqs.Queue(this, 'TargetQueue', {
      queueName: 'loadtest-target',
      visibilityTimeout: cdk.Duration.seconds(30),
      retentionPeriod: cdk.Duration.hours(1),
      enforceSSL: true,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
    });

    // ── IAM Roles ─────────────────────────────────────────────────────────────
    const ecsTaskPrincipal = new iam.ServicePrincipal('ecs-tasks.amazonaws.com');

    this.controlPlaneTaskRole = new iam.Role(this, 'ControlPlaneTaskRole', {
      roleName: 'ControlPlaneTaskRole',
      assumedBy: ecsTaskPrincipal,
      description: 'Task role for the load test control plane (least privilege)',
    });

    this.controlPlaneTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SQSSend',
      actions: ['sqs:SendMessage', 'sqs:GetQueueAttributes', 'sqs:GetQueueUrl'],
      resources: [this.jobQueue.queueArn],
    }));

    this.controlPlaneTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DynamoDBAccess',
      actions: [
        'dynamodb:GetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:Query',
        'dynamodb:Scan',
      ],
      resources: [this.table.tableArn, `${this.table.tableArn}/index/*`],
    }));

    this.workerTaskRole = new iam.Role(this, 'WorkerTaskRole', {
      roleName: 'WorkerTaskRole',
      assumedBy: ecsTaskPrincipal,
      description: 'Task role for load test workers (least privilege)',
    });

    this.workerTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SQSConsume',
      actions: [
        'sqs:ReceiveMessage',
        'sqs:DeleteMessage',
        'sqs:ChangeMessageVisibility',
        'sqs:GetQueueAttributes',
      ],
      resources: [this.jobQueue.queueArn],
    }));

    this.workerTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'SQSPublish',
      actions: ['sqs:SendMessage', 'sqs:SendMessageBatch', 'sqs:GetQueueAttributes'],
      resources: [this.targetQueue.queueArn],
    }));

    this.workerTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'DynamoDBWrite',
      actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:Query'],
      resources: [this.table.tableArn, `${this.table.tableArn}/index/*`],
    }));

    this.workerTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'S3Write',
      actions: ['s3:PutObject'],
      resources: [`${this.resultsBucket.bucketArn}/*`],
    }));

    this.workerTaskRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchLogs',
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: ['*'],
    }));

    // ── GitHub Actions OIDC ───────────────────────────────────────────────────
    // Allows GitHub Actions to assume an AWS role without storing any long-lived
    // credentials as secrets. GitHub's identity token is verified by AWS directly.
    // Import the existing GitHub OIDC provider — only one is allowed per account
    const githubOidcProvider = iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
      this,
      'GitHubOidcProvider',
      `arn:aws:iam::${this.account}:oidc-provider/token.actions.githubusercontent.com`,
    );

    const githubActionsRole = new iam.Role(this, 'GitHubActionsDeployRole', {
      roleName: 'GitHubActionsDeployRole',
      assumedBy: new iam.WebIdentityPrincipal(
        githubOidcProvider.openIdConnectProviderArn,
        {
          StringEquals: {
            'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
          },
          StringLike: {
            // GitHub now appends numeric user/repo IDs to the sub claim:
            // e.g. repo:user@<userId>/repo@<repoId>:ref:refs/heads/main
            // The wildcard prefix and suffix handle both old and new formats.
            'token.actions.githubusercontent.com:sub':
              'repo:ajaydhungel7*distributed-loadtesting*',
          },
        },
      ),
      description: 'Assumed by GitHub Actions via OIDC (no long-lived keys)',
    });

    // ECR push permissions — needed to docker push images
    githubActionsRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ECRPush',
      actions: [
        'ecr:GetAuthorizationToken',
        'ecr:BatchCheckLayerAvailability',
        'ecr:PutImage',
        'ecr:InitiateLayerUpload',
        'ecr:UploadLayerPart',
        'ecr:CompleteLayerUpload',
        'ecr:BatchGetImage',
        'ecr:GetDownloadUrlForLayer',
      ],
      resources: ['*'],
    }));

    // CDK deploy permissions — assumes the CDK bootstrap roles which have the
    // actual CloudFormation permissions. This keeps this role's blast radius small.
    githubActionsRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CDKBootstrapRoles',
      actions: ['sts:AssumeRole'],
      resources: [
        `arn:aws:iam::${this.account}:role/cdk-*`,
      ],
    }));

    new cdk.CfnOutput(this, 'GitHubActionsRoleArn', {
      value: githubActionsRole.roleArn,
      description: 'Paste this ARN into GitHub Actions workflow as role-to-assume',
    });

    // ── Stack Outputs ─────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'VpcId', { value: this.vpc.vpcId });
    new cdk.CfnOutput(this, 'JobQueueUrl', { value: this.jobQueue.queueUrl });
    new cdk.CfnOutput(this, 'TargetQueueUrl', { value: this.targetQueue.queueUrl });
    new cdk.CfnOutput(this, 'TableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'ResultsBucketName', { value: this.resultsBucket.bucketName });
    new cdk.CfnOutput(this, 'ControlPlaneRepoUri', { value: this.controlPlaneRepo.repositoryUri });
    new cdk.CfnOutput(this, 'WorkerRepoUri', { value: this.workerRepo.repositoryUri });
    new cdk.CfnOutput(this, 'ControlPlaneTaskRoleArn', {
      value: this.controlPlaneTaskRole.roleArn,
      exportName: 'ControlPlaneTaskRoleArn',
    });
    new cdk.CfnOutput(this, 'WorkerTaskRoleArn', {
      value: this.workerTaskRole.roleArn,
      exportName: 'WorkerTaskRoleArn',
    });
  }
}
