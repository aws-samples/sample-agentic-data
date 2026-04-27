import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecs_patterns from 'aws-cdk-lib/aws-ecs-patterns';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as path from 'path';
import { Construct } from 'constructs';

// ═══════════════════════════════════════════════════════════════
// Agentic Data CDK Stack
// Multi-Agent Data Analytics Platform
// ═══════════════════════════════════════════════════════════════

export class AgenticDataStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Auto-detect China region from actual deploy region
    const isChina = this.region.startsWith('cn-');
    const arnPartition = isChina ? 'aws-cn' : 'aws';

    // ── Parameters ──
    const adminPassword = new cdk.CfnParameter(this, 'AdminPassword', {
      type: 'String',
      noEcho: true,
      minLength: 8,
      description: 'Admin user password for local auth (min 8 chars, no default)',
    });

    const allowedCidrs = new cdk.CfnParameter(this, 'AllowedCIDRs', {
      type: 'String',
      default: '10.0.0.0/8',
      description: 'Comma-separated CIDRs for ALB access (NEVER use 0.0.0.0/0)',
    });

    // Model config is OPTIONAL at deploy time — can be configured later via Admin UI
    const modelProvider = this.node.tryGetContext('model_provider') || (isChina ? 'openai' : 'bedrock');
    const modelId = this.node.tryGetContext('model_id') || (isChina ? '' : 'us.anthropic.claude-sonnet-4-6');
    const openaiBaseUrl = this.node.tryGetContext('openai_base_url') || '';
    const openaiApiKey = this.node.tryGetContext('openai_api_key') || '';
    const openaiModel = this.node.tryGetContext('openai_model') || '';

    const authProvider = this.node.tryGetContext('auth_provider') || 'local';
    const vpcId = this.node.tryGetContext('vpc_id') || '';

    // ── Datasource config (optional at deploy time) ──
    const rdsEngine = this.node.tryGetContext('rds_engine') || '';      // 'mysql' | 'postgresql' | ''
    const rdsHost = this.node.tryGetContext('rds_host') || '';
    const rdsPort = this.node.tryGetContext('rds_port') || '';
    const rdsDatabase = this.node.tryGetContext('rds_database') || '';
    const rdsUser = this.node.tryGetContext('rds_user') || '';
    const rdsPassword = this.node.tryGetContext('rds_password') || '';
    const athenaDatabase = this.node.tryGetContext('athena_database') || '';
    const athenaOutput = this.node.tryGetContext('athena_output') || '';

    // ═══════ Networking ═══════
    const vpc = vpcId
      ? ec2.Vpc.fromLookup(this, 'ExistingVpc', { vpcId })
      : new ec2.Vpc(this, 'Vpc', {
          maxAzs: 2,
          natGateways: 1,
          subnetConfiguration: [
            { name: 'Public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
            { name: 'Private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
          ],
        });

    // ALB Security Group — NEVER 0.0.0.0/0
    const albSg = new ec2.SecurityGroup(this, 'AlbSg', {
      vpc,
      description: 'ALB - allowed CIDRs only',
      allowAllOutbound: true,
    });

    // Use a Lambda-backed CustomResource to dynamically add SG ingress rules
    // This avoids the Fn::Select index-out-of-bounds issue with variable-length CIDR lists
    const sgRuleFn = new lambda.Function(this, 'SgRuleFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline(`
import json, boto3, urllib.request

def handler(event, context):
    response_url = event.get('ResponseURL', '')
    try:
        if event['RequestType'] == 'Delete':
            # SG rules are deleted when SG is deleted
            send_response(response_url, event, 'SUCCESS')
            return
        
        sg_id = event['ResourceProperties']['SecurityGroupId']
        cidrs = event['ResourceProperties']['CIDRs'].split(',')
        ec2 = boto3.client('ec2')
        
        # Revoke existing custom rules first (idempotent)
        try:
            sg = ec2.describe_security_groups(GroupIds=[sg_id])['SecurityGroups'][0]
            for perm in sg.get('IpPermissions', []):
                if perm.get('FromPort') == 80 and perm.get('ToPort') == 80:
                    ec2.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=[perm])
        except: pass
        
        # Add rules for each CIDR
        for cidr in cidrs:
            cidr = cidr.strip()
            if not cidr: continue
            try:
                ec2.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=[{
                        'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80,
                        'IpRanges': [{'CidrIp': cidr, 'Description': f'Allowed: {cidr}'}],
                    }],
                )
            except Exception as e:
                if 'Duplicate' not in str(e): raise
        
        # Add CloudFront managed prefix list (Global regions only — not available in China)
        import os
        region = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', ''))
        if not region.startswith('cn-'):
            try:
                pl_resp = ec2.describe_managed_prefix_lists(
                    Filters=[{'Name': 'prefix-list-name', 'Values': ['com.amazonaws.global.cloudfront.origin-facing']}]
                )
                pl_id = pl_resp['PrefixLists'][0]['PrefixListId'] if pl_resp['PrefixLists'] else None
                if pl_id:
                    ec2.authorize_security_group_ingress(
                        GroupId=sg_id,
                        IpPermissions=[{
                            'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80,
                            'PrefixListIds': [{'PrefixListId': pl_id, 'Description': 'CloudFront'}],
                        }],
                    )
            except Exception as e:
                if 'Duplicate' not in str(e): print(f'CloudFront prefix warning: {e}')
        else:
            print('China region detected, skipping CloudFront managed prefix list')
        
        send_response(response_url, event, 'SUCCESS')
    except Exception as e:
        print(f'Error: {e}')
        send_response(response_url, event, 'FAILED', str(e))

def send_response(url, event, status, reason=''):
    if not url: return
    body = json.dumps({
        'Status': status,
        'Reason': reason or 'OK',
        'PhysicalResourceId': event.get('PhysicalResourceId', 'sg-rules'),
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
    }).encode()
    req = urllib.request.Request(url, data=body, method='PUT',
                                headers={'Content-Type': ''})
    urllib.request.urlopen(req)
`),
      timeout: cdk.Duration.seconds(60),
    });
    sgRuleFn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'ec2:AuthorizeSecurityGroupIngress',
        'ec2:RevokeSecurityGroupIngress',
        'ec2:DescribeSecurityGroups',
        'ec2:DescribeManagedPrefixLists',
      ],
      resources: ['*'],
    }));

    new cdk.CustomResource(this, 'AlbSgRules', {
      serviceToken: sgRuleFn.functionArn,
      properties: {
        SecurityGroupId: albSg.securityGroupId,
        CIDRs: allowedCidrs.valueAsString,
      },
    });

    // App Security Group
    const appSg = new ec2.SecurityGroup(this, 'AppSg', {
      vpc,
      description: 'App - ALB only',
      allowAllOutbound: true,
    });
    appSg.addIngressRule(albSg, ec2.Port.tcp(8501), 'From ALB');

    // ═══════ Storage: DynamoDB ═══════
    const tableNames = {
      config: 'agentic-data-config',
      chatHistory: 'agentic-data-chat-history',
      cost: 'agentic-data-cost',
      events: 'agentic-data-events',
      feedback: 'agentic-data-feedback',
    };

    const configTable = new dynamodb.Table(this, 'ConfigTable', {
      tableName: tableNames.config,
      partitionKey: { name: 'config_key', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const chatTable = new dynamodb.Table(this, 'ChatTable', {
      tableName: tableNames.chatHistory,
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      timeToLiveAttribute: 'ttl',
    });

    const costTable = new dynamodb.Table(this, 'CostTable', {
      tableName: tableNames.cost,
      partitionKey: { name: 'date', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const eventsTable = new dynamodb.Table(this, 'EventsTable', {
      tableName: tableNames.events,
      partitionKey: { name: 'event_type', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const feedbackTable = new dynamodb.Table(this, 'FeedbackTable', {
      tableName: tableNames.feedback,
      partitionKey: { name: 'session_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ═══════ Storage: S3 ═══════
    const dataBucket = new s3.Bucket(this, 'DataBucket', {
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    });

    // ═══════ IAM: Task Role ═══════
    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Agentic Data ECS Task Role',
    });

    // DynamoDB
    configTable.grantReadWriteData(taskRole);
    chatTable.grantReadWriteData(taskRole);
    costTable.grantReadWriteData(taskRole);
    eventsTable.grantReadWriteData(taskRole);
    feedbackTable.grantReadWriteData(taskRole);

    // S3
    dataBucket.grantReadWrite(taskRole);

    // Bedrock (Global regions only — not available in China)
    if (modelProvider === 'bedrock' && !isChina) {
      taskRole.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: [
          `arn:${arnPartition}:bedrock:*::foundation-model/*`,
          `arn:${arnPartition}:bedrock:*:*:inference-profile/*`,
        ],
      }));
    }

    // Athena + Glue (for data lake queries)
    taskRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'athena:StartQueryExecution',
        'athena:GetQueryExecution',
        'athena:GetQueryResults',
        'athena:StopQueryExecution',
        'glue:GetTable',
        'glue:GetTables',
        'glue:GetDatabase',
        'glue:GetDatabases',
      ],
      resources: ['*'],
    }));

    // SSM for ECS Exec
    taskRole.addManagedPolicy(
      iam.ManagedPolicy.fromManagedPolicyArn(this, 'SSMPolicy',
        `arn:${arnPartition}:iam::aws:policy/AmazonSSMManagedInstanceCore`)
    );

    // ═══════ ECS Fargate ═══════
    const cluster = new ecs.Cluster(this, 'Cluster', {
      vpc,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      memoryLimitMiB: 2048,
      cpu: 1024,
      taskRole,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    const logGroup = new logs.LogGroup(this, 'AppLogGroup', {
      logGroupName: '/agentic-data/app',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Container Image: ECR (pre-built) or local build ──
    const ecrImageUri = this.node.tryGetContext('ecr_image') || '';

    // Grant ECR pull if using pre-built image from another account/region
    if (ecrImageUri) {
      taskRole.addToPolicy(new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
          'ecr:GetAuthorizationToken',
        ],
        resources: ['*'],
      }));
    }

    // Environment variables
    const bedrockRegion = this.node.tryGetContext('bedrock_region') || (isChina ? this.region : 'us-east-1');
    const envVars: Record<string, string> = {
      AGENTIC_AUTO_REGION: this.region,
      BEDROCK_REGION: bedrockRegion,
      AGENTIC_AUTO_CONFIG_TABLE: tableNames.config,
      AGENTIC_AUTO_CHAT_TABLE: tableNames.chatHistory,
      AGENTIC_AUTO_COST_TABLE: tableNames.cost,
      AGENTIC_AUTO_EVENTS_TABLE: tableNames.events,
      AGENTIC_AUTO_FEEDBACK_TABLE: tableNames.feedback,
      AGENTIC_AUTO_DATA_BUCKET: dataBucket.bucketName,
      AGENTIC_AUTO_REPORTS_BUCKET: dataBucket.bucketName,
      AGENTIC_DATA_ENV: 'production',
      AUTH_PROVIDER: authProvider,
      MODEL_PROVIDER: modelProvider,
      DEFAULT_MODEL: modelId,
    };

    if (openaiBaseUrl) envVars['OPENAI_BASE_URL'] = openaiBaseUrl;
    if (openaiApiKey) envVars['OPENAI_API_KEY'] = openaiApiKey;
    if (openaiModel) envVars['OPENAI_MODEL'] = openaiModel;

    // ── RDS datasource env vars ──
    if (rdsEngine === 'mysql') {
      envVars['MYSQL_HOST'] = rdsHost;
      envVars['MYSQL_PORT'] = rdsPort || '3306';
      envVars['MYSQL_DATABASE'] = rdsDatabase;
      envVars['MYSQL_USER'] = rdsUser;
      envVars['MYSQL_PASSWORD'] = rdsPassword;
    }
    if (rdsEngine === 'postgresql') {
      envVars['POSTGRES_HOST'] = rdsHost;
      envVars['POSTGRES_PORT'] = rdsPort || '5432';
      envVars['POSTGRES_DATABASE'] = rdsDatabase;
      envVars['POSTGRES_USER'] = rdsUser;
      envVars['POSTGRES_PASSWORD'] = rdsPassword;
    }
    // ── Athena ──
    if (athenaDatabase) {
      envVars['ATHENA_DATABASE'] = athenaDatabase;
      envVars['ATHENA_OUTPUT'] = athenaOutput;
    }

    const container = taskDef.addContainer('App', {
      image: ecrImageUri
        ? ecs.ContainerImage.fromRegistry(ecrImageUri)
        : ecs.ContainerImage.fromAsset(path.join(__dirname, '../../'), {
            file: 'deploy/Dockerfile',
            exclude: ['cdk', 'node_modules', '.git', '__pycache__', '*.pyc'],
            platform: cdk.aws_ecr_assets.Platform.LINUX_ARM64,
          }),
      environment: envVars,
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: 'app',
      }),
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://localhost:8501/api/health || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(10),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    container.addPortMappings({ containerPort: 8501 });

    // ALB + Fargate Service
    const alb = new elbv2.ApplicationLoadBalancer(this, 'ALB', {
      vpc,
      internetFacing: true,
      securityGroup: albSg,
    });

    const listener = alb.addListener('Listener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
    });

    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      securityGroups: [appSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      assignPublicIp: false,
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      enableExecuteCommand: true,
    });

    const targetGroup = listener.addTargets('AppTarget', {
      port: 8501,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
      healthCheck: {
        path: '/api/health',
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(10),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
      deregistrationDelay: cdk.Duration.seconds(30),
      // SSE needs long idle timeout
      stickinessCookieDuration: cdk.Duration.hours(1),
    });

    // ALB idle timeout for SSE streaming
    alb.setAttribute('idle_timeout.timeout_seconds', '120');

    // ═══════ Init Data: CustomResource Lambda ═══════
    const initFn = new lambda.Function(this, 'InitDataFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline(this.getInitLambdaCode()),
      timeout: cdk.Duration.seconds(60),
      environment: {
        CONFIG_TABLE: tableNames.config,
        ADMIN_PASSWORD: adminPassword.valueAsString,
        MODEL_PROVIDER: modelProvider,
        DEFAULT_MODEL: modelId,
        AUTH_PROVIDER: authProvider,
        OPENAI_BASE_URL: openaiBaseUrl,
        OPENAI_API_KEY: openaiApiKey,
        OPENAI_MODEL: openaiModel,
        RDS_ENGINE: rdsEngine,
        RDS_HOST: rdsHost,
        RDS_PORT: rdsPort,
        RDS_DATABASE: rdsDatabase,
        RDS_USER: rdsUser,
        RDS_PASSWORD: rdsPassword,
        ATHENA_DATABASE: athenaDatabase,
        ATHENA_OUTPUT: athenaOutput,
      },
    });
    configTable.grantReadWriteData(initFn);

    new cr.Provider(this, 'InitProvider', {
      onEventHandler: initFn,
    });

    new cdk.CustomResource(this, 'InitData', {
      serviceToken: cdk.Lazy.string({
        produce: () => {
          const provider = this.node.findChild('InitProvider') as cr.Provider;
          return provider.serviceToken;
        },
      }),
      properties: {
        // Force update on password or model change
        adminPassword: adminPassword.valueAsString,
        modelProvider,
        timestamp: Date.now().toString(),
      },
    });

    // ═══════ Outputs ═══════
    new cdk.CfnOutput(this, 'ApplicationURL', {
      value: `http://${alb.loadBalancerDnsName}`,
      description: 'Agentic ChatBI URL',
    });

    new cdk.CfnOutput(this, 'AdminUser', {
      value: 'admin',
      description: 'Default admin username',
    });

    new cdk.CfnOutput(this, 'ConfigTableName', {
      value: configTable.tableName,
    });

    new cdk.CfnOutput(this, 'DataBucketName', {
      value: dataBucket.bucketName,
    });

    if (rdsHost) {
      new cdk.CfnOutput(this, 'RdsEndpoint', {
        value: `${rdsEngine}://${rdsHost}:${rdsPort || (rdsEngine === 'mysql' ? '3306' : '5432')}/${rdsDatabase}`,
        description: `RDS ${rdsEngine} endpoint`,
      });
    }

    if (athenaDatabase) {
      new cdk.CfnOutput(this, 'AthenaDatabase', {
        value: athenaDatabase,
        description: 'Athena database',
      });
    }
  }

  /** Inline Python code for the init Lambda */
  private getInitLambdaCode(): string {
    return `
import json, os, hashlib, time
import boto3

def handler(event, context):
    """Initialize DynamoDB config table with empty platform data."""
    if event.get('RequestType') == 'Delete':
        return {'PhysicalResourceId': event.get('PhysicalResourceId', 'init')}
    
    table = boto3.resource('dynamodb').Table(os.environ['CONFIG_TABLE'])
    
    # Hash admin password
    pw = os.environ['ADMIN_PASSWORD']  # Required — set via CFN parameter
    pw_hash = hashlib.sha256(pw.encode()).hexdigest()
    
    # Model config
    model_provider = os.environ.get('MODEL_PROVIDER', 'bedrock')
    default_model = os.environ.get('DEFAULT_MODEL', '')
    auth_provider = os.environ.get('AUTH_PROVIDER', 'local')
    
    # Build default model entries (empty list if no model configured at deploy time)
    default_models = []
    if model_provider == 'bedrock' and default_model:
        default_model_id = default_model
        default_models = [
            {'name': 'Claude Sonnet 4.6', 'endpoint': 'bedrock', 'model_id': 'us.anthropic.claude-sonnet-4-6', 'protocol': 'bedrock', 'provider': 'bedrock'},
            {'name': 'Claude Haiku 4.5', 'endpoint': 'bedrock', 'model_id': 'us.anthropic.claude-haiku-4-5-20251001-v1:0', 'protocol': 'bedrock', 'provider': 'bedrock'},
        ]
        if default_model_id not in [m['model_id'] for m in default_models]:
            default_models.insert(0, {'name': 'Bedrock Default', 'endpoint': 'bedrock', 'model_id': default_model_id, 'protocol': 'bedrock', 'provider': 'bedrock'})
    elif model_provider == 'bedrock' and not default_model:
        # Global region, Bedrock available but no specific model chosen — use defaults
        default_models = [
            {'name': 'Claude Sonnet 4.6', 'endpoint': 'bedrock', 'model_id': 'us.anthropic.claude-sonnet-4-6', 'protocol': 'bedrock', 'provider': 'bedrock'},
            {'name': 'Claude Haiku 4.5', 'endpoint': 'bedrock', 'model_id': 'us.anthropic.claude-haiku-4-5-20251001-v1:0', 'protocol': 'bedrock', 'provider': 'bedrock'},
        ]
    else:
        # OpenAI-compatible: for China region use third-party LLM providers
        base_url = os.environ.get('OPENAI_BASE_URL', '')
        api_key = os.environ.get('OPENAI_API_KEY', '')
        model_name = os.environ.get('OPENAI_MODEL', '')
        # Only add model entry if at least base_url is provided
        if base_url:
            provider_label = 'OpenAI Compatible'
            if 'siliconflow' in base_url.lower():
                provider_label = 'SiliconFlow'
            elif 'zhipuai' in base_url.lower() or 'bigmodel' in base_url.lower():
                provider_label = 'ZhipuAI (GLM)'
            default_models = [{
                'name': provider_label,
                'endpoint': base_url,
                'model_id': model_name,
                'api_key': api_key,
                'protocol': 'openai',
                'provider': 'third-party',
            }]
        # else: empty list — user configures models via Admin UI after deployment
        print(f'Model config: provider={model_provider}, models={len(default_models)} entries')
    
    # Initial config items — only write if key doesn't exist (don't overwrite on update)
    # Pre-configure datasources from deploy-time settings
    rds_engine = os.environ.get('RDS_ENGINE', '')
    rds_host = os.environ.get('RDS_HOST', '')
    rds_port = os.environ.get('RDS_PORT', '')
    rds_database = os.environ.get('RDS_DATABASE', '')
    rds_user = os.environ.get('RDS_USER', '')
    rds_password = os.environ.get('RDS_PASSWORD', '')
    athena_db = os.environ.get('ATHENA_DATABASE', '')
    athena_output = os.environ.get('ATHENA_OUTPUT', '')
    
    datasources = []
    if rds_host and rds_engine:
        ds_port = rds_port or ('3306' if rds_engine == 'mysql' else '5432')
        datasources.append({
            'id': f'deploy-rds-{rds_engine}',
            'name': f'{rds_engine.upper()} ({rds_database})',
            'type': 'RDS',
            'icon': '\U0001f42c' if rds_engine == 'mysql' else '\U0001f418',
            'config': {
                'engine': rds_engine,
                'host': rds_host,
                'port': ds_port,
                'database': rds_database,
                'username': rds_user,
                'password': rds_password,
            },
            'description': f'{rds_engine} {rds_host}/{rds_database}',
            'custom': True, 'status': 'connected', 'enabled': True,
        })
        print(f'Pre-configured RDS {rds_engine} datasource: {rds_host}/{rds_database}')
    if athena_db:
        datasources.append({
            'id': 'deploy-athena',
            'name': f'Athena ({athena_db})',
            'type': 'Athena',
            'icon': '\U0001f4ca',
            'config': {
                'database': athena_db,
                'region': os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', '')),
            },
            'database': athena_db,
            'output_location': athena_output,
            'custom': True, 'status': 'connected', 'enabled': True,
        })
        print(f'Pre-configured Athena datasource: {athena_db}')
    
    init_items = {
        'custom_datasources': json.dumps(datasources),
        'custom_tools': '{}',
        'ext_mcp_servers': '[]',
        'skills': '{}',
        'semantic_layer': json.dumps({'metrics': {}, 'dimensions': {}, 'synonyms': {}, 'templates': {}}),
        'global_config': json.dumps({'auth_provider': auth_provider}),
        'custom_models': json.dumps(default_models),
        'local_users': json.dumps({
            'admin': {'password_hash': pw_hash, 'role': 'admin', 'email': 'admin@agentic-data.local'},
        }),
    }
    
    for key, value in init_items.items():
        try:
            # ConditionExpression prevents overwrite on stack update
            table.put_item(
                Item={'config_key': key, 'data': value, 'value': value},
                ConditionExpression='attribute_not_exists(config_key)',
            )
            print(f'Initialized: {key}')
        except Exception as e:
            if 'ConditionalCheckFailed' in str(e):
                print(f'Skipped (exists): {key}')
            else:
                print(f'Error writing {key}: {e}')
    
    return {
        'PhysicalResourceId': 'agentic-data-init',
        'Data': {'status': 'initialized'},
    }
`;
  }
}
