"""
core/agents/rollback_pr_agent.py — **revert-commit flow** rollback PR 생성기 (ADR-005)

ReCoder 에는 두 개의 rollback PR 변종이 존재한다.

  ┌─────────────────────────────────┬─────────────────────────────────────┐
  │ 파일                             │ 시나리오                             │
  ├─────────────────────────────────┼─────────────────────────────────────┤
  │ core/rollback_pr_agent.py        │ Helm-values flow. ArgoCD 가          │
  │                                  │ Helm chart values 를 watch 하는 환경. │
  ├─────────────────────────────────┼─────────────────────────────────────┤
  │ core/agents/rollback_pr_agent.py │ **revert-commit flow** (이 파일).    │
  │                                  │ 임의 commit SHA 를 GitHub API 로      │
  │                                  │ revert 하고 PR 을 연다. Helm 비사용    │
  │                                  │ 또는 일반 Git 기반 GitOps 환경.        │
  └─────────────────────────────────┴─────────────────────────────────────┘

두 변종 모두 ADR-005 의 production rollback 정책을 충족한다.

설계서 ADR-005:
- 프로덕션 rollback 기본 경로 = Git revert PR
- ArgoCD 직접 rollback은 Severity 1만 허용 (gitops_agent.rollback_app() 이 강제)
- rollback PR은 Level 3 승인 필요 (Multi-Approver)

동작 흐름:
1. 대상 commit SHA를 git revert (GitHub API)
2. revert branch 생성
3. PR 오픈 (title + body 자동 생성)
4. 승인 요청 링크 연결
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from core.schemas import RollbackPRRecord, RollbackPRRequest

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_DEFAULT_TIMEOUT = 30


class RollbackPRAgent:
    """
    GitHub API를 통해 git revert PR을 생성하는 에이전트.

    ADR-005 준수: 코드 변경 rollback은 항상 PR 경로.
    """

    async def create_rollback_pr(self, request: RollbackPRRequest) -> RollbackPRRecord:
        """
        지정된 commit SHA를 revert하는 PR을 생성한다.

        단계:
        1. 대상 commit 조회 (parent SHA 확인)
        2. revert branch 생성
        3. revert commit 생성
        4. PR 오픈
        """
        record = RollbackPRRecord(
            project_id=request.project_id,
            repo_full_name=f"{request.repo_owner}/{request.repo_name}",
            target_commit_sha=request.target_commit_sha,
            revert_branch=f"revert/{request.target_commit_sha[:8]}-auto",
        )

        headers = {
            "Authorization": f"Bearer {request.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        repo = f"{request.repo_owner}/{request.repo_name}"

        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            try:
                # 1. 대상 commit 조회
                commit_data = await self._get_commit(client, headers, repo, request.target_commit_sha)
                commit_message = commit_data.get("commit", {}).get("message", "").split("\n")[0]
                parent_sha = commit_data.get("parents", [{}])[0].get("sha", "")

                # 2. revert branch 생성 (base_branch의 HEAD에서)
                base_sha = await self._get_branch_sha(client, headers, repo, request.base_branch)
                await self._create_branch(client, headers, repo, record.revert_branch, base_sha)

                # 3. revert commit 생성
                revert_sha = await self._create_revert_commit(
                    client, headers, repo,
                    request.target_commit_sha,
                    parent_sha,
                    record.revert_branch,
                    commit_message,
                )
                record.target_commit_sha = revert_sha or request.target_commit_sha

                # 4. PR 생성
                pr_title = request.pr_title or f"revert: {commit_message[:60]}"
                pr_body = request.pr_body or self._generate_pr_body(request, commit_message)

                pr_data = await self._create_pr(
                    client, headers, repo,
                    title=pr_title,
                    body=pr_body,
                    head=record.revert_branch,
                    base=request.base_branch,
                )

                record.pr_number = pr_data.get("number")
                record.pr_url = pr_data.get("html_url")
                record.status = "opened"

                logger.info(
                    "Rollback PR created: %s#%d url=%s",
                    repo, record.pr_number, record.pr_url,
                )

            except httpx.HTTPStatusError as exc:
                record.status = "failed"
                record.error_message = (
                    f"GitHub API 오류 {exc.response.status_code}: {exc.response.text[:300]}"
                )
                logger.error("Rollback PR GitHub error: %s", exc)
            except Exception as exc:
                record.status = "failed"
                record.error_message = str(exc)
                logger.error("Rollback PR error: %s", exc, exc_info=True)

        return record

    # ------------------------------------------------------------------
    # GitHub API 헬퍼
    # ------------------------------------------------------------------

    async def _get_commit(self, client: httpx.AsyncClient, headers: dict, repo: str, sha: str) -> dict:
        url = f"{_GITHUB_API}/repos/{repo}/commits/{sha}"
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _get_branch_sha(
        self, client: httpx.AsyncClient, headers: dict, repo: str, branch: str
    ) -> str:
        url = f"{_GITHUB_API}/repos/{repo}/git/ref/heads/{branch}"
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()["object"]["sha"]

    async def _create_branch(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        repo: str,
        branch_name: str,
        from_sha: str,
    ) -> None:
        url = f"{_GITHUB_API}/repos/{repo}/git/refs"
        body = {"ref": f"refs/heads/{branch_name}", "sha": from_sha}
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        logger.debug("Branch created: %s @ %s", branch_name, from_sha[:8])

    async def _create_revert_commit(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        repo: str,
        target_sha: str,
        parent_sha: str,
        branch: str,
        original_message: str,
    ) -> Optional[str]:
        """
        실제 revert는 GitHub API가 직접 지원하지 않으므로
        tree를 parent 상태로 되돌리는 commit을 생성한다.
        """
        try:
            # parent tree SHA 조회
            parent_commit_url = f"{_GITHUB_API}/repos/{repo}/git/commits/{parent_sha}"
            resp = await client.get(parent_commit_url, headers=headers)
            resp.raise_for_status()
            parent_tree_sha = resp.json()["tree"]["sha"]

            # revert commit 생성
            create_commit_url = f"{_GITHUB_API}/repos/{repo}/git/commits"
            body = {
                "message": f'Revert "{original_message}"\n\nThis reverts commit {target_sha}.\n\nAuto-generated by ReCoder (ADR-005)',
                "tree": parent_tree_sha,
                "parents": [parent_sha],
            }
            resp = await client.post(create_commit_url, headers=headers, json=body)
            resp.raise_for_status()
            new_sha = resp.json()["sha"]

            # branch ref 업데이트
            update_ref_url = f"{_GITHUB_API}/repos/{repo}/git/refs/heads/{branch}"
            resp = await client.patch(
                update_ref_url, headers=headers, json={"sha": new_sha, "force": True}
            )
            resp.raise_for_status()
            logger.debug("Revert commit created: %s", new_sha[:8])
            return new_sha

        except Exception as exc:
            logger.warning("Could not create revert commit via API: %s", exc)
            return None

    async def _create_pr(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict:
        url = f"{_GITHUB_API}/repos/{repo}/pulls"
        payload = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": False,
        }
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # PR body 생성
    # ------------------------------------------------------------------

    def _generate_pr_body(self, request: RollbackPRRequest, original_message: str) -> str:
        return f"""## 🔄 자동 Rollback PR

> **ReCoder가 ADR-005에 따라 자동 생성한 revert PR입니다.**

### 대상 커밋
- SHA: `{request.target_commit_sha}`
- 메시지: {original_message}

### 롤백 이유
{f'승인 요청 ID: `{request.approval_request_id}`' if request.approval_request_id else '직접 요청에 의한 롤백'}

### 체크리스트

- [ ] 변경 사항을 검토했습니다
- [ ] 로컬 환경에서 테스트했습니다
- [ ] 관련 모니터링 지표를 확인했습니다
- [ ] On-call 담당자에게 통보했습니다

### 참고

- ADR-005: 프로덕션 롤백은 Git revert PR이 기본 경로
- ADR-006: ArgoCD 직접 롤백은 Severity 1 장애만 허용
- 이 PR은 **Level 3 승인** (2인 이상) 후 머지 가능합니다

---
_Generated by ReCoder — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_
"""


# 모듈 레벨 싱글톤
rollback_pr_agent = RollbackPRAgent()
