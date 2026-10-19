# Execution facts are constructed by Core, never by the model.
# OPA is an additional restriction; it cannot override Core's local path/approval checks.
package recoder.execution

import rego.v1

policy_bundle_version := "execution-v1"

default allow := {
	"decision": "deny",
	"reason": "실행 작업 또는 작업 폴더가 정책에서 허용되지 않았습니다.",
	"approval_level": 3,
	"policy_bundle_version": "execution-v1",
}

default config := {}

config := data.recoder_execution
blocked_actions := object.get(config, "blocked_actions", [])
workspace_ids := object.get(config, "workspace_ids", [])

workspace_allowed if {
	count(workspace_ids) == 0
}

workspace_allowed if {
	input.workspace_id in workspace_ids
}

action_allowed if {
	input.action in {"repair.inspect", "repair.generate", "repair.verify", "repair.rule_fix"}
	input.snapshot == true
}

action_allowed if {
	input.action == "repair.edit"
	input.snapshot == true
	count(input.paths) > 0
}

action_allowed if {
	input.action == "repair.apply"
	input.verified == true
	input.human_approved == true
	input.stage == "build"
	count(input.paths) > 0
}

action_allowed if {
	input.action == "mcp.call"
	input.tool == "recoder_analyze"
}

allow := {
	"decision": "allow",
	"reason": "실행 정책을 통과했습니다.",
	"approval_level": 3,
	"policy_bundle_version": policy_bundle_version,
} if {
	input.schema_version == 1
	input.local_allowed == true
	is_string(input.workspace_id)
	count(input.workspace_id) == 64
	is_array(blocked_actions)
	is_array(workspace_ids)
	action_allowed
	workspace_allowed
	not input.action in blocked_actions
}
