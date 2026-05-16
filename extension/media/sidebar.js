// ReCoder Sidebar -- Mini Controller v3 (inline panels)
(function () {
  'use strict';

  const vscode = acquireVsCodeApi();

  // ── State ─────────────────────────────────────────────────────────
  var _gitBranchDropdownOpen = false;
  var _gitCommitPanelOpen = false;
  var _gitCurrentBranch = '';
  var _gitHasRemote = false;
  var _ghUser = '';
  var _ghRepo = '';
  var _toastTimer = null;
  var _latestIssue = null;
  var _currentProposalId = null;
  var _openPanel = null; // 'error' | 'github' | 'deploy' | null

  // ── Helpers ───────────────────────────────────────────────────────
  function showToast(msg, ms) {
    var t = document.getElementById('toast');
    if (!t) return;
    if (_toastTimer) clearTimeout(_toastTimer);
    t.textContent = msg;
    t.classList.add('show');
    _toastTimer = setTimeout(function() { t.classList.remove('show'); }, ms || 2500);
  }
  function qs(id) { return document.getElementById(id); }
  function on(id, ev, fn) { var el = qs(id); if (el) el.addEventListener(ev, fn); }
  function timeAgo(ts) {
    if (!ts) return '';
    var diff = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
    if (diff < 60) return diff + '초 전';
    if (diff < 3600) return Math.floor(diff/60) + '분 전';
    if (diff < 86400) return Math.floor(diff/3600) + '시간 전';
    return Math.floor(diff/86400) + '일 전';
  }

  // ── Workbench page navigation ─────────────────────────────────────
  // CSP 준수: inline onclick 대신 이벤트 위임으로 처리
  // HTML에서 <button class="sb-wb-link" data-wb-page="error"> 형식 사용
  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.sb-wb-link[data-wb-page]');
    if (btn) {
      vscode.postMessage({ type: 'open_workbench_page', page: btn.getAttribute('data-wb-page') });
    }
  });

  // ── Panel accordion logic ─────────────────────────────────────────
  function togglePanel(name) {
    var btnId   = 'sb-btn-' + name;
    var panelId = 'sb-panel-' + name;
    var btn   = qs(btnId);
    var panel = qs(panelId);
    if (!btn || !panel) return;

    var isOpen = _openPanel === name;

    // close currently open panel
    if (_openPanel) {
      var ob = qs('sb-btn-' + _openPanel);
      var op = qs('sb-panel-' + _openPanel);
      if (ob) ob.classList.remove('active');
      if (op) op.classList.remove('open');
    }

    if (isOpen) {
      _openPanel = null;
    } else {
      btn.classList.add('active');
      panel.classList.add('open');
      _openPanel = name;
      _onPanelOpen(name);
    }
  }

  function _onPanelOpen(name) {
    if (name === 'error') {
      // pre-fill with latest issue text if textarea is empty
      var inp = qs('sb-error-input');
      if (inp && _latestIssue && _latestIssue.summary && !inp.value) {
        inp.value = _latestIssue.summary;
      }
    } else if (name === 'github') {
      _refreshGhPanelStatus();        // status 텍스트 + 섹션 show/hide
      vscode.postMessage({ type: 'gh_status' }); // 최신 인증 상태 재확인
    } else if (name === 'deploy') {
      vscode.postMessage({ type: 'deploy_status_poll' });
    }
  }

  function _refreshGhPanelStatus() {
    var el = qs('sb-gh-panel-status');
    if (!el) return;
    var parts = [];
    if (_ghUser) parts.push(_ghUser);
    // 원격이 없으면 레포/브랜치 정보를 패널 상태에도 숨김
    if (_ghRepo && _gitHasRemote) parts.push(_ghRepo);
    if (_gitCurrentBranch && _gitHasRemote) parts.push(_gitCurrentBranch);
    el.textContent = parts.length ? parts.join('  ·  ') : '미연결';
    el.style.color = _ghUser ? 'var(--green)' : 'var(--t3)';
    _updateGhPanelSections();
  }

  // ── Quick nav buttons ─────────────────────────────────────────────
  on('sb-btn-error',  'click', function() { togglePanel('error'); });
  on('sb-btn-github', 'click', function() { togglePanel('github'); });
  on('sb-btn-deploy', 'click', function() { togglePanel('deploy'); });
  on('btn-open-workbench', 'click', function() {
    vscode.postMessage({ type: 'open_workbench' });
  });

  // ══════════════════════════════════════════════════════════════════
  // PANEL: 에러 분석 (inline)
  // ══════════════════════════════════════════════════════════════════
  on('sb-do-analyze', 'click', function() {
    var inp = qs('sb-error-input');
    var errorText = inp ? inp.value.trim() : '';
    var statusEl = qs('sb-analyze-status');
    var resultEl = qs('sb-analyze-result');
    var btn = qs('sb-do-analyze');
    var patchBtns = qs('sb-patch-btns');
    if (statusEl) statusEl.textContent = 'AI 분석 중...';
    if (resultEl) resultEl.className = 'sb-result';
    if (btn) btn.disabled = true;
    if (patchBtns) patchBtns.style.display = 'none';
    _currentProposalId = null;
    vscode.postMessage({ type: 'analyze', error_text: errorText, terminal_output: errorText });
  });

  on('sb-do-approve', 'click', function() {
    if (!_currentProposalId) return;
    vscode.postMessage({ type: 'approve_patch', proposal_id: _currentProposalId });
    showToast('패치 적용 중...');
    var patchBtns = qs('sb-patch-btns');
    if (patchBtns) patchBtns.style.display = 'none';
    var statusEl = qs('sb-analyze-status');
    if (statusEl) statusEl.textContent = '적용 중...';
  });

  on('sb-do-reject', 'click', function() {
    if (!_currentProposalId) return;
    vscode.postMessage({ type: 'reject_patch', proposal_id: _currentProposalId });
    showToast('패치 거절됨');
    var patchBtns = qs('sb-patch-btns');
    if (patchBtns) patchBtns.style.display = 'none';
    _currentProposalId = null;
    var statusEl = qs('sb-analyze-status');
    if (statusEl) statusEl.textContent = '거절됨';
  });

  // ══════════════════════════════════════════════════════════════════
  // PANEL: GitHub Hub (inline)
  // ══════════════════════════════════════════════════════════════════

  /** 인증 상태에 따라 GitHub 패널 내 섹션 표시/숨김 */
  function _updateGhPanelSections() {
    var connectSec = qs('sb-gh-connect-section');
    var authedSec  = qs('sb-gh-authed-section');
    if (connectSec) connectSec.style.display = _ghUser ? 'none' : '';
    if (authedSec)  authedSec.style.display  = _ghUser ? '' : 'none';
  }

  // GitHub 연결 버튼 (VS Code OAuth 트리거)
  on('sb-gh-connect', 'click', function() {
    var btn = qs('sb-gh-connect');
    if (btn) btn.disabled = true;
    vscode.postMessage({ type: 'gh_login' });
    var st = qs('sb-gh-connect-status');
    if (st) st.textContent = 'GitHub 인증 창 열는 중...';
  });

  on('sb-gh-do-commit', 'click', function() {
    var msg = (qs('sb-gh-commit-msg') || {}).value.trim();
    if (!msg) { showToast('커밋 메시지를 입력하세요'); return; }
    vscode.postMessage({ type: 'git_commit_and_push', message: msg, push: false });
    showToast('커밋 중...');
    _setGhStatus('커밋 중...');
  });

  on('sb-gh-do-push', 'click', function() {
    vscode.postMessage({ type: 'git_push', branch: _gitCurrentBranch });
    showToast('Push 중...');
    _setGhStatus('Push 중...');
  });

  on('sb-gh-do-commit-push', 'click', function() {
    var msg = (qs('sb-gh-commit-msg') || {}).value.trim();
    if (!msg) { showToast('커밋 메시지를 입력하세요'); return; }
    vscode.postMessage({ type: 'git_commit_and_push', message: msg, push: true });
    showToast('커밋+Push 중...');
    _setGhStatus('커밋+Push 중...');
  });

  function _setGhStatus(msg) {
    var el = qs('sb-gh-panel-status-line');
    if (el) el.textContent = msg;
  }

  // ══════════════════════════════════════════════════════════════════
  // PANEL: 배포 센터 (inline)
  // ══════════════════════════════════════════════════════════════════
  on('sb-dep-dockerfile', 'click', function() {
    vscode.postMessage({ type: 'generate_dockerfile', file_type: 'dockerfile' });
    showToast('Dockerfile 생성 중...');
    _setDepStatus('Dockerfile 생성 중...');
  });

  on('sb-dep-build', 'click', function() {
    vscode.postMessage({ type: 'deploy_local', plan_id: '' });
    showToast('Docker 빌드 시작...');
    _setDepStatus('빌드 중...');
  });

  on('sb-dep-start', 'click', function() {
    vscode.postMessage({ type: 'deploy_local', plan_id: '' });
    showToast('배포 시작...');
    _setDepStatus('배포 시작 중...');
  });

  on('sb-dep-rollback', 'click', function() {
    vscode.postMessage({ type: 'deploy_rollback', plan_id: '' });
    showToast('롤백 중...');
    _setDepStatus('롤백 중...');
  });

  // ── EC2 배포 ──────────────────────────────────────────────────────
  // 배포 센터 패널이 열릴 때 EC2 준비 상태 확인
  document.addEventListener('click', function(e) {
    if (e.target && e.target.id === 'sb-btn-deploy') {
      vscode.postMessage({ type: 'deploy_ec2_ready_check' });
    }
  });

  on('sb-ec2-deploy-btn', 'click', function() {
    var imageName = (qs('sb-ec2-image') || {}).value || 'recoder-app';
    var repoName  = (qs('sb-ec2-repo')  || {}).value || 'recoder-app';
    var tag       = (qs('sb-ec2-tag')   || {}).value || 'latest';
    vscode.postMessage({
      type: 'deploy_ec2',
      image_name: imageName.trim() || 'recoder-app',
      repo_name:  repoName.trim()  || 'recoder-app',
      tag:        tag.trim()       || 'latest',
    });
    var prog = qs('sb-ec2-progress');
    if (prog) prog.style.display = 'block';
    _setEC2Stage('시작 중...');
    showToast('EC2 배포 시작...');
  });

  function _setEC2Stage(txt) {
    var el = qs('sb-ec2-stage'); if (el) el.textContent = txt || '—';
  }

  function _appendEC2Log(lines) {
    var el = qs('sb-ec2-log');
    if (!el || !lines || !lines.length) return;
    el.textContent += lines.join('\n') + '\n';
    el.scrollTop = el.scrollHeight;
  }

  function _setEC2ReadyChip(ready, issues) {
    var chip = qs('sb-ec2-ready-chip');
    var issueDiv = qs('sb-ec2-issues');
    if (chip) {
      if (ready) {
        chip.textContent = '준비됨';
        chip.style.background = 'var(--green-bg)';
        chip.style.color = 'var(--green)';
      } else {
        chip.textContent = '설정 필요';
        chip.style.background = 'var(--red-bg)';
        chip.style.color = 'var(--red)';
      }
    }
    if (issueDiv) {
      if (issues && issues.length) {
        issueDiv.textContent = issues.join('\n');
        issueDiv.style.display = 'block';
      } else {
        issueDiv.style.display = 'none';
      }
    }
  }

  // ── ECS Fargate 배포 ──────────────────────────────────────────────
  document.addEventListener('click', function(e) {
    if (e.target && e.target.id === 'sb-btn-deploy') {
      vscode.postMessage({ type: 'deploy_ecs_ready_check' });
    }
  });

  on('sb-ecs-deploy-btn', 'click', function() {
    var imageName = (qs('sb-ecs-image')   || {}).value || 'recoder-app';
    var repoName  = (qs('sb-ecs-repo')    || {}).value || 'recoder-app';
    var cluster   = (qs('sb-ecs-cluster') || {}).value || '';
    var service   = (qs('sb-ecs-service') || {}).value || '';
    var tag       = (qs('sb-ecs-tag')     || {}).value || 'latest';
    vscode.postMessage({
      type:        'deploy_ecs',
      image_name:  imageName.trim() || 'recoder-app',
      repo_name:   repoName.trim()  || 'recoder-app',
      ecs_cluster: cluster.trim(),
      ecs_service: service.trim(),
      tag:         tag.trim() || 'latest',
    });
    var prog = qs('sb-ecs-progress');
    if (prog) prog.style.display = 'block';
    _setECSStage('시작 중...');
    showToast('ECS Fargate 배포 시작...');
  });

  function _setECSStage(txt) {
    var el = qs('sb-ecs-stage'); if (el) el.textContent = txt || '—';
  }

  function _setECSReadyChip(ready, issues) {
    var chip     = qs('sb-ecs-ready-chip');
    var issueDiv = qs('sb-ecs-issues');
    if (chip) {
      chip.textContent = ready ? '준비됨' : '설정 필요';
      chip.style.background = ready ? 'var(--green-bg)' : 'var(--red-bg)';
      chip.style.color      = ready ? 'var(--green)'    : 'var(--red)';
    }
    if (issueDiv) {
      if (issues && issues.length) {
        issueDiv.textContent = issues.join('\n');
        issueDiv.style.display = 'block';
      } else {
        issueDiv.style.display = 'none';
      }
    }
  }

  function _setDepStatus(msg) {
    var el = qs('sb-dep-status-line');
    if (el) el.textContent = msg;
  }

  function _showDepResult(title, body, cls) {
    var r = qs('sb-dep-result');
    if (!r) return;
    var t = qs('sb-dep-res-title');
    var b = qs('sb-dep-res-body');
    if (t) t.textContent = title;
    if (b) b.textContent = body;
    r.className = 'sb-result show ' + (cls || '');
  }

  // ── Ready chips ───────────────────────────────────────────────────
  function setChip(id, state) {
    var el = qs(id);
    if (!el) return;
    el.className = 'rc';
    if (state === 'ok') el.classList.add('ok');
    else if (state === 'partial') el.classList.add('warn');
    else if (state === 'fail') el.classList.add('fail');
  }

  // ── Current Issue ─────────────────────────────────────────────────
  function renderIssue(proposal) {
    _latestIssue = proposal;
    var noEl  = qs('sb-no-issue');
    var hasEl = qs('sb-issue-content');
    var dotEl = qs('sb-issue-dot');
    var badge = qs('sb-error-badge');
    if (!proposal || !proposal.summary) {
      if (noEl) noEl.classList.remove('hidden');
      if (hasEl) hasEl.classList.add('hidden');
      if (dotEl) dotEl.style.display = 'none';
      if (badge) badge.style.display = 'none';
      return;
    }
    if (noEl) noEl.classList.add('hidden');
    if (hasEl) {
      hasEl.classList.remove('hidden');
      var summEl = qs('sb-issue-summary');
      var timeEl = qs('sb-issue-time');
      if (summEl) summEl.textContent = proposal.summary || '';
      if (timeEl) timeEl.textContent = timeAgo(proposal._ts || Date.now());
    }
    if (dotEl) dotEl.style.display = 'inline-block';
    if (badge) { badge.style.display = ''; badge.textContent = '1'; }
    if (_openPanel === 'error') _renderAnalyzeResult(proposal);
  }

  function _renderAnalyzeResult(proposal) {
    var resultEl  = qs('sb-analyze-result');
    var titleEl   = qs('sb-ar-title');
    var bodyEl    = qs('sb-ar-body');
    var statusEl  = qs('sb-analyze-status');
    var btn       = qs('sb-do-analyze');
    var patchBtns = qs('sb-patch-btns');
    if (!resultEl) return;
    if (btn) btn.disabled = false;
    var hasPatch = proposal && (proposal.proposal_id || proposal.id);
    if (titleEl) titleEl.textContent = proposal.summary || '분석 완료';
    if (bodyEl)  bodyEl.textContent  = proposal.root_cause || proposal.description || '';
    resultEl.className = 'sb-result show ok';
    if (statusEl) statusEl.textContent = hasPatch ? '패치 준비됨 — 아래에서 적용하세요' : '분석 완료 (패치 없음)';
    if (patchBtns) {
      patchBtns.style.display = hasPatch ? 'flex' : 'none';
      _currentProposalId = hasPatch ? (proposal.proposal_id || proposal.id) : null;
    }
  }

  // ── GitHub info ───────────────────────────────────────────────────
  function renderGitInfo(data) {
    if (!data) return;
    // gh_user는 gh_status_result가 권한 있는 소스 — git_info가 빈 값을 돌려줘도 덮어쓰지 않음
    if (data.gh_user) _ghUser = data.gh_user;
    _gitCurrentBranch = data.branch || '';
    _gitHasRemote = !!data.has_remote;

    // ★ GitHub 인증이 된 경우에만 레포 이름 표시
    // 로그아웃 상태·미연결 상태에서는 레포/브랜치를 표시하지 않음
    var repoName = '';
    if (_ghUser && data.has_remote && data.remote_url) {
      var m = data.remote_url.match(/([^/]+\/[^/]+?)(?:\.git)?$/);
      if (m) repoName = m[1];
    }
    _ghRepo = repoName;

    var userEl   = qs('sb-gh-user');
    var repoEl   = qs('sb-gh-repo');
    var branchEl = qs('sb-gh-branch');
    var ghCard   = qs('sb-github-card');
    if (userEl)   userEl.textContent   = _ghUser || '미연결';
    // 인증된 경우에만 레포 이름 표시, 미연결이면 '—'
    if (repoEl)   repoEl.textContent   = _ghUser ? (repoName || '로컬 저장소') : '—';
    // 인증된 경우에만 브랜치 표시, 미연결이면 '—'
    if (branchEl) branchEl.textContent = _ghUser ? (data.branch || '—') : '—';
    if (ghCard)   ghCard.classList.toggle('linked', !!_ghUser);

    // git-panel 전체: GitHub 인증된 경우에만 표시
    var gitPanel = qs('git-panel');
    if (gitPanel) gitPanel.style.display = _ghUser ? '' : 'none';

    var gitNameEl   = qs('git-account-name');
    var gitAcctEl   = qs('git-account');
    var gitBranchEl = qs('git-branch-name');
    if (gitNameEl)   gitNameEl.textContent   = _ghUser || '미연결';
    if (gitAcctEl)   gitAcctEl.classList.toggle('ok', !!_ghUser);
    if (gitBranchEl) gitBranchEl.textContent = data.is_git_repo ? (data.branch || 'detached') : 'git 없음';

    var changed = qs('git-uncommitted-badge');
    if (changed) {
      changed.textContent = String(data.uncommitted || 0) + ' 변경';
      changed.classList.toggle('hidden', !data.uncommitted);
    }
    var ahead = qs('git-ahead-badge');
    if (ahead) {
      ahead.textContent = String(data.ahead || 0) + ' 커밋 앞';
      ahead.classList.toggle('hidden', !data.ahead);
    }

    if (_openPanel === 'github') _refreshGhPanelStatus();
  }

  // ── Deploy status ─────────────────────────────────────────────────
  function renderDeployStatus(ds) {
    var stEl     = qs('sb-deploy-status');
    var healthEl = qs('sb-deploy-health');
    var depStage  = qs('sb-dep-stage');
    var depHealth = qs('sb-dep-health');
    if (!ds) return;
    var stageText = ds.stage === 'done' ? 'Production' : ds.stage || '—';
    var ok = ds.health === true;
    if (stEl) stEl.textContent = stageText;
    if (healthEl) {
      healthEl.textContent = ok ? 'Healthy' : ds.stage || '—';
      healthEl.className = 'sb-health-badge ' + (ok ? 'ok' : '');
    }
    if (depStage)  depStage.textContent  = stageText;
    if (depHealth) {
      depHealth.textContent = ok ? 'Healthy' : '—';
      depHealth.className   = 'sb-dep-badge ' + (ok ? 'ok' : '');
    }
  }

  // ── Git branch dropdown ───────────────────────────────────────────
  // CSP 준수: innerHTML+onclick 대신 DOM API + 이벤트 위임 방식 사용
  function _makeBranchEl(name, isCurrent, isRemote) {
    var div = document.createElement('div');
    div.className = 'gd-branch' + (isCurrent ? ' current' : '');
    div.setAttribute('data-branch', name);

    var dot = document.createElement('div');
    dot.className = 'gd-dot' + (isCurrent ? ' current' : '');
    if (isRemote) dot.style.background = 'var(--t3)';
    div.appendChild(dot);

    var nameSpan = document.createElement('span');
    nameSpan.textContent = name;
    div.appendChild(nameSpan);

    if (isCurrent) {
      var curLabel = document.createElement('span');
      curLabel.style.cssText = 'margin-left:auto;font-size:8px;color:var(--green)';
      curLabel.textContent = '현재';
      div.appendChild(curLabel);
    }
    if (isRemote) {
      var tag = document.createElement('span');
      tag.className = 'gd-remote-tag';
      tag.textContent = 'origin';
      div.appendChild(tag);
    }
    return div;
  }

  function renderGitBranches(data) {
    if (!data) return;
    var localEl  = qs('git-local-branches');
    var remoteEl = qs('git-remote-branches');
    var current  = data.current || '';

    if (localEl) {
      localEl.innerHTML = '';
      if (data.branches && data.branches.length) {
        data.branches.forEach(function(b) {
          localEl.appendChild(_makeBranchEl(b, b === current, false));
        });
      } else {
        var noEl = document.createElement('div');
        noEl.className = 'gd-branch';
        noEl.style.color = 'var(--t3)';
        noEl.textContent = '브랜치 없음';
        localEl.appendChild(noEl);
      }
    }

    if (remoteEl) {
      remoteEl.innerHTML = '';
      if (data.remote_branches && data.remote_branches.length) {
        data.remote_branches.forEach(function(b) {
          var short = b.replace(/^origin\//, '');
          remoteEl.appendChild(_makeBranchEl(short, false, true));
        });
      } else {
        var noEl2 = document.createElement('div');
        noEl2.className = 'gd-branch';
        noEl2.style.color = 'var(--t3)';
        noEl2.textContent = '원격 없음';
        remoteEl.appendChild(noEl2);
      }
    }
  }

  // 이벤트 위임: git-dropdown 내 브랜치 클릭 → checkout
  (function() {
    var dd = qs('git-dropdown');
    if (!dd) return;
    dd.addEventListener('click', function(e) {
      var target = e.target.closest('.gd-branch[data-branch]');
      if (!target) return;
      var branch = target.getAttribute('data-branch');
      if (!branch) return;
      vscode.postMessage({ type: 'git_checkout', branch: branch });
      showToast('브랜치 전환: ' + branch);
      _gitBranchDropdownOpen = false;
      dd.classList.remove('open');
    });
  }());

  // ── Git strip controls ────────────────────────────────────────────
  on('git-branch-btn', 'click', function() {
    _gitBranchDropdownOpen = !_gitBranchDropdownOpen;
    var dd = qs('git-dropdown');
    if (dd) dd.classList.toggle('open', _gitBranchDropdownOpen);
    if (_gitBranchDropdownOpen) vscode.postMessage({ type: 'git_branches_request' });
  });
  on('git-btn-commit', 'click', function() {
    _gitCommitPanelOpen = !_gitCommitPanelOpen;
    var p = qs('git-commit-panel');
    if (p) p.classList.toggle('open', _gitCommitPanelOpen);
  });
  on('git-btn-push', 'click', function() {
    if (!_ghUser) { showToast('Push하려면 GitHub에 먼저 연결하세요'); return; }
    vscode.postMessage({ type: 'git_push', branch: _gitCurrentBranch });
    showToast('Push 중...');
  });
  on('git-btn-branch-create', 'click', function() {
    var inp = qs('git-new-branch-input');
    var name = inp ? inp.value.trim() : '';
    if (!name) { showToast('브랜치 이름을 입력하세요'); return; }
    vscode.postMessage({ type: 'git_branch_create', branch_name: name });
    showToast('브랜치 생성: ' + name);
    if (inp) inp.value = '';
  });
  on('git-btn-do-commit', 'click', function() {
    var txt = (qs('git-commit-msg') || {}).value.trim();
    if (!txt) { showToast('커밋 메시지를 입력하세요'); return; }
    vscode.postMessage({ type: 'git_commit_and_push', message: txt, push: false });
    showToast('커밋 중...');
    var st = qs('git-commit-status'); if (st) st.textContent = '커밋 중...';
  });
  on('git-btn-commit-push', 'click', function() {
    var txt = (qs('git-commit-msg') || {}).value.trim();
    if (!txt) { showToast('커밋 메시지를 입력하세요'); return; }
    if (!_ghUser) { showToast('Push하려면 GitHub에 먼저 연결하세요'); return; }
    vscode.postMessage({ type: 'git_commit_and_push', message: txt, push: true });
    showToast('커밋+Push 중...');
    var st = qs('git-commit-status'); if (st) st.textContent = '커밋+Push 중...';
  });

  // ── Message handler ───────────────────────────────────────────────
  window.addEventListener('message', function(event) {
    var msg = event.data;
    if (!msg || !msg.type) return;
    switch (msg.type) {
      case 'ready_state':
      case 'ready_update': {
        var d = msg.data || msg.ready || {};
        setChip('chip-core', d.core_ready);
        setChip('chip-ai', d.ai_ready);
        setChip('chip-docker', d.docker_ready);
        // GitHub chip: only set 'ok' if we know the user; don't clobber otherwise
        if (_ghUser) setChip('chip-github', 'ok');
        break;
      }
      case 'gh_status_result': {
        var gs = msg.data || {};
        if (gs.user) {
          _ghUser = gs.user;
          setChip('chip-github', 'ok');
          var userEl = qs('sb-gh-user');
          if (userEl) userEl.textContent = gs.user;
          var ghCard = qs('sb-github-card');
          if (ghCard) ghCard.classList.add('linked');
          // 연결 완료 → 연결 버튼 초기화
          var connectBtn = qs('sb-gh-connect');
          if (connectBtn) connectBtn.disabled = false;
          var connectSt = qs('sb-gh-connect-status');
          if (connectSt) connectSt.textContent = '';
        } else if (gs.installed === false || gs.authed === false) {
          _ghUser = '';
          // 로그아웃 시 레포·브랜치 정보 즉시 초기화
          _ghRepo = '';
          _gitCurrentBranch = '';
          _gitHasRemote = false;
          var repoEl2 = qs('sb-gh-repo');
          if (repoEl2) repoEl2.textContent = '—';
          var branchEl2 = qs('sb-gh-branch');
          if (branchEl2) branchEl2.textContent = '—';
          var userEl2 = qs('sb-gh-user');
          if (userEl2) userEl2.textContent = '미연결';
          var ghCard2 = qs('sb-github-card');
          if (ghCard2) ghCard2.classList.remove('linked');
          // git-panel 전체 숨김
          var gitPanel2 = qs('git-panel');
          if (gitPanel2) gitPanel2.style.display = 'none';
          // 2026-05-11: VS Code 에 GitHub 계정은 있는데 ReCoder 권한이 없는 경우
          if (gs.vscode_detected && gs.vscode_user) {
            setChip('chip-github', 'partial');
            var hintEl = qs('sb-gh-connect-status');
            if (hintEl) hintEl.textContent = 'VS Code 계정 감지: ' + gs.vscode_user + ' — 권한 부여 필요';
          } else {
            setChip('chip-github', 'fail');
          }
          // 미연결 → 연결 버튼 활성화
          var connectBtn = qs('sb-gh-connect');
          if (connectBtn) connectBtn.disabled = false;
        }
        if (_openPanel === 'github') {
          _refreshGhPanelStatus();
          _updateGhPanelSections();
        }
        break;
      }
      case 'gh_login_progress': {
        // 로그인 진행 상태 표시 (VS Code OAuth 진행 중)
        var st = qs('sb-gh-connect-status');
        if (st) st.textContent = msg.message || '';
        if (!msg.message) {
          var connectBtn = qs('sb-gh-connect');
          if (connectBtn) connectBtn.disabled = false;
        }
        break;
      }
      case 'analyze_result':
        if (msg.data) {
          msg.data._ts = Date.now();
          renderIssue(msg.data);
          if (_openPanel === 'error') _renderAnalyzeResult(msg.data);
          var btn = qs('sb-do-analyze'); if (btn) btn.disabled = false;
        }
        break;
      case 'patch_result': {
        var d = msg.data || {};
        var statusEl = qs('sb-analyze-status');
        if (d.status === 'ok' || d.status === 'applied') {
          if (statusEl) statusEl.textContent = '패치 적용 완료';
          showToast('패치 적용 완료!');
        } else {
          if (statusEl) statusEl.textContent = '[실패] 적용 오류: ' + (d.message || '');
          showToast('패치 실패');
        }
        break;
      }
      case 'patch_rejected':
        showToast('패치 거절됨');
        break;
      case 'auto_detected':
        showToast('터미널 오류 감지! 에러 분석 패널 확인');
        if (_openPanel !== 'error') togglePanel('error');
        break;
      case 'git_info_result':
        renderGitInfo(msg.data);
        break;
      case 'git_branches_result':
        renderGitBranches(msg.data);
        break;
      case 'git_checkout_result':
      case 'git_branch_create_result':
      case 'git_push_result': {
        var d = msg.data || {};
        var st = qs('git-commit-status'); if (st) st.textContent = d.message || '';
        _setGhStatus(d.message || '완료');
        showToast(d.message || '완료');
        vscode.postMessage({ type: 'git_info_request' });
        break;
      }
      case 'git_commit_result': {
        var d = msg.data || {};
        var sha = d.commit_hash ? ' (' + d.commit_hash.slice(0,7) + ')' : '';
        if (d.status === 'ok' || d.status === 'committed') {
          showToast('커밋 완료' + sha);
          _setGhStatus('커밋 완료' + sha);
          var cm  = qs('git-commit-msg');    if (cm)  cm.value  = '';
          var cm2 = qs('sb-gh-commit-msg'); if (cm2) cm2.value = '';
          var st  = qs('git-commit-status'); if (st)  st.textContent = 'done' + sha;
        } else {
          _setGhStatus('커밋 실패: ' + (d.message || ''));
          showToast('커밋 실패');
          var st = qs('git-commit-status'); if (st) st.textContent = 'fail: ' + (d.message || '');
        }
        vscode.postMessage({ type: 'git_info_request' });
        break;
      }
      case 'deploy_status':
        renderDeployStatus(msg.data);
        break;

      case 'ec2_ready_result': {
        var d = msg.data || {};
        _setEC2ReadyChip(d.ready, d.issues);
        break;
      }
      case 'ec2_deploy_started': {
        var d = msg.data || {};
        if (d.status === 'ok') {
          _setEC2Stage('배포 시작됨');
          var prog = qs('sb-ec2-progress');
          if (prog) prog.style.display = 'block';
        } else {
          _setEC2Stage('시작 실패: ' + (d.message || ''));
          showToast('EC2 배포 시작 실패: ' + (d.message || ''), 3500);
        }
        break;
      }
      case 'ec2_deploy_status': {
        var d = msg.data || {};
        var stageMap = {
          idle: '대기', building: '빌드 중', ecr_login: 'ECR 로그인',
          ecr_push: 'ECR Push 중', ec2_deploy: 'EC2 배포 중',
          done: '✅ 완료', failed: '❌ 실패',
        };
        _setEC2Stage(stageMap[d.stage] || d.stage || '—');
        if (d.log_tail && d.log_tail.length) {
          var logEl = qs('sb-ec2-log');
          if (logEl) {
            logEl.textContent = d.log_tail.slice(-30).join('\n');
            logEl.scrollTop = logEl.scrollHeight;
          }
        }
        if (d.stage === 'done') {
          _setDepStatus('EC2 배포 완료');
          showToast('EC2 배포 완료 ✅');
        } else if (d.stage === 'failed') {
          _setDepStatus('EC2 배포 실패: ' + (d.error || ''));
          showToast('EC2 배포 실패: ' + (d.error || ''), 4000);
        }
        break;
      }

      case 'ecs_ready_result': {
        var d = msg.data || {};
        _setECSReadyChip(d.ready, d.issues);
        break;
      }
      case 'ecs_deploy_started': {
        var d = msg.data || {};
        if (d.status === 'ok') {
          _setECSStage('배포 시작됨');
          var prog = qs('sb-ecs-progress');
          if (prog) prog.style.display = 'block';
        } else {
          _setECSStage('시작 실패: ' + (d.message || ''));
          showToast('ECS 배포 시작 실패: ' + (d.message || ''), 3500);
        }
        break;
      }
      case 'ecs_deploy_status': {
        var d = msg.data || {};
        var stageMap = {
          idle:       '대기',
          building:   '빌드 중',
          ecr_push:   'ECR Push 중',
          task_def:   'Task Def 등록',
          svc_update: 'Service 업데이트',
          deploying:  '배포 중 (폴링)',
          done:       '✅ 완료',
          failed:     '❌ 실패',
        };
        _setECSStage(stageMap[d.stage] || d.stage || '—');
        if (d.log_tail && d.log_tail.length) {
          var logEl = qs('sb-ecs-log');
          if (logEl) {
            logEl.textContent = d.log_tail.slice(-40).join('\n');
            logEl.scrollTop = logEl.scrollHeight;
          }
        }
        // Rollback proposal 힌트 표시
        var hintEl = qs('sb-ecs-rollback-hint');
        if (hintEl) {
          if (d.rollback_proposal) {
            hintEl.textContent = '⚠️ Rollback Proposal 생성됨 (Approval Level 3) — 이전 Task Definition으로 되돌릴 수 있습니다.';
            hintEl.style.display = 'block';
          } else {
            hintEl.style.display = 'none';
          }
        }
        if (d.stage === 'done') {
          _setDepStatus('ECS 배포 완료');
          showToast('ECS Fargate 배포 완료 ✅');
        } else if (d.stage === 'failed') {
          _setDepStatus('ECS 배포 실패: ' + (d.error || '').slice(0, 80));
          showToast('ECS 배포 실패: ' + (d.error || '').slice(0, 60), 4000);
        }
        break;
      }

      case 'deploy_started': {
        var d = msg.data || {};
        _setDepStatus('배포 시작됨: ' + (d.status || ''));
        showToast('배포 시작됨');
        break;
      }
      case 'dockerfile_result': {
        var d = msg.data || {};
        _showDepResult('Dockerfile 생성됨', d.file_path || d.summary || '생성 완료', 'ok');
        _setDepStatus('');
        showToast('Dockerfile 생성 완료');
        break;
      }
      case 'rollback_result': {
        var d = msg.data || {};
        _setDepStatus(d.status === 'ok' ? '롤백 완료' : '롤백 실패: ' + (d.message || ''));
        showToast(d.status === 'ok' ? '롤백 완료' : '롤백 실패');
        break;
      }
      case 'cost_update': {
        var d = msg.data || {};
        var daily   = qs('cost-daily');   if (daily)   daily.textContent   = '$' + (d.daily  ||0).toFixed(4);
        var monthly = qs('cost-monthly'); if (monthly) monthly.textContent = '$' + (d.monthly||0).toFixed(2);
        var calls   = qs('cost-calls');   if (calls)   calls.textContent   = String(d.calls||0);
        break;
      }
      case 'error': {
        if (_openPanel === 'error') {
          var statusEl = qs('sb-analyze-status');
          if (statusEl) statusEl.textContent = '[오류] ' + (msg.message || '오류 발생');
          var btn = qs('sb-do-analyze'); if (btn) btn.disabled = false;
        }
        if (_openPanel === 'deploy') _setDepStatus('[오류] ' + (msg.message || '오류'));
        if (_openPanel === 'github') _setGhStatus('[오류] ' + (msg.message || '오류'));
        showToast(msg.message || '오류 발생', 3500);
        break;
      }
    }
  });

  // ── Init ─────────────────────────────────────────────────────────
  // gh_status 폴링은 sidebarProvider 서버 사이드에서 관리.
  // 여기서 중복 gh_status 를 보내면 _autoDetectGhSession 이 동시에 여러 번 실행되어
  // "Connect to GitHub" 팝업이 중복 표시되는 원인이 됨 → 제거.
  vscode.postMessage({ type: 'ready' });
  vscode.postMessage({ type: 'git_info_request' });

})();
