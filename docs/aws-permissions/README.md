# ReCoder 관리형 실행 AWS 권한

2026-11-05 · 3.0.8. **관리형 실행을 별도로 선택한 관리자를 위한 참고 문서다. 기본 보안 검사·AI 감사·기존 배포에 추가 정책을 요구하지 않는다.** 현재 작업에서는 사용자가 정책 추가를 거절했으므로 관리형 환경을 설치하지 않았다. 기존 AWS 연결을 유지한다.

정책은 현재 연결의 계정·리전에 맞춰 아래 도구로 생성한다. 계정별 생성 JSON은 저장소에 포함하지 않는다. 예시 계정 `123456789012`와 `us-east-1`을 그대로 적용하지 않는다. 이미 연결된 계정의 정책을 검토할 때 새 로그인이나 키 발급은 필요 없다. 기존 ECS·S3 배포 권한을 교체하지 않는다.

## 관리자에게 요청할 작업

1. AWS 콘솔에서 **IAM → 정책 → 정책 생성 → JSON**을 연다.
2. 현재 계정·리전으로 생성한 `recoder-managed-runtime.json` 전체를 붙여 넣고 `ReCoderManagedRuntime`이라는 고객 관리형 정책으로 생성한다.
3. 같은 방법으로 `recoder-managed-bootstrap.json`을 `ReCoderManagedBootstrap` 정책으로 생성한다.
4. **현재 연결이 사용하는 IAM 사용자·역할**에 위 두 정책을 연결한다. 사용자 연결이면 **IAM → 사용자 → 해당 사용자 → 권한 추가 → 직접 정책 연결**을 사용한다. 기존 정책은 유지한다. 역할/Identity Center 연결이면 해당 역할/권한 세트에 관리자가 적용한다. 원본 사용자 권한만 늘려도 현재 역할의 권한은 늘어나지 않는다.
5. ReCoder의 **상태 확인**을 누른다. 연결은 그대로 유지한다. Security의 **관리형 실행 준비**로 검사 환경을 설치한다. S3 또는 ECS 배포 준비에서 **AWS에서 빌드**를 선택하면 각각 별도 빌드 환경 설치를 제안한다. 계정·리소스·비용 확인 후 설치한다.
6. 필요한 CloudFormation 스택이 모두 `CREATE_COMPLETE`이면 **ReCoderManagedBootstrap만 분리**한다. 전체 기능은 `recoder-security-v1`, `recoder-build-v1`, `recoder-container-v1`을 사용한다. 실행용 정책은 유지한다. 설치하지 않은 스택이 있으면 해당 스택 설치 때 관리자가 설치 권한을 다시 제공해야 한다.

사용자 본인에게 정책 생성/연결 권한이 없다면 기존 IAM 관리자에게 요청한다. 새 액세스 키를 발급하거나 채팅에 보내지 않는다. 이 변경은 JSON을 생성하는 것뿐이며 ReCoder가 IAM 정책을 자동으로 연결하지 않는다.

관리자가 먼저 설치하는 경우 동봉한 `recoder-security.template.json`, `recoder-build.template.json`, `recoder-container.template.json`을 CloudFormation에 업로드한다. 스택 이름은 각각 `recoder-security-v1`, `recoder-build-v1`, `recoder-container-v1`, 스택 태그는 각각 `recoder:purpose=security-runner-v1`, `build-runner-v1`, `container-runner-v1`과 공통 `recoder:managed=true`로 지정하고 IAM 리소스 생성에 동의한다. 사용자에게는 실행용 정책만 연결할 수 있다. 템플릿 안의 리소스 태그와 별개로 **스택 태그도 지정**해야 ReCoder가 설치 소유자를 확인한다.

`SignInLocalDevelopmentAccess`는 새 브라우저 로그인 방식을 선택하는 경우에만 필요하다. 기존 키·프로필·임시 역할 연결을 관리형 기능에서 재사용하기 위한 필수 정책이 아니다. 연결 방식 전환이나 재로그인을 먼저 요구하지 않는다.

## 정책 범위

| 정책 | 작업 | 리소스 |
|---|---|---|
| SignInLocalDevelopmentAccess (브라우저 로그인 선택 시만) | `signin:AuthorizeOAuth2Access`, `signin:CreateOAuth2Token` | AWS 개발 도구 OAuth 공개 클라이언트 |
| 실행용 | CloudFormation 상태 읽기; CodeBuild 시작·조회·중단; S3 소스 업로드/삭제·결과 읽기; DynamoDB 잠금 읽기/쓰기; 전용 ECR 이미지 digest 확인 | 아래 세 실행 환경만 |
| 설치용 | CloudFormation 생성/진단/삭제; 전용 S3 설정; 전용 CodeBuild 생성/설정; 전용 IAM 역할 생성·인라인 정책 설정; 로그와 작업 잠금 테이블 생성/설정; 전용 ECR 생성·조회·태그 설정 | 아래 세 실행 환경만. `ValidateTemplate`, `DescribeLogGroups`는 AWS API 특성상 Resource `*` |

