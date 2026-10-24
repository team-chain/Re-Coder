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


class RevertError(RuntimeError):
    """되돌림 PR 을 만들 수 없는 이유(사용자에게 그대로 보여 줄 한국어 문장)."""

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
        1. 대상 commit 조회 (바뀐 파일·부모 확인)
        2. base 브랜치 현재 HEAD 위에 revert commit 생성(대상이 바꾼 파일만 되돌림)
        3. 그 commit 을 가리키는 revert branch 생성(강제 갱신 없음)
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
                # 1. 대상 commit 조회 — 바뀐 파일 목록과 부모
                commit_data = await self._get_commit(client, headers, repo, request.target_commit_sha)
                commit_message = commit_data.get("commit", {}).get("message", "").split("\n")[0]
                parents = commit_data.get("parents") or []
                if len(parents) != 1:
                    raise RevertError(
                        "병합 커밋이거나 부모가 없는 커밋은 자동으로 되돌릴 수 없습니다. 로컬에서 git revert -m 으로 되돌려 주십시오."
                    )
                parent_sha = parents[0].get("sha", "")

                # 2. base 브랜치의 **현재** HEAD 위에 되돌림 커밋을 만든다(강제 갱신 없음)
                base_sha = await self._get_branch_sha(client, headers, repo, request.base_branch)
                revert_sha = await self._create_revert_commit(
                    client, headers, repo,
                    target_sha=request.target_commit_sha,
                    parent_sha=parent_sha,
                    base_sha=base_sha,
                    files=commit_data.get("files") or [],
                    original_message=commit_message,
                )

                # 3. 되돌림 커밋을 가리키는 새 브랜치(이미 있으면 실패 — 남의 브랜치를 덮어쓰지 않는다)
                await self._create_branch(client, headers, repo, record.revert_branch, revert_sha)

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
                    "Rollback PR created: %s#%s url=%s revert=%s",
                    repo, record.pr_number, record.pr_url, revert_sha[:8],
                )

            except RevertError as exc:
                record.status = "failed"
                record.error_message = str(exc)
                logger.warning("Rollback PR refused: %s", exc)
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

    async def _get_tree(self, client: httpx.AsyncClient, headers: dict, repo: str, commit_sha: str) -> tuple[str, dict]:
        """커밋의 (트리 SHA, {경로: (mode, blob sha)}) — 하위 폴더까지."""
        resp = await client.get(f"{_GITHUB_API}/repos/{repo}/git/commits/{commit_sha}", headers=headers)
        resp.raise_for_status()
        tree_sha = resp.json()["tree"]["sha"]
        resp = await client.get(f"{_GITHUB_API}/repos/{repo}/git/trees/{tree_sha}", headers=headers,
                                params={"recursive": "1"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("truncated"):
            raise RevertError("저장소 트리가 너무 커서 자동으로 되돌릴 수 없습니다. 로컬에서 git revert 로 되돌려 주십시오.")
        entries = {e["path"]: (e.get("mode", "100644"), e.get("sha")) for e in data.get("tree") or []
                   if e.get("type") == "blob"}
        return tree_sha, entries

    async def _create_revert_commit(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        repo: str,
        *,
        target_sha: str,
        parent_sha: str,
        base_sha: str,
        files: list,
        original_message: str,
    ) -> str:
        """대상 커밋이 바꾼 파일만 부모 상태로 되돌린 트리를 **base 의 현재 HEAD 위에** 커밋한다.

        예전에는 대상 커밋의 부모 트리를 통째로 복사하고 부모를 parent_sha 로 둔 커밋을 만든 뒤 브랜치를
        force 로 그 커밋에 옮겼다 — 브랜치가 base 보다 **뒤로** 가서 PR 이 비거나, 그 뒤의 커밋을 모두 되돌렸다.

        이제는 바뀐 파일마다: 대상 커밋에서 추가된 파일은 지우고, 수정·삭제된 파일은 부모의 내용으로 되돌린다.
        그 사이 base 에서 같은 파일이 또 바뀌었으면(충돌) 남의 변경을 덮지 않도록 멈춘다.
        결과 트리가 base 와 같으면(되돌릴 것이 없음) 빈 PR 을 만들지 않고 실패한다.
        """
        if not files:
            raise RevertError("대상 커밋에서 바뀐 파일을 찾지 못해 되돌릴 내용이 없습니다.")
        _parent_tree, parent_entries = await self._get_tree(client, headers, repo, parent_sha)
        _target_tree, target_entries = await self._get_tree(client, headers, repo, target_sha)
        base_tree, base_entries = await self._get_tree(client, headers, repo, base_sha)

        paths: set[str] = set()
        for f in files:
            if f.get("filename"):
                paths.add(str(f["filename"]))
            if f.get("previous_filename"):
                paths.add(str(f["previous_filename"]))

        changes: list[dict] = []
        conflicts: list[str] = []
        for path in sorted(paths):
            if base_entries.get(path) == parent_entries.get(path):
                continue  # base 에서 이미 대상 커밋 이전 상태로 돌아가 있다
            if base_entries.get(path) != target_entries.get(path):
                conflicts.append(path)
                continue
            before = parent_entries.get(path)
            if before == target_entries.get(path):
                continue
            if before is None:
                if path in base_entries:
                    changes.append({"path": path, "mode": base_entries[path][0], "type": "blob", "sha": None})
            else:
                changes.append({"path": path, "mode": before[0], "type": "blob", "sha": before[1]})
        if conflicts:
            raise RevertError(
                "대상 커밋 뒤에 같은 파일이 다시 바뀌어 자동으로 되돌리면 그 변경을 덮어씁니다: "
                + ", ".join(conflicts[:5]) + (" 외" if len(conflicts) > 5 else "")
                + ". 로컬에서 git revert 로 충돌을 해결해 주십시오."
            )
        if not changes:
            raise RevertError("되돌릴 변경이 없습니다 — 대상 커밋의 변경이 이미 base 브랜치에서 되돌려져 있습니다.")

        resp = await client.post(f"{_GITHUB_API}/repos/{repo}/git/trees", headers=headers,
                                 json={"base_tree": base_tree, "tree": changes})
        resp.raise_for_status()
        new_tree = resp.json()["sha"]
        if new_tree == base_tree:
            raise RevertError("되돌릴 변경이 없습니다 — 만든 트리가 base 브랜치와 같습니다.")

        resp = await client.post(
            f"{_GITHUB_API}/repos/{repo}/git/commits", headers=headers,
            json={
                "message": f'Revert "{original_message}"\n\nThis reverts commit {target_sha}.\n\nAuto-generated by ReCoder (ADR-005)',
                "tree": new_tree,
                "parents": [base_sha],
            },
        )
        resp.raise_for_status()
        new_sha = resp.json()["sha"]
        logger.debug("Revert commit created: %s on %s", new_sha[:8], base_sha[:8])
        return new_sha

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
