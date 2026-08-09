# ReCoder AWS 권한 가이드 (최소권한)

ReCoder 는 **여러분의 AWS 계정**에 배포합니다(BYO). 그래서 여러분이 직접 키를
만들어 연결해야 하는데, 이때 **어떤 권한을 줄지**가 문제가 됩니다.

권한을 너무 적게 주면 배포하다 중간에 막히고, 반대로 `AdministratorAccess` 를
붙이면 그 키 하나가 유출됐을 때 계정 전체가 넘어갑니다.

이 문서는 **ReCoder 가 실제로 쓰는 것만 담은 정답표**입니다. 그대로 복사해서
쓰면 됩니다.

> **학교에서 받은 AWS Academy 계정인가요?** 아래 1~6단계는 **동작하지
> 않습니다.** IAM 사용자 생성이 막혀 있습니다. [학교 계정](#학교-aws-academy-계정)
> 절로 바로 가세요.

## 5분 안에 끝내기

먼저 정책을 받습니다. 코어에 요청하면 됩니다.

```
GET /api/aws/policy
```

그다음 AWS 콘솔에서 순서대로 진행합니다.

1. **IAM → 정책 → 정책 생성 → JSON 탭** 에 받은 정책을 붙여넣습니다.
2. 정책 안에 `<ACCOUNT_ID>` `<REGION>` `<PARTITION>` 이 남아 있으면 본인 값으로
   바꿉니다. `<PARTITION>` 은 대부분 `aws` 이고, 미국 정부용(GovCloud)이면
   `aws-us-gov`, 중국이면 `aws-cn` 입니다.
   계정 ID 는 콘솔 오른쪽 위 계정 메뉴에서 볼 수 있습니다.
   (ReCoder 가 확실히 알 수 있는 값은 자동으로 채워져서 나옵니다. **확실하지
   않으면 일부러 자리표시자를 남깁니다** — 틀린 값이 채워진 채로 "완료"처럼
   보이는 것보다 낫기 때문입니다. 리전을 직접 지정하려면
   `GET /api/aws/policy?region=us-west-2` 처럼 넘기세요.)
3. 이름을 `ReCoderMinimal` 로 저장합니다.
4. **IAM → 사용자 → 사용자 생성** 후 방금 만든 정책을 연결합니다.
5. 그 사용자에서 **액세스 키** 를 발급합니다 (용도: *로컬에서 실행되는 코드*).
6. 발급된 키를 ReCoder 의 **AWS 연결** 화면에 입력합니다.

> 키는 VSCode 의 SecretStorage(운영체제 금고)에 암호화 보관되며, 평문 파일로
> 남지 않습니다.

응답에는 정책 JSON 과 함께 위 순서가 `steps` 로 들어 있습니다. 확장 화면에서
그대로 보여주기 위한 것입니다.

## 어떤 권한을, 왜 주나요

정책은 **필요한 것만** 담고 있습니다. 아래가 전부이고, 각 항목이 왜 필요한지
근거가 되는 코드까지 밝혀 둡니다.

| 하는 일 | 필요한 권한 | 왜 |
| --- | --- | --- |
| 연결 확인 | `sts:GetCallerIdentity` | 키가 유효한지, 어느 계정인지 확인 |
| 이미지 저장소 | `ecr:GetAuthorizationToken` · `CreateRepository` · `DescribeRepositories` | 로그인하고 저장소를 준비 |
| 이미지 올리기 | `ecr:BatchCheckLayerAvailability` · `InitiateLayerUpload` · `UploadLayerPart` · `CompleteLayerUpload` · `PutImage` | `docker push` 가 내부적으로 호출 |
| 이미지 내려받기 | `ecr:BatchGetImage` · `GetDownloadUrlForLayer` | 배포 전 보안 스캔(trivy)·SBOM(syft)이 원격 이미지를 끌어옴 |
| 컨테이너 배포 | `ecs:RegisterTaskDefinition` · `DescribeTaskDefinition` · `DescribeClusters` · `DescribeServices` · `UpdateService` | 새 버전 등록 후 서비스 교체 |
| 실행 역할 확인·연결 | `iam:GetRole` · `iam:PassRole` | 컨테이너가 이미지를 받아오려면 실행 역할이 필요 |
| 태스크 역할 연결 | `iam:PassRole` (역할 하나 더) | 컨테이너 **안의 코드**가 쓰는 역할. 실행 역할과 다르다 |
| 배포 전 점검 | `logs:DescribeLogGroups` | 로그 그룹이 준비됐는지 확인 |
| 정적 사이트 | `s3:CreateBucket` · `PutObject` · `ListBucket` 외 | 빌드 결과를 버킷에 올리고 공개 |
| AI 호출 | `bedrock:InvokeModel` · `ListFoundationModels` | 코드 생성·에러 분석 |

### 왜 `bedrock:Converse` 가 없나요

ReCoder 는 Bedrock 의 **Converse** API 를 씁니다. 그런데 이 API 는 이름과 달리
`bedrock:Converse` 가 아니라 **`bedrock:InvokeModel`** 로 인가됩니다. AWS 문서
원문은 이렇습니다 — *"This operation requires permission for the
`bedrock:InvokeModel` action."*
([Converse API 레퍼런스](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html))

이름만 보고 `bedrock:Converse` 를 주면 실제로는 막히고, 안전하게 하겠다고 둘 다
주면 쓰지도 않는 권한이 하나 늘어납니다. 그래서 문서가 요구하는 것 하나만
줍니다. 스트리밍(`ConverseStream`)은 지금 코드에 호출이 없어서 빼놨습니다.

### 역할이 두 개인 이유

ECS 작업에는 역할이 **두 개** 붙습니다. 헷갈리기 쉬운데 하는 일이 다릅니다.

| 역할 | 누가 쓰나 | 하는 일 |
| --- | --- | --- |
| 실행 역할 (execution role) | ECS 서비스 자체 | 이미지를 받아오고 로그를 씁니다 |
| 태스크 역할 (task role) | 컨테이너 **안의** 앱 | 앱이 S3 등 AWS 를 부를 때 씁니다 |

`RegisterTaskDefinition` 은 **두 역할 모두에 대해** `iam:PassRole` 을 요구합니다.
하나만 주면 이미지 빌드와 푸시가 다 끝난 **마지막 단계에서** 거부됩니다.

학교 계정은 역할을 만들 수 없어서 둘 다 `LabRole` 을 씁니다. 그때는 ARN 이
하나로 합쳐집니다.

### 범위를 어떻게 좁혔나

- **이름 접두사 제한** — ECR 저장소와 S3 버킷은 `recoder-*` 로 시작하는 것만
  건드릴 수 있습니다. 여러분의 다른 저장소·버킷에는 손대지 못합니다.
- **ECS 클러스터·서비스도 이름으로 제한** — 기본값이 `recoder-*` 입니다.
  **이미 있는 클러스터(`default` 등)에 배포한다면 그 이름을 알려주셔야 합니다.**
  안 그러면 정책을 그대로 붙여도 배포 전 점검에서 막힙니다. 바로 아래
  「필요한 것만 골라 받기」를 보세요.
- **`iam:PassRole` 에 조건** — 역할을 **ECS 작업에 넘길 때만** 허용합니다.
  이 조건이 없으면 이 키로 아무 서비스에나 역할을 붙일 수 있어, 사실상 권한
  상승 통로가 됩니다.
- **역할·클러스터 이름에 와일드카드 금지** — `*` 같은 값을 넣으면 400 으로
  거부합니다. 허용하면 `role/*` 짜리 정책이 만들어져 계정의 모든 역할을
  ECS 에 넘길 수 있게 되고, 이 문서의 존재 이유가 사라집니다.
- **와일드카드는 불가피한 곳만** — `Resource: "*"` 는 AWS 가 리소스 단위
  제한을 지원하지 않는 액션(`ecs:RegisterTaskDefinition`, `logs:DescribeLogGroups`
  등)에만 씁니다. 이 목록은 테스트로 고정돼 있어, 근거 없이 늘어나면 CI 가
  실패합니다.

## 필요한 것만 골라 받기

전부 필요하지 않다면 대상을 지정할 수 있습니다.

```
GET /api/aws/policy?targets=ecs          # 컨테이너 배포만
GET /api/aws/policy?targets=s3           # 정적 사이트만
GET /api/aws/policy?targets=ecs,bedrock  # 배포 + AI
```

연결 확인(`sts:GetCallerIdentity`)은 대상과 무관하게 항상 포함됩니다.

**배포 대상 이름을 알려주면 범위가 그만큼 좁아집니다.** 이미 쓰고 있는
클러스터가 있다면 반드시 넘기세요.

```
GET /api/aws/policy?cluster=default&service=my-api
GET /api/aws/policy?task_execution_role=LabRole&task_role=LabRole
```

| 인자 | 기본값 | 언제 넘기나 |
| --- | --- | --- |
| `cluster` | `recoder-*` | 이미 있는 클러스터에 배포할 때 |
| `service` | `recoder-*` | 서비스 이름이 `recoder-` 로 시작하지 않을 때 |
| `ecr_repo` | `recoder-*` | 이미 있는 이미지 저장소에 올릴 때 |
| `task_execution_role` | `ecsTaskExecutionRole` | 환경변수로 다른 역할을 쓸 때 |
| `task_role` | `ecsTaskRole` | 환경변수로 다른 역할을 쓸 때 |
| `region` | 세션에서 자동 | 세션 리전과 다른 곳에 배포할 때 |

**역할은 자동으로 바꾸지 않습니다.** 학교 계정으로 접속한 것이 확인되면
"환경변수를 이렇게 설정하세요"라고 안내만 합니다. 정책만 `LabRole` 로 바꿔봐야
실제 배포 코드는 환경변수를 보므로 둘이 갈라지기 때문입니다.

### 역할을 환경변수로 지정했다면

배포 경로는 `ECS_EXECUTION_ROLE_ARN` · `ECS_TASK_ROLE_ARN` 환경변수를 봅니다.
이 값이 설정돼 있으면 **권한표도 같은 역할을 인가합니다.** 둘이 갈라지면
정책은 A 를 허용하는데 배포는 B 를 쓰는 상황이 되어, 배포 마지막 단계에서
`PassRole` 오류가 납니다.

우선순위는 **직접 넘긴 값 → 환경변수 → 기본값** 입니다. 학교 계정 감지는 값을
바꾸지 않고 안내만 합니다.

## 학교 (AWS Academy) 계정

AWS Academy 러너랩 계정에서는 **이 가이드의 4~6단계를 할 수 없습니다.**
실제 계정에서 확인한 내용입니다.

| 해보려던 것 | 결과 |
| --- | --- |
| IAM 사용자 생성 | ❌ `iam:CreateUser` 허용 안 됨 |
| `iam:GetRole` on `voclabs` | ❌ 명시적 거부 |
| `iam:GetRole` on `LabRole` | ✅ 됨 |
| ECS Fargate 배포 | ✅ 됨 |
| Bedrock 호출 | ❌ 허용 안 됨 |

권한이 부족해서가 아니라 **학교가 계정 차원에서 잠가둔 것**이라, 어떤 정책을
붙여도 열리지 않습니다.

### 그래서 학교 계정에서는 이렇게 합니다

정책을 만들어 붙이는 대신, **랩이 주는 임시 자격증명을 그대로 씁니다.**

1. 러너랩 화면에서 **AWS Details → AWS CLI** 를 엽니다.
2. 거기 나오는 3줄(액세스 키·비밀 키·**세션 토큰**)을 통째로 복사합니다.
3. `~/.aws/credentials` 파일에 붙여넣습니다. 리전도 같이 적습니다.

```ini
[default]
aws_access_key_id = ASIA...
aws_secret_access_key = ...
aws_session_token = IQoJb3JpZ2lu...
region = us-east-1
```

> **왜 화면이 아니라 파일인가** — 지금 ReCoder 의 **AWS 연결** 화면에는 입력칸이
> 액세스 키·비밀 키·리전 세 개뿐이고, **세션 토큰 칸이 없습니다.** 학교 계정
> 자격증명은 세션 토큰이 반드시 있어야 해서 그 화면으로는 연결할 수 없습니다.
> 코어는 이 파일을 그대로 읽으므로 개발에는 지장이 없습니다.
> (입력칸 추가는 FR-04-01 후속 작업입니다.)

세션 토큰을 빠뜨리면 `The security token included in the request is invalid`
가 납니다. 가장 흔한 실수입니다.

자격증명은 랩 세션이 끝나면 만료됩니다. 만료되면 위 3줄을 다시 덮어써야 하고,
장기 액세스 키는 만들 수 없습니다.

### 실행 역할은 `LabRole` 을 씁니다 — 환경변수로 알려주세요

학교 계정에는 `ecsTaskExecutionRole` 이 **없습니다.** 대신 미리 만들어져 있는
`LabRole` 을 그대로 쓰면 됩니다. 러너랩 계정에서 `LabRole` 의 신뢰 정책을 직접
확인했고 `ecs-tasks.amazonaws.com` 이 들어 있습니다.

**아래 두 환경변수를 설정하세요.**

```
ECS_EXECUTION_ROLE_ARN=arn:aws:iam::<계정ID>:role/LabRole
ECS_TASK_ROLE_ARN=arn:aws:iam::<계정ID>:role/LabRole
```

설정하면 권한표도 자동으로 `LabRole` 기준으로 나옵니다. 설정하지 않으면
`GET /api/aws/policy` 가 학교 계정임을 알아보고 이 안내를 응답에 붙여 줍니다.

> **왜 자동으로 안 바꾸나** — 실제 배포 코드는 위 환경변수를 봅니다. 권한표만
> `LabRole` 로 바꾸면 **정책은 LabRole, 배포는 ecsTaskExecutionRole** 이 되어
> 시킨 대로 했는데도 배포 전 점검에서 실패합니다. 한쪽만 바꾸는 게 아무것도
> 안 바꾸는 것보다 나쁩니다.

### 학교 계정에서 배포할 때 자주 막히는 것

모르면 원인 찾는 데 한참 걸리는 것들입니다.

- **작업에 공인 IP 를 켜야 합니다.** 기본 VPC 에 NAT 이 없어서, 끄면 ECR 에서
  이미지를 못 받아오고 조용히 실패합니다.
- **로그 그룹을 미리 만들어야 합니다.** 실행 역할에 `logs:CreateLogGroup` 이
  없어서, 없으면 컨테이너가 시작하다 죽습니다.
- **첫 배포 전에 ECS 콘솔을 한 번 열어야 합니다.** 서비스 연결 역할
  (`AWSServiceRoleForECS`)이 없으면 첫 배포가 실패합니다.
- **리전은 `us-east-1`** 을 씁니다. 다른 리전은 `AccessDenied` 로 나타나서
  리전 문제인 줄 모르게 됩니다.
- **`docker build --platform linux/amd64`** 로 빌드합니다. 아키텍처가 다르면
  컨테이너가 `exec format error` 로 죽습니다.

## 자주 막히는 곳

**`AccessDenied` 가 뜨는데 정책은 붙였습니다**
정책의 `<ACCOUNT_ID>` `<REGION>` 을 안 바꿨을 가능성이 큽니다. 자리표시자가
남아 있으면 어떤 리소스와도 매칭되지 않습니다.

**저장소·버킷 이름이 `recoder-` 로 시작하지 않습니다**
정책이 접두사로 범위를 좁히고 있어서입니다. 다른 이름을 쓰려면 정책의
`recoder-*` 부분을 원하는 접두사로 바꾸세요.

**배포 마지막 단계에서 `PassRole` 오류가 납니다**
두 가지 중 하나입니다. 계정에 `ecsTaskExecutionRole` 이 없거나(ECS 콘솔에서
서비스를 한 번 만들면 AWS 가 자동 생성합니다. 학교 계정이라면 `LabRole`),
아니면 **태스크 역할 이름이 기본값과 다른** 경우입니다. 후자라면
`?task_role=<실제이름>` 으로 정책을 다시 받으세요.

**ECR 저장소 목록이 비어 있고 "목록 조회가 허용되지 않는다"고 나옵니다**
**정상입니다.** 이 권한표는 ECR 조회를 `recoder-*` 로 좁혀 놓습니다 — 여러분의
다른 저장소 이름까지 ReCoder 가 볼 이유가 없기 때문입니다. 그래서 계정 전체
목록을 훑는 화면에서는 막힙니다. **자격증명 문제가 아닙니다.** 연결이
살아 있는지는 **AWS 연결** 화면의 상태 표시로 확인하세요.

굳이 목록을 다 보고 싶다면 정책의 `ecr:DescribeRepositories` 를 별도
문장으로 빼서 `Resource: "*"` 를 주면 됩니다. 대신 그 키로 계정의 모든
저장소 이름을 볼 수 있게 되므로 권하기는 어렵습니다.

**보안 스캔이 "취약점 없음"으로 통과하는데 뭔가 이상합니다**
정말 깨끗한 건지, **스캔 자체가 실패한 건지** 확인하세요. 결과에
`trivy_scan_failed` 항목이 있으면 이미지를 아예 못 들여다본 겁니다. 가장 흔한
원인은 ECR **내려받기** 권한(`ecr:BatchGetImage` · `GetDownloadUrlForLayer`)
누락입니다. 올리기 권한만으로는 스캐너가 원격 이미지를 못 가져옵니다.

**클러스터를 못 찾는다거나 `DescribeClusters` 가 거부됩니다**
정책이 클러스터 이름을 `recoder-*` 로 좁히고 있는데, 실제로는 다른 이름
(`default` 등)에 배포하고 있어서입니다. `?cluster=<실제이름>&service=<실제이름>`
으로 다시 받으세요.

## 이 목록은 어디서 나왔나

`core/aws_policy.py` 가 단일 출처입니다. 문서와 화면과 코드가 갈라지지 않도록
정책을 그 파일에서만 생성합니다.

그리고 이 목록이 **낡지 않도록** 두 가지로 대조합니다.

**`core/aws_calls.py` 의 정적 분석** — 코어 소스를 문법 트리로 읽어서 boto3
클라이언트를 추적하고, 실제로 호출하는 AWS API 를 전부 찾아냅니다. 변수 이름에
좌우되지 않고, AWS 와 무관한 호출을 AWS 호출로 착각하지 않으며,
`["aws", "ecr", ...]` 같은 CLI 경로와 함수 인자로 넘어간 클라이언트까지
따라갑니다.

**같은 파일의 실행 기록기** — 실제로 배포를 돌리면서 나간 호출을 그대로
기록합니다. IAM 권한이 전혀 필요 없어서, 권한이 막힌 학교 계정에서도 그냥
됩니다.

정적 분석은 *안 걸어본 경로*까지 훑고, 실행 기록은 *실제로 나간 호출*을
증명합니다. 둘 중 하나만으로는 반쪽입니다.

`core/tests/test_aws_policy.py` 가 이 결과와 권한표를 맞춰봅니다. 코드가
부르는데 권한표에 없으면 실패하고, 권한표에 있는데 근거가 없어도 실패합니다.
**모르는 호출을 만나면 조용히 넘어가지 않고 실패합니다** — 이전 판이 바로
그것 때문에 뚫렸습니다.

아직 코드가 쓰지 않지만 미리 발급하는 권한(현재 S3 8종, FR-05-03 예정)은
테스트 안에 카드 번호와 함께 적혀 있습니다. 그 카드가 끝나 코드가 실제로 쓰기
시작하면, 테스트가 "이제 목록에서 빼라"고 알려줍니다.
