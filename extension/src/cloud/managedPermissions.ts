import {awsRegion} from './awsTransport';

/** Additive policies for the ReCoder runners, never for the user's deployed apps. */
export function managedPermissions(account:string, region:string) {
    if(!/^\d{12}$/.test(account))throw new Error('AWS 계정 번호가 올바르지 않습니다.');
    awsRegion(region);
    const kinds=['security','build','container'];
    const arn=(service:string,resource:string)=>`arn:aws:${service}:${region}:${account}:${resource}`;
    const stacks=kinds.map(k=>arn('cloudformation',`stack/recoder-${k}-v1/*`));
    const projects=kinds.map(k=>arn('codebuild',`project/recoder-${k}-v1`));
    const buckets=kinds.map(k=>`arn:aws:s3:::recoder-${k}-${account}-${region}`);
    const locks=kinds.map(k=>arn('dynamodb',`table/recoder-${k}-lock-v1`));
    const roles=kinds.map(k=>`arn:aws:iam::${account}:role/recoder-${k}-v1-Role-*`);
    const logs=kinds.map(k=>arn('logs',`log-group:/recoder/${k}-v1`));
    const allow=(Sid:string,Action:string[],Resource:string[]|string,Condition?:unknown)=>({Sid,Effect:'Allow',Action,Resource,...(Condition?{Condition}:{})});
    return {
        runtime:{Version:'2012-10-17',Statement:[
            allow('ReadRunnerStacks',['cloudformation:DescribeStacks'],stacks),
            // CodeBuild authorizes these actions on PROJECT ARNs, including BatchGetBuilds/StopBuild.
            allow('RunOwnedProjects',['codebuild:StartBuild','codebuild:BatchGetBuilds','codebuild:ListBuildsForProject','codebuild:StopBuild'],projects),
            allow('UploadAndRemoveSources',['s3:PutObject','s3:DeleteObject'],buckets.map(b=>b+'/sources/*')),
            allow('ReadJobResults',['s3:GetObject'],buckets.map(b=>b+'/reports/*')),
            allow('CoordinateJobs',['dynamodb:PutItem','dynamodb:GetItem','dynamodb:UpdateItem','dynamodb:DeleteItem'],locks),
            allow('VerifyBuiltImages',['ecr:DescribeImages'],arn('ecr','repository/recoder-managed-images-v1')),
        ]},
        bootstrap:{Version:'2012-10-17',Statement:[
            allow('ValidateTemplate',['cloudformation:ValidateTemplate'],'*'),
            allow('InstallRunnerStacks',['cloudformation:CreateStack','cloudformation:DescribeStacks','cloudformation:DescribeStackEvents','cloudformation:DescribeStackResources','cloudformation:DescribeStackResource','cloudformation:GetTemplate','cloudformation:DeleteStack'],stacks),
            allow('ManageRunnerProjects',['codebuild:CreateProject','codebuild:BatchGetProjects','codebuild:UpdateProject','codebuild:DeleteProject'],projects),
            allow('ManageRunnerBuckets',['s3:CreateBucket','s3:DeleteBucket','s3:ListBucket','s3:GetBucket*','s3:GetEncryptionConfiguration','s3:GetLifecycleConfiguration','s3:PutBucketTagging','s3:PutEncryptionConfiguration','s3:PutBucketOwnershipControls','s3:PutBucketPublicAccessBlock','s3:PutLifecycleConfiguration','s3:PutBucketPolicy','s3:DeleteBucketPolicy'],buckets),
            allow('ManageRunnerRoles',['iam:CreateRole','iam:DeleteRole','iam:GetRole','iam:UpdateAssumeRolePolicy','iam:PutRolePolicy','iam:GetRolePolicy','iam:DeleteRolePolicy','iam:ListRolePolicies','iam:ListAttachedRolePolicies','iam:TagRole','iam:UntagRole','iam:ListRoleTags'],roles),
            allow('PassRunnerRoleToCodeBuild',['iam:PassRole'],roles,{StringEquals:{'iam:PassedToService':'codebuild.amazonaws.com'}}),
            allow('DiscoverLogGroup',['logs:DescribeLogGroups'],'*'),
            allow('ManageRunnerLogs',['logs:CreateLogGroup','logs:DeleteLogGroup','logs:PutRetentionPolicy','logs:DeleteRetentionPolicy','logs:TagResource','logs:UntagResource','logs:ListTagsForResource','logs:TagLogGroup','logs:UntagLogGroup','logs:ListTagsLogGroup'],logs.flatMap(l=>[l,l+':*'])),
            allow('ManageJobLocks',['dynamodb:CreateTable','dynamodb:DeleteTable','dynamodb:UpdateTable','dynamodb:DescribeTable','dynamodb:UpdateTimeToLive','dynamodb:DescribeTimeToLive','dynamodb:DescribeContinuousBackups','dynamodb:TagResource','dynamodb:UntagResource','dynamodb:ListTagsOfResource'],locks),
            allow('ManageBuildImageRepository',['ecr:CreateRepository','ecr:DescribeRepositories','ecr:GetRepositoryPolicy','ecr:GetLifecyclePolicy','ecr:DeleteRepository','ecr:TagResource','ecr:UntagResource','ecr:ListTagsForResource','ecr:PutImageTagMutability'],arn('ecr','repository/recoder-managed-images-v1')),
        ]},
    };
}