- 스택·프로젝트: `recoder-security-v1`, `recoder-build-v1`, `recoder-container-v1`
- 버킷: `recoder-security-<계정>-<리전>`, `recoder-build-<계정>-<리전>`, `recoder-container-<계정>-<리전>`
- 잠금 테이블: `recoder-security-lock-v1`, `recoder-build-lock-v1`, `recoder-container-lock-v1`
- 로그 그룹: `/recoder/security-v1`, `/recoder/build-v1`, `/recoder/container-v1`
- 실행 역할: CloudFormation이 만드는 `recoder-security-v1-Role-*`, `recoder-build-v1-Role-*`, `recoder-container-v1-Role-*`. `iam:PassRole`은 CodeBuild 서비스에만 허용한다.
- ECR: `recoder-managed-images-v1`. 변경 불가 태그로 검사한 이미지를 보관하고 배포는 digest로 고정한다. 롤백용 이미지가 사라지지 않도록 자동 삭제 정책을 설정하지 않으며 스택 삭제 시에도 저장소를 보존한다. 사용자는 이미지 저장 비용을 부담한다.

설치용 정책은 위 이름의 역할에 인라인 권한을 설정할 수 있는 **관리자용 부트스트랩 권한**이다. 이름 제한만으로 권한 상승을 완전히 막는 정책은 아니다. 일반 사용자에게 상시 부여하지 않고, 관리자가 설치하거나 일시 부여 후 제거한다. 더 엄격한 조직은 별도 CloudFormation 서비스 역할·permissions boundary를 검토해야 하며 현재 자동 설치 경로에는 이를 구성하는 UI가 없다.

CodeBuild 작업 조회/중단도 IAM에서는 **project ARN**으로 권한을 검사한다. `build/...` ARN에만 허용하면 동작하지 않는다. 현재 코드는 CloudFormation 서비스 역할을 지정하지 않으므로 설치자에게 스택 안의 리소스를 만드는 권한도 필요하다.

설치가 만들어 주는 **검사 실행 역할**에는 저장소/로그와 Claude·Titan embedding Bedrock 호출 권한이 들어간다. 화면 빌드 역할에는 Bedrock·ECS·EC2·IAM·운영 배포 버킷 권한이 없다. 컨테이너 빌드 역할에는 전용 저장소 이미지 업로드·조회와 ECR 인증 권한만 추가된다. ECS를 바꾸는 것은 검토·승인을 거친 기존 배포 경로다. 따라서 이 두 정책은 기존 배포 권한의 대체물이 아니다.

## Bedrock 최초 모델 활성화

관리형 AI 감사와 PentAGI는 Bedrock을 사용한다. 일반 보안 검사·화면 빌드에는 모델 권한이 필요 없다. 계정에서 Claude를 처음 사용하는 경우 관리자가 Bedrock 콘솔의 **Model catalog → 사용할 Anthropic 모델 → Playground**에서 최초 사용 양식과 모델 활성화를 완료한다. 기본 후보는 `global.anthropic.claude-haiku-4-5-20251001-v1:0`이며 실제 계정·리전에서 사용 가능한 모델인지 확인한다.

최초 활성화 담당자에게는 AWS Marketplace의 `Subscribe`, `Unsubscribe`, `ViewSubscriptions`와 Anthropic 최초 사용 양식을 제출할 `bedrock:PutUseCaseForModelAccess` 권한이 필요할 수 있다. 구매 권한은 위 실행/설치 정책과 CodeBuild 역할에 포함하지 않았다. 이미 계정에서 활성화한 모델을 호출할 때는 이 구매 권한이 필요 없다. AWS가 별도로 요구하는 계정 접근, 결제 수단, 조직 SCP/권한 경계의 명시적 거부는 IAM Allow 추가만으로 해결되지 않는다.

## 재생성·검증

확장 명령 팔레트의 **ReCoder: 관리형 AWS 권한 보기**는 현재 연결한 계정/리전용 정책을 보여 준다. 개발 저장소에서는 다음 명령으로 JSON을 생성할 수 있다.

```powershell
cd extension
npm run compile
# 아래 예시 계정/리전을 현재 연결의 값으로 바꾼다.
node scripts/export-managed-permissions.js 123456789012 us-east-1 ../.test-artifacts/aws-permissions
```

현재 JSON의 IAM 작업 표현식은 AWS 공식 서비스 메타데이터의 작업 이름·Resource `*` 필요 여부와 대조했다. ECR CloudFormation 공급자의 읽기 작업에 필요한 `GetRepositoryPolicy`, `GetLifecyclePolicy`도 포함한다. 정책 크기와 ARN 생성도 자동 검사한다. **실계정 정책 연결·CloudFormation 설치 성공은 아직 확인하지 않았다.** 권한 추가 후 실제 설치로 CloudFormation 리소스 공급자의 권한과 조직 정책까지 확인해야 한다.

공식 근거: [브라우저 로그인 정책](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/SignInLocalDevelopmentAccess.html), [CloudFormation 설치자 권한](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/control-access-with-iam.html), [CodeBuild 작업별 권한](https://docs.aws.amazon.com/service-authorization/latest/reference/list_codebuild.html), [AWS 서비스 권한 JSON](https://docs.aws.amazon.com/service-authorization/latest/reference/service-reference.html), [Bedrock 모델 활성화](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).

ECR 근거: [CloudFormation 공급자 스키마](https://schema.cloudformation.us-east-1.amazonaws.com/aws-ecr-repository.json), [이미지 업로드 권한](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-push-iam.html), [변경 불가 태그](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-tag-mutability.html).
