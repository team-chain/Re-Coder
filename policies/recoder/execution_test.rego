package recoder.execution_test

import data.recoder.execution
import rego.v1

base := {
	"schema_version": 1, "local_allowed": true,
	"workspace_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
	"action": "repair.verify", "snapshot": true, "paths": [],
	"verified": false, "human_approved": false, "stage": "build", "tool": "docker-build-v1",
}

test_snapshot_verification_allowed if {
	execution.allow.decision == "allow" with input as base
}

test_unknown_action_denied if {
	execution.allow.decision == "deny" with input as object.union(base, {"action": "shell.execute"})
}

test_original_workspace_verification_denied if {
	execution.allow.decision == "deny" with input as object.union(base, {"snapshot": false})
}

test_local_denial_cannot_be_overridden if {
	execution.allow.decision == "deny" with input as object.union(base, {"local_allowed": false})
}

test_apply_needs_approval_and_verification if {
	request := object.union(base, {"action": "repair.apply", "paths": ["Dockerfile"]})
	execution.allow.decision == "deny" with input as request
	execution.allow.decision == "deny" with input as object.union(request, {"human_approved": true})
	execution.allow.decision == "deny" with input as object.union(request, {"verified": true})
	execution.allow.decision == "allow" with input as object.union(request, {"verified": true, "human_approved": true})
}

test_cloud_stage_cannot_be_applied if {
	request := object.union(base, {"action": "repair.apply", "paths": ["Dockerfile"], "verified": true, "human_approved": true, "stage": "ecs"})
	execution.allow.decision == "deny" with input as request
}

test_operator_revokes_execution if {
	execution.allow.decision == "deny" with input as base with data.recoder_execution as {"blocked_actions": ["repair.verify"]}
}

test_workspace_restriction if {
	execution.allow.decision == "deny" with input as base with data.recoder_execution as {"workspace_ids": ["different-workspace"]}
	execution.allow.decision == "allow" with input as base with data.recoder_execution as {"workspace_ids": [base.workspace_id]}
}

test_mcp_known_tool_only if {
	request := object.union(base, {"action": "mcp.call", "tool": "recoder_analyze"})
	execution.allow.decision == "allow" with input as request
	execution.allow.decision == "deny" with input as object.union(request, {"tool": "recoder_deploy"})
}

test_missing_or_unknown_schema_denied if {
	execution.allow.decision == "deny" with input as {}
	execution.allow.decision == "deny" with input as object.union(base, {"schema_version": 2})
}

test_malformed_operator_data_denied if {
	execution.allow.decision == "deny" with input as base with data.recoder_execution as {"blocked_actions": ""}
	execution.allow.decision == "deny" with input as base with data.recoder_execution as {"workspace_ids": ""}
}
