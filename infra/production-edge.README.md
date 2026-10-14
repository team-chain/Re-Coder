# ECS의 HTTPS 접속 경로

`production-edge.yaml`은 CloudFront의 기본 HTTPS 도메인 → VPC origin → 내부 ALB → ECS 대상 그룹을 만듭니다. VPC에는 인터넷 게이트웨이가 있어야 하며, 입력한 두 CIDR은 기존 서브넷과 겹치면 안 됩니다. 두 AZ와 CloudFront origin-facing 관리형 prefix list ID를 해당 리전에서 선택합니다. [AWS VPC origin 설명](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html)

템플릿은 앱 보안 그룹에 ALB에서 앱 포트로 들어오는 규칙만 추가합니다. 기존 그룹에 인터넷 전체 허용 규칙이 있다면 운영자가 제거해야 합니다. 새 앱 보안 그룹을 사용하는 것을 권장합니다. 비공개 ALB의 두 서브넷에는 기본 인터넷 경로를 만들지 않습니다.

대상 그룹을 쓰는 배포에서는 `security_group_ids`를 명시해야 합니다. 이를 생략했을 때 기존 직접 접속용 공개 보안 그룹을 자동 생성하는 경로는 차단합니다.

CloudFormation 출력 `TargetGroupArn`, `Domain`을 배포 화면의 **앱 실행 설정 → 네트워크·HTTPS**에 넣습니다.

```json
{
  "target_group_arn": "arn:aws:elasticloadbalancing:REGION:ACCOUNT:targetgroup/NAME/ID",
  "cloudfront_domain": "DISTRIBUTION.cloudfront.net",
  "subnet_ids": ["subnet-...", "subnet-..."],
  "security_group_ids": ["sg-..."],
  "assign_public_ip": false
}
```

위 ID와 도메인은 실제 출력으로 바꿉니다. 예시의 대문자 자리표시는 유효한 요청이 아닙니다. 공인 IP 없는 ECS 태스크에는 ECR·로그·Secrets Manager 접근용 NAT 또는 VPC 엔드포인트가 필요합니다. 저비용 검증에서는 태스크 공인 IP를 켜고 인바운드를 ALB 보안 그룹으로 제한할 수 있습니다.

ECS 서브넷은 ALB와 같은 VPC이며 템플릿에서 선택한 두 가용 영역 안에 있어야 합니다. 다른 영역의 서브넷을 섞으면 태스크 자체가 정상이어도 ALB가 사용하지 못해 ECS가 계속 교체합니다. 배포기는 빌드 전에 이 조합과 조회 권한을 확인하며, 맞지 않으면 해당 서브넷을 표시하고 중단합니다. [AWS의 가용 영역 오류 설명](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/troubleshoot-service-load-balancers.html)

앱 환경의 `FRONTEND_URL`은 `https://<Domain>`으로 설정합니다. CloudFront는 인증·주문 응답을 캐시하지 않고 쿠키·Authorization·쿼리를 원본으로 전달합니다. HSTS, nosniff, frame DENY를 설정합니다. ECS 서비스 갱신은 대상 그룹을 지정하지 않았을 때 기존 ALB 연결을 제거하지 않습니다.

이 템플릿은 유료 AWS 자원을 만듭니다. 삭제 시 ECS 서비스를 중지한 다음 CloudFormation 스택을 삭제하고 완료 상태를 확인합니다. 사용자 도메인은 별도 ACM 인증서와 DNS 설정이 필요합니다. 데이터베이스의 백업·복구는 별도 운영 정책으로 관리하며 복구 시 새 DB 인스턴스가 생성됩니다. [RDS 복구 설명](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_RestoreFromSnapshot.html)
