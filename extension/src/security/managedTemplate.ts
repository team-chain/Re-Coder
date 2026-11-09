export const MANAGED_STACK='recoder-security-v1';
export const MANAGED_PROJECT='recoder-security-v1';
export type ManagedKind='security'|'build'|'container';
export const MANAGED_IMAGE_REPOSITORY='recoder-managed-images-v1';
export function managedTemplate(account:string, region:string, kind:ManagedKind='security'):string {
    if(!/^\d{12}$/.test(account)||!/^[a-z]{2}-[a-z]+-\d$/.test(region))throw new Error('AWS 계정·리전이 올바르지 않습니다.');
    const bucket=`recoder-${kind}-${account}-${region}`, project=`recoder-${kind}-v1`, sub=(s:string)=>({'Fn::Sub':s}), ref=(s:string)=>({Ref:s});
    const tags=[{Key:'recoder:managed',Value:'true'},{Key:'recoder:purpose',Value:`${kind}-runner-v1`}];
    return JSON.stringify({AWSTemplateFormatVersion:'2010-09-09',Description:'ReCoder isolated, on-demand execution in your AWS account',Resources:{
        ...(kind==='container'?{Images:{Type:'AWS::ECR::Repository',DeletionPolicy:'Retain',UpdateReplacePolicy:'Retain',Properties:{RepositoryName:MANAGED_IMAGE_REPOSITORY,ImageTagMutability:'IMMUTABLE',EncryptionConfiguration:{EncryptionType:'AES256'},Tags:tags}}}:{}),
        Bucket:{Type:'AWS::S3::Bucket',DeletionPolicy:'Retain',UpdateReplacePolicy:'Retain',Properties:{BucketName:bucket,Tags:tags,
            BucketEncryption:{ServerSideEncryptionConfiguration:[{ServerSideEncryptionByDefault:{SSEAlgorithm:'AES256'}}]},
            OwnershipControls:{Rules:[{ObjectOwnership:'BucketOwnerEnforced'}]},PublicAccessBlockConfiguration:{BlockPublicAcls:true,BlockPublicPolicy:true,IgnorePublicAcls:true,RestrictPublicBuckets:true},
            LifecycleConfiguration:{Rules:[{Id:'source-expiration',Status:'Enabled',Prefix:'sources/',ExpirationInDays:1,AbortIncompleteMultipartUpload:{DaysAfterInitiation:1}},{Id:'report-expiration',Status:'Enabled',Prefix:'reports/',ExpirationInDays:7}]}}},
        BucketPolicy:{Type:'AWS::S3::BucketPolicy',Properties:{Bucket:ref('Bucket'),PolicyDocument:{Version:'2012-10-17',Statement:[{Effect:'Deny',Principal:'*',Action:'s3:*',Resource:[`arn:aws:s3:::${bucket}`,`arn:aws:s3:::${bucket}/*`],Condition:{Bool:{'aws:SecureTransport':'false'}}}]}}},
        Logs:{Type:'AWS::Logs::LogGroup',Properties:{LogGroupName:`/recoder/${kind}-v1`,RetentionInDays:7,Tags:tags}},
        // DynamoDB's default AWS-owned encryption needs no caller KMS grant or paid KMS key.
        Lock:{Type:'AWS::DynamoDB::Table',Properties:{TableName:`recoder-${kind}-lock-v1`,BillingMode:'PAY_PER_REQUEST',AttributeDefinitions:[{AttributeName:'pk',AttributeType:'S'}],KeySchema:[{AttributeName:'pk',KeyType:'HASH'}],TimeToLiveSpecification:{AttributeName:'expiresAt',Enabled:true},Tags:tags}},
        Role:{Type:'AWS::IAM::Role',Properties:{Tags:tags,AssumeRolePolicyDocument:{Version:'2012-10-17',Statement:[{Effect:'Allow',Principal:{Service:'codebuild.amazonaws.com'},Action:'sts:AssumeRole',Condition:{StringEquals:{'aws:SourceAccount':account},ArnEquals:{'aws:SourceArn':`arn:aws:codebuild:${region}:${account}:project/${project}`}}}]},Policies:[{PolicyName:'SecurityJobOnly',PolicyDocument:{Version:'2012-10-17',Statement:[
            {Effect:'Allow',Action:['logs:CreateLogStream','logs:PutLogEvents'],Resource:`arn:aws:logs:${region}:${account}:log-group:/recoder/${kind}-v1:*`},
            {Effect:'Allow',Action:['s3:GetBucketLocation','s3:GetBucketAcl'],Resource:`arn:aws:s3:::${bucket}`},
            {Effect:'Allow',Action:['s3:GetObject','s3:GetObjectVersion'],Resource:`arn:aws:s3:::${bucket}/sources/*`},
            {Effect:'Allow',Action:['s3:PutObject'],Resource:`arn:aws:s3:::${bucket}/reports/*`},
            ...(kind==='security'?[{Effect:'Allow',Action:['bedrock:InvokeModel','bedrock:InvokeModelWithResponseStream'],Resource:[`arn:aws:bedrock:*::foundation-model/anthropic.claude-*`,`arn:aws:bedrock:*::foundation-model/amazon.titan-embed-*`,`arn:aws:bedrock:*:${account}:inference-profile/*`]}]:[]),
            ...(kind==='container'?[
                {Effect:'Allow',Action:['ecr:GetAuthorizationToken'],Resource:'*'},
                {Effect:'Allow',Action:['ecr:BatchCheckLayerAvailability','ecr:InitiateLayerUpload','ecr:UploadLayerPart','ecr:CompleteLayerUpload','ecr:PutImage','ecr:BatchGetImage','ecr:DescribeImages'],Resource:`arn:aws:ecr:${region}:${account}:repository/${MANAGED_IMAGE_REPOSITORY}`}
            ]:[])
        ]}}]}},
        Project:{Type:'AWS::CodeBuild::Project',...(kind==='container'?{DependsOn:['Images']}:{}),Properties:{Name:project,Tags:tags,ServiceRole:{'Fn::GetAtt':['Role','Arn']},ConcurrentBuildLimit:1,TimeoutInMinutes:60,QueuedTimeoutInMinutes:5,
            Environment:{Type:'LINUX_CONTAINER',ComputeType:'BUILD_GENERAL1_MEDIUM',Image:'aws/codebuild/standard:7.0',ImagePullCredentialsType:'CODEBUILD',PrivilegedMode:true},
            Source:{Type:'S3',Location:`${bucket}/sources/pending.zip`,BuildSpec:JSON.stringify({version:'0.2',phases:{build:{commands:[`node runtime/${kind==='security'?'managedPentagiWorker':kind==='container'?'managedContainerWorker':'managedFrontendWorker'}.js`]}},artifacts:{files:['report.json','sbom.json']}})},
            Artifacts:{Type:'S3',Location:ref('Bucket'),Path:'reports',NamespaceType:'BUILD_ID',Name:'result',Packaging:'NONE',EncryptionDisabled:false},
            LogsConfig:{CloudWatchLogs:{Status:'ENABLED',GroupName:ref('Logs')}}}}
    },Outputs:{Bucket:{Value:ref('Bucket')},Project:{Value:ref('Project')},ProjectArn:{Value:sub(`arn:aws:codebuild:${region}:${account}:project/${project}`)}}});
}
