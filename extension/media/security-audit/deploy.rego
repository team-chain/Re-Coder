# ReCoder 배포 게이트 정책 (설계서 §Q3 Preset Policy)
#
# 무엇인가
#   코어의 `opa_gate.OPAGate` 가 배포 직전에 질의하는 정책이다.
#     POST {OPA_URL}/v1/data/recoder/deploy/allow
#   OPA 서버가 없으면 코어가 같은 규칙을 로컬 폴백으로 적용하므로
#   (`opa_gate._local_deploy_gate`), **두 구현은 항상 같은 판단을 해야 한다.**
#   규칙을 고칠 때는 양쪽을 함께 고치고 `deploy_test.rego` 를 돌린다.
#
# 왜 객체를 돌려주나
#   코어는 `result` 에서 decision·reason·fix_suggestion·approval_level·
#   policy_bundle_version 을 읽는다. boolean 을 돌려주면 사용자는 "왜
#   막혔는지" 를 영영 알 수 없다 — 차단 사유는 정책의 일부다.
#
# 실행
#   opa run --server --addr localhost:8181 policies/
#   opa test policies/            # 규칙 검증
package recoder.deploy

import rego.v1

policy_bundle_version := "1.0.0"

# 프로덕션 배포가 허용되는 브랜치.
# 코어의 RECODER_PRODUCTION_BRANCHES 와 같은 목적. 서버 정책이 우선이므로
# 운영자가 여기를 바꾸면 모든 클라이언트에 즉시 적용된다.
production_branches := {"main"}

default allow := {
	"decision": "deny",
	"reason": "정책을 평가하지 못했습니다.",
	"fix_suggestion": "",
	"approval_level": 3,
	"policy_bundle_version": "1.0.0",
}

# ── 차단 규칙 ────────────────────────────────────────────────────────
#
# 순서는 로컬 폴백(_local_deploy_gate)과 같다. 하나라도 걸리면 deny 이므로
# 실제 평가 순서는 중요하지 않지만, 사람이 두 구현을 나란히 읽을 수 있도록
# 같은 차례로 적어 둔다.

# 규칙 1 — SBOM 없는 이미지는 배포할 수 없다.
deny contains msg if {
	not input.sbom.present
	msg := {
		"reason": "SBOM이 생성되지 않은 이미지는 배포할 수 없습니다. (Preset: SBOM 필수)",
		"fix": "배포 파이프라인에서 SBOM 생성(Syft) 단계가 성공했는지 확인하세요.",
	}
}

# 규칙 2 — Trivy critical 취약점.
deny contains msg if {
	count := object.get(input, ["trivy", "critical_count"], 0)
	count > 0
	msg := {
		"reason": sprintf("Trivy critical 취약점 %d건 감지. (Preset: Trivy critical 취약점 차단)", [count]),
		"fix": "취약한 패키지를 업그레이드하거나 베이스 이미지를 최신으로 교체하세요.",
	}
}

# 규칙 3 — 소스에 시크릿이 있다.
deny contains msg if {
	not object.get(input, ["gitleaks", "passed"], true)
	msg := {
		"reason": "gitleaks: secret 이 감지됐습니다. 소스코드에 자격증명이 포함돼 있을 수 있습니다.",
		"fix": "감지된 자격증명을 제거하고 **반드시 회전(rotate)** 시킨 뒤 다시 배포하세요.",
	}
}

# 규칙 4 — Dockerfile 오류.
deny contains msg if {
	not object.get(input, ["hadolint", "passed"], true)
	msg := {
		"reason": "Hadolint: Dockerfile 에서 오류가 감지됐습니다.",
		"fix": "Hadolint 가 지적한 error 수준 항목을 수정하세요.",
	}
}

# 규칙 5 — 프로덕션은 허용된 브랜치에서만.
#
# 브랜치를 **모르는** 경우도 막는다. 확인하지 못한 것을 "괜찮다"로 바꾸지
# 않는다 — 스캔 미실행을 통과로 치지 않는 것과 같은 규칙이다.
deny contains msg if {
	input.environment == "production"
	branch := trim_space(object.get(input, "branch", ""))
	branch == ""
	msg := {
		"reason": "프로덕션 배포인데 현재 브랜치를 확인할 수 없습니다.",
		"fix": "배포 요청에 branch 를 포함하세요.",
	}
}

deny contains msg if {
	input.environment == "production"
	branch := trim_space(object.get(input, "branch", ""))
	branch != ""
	not branch in production_branches
	msg := {
		"reason": sprintf(
			"프로덕션 배포는 %v 브랜치에서만 허용됩니다. (현재: %s)",
			[concat(", ", sort(production_branches)), branch],
		),
		"fix": "허용된 브랜치로 병합한 뒤 배포하세요.",
	}
}

# ── 최종 판단 ────────────────────────────────────────────────────────

allow := result if {
	count(deny) == 0
	result := {
		"decision": "allow",
		"reason": "모든 배포 게이트를 통과했습니다.",
		"fix_suggestion": "",
		"approval_level": 3,
		"policy_bundle_version": policy_bundle_version,
	}
}

allow := result if {
	count(deny) > 0
	#: 사유가 여럿이면 전부 보여 준다. 하나만 고치고 다시 막히는 것보다
	#: 한 번에 다 아는 편이 낫다.
	reasons := sort([m.reason | some m in deny])
	fixes := sort([m.fix | some m in deny; m.fix != ""])
	result := {
		"decision": "deny",
		"reason": concat(" / ", reasons),
		"fix_suggestion": concat(" / ", fixes),
		"approval_level": 4,
		"policy_bundle_version": policy_bundle_version,
	}
}
