# `opa test policies/` 로 실행한다.
#
# 여기서 검사하는 것은 **차단 규칙 5개와 통과 조건**이다. 코어의 로컬
# 폴백(`opa_gate._local_deploy_gate`)이 같은 판단을 하는지는
# core/tests/test_opa_policy_bundle.py 가 함께 본다 — 두 구현이 갈라지면
# OPA 서버가 있을 때와 없을 때 결과가 달라지고, 그건 아무도 눈치채지 못한다.
package recoder.deploy_test

import rego.v1

import data.recoder.deploy

# 모든 게이트를 통과하는 기본 입력.
clean := {
	"image_uri": "123.dkr.ecr.us-east-1.amazonaws.com/app:abc123",
	"environment": "staging",
	"branch": "develop",
	"sbom": {"present": true, "package_count": 142, "sbom_hash": "sha256:aaa"},
	"trivy": {"passed": true, "critical_count": 0, "high_count": 3},
	"gitleaks": {"passed": true},
	"hadolint": {"passed": true},
}

test_clean_deploy_is_allowed if {
	result := deploy.allow with input as clean
	result.decision == "allow"
	result.policy_bundle_version != ""
}

test_missing_sbom_is_denied if {
	result := deploy.allow with input as object.union(clean, {"sbom": {"present": false}})
	result.decision == "deny"
	contains(result.reason, "SBOM")
}

test_trivy_critical_is_denied if {
	inp := object.union(clean, {"trivy": {"passed": false, "critical_count": 2, "high_count": 0}})
	result := deploy.allow with input as inp
	result.decision == "deny"
	contains(result.reason, "critical")
}

# [음성 대조] high 는 경고지 차단이 아니다. 여기서 막으면 거의 모든 배포가 막힌다.
test_trivy_high_only_is_allowed if {
	inp := object.union(clean, {"trivy": {"passed": true, "critical_count": 0, "high_count": 47}})
	result := deploy.allow with input as inp
	result.decision == "allow"
}

test_gitleaks_failure_is_denied if {
	result := deploy.allow with input as object.union(clean, {"gitleaks": {"passed": false}})
	result.decision == "deny"
	contains(result.reason, "secret")
}

test_hadolint_failure_is_denied if {
	result := deploy.allow with input as object.union(clean, {"hadolint": {"passed": false}})
	result.decision == "deny"
	contains(result.reason, "Hadolint")
}

test_production_from_main_is_allowed if {
	inp := object.union(clean, {"environment": "production", "branch": "main"})
	result := deploy.allow with input as inp
	result.decision == "allow"
}

test_production_from_feature_branch_is_denied if {
	inp := object.union(clean, {"environment": "production", "branch": "feature/x"})
	result := deploy.allow with input as inp
	result.decision == "deny"
	contains(result.reason, "프로덕션")
}

# 브랜치를 모르는 채로 프로덕션에 나가면 안 된다 — 확인하지 못한 것을
# "괜찮다"로 바꾸지 않는다.
test_production_without_branch_is_denied if {
	inp := object.union(clean, {"environment": "production", "branch": ""})
	result := deploy.allow with input as inp
	result.decision == "deny"
}

# staging 은 어느 브랜치에서든 나갈 수 있다.
test_staging_from_any_branch_is_allowed if {
	inp := object.union(clean, {"environment": "staging", "branch": "feature/x"})
	result := deploy.allow with input as inp
	result.decision == "allow"
}

# 사유가 여럿이면 전부 보여 준다 — 하나 고치고 또 막히는 것보다 낫다.
test_multiple_violations_are_all_reported if {
	inp := object.union(clean, {
		"sbom": {"present": false},
		"gitleaks": {"passed": false},
	})
	result := deploy.allow with input as inp
	result.decision == "deny"
	contains(result.reason, "SBOM")
	contains(result.reason, "secret")
}

# 차단은 최고 승인 등급으로 올라간다.
test_denied_deploy_escalates_approval_level if {
	result := deploy.allow with input as object.union(clean, {"sbom": {"present": false}})
	result.approval_level == 4
}
