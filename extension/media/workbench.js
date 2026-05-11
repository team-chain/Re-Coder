// ReCoder Workbench Script v2 -- dashboard + log panel
(function () {
  'use strict';

  const vscode = acquireVsCodeApi();

  // ── State ─────────────────────────────────────────────────────────
  let _currentPatchProposal = null;
  let _currentInfraProposal = null;
  let _currentDeployPlan = null;
  let _gitBranchDropdownOpen = false;
  let _gitCommitPanelOpen = false;
  let _gitCurrentBranch = '';
  let _ghUser = '';
  let _ghRepo = '';
  let _deployPollTimer = null;
  let _toastTimer = null;
  let _activity = []; // [{dot, text, time}]
  let _currentLogTab = 'ai';

  // ── Helpers ───────────────────────────────────────────────────────
  function showToast(msg, ms) {
    const t = document.getElementById('wb-toast');
    if (!t) return;
    if (_toastTimer) clearTimeout(_toastTimer);
    t.textContent = msg;
    t.classList.add('show');
    _toastTimer = setTimeout(() => t.classList.remove('show'), ms || 2800);
  }
  function hide(id) { const el = document.getElementById(id); if (el) el.classList.add('hidden'); }
  function show(id) { const el = document.getElementById(id); if (el) el.classList.remove('hidden'); }
  function qs(id) { return document.getElementById(id); }
  function on(id, ev, fn) { const el = qs(id); if (el) el.addEventListener(ev, fn); }
  function now() { return new Date().toLocaleTimeString('ko-KR', {hour:'2-digit',minute:'2-digit',second:'2-digit'}); }

  // ── Tab switching ─────────────────────────────────────────────────
  window.switchPage = function(name) {
    document.querySelectorAll('.wb-tab').forEach(t => t.classList.toggle('active', t.dataset.page === name));
    document.querySelectorAll('.wb-page').forEach(p => p.classList.toggle('active', p.id === 'page-' + name));
    if (name === 'github') {
      vscode.postMessage({ type: 'gh_status' });
      // force: true → 원격 접근 가능 여부 캐시 무시, 즉시 재확인
      vscode.postMessage({ type: 'git_info_request', force: true });
      vscode.postMessage({ type: 'git_branches_request' });
    }
  };
  document.querySelectorAll('.wb-tab').forEach(tab => {
    tab.addEventListener('click', () => switchPage(tab.dataset.page));
  });

  // ── Log tabs ──────────────────────────────────────────────────────
  document.querySelectorAll('.log-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      _currentLogTab = tab.dataset.log;
      document.querySelectorAll('.log-tab').forEach(t => t.classList.toggle('active', t.dataset.log === _currentLogTab));
      document.querySelectorAll('.log-pane').forEach(p => p.classList.toggle('active', p.id === 'log-' + _currentLogTab));
    });
  });
  on('btn-log-clear', 'click', () => {
    const el = qs('log-' + _currentLogTab);
    if (el) el.innerHTML = '';
  });

  function addLog(pane, text, cls) {
    const el = qs('log-' + pane);
    if (!el) return;
    const line = document.createElement('div');
    line.className = 'log-line' + (cls ? ' ' + cls : '');
    line.textContent = '[' + now() + '] ' + text;
    el.appendChild(line);
    el.scrollTop = el.scrollHeight;
  }

  // ── Activity list ─────────────────────────────────────────────────
  function addActivity(dotClass, text) {
    _activity.unshift({ dot: dotClass, text, time: now() });
    if (_activity.length > 10) _activity.pop();
    renderActivity();
  }
  function renderActivity() {
    const el = qs('activity-list');
    if (!el) return;
    if (!_activity.length) {
      el.innerHTML = '<div class="activity-item"><div class="activity-dot" style="background:var(--t3)"></div><span style="color:var(--t3)">활동 이력 없음</span></div>';
      return;
    }
    el.innerHTML = _activity.map(a =>
      `<div class="activity-item"><div class="activity-dot ${a.dot}"></div><span>${a.text}</span><span class="activity-time">${a.time}</span></div>`
    ).join('');
  }

  // ── Status chips ──────────────────────────────────────────────────
  function setChip(id, state) {
    const el = qs(id);
    if (!el) return;
    const base = el.className.split(' ')[0]; // 'wc' or 'status-chip'
    el.className = base;
    if (state === 'ok') el.classList.add('ok');
    else if (state === 'partial') el.classList.add('warn');
    else if (state === 'fail') el.classList.add('fail');
  }

  function renderReady(data) {
    const map = { core_ready: ['wb-chip-core','sc-core'], ai_ready: ['wb-chip-ai','sc-ai'], docker_ready: ['wb-chip-docker','sc-docker'] };
    for (const [key, ids] of Object.entries(map)) {
      ids.forEach(id => setChip(id, data[key] || 'fail'));
    }
  }

  // ── Command Center cards ──────────────────────────────────────────
  function updateErrorCard(proposal) {
    const descEl = qs('fc-error-desc');
    const metaEl = qs('fc-error-meta');
    const badgeEl = qs('fc-error-badge');
    if (proposal) {
      if (descEl) descEl.textContent = proposal.summary || '이슈 감지됨';
      if (metaEl) metaEl.textContent = proposal.patches?.length + '개 파일 수정 제안';
      if (badgeEl) { badgeEl.textContent = '1'; badgeEl.classList.remove('hidden'); }
    } else {
      if (descEl) descEl.textContent = '감지된 오류 없음';
      if (metaEl) metaEl.textContent = '';
      if (badgeEl) badgeEl.classList.add('hidden');
    }
  }

  function updateGitCard(data) {
    if (!data) return;
    if (data.gh_user) _ghUser = data.gh_user;
    // ★ GitHub 인증된 경우에만 레포 이름 표시 — 미연결/로그아웃 상태에서 stale 레포 숨김
    _ghRepo = '';
    if (_ghUser && data.has_remote && data.remote_url) {
      const m = data.remote_url.match(/([^/]+\/[^/]+?)(?:\.git)?$/);
      if (m) _ghRepo = m[1];
    }
    const descEl = qs('fc-github-desc');
    const metaEl = qs('fc-github-meta');
    if (descEl) descEl.textContent = _ghUser ? (_ghRepo || 'GitHub 연결됨') : '연결 안됨';
    if (metaEl) metaEl.textContent = data.is_git_repo ? (data.branch || '') + (data.uncommitted ? ' · 변경 파일 ' + data.uncommitted + '개' : '') : '';

    // GitHub chip — only set 'ok' here. The authoritative 'fail' state is
    // managed by gh_status_result so a missing gh_user in git_info doesn't
    // clobber a known-good chip.
    if (_ghUser) {
      setChip('wb-chip-github', 'ok');
      setChip('sc-github', 'ok');
    }

    // greeting
    const greetEl = qs('cmd-greeting');
    if (greetEl && _ghUser) greetEl.textContent = '안녕하세요, ' + _ghUser + '님!';
  }

  function updateDeployCard(ds) {
    if (!ds) return;
    const descEl = qs('fc-deploy-desc');
    const metaEl = qs('fc-deploy-meta');
    if (ds.stage === 'done') {
      if (descEl) descEl.textContent = 'Production 환경';
      if (metaEl) metaEl.textContent = ds.health ? '● Healthy' : '⚠ Unhealthy';
    } else if (ds.stage === 'building' || ds.stage === 'running') {
      if (descEl) descEl.textContent = '배포 진행 중...';
    }
  }

  // ── Error Center ──────────────────────────────────────────────────
  function setStageBar(step) {
    const order = ['collect','patch','infra','deploy'];
    const idx = order.indexOf(step);
    order.forEach((s, i) => {
      const el = qs('stage-' + s);
      if (!el) return;
      el.classList.remove('done','active');
      if (i < idx) el.classList.add('done');
      else if (i === idx) el.classList.add('active');
    });
  }

  on('btn-analyze', 'click', () => {
    const txt = (qs('paste-input') || {}).value?.trim();
    if (!txt) { showToast('오류 로그를 입력해주세요'); return; }
    show('analyzing-state');
    hide('patch-card');
    hide('no-error-state');
    setStageBar('collect');
    addLog('ai', '오류 분석 시작...', 'ai');
    vscode.postMessage({ type: 'analyze', error_text: txt, terminal_output: txt });
  });

  function renderPatchProposal(p) {
    _currentPatchProposal = p;
    hide('analyzing-state');
    hide('no-error-state');
    show('patch-card');
    setStageBar('patch');
    updateErrorCard(p);

    const badge = qs('patch-risk-badge');
    if (badge) {
      const cls = p.risk_level === 'low' ? 'badge-ok' : p.risk_level === 'high' ? 'badge-err' : 'badge-warn';
      badge.textContent = p.risk_level; badge.className = 'badge ' + cls;
    }
    const summary = qs('patch-summary');
    if (summary) summary.textContent = p.summary || '';

    const tabsEl = qs('file-tabs-container');
    if (tabsEl && p.patches?.length) {
      tabsEl.innerHTML = p.patches.map((patch, i) =>
        `<div class="file-tab${i===0?' active':''}" data-idx="${i}">
          <input class="file-tab-check" type="checkbox" checked data-stop="1">
          <span>${patch.file || 'file'+i}</span>
        </div>`
      ).join('');
      // 이벤트 위임 — onclick 대신 위임 사용 (CSP 준수)
      tabsEl.querySelectorAll('.file-tab').forEach((tab, i) => {
        tab.addEventListener('click', (e) => {
          if (e.target && e.target.dataset && e.target.dataset.stop) { e.stopPropagation(); return; }
          selectPatchFile(i);
        });
      });
      showDiff(0);
    }
    hide('patch-approved-result');
    addLog('ai', '분석 완료 — ' + (p.patches?.length || 0) + '개 파일 수정 제안', 'ok');
    addActivity('ok', '오류 분석 완료 — ' + (p.summary || ''));
  }

  window.selectPatchFile = function(idx) {
    document.querySelectorAll('.file-tab').forEach((t, i) => t.classList.toggle('active', i === idx));
    showDiff(idx);
  };
  function showDiff(idx) {
    const el = qs('diff-content');
    if (!el || !_currentPatchProposal) return;
    const patch = _currentPatchProposal.patches[idx];
    el.textContent = patch ? (patch.unified_diff || '(diff 없음)') : '';
  }
  function getSelectedPatches() {
    const selected = [];
    document.querySelectorAll('.file-tab').forEach(btn => {
      const cb = btn.querySelector('.file-tab-check');
      if (cb?.checked) selected.push(btn.querySelector('span')?.textContent || '');
    });
    return selected.length ? selected : (_currentPatchProposal?.patches?.map(p => p.file || '') || []);
  }

  on('btn-approve-patch', 'click', () => {
    if (!_currentPatchProposal) return;
    vscode.postMessage({ type: 'approve_patch', proposal_id: _currentPatchProposal.proposal_id, selected_files: getSelectedPatches() });
    hide('patch-card');
    showToast('패치 적용 중...');
    addLog('ai', '패치 적용 요청 — ' + getSelectedPatches().join(', '));
  });
  on('btn-reject-patch', 'click', () => {
    if (!_currentPatchProposal) return;
    vscode.postMessage({ type: 'reject_patch', proposal_id: _currentPatchProposal.proposal_id });
    hide('patch-card');
    showToast('패치 거절됨');
    addActivity('err', '패치 거절');
  });
  on('btn-git-commit', 'click', () => {
    if (!_currentPatchProposal) { showToast('패치 정보가 없습니다'); return; }
    vscode.postMessage({ type: 'git_commit', proposal_id: _currentPatchProposal.proposal_id });
    showToast('Git 커밋 중...');
  });

  // ── Git Panel (GitHub Hub) ─────────────────────────────────────────
  function renderGitInfo(data) {
    if (!data) return;
    const nameEl = qs('git-account-name');
    const acctEl = qs('git-account');
    if (nameEl) nameEl.textContent = data.gh_user || (data.has_remote ? '원격 연결됨' : '연결 안됨');
    if (acctEl) acctEl.classList.toggle('ok', !!(data.gh_user || _ghUser));
    if (!data.gh_user && _ghUser && nameEl) nameEl.textContent = _ghUser;
    const branchEl = qs('git-branch-name');
    if (branchEl) branchEl.textContent = data.is_git_repo ? (data.branch || 'detached') : 'git 없음';
    _gitCurrentBranch = data.branch || '';

    const changed = qs('git-uncommitted-badge');
    if (changed) { changed.textContent = '●' + (data.uncommitted||0); changed.classList.toggle('hidden', !data.uncommitted); }
    const ahead = qs('git-ahead-badge');
    if (ahead) { ahead.textContent = '↑' + (data.ahead||0); ahead.classList.toggle('hidden', !data.ahead); }

    updateGitCard(data);
  }

  function renderGitBranches(data) {
    if (!data) return;
    const localEl = qs('git-local-branches');
    const remoteEl = qs('git-remote-branches');
    const current = data.current || '';
    if (localEl) {
      localEl.innerHTML = data.branches?.length
        ? data.branches.map(b => `<div class="gd-branch${b===current?' current':''}" data-branch="${b}"><div class="gd-dot${b===current?' current':''}"></div><span>${b}</span>${b===current?'<span style="margin-left:auto;font-size:9px;color:var(--green)">현재</span>':''}</div>`).join('')
        : '<div class="gd-branch" style="color:var(--t3)">브랜치 없음</div>';
      localEl.querySelectorAll('.gd-branch[data-branch]').forEach(el => {
        el.addEventListener('click', () => gitCheckout(el.dataset.branch || ''));
      });
    }
    if (remoteEl) {
      remoteEl.innerHTML = data.remote_branches?.length
        ? data.remote_branches.map(b => { const s=b.replace(/^origin\//,''); return `<div class="gd-branch" data-branch="${s}"><div class="gd-dot" style="background:var(--t3)"></div><span>${s}</span><span class="gd-remote-tag">origin</span></div>`; }).join('')
        : '<div class="gd-branch" style="color:var(--t3)">원격 없음</div>';
      remoteEl.querySelectorAll('.gd-branch[data-branch]').forEach(el => {
        el.addEventListener('click', () => gitCheckout(el.dataset.branch || ''));
      });
    }
    renderBranches(data);
  }

  function renderBranches(data) {
    const listEl = qs('gh-branch-list');
    if (!listEl) return;
    const all = [...new Set([...(data.branches||[]), ...(data.remote_branches||[]).map(b=>b.replace(/^origin\//,''))])];
    listEl.innerHTML = all.length
      ? all.map(b => `<div style="padding:4px 10px;cursor:pointer;color:${b===(data.current||'')?'var(--green)':'var(--t2)'};font-weight:${b===(data.current||'')?'600':'400'}">${b===(data.current||'')?'● ':'○ '}${b}</div>`).join('')
      : '<div style="padding:4px 10px;color:var(--t3)">브랜치 없음</div>';
  }

  window.gitCheckout = function(branch) {
    vscode.postMessage({ type: 'git_checkout', branch });
    showToast('브랜치 전환: ' + branch);
    _gitBranchDropdownOpen = false;
    qs('git-dropdown')?.classList.remove('open');
  };

  on('git-branch-btn', 'click', () => {
    _gitBranchDropdownOpen = !_gitBranchDropdownOpen;
    qs('git-dropdown')?.classList.toggle('open', _gitBranchDropdownOpen);
    if (_gitBranchDropdownOpen) vscode.postMessage({ type: 'git_branches_request' });
  });
  on('git-btn-branch-create', 'click', () => {
    const name = qs('git-new-branch-input')?.value.trim();
    if (!name) { showToast('브랜치 이름을 입력하세요'); return; }
    vscode.postMessage({ type: 'git_branch_create', branch_name: name });
    showToast('브랜치 생성: ' + name);
    if (qs('git-new-branch-input')) qs('git-new-branch-input').value = '';
  });
  on('git-btn-commit', 'click', () => {
    _gitCommitPanelOpen = !_gitCommitPanelOpen;
    qs('git-commit-panel')?.classList.toggle('open', _gitCommitPanelOpen);
  });
  on('git-btn-push', 'click', () => {
    vscode.postMessage({ type: 'git_push', branch: _gitCurrentBranch });
    showToast('Push 중...');
    addLog('ai', 'git push ' + _gitCurrentBranch);
  });
  on('git-btn-do-commit', 'click', () => {
    const msg = qs('git-commit-msg')?.value.trim();
    if (!msg) { showToast('커밋 메시지를 입력하세요'); return; }
    vscode.postMessage({ type: 'git_commit_and_push', message: msg, push: false });
    addLog('ai', 'git commit -m "' + msg + '"');
    showToast('커밋 중...');
  });
  on('git-btn-commit-push', 'click', () => {
    const msg = qs('git-commit-msg')?.value.trim();
    if (!msg) { showToast('커밋 메시지를 입력하세요'); return; }
    vscode.postMessage({ type: 'git_commit_and_push', message: msg, push: true });
    addLog('ai', 'git commit -m "' + msg + '" && git push');
    showToast('커밋 + Push 중...');
  });

  // ── GitHub Ship ───────────────────────────────────────────────────
  function renderGhStatus(gs) {
    hide('gh-loading-card');
    // VS Code OAuth 방식: installed 여부 무관, authed 상태만 판단
    if (!gs.authed) {
      _ghUser = '';
      hide('gh-ship-card');
      show('gh-login-card');
      const badge = qs('gh-login-badge');
      const prog = qs('gh-login-progress');
      const btn = qs('btn-gh-login');
      // 2026-05-11 (P0 후속): VS Code 에 GitHub 계정은 있는데 ReCoder 가 요구하는
      // 스코프로 발급된 토큰이 없는 경우, "감지됨 — 권한 부여 필요" 로 명시 표시.
      if (gs.vscode_detected && gs.vscode_user) {
        if (badge) badge.textContent = '권한 필요';
        if (prog) prog.textContent = 'VS Code 계정 감지됨: ' + gs.vscode_user + ' — 연결 버튼을 눌러 권한을 부여하세요';
        if (btn) { btn.disabled = false; btn.textContent = '권한 부여하고 연결 (' + gs.vscode_user + ')'; }
        setChip('wb-chip-github', 'partial'); setChip('sc-github', 'partial');
      } else {
        if (badge) badge.textContent = '미연결';
        if (prog) prog.textContent = '';
        if (btn) { btn.disabled = false; btn.textContent = 'GitHub 연결'; }
        setChip('wb-chip-github', 'fail'); setChip('sc-github', 'fail');
      }
    } else {
      hide('gh-login-card');
      show('gh-ship-card');
      const badge = qs('gh-user-badge'); if (badge) badge.textContent = gs.user || '연결됨';
      const nameEl = qs('gh-account-name'); if (nameEl) nameEl.textContent = gs.user || '—';
      setChip('wb-chip-github', 'ok');
      setChip('sc-github', 'ok');
      if (gs.user) _ghUser = gs.user;
      const greetEl = qs('cmd-greeting');
      if (greetEl && gs.user) greetEl.textContent = '안녕하세요, ' + gs.user + '님!';
      vscode.postMessage({ type: 'git_branches_request' });
      vscode.postMessage({ type: 'gh_repos_request' });
    }
  }

  function renderGhRepos(data) {
    const listEl = qs('gh-repo-list');
    if (!listEl) return;
    const repos = data?.repos || [];
    if (!repos.length) {
      listEl.innerHTML = '<div style="padding:4px 10px;color:var(--t3)">레포지토리 없음</div>';
      return;
    }
    listEl.innerHTML = repos.map(r => {
      const lock = r.private ? '🔒 ' : '🌐 ';
      return `<div class="repo-item" data-full="${r.full_name}" data-name="${r.name}" style="padding:4px 10px;cursor:pointer;color:var(--t2);font-size:11px;display:flex;align-items:center;gap:4px;transition:background .1s" title="${r.description || r.full_name}">${lock}<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.name}</span><span style="font-size:9px;color:var(--t3);margin-left:4px">${r.full_name.split('/')[0]}</span></div>`;
    }).join('');
    // 이벤트 위임 (CSP: inline onclick 금지)
    listEl.querySelectorAll('.repo-item').forEach(el => {
      el.addEventListener('mouseover', () => { el.style.background = 'var(--bg2)'; });
      el.addEventListener('mouseout',  () => { el.style.background = ''; });
      el.addEventListener('click', () => confirmRepoChange(el.dataset.full || '', el.dataset.name || ''));
    });
  }

  // ── 레포 확인 모달 ───────────────────────────────────────────────
  let _pendingRepoFullName = '';

  window.confirmRepoChange = function(fullName, displayName) {
    _pendingRepoFullName = fullName;
    const overlay = qs('repo-confirm-overlay');
    const msgEl   = qs('repo-confirm-msg');
    if (msgEl) msgEl.textContent =
      '"' + displayName + '"으로 원격 저장소(origin)를 변경하시겠습니까?\n\nhttps://github.com/' + fullName;
    if (overlay) { overlay.style.display = 'flex'; }
  };

  on('btn-repo-confirm-cancel', 'click', () => {
    const overlay = qs('repo-confirm-overlay');
    if (overlay) overlay.style.display = 'none';
    _pendingRepoFullName = '';
  });

  on('btn-repo-confirm-ok', 'click', () => {
    const overlay = qs('repo-confirm-overlay');
    if (overlay) overlay.style.display = 'none';
    if (!_pendingRepoFullName) return;
    const fullName = _pendingRepoFullName;
    _pendingRepoFullName = '';
    // 입력창에도 레포 이름 채워주기 (배포용)
    const inp = qs('gh-repo-name');
    if (inp) inp.value = fullName.split('/')[1] || fullName;
    // 원격 저장소 변경 요청
    vscode.postMessage({ type: 'git_set_remote', repo_full_name: fullName });
    showToast('원격 저장소 변경 중...');
    addLog('gha', '원격 저장소 변경: ' + fullName, 'ai');
  });

  window.selectRepo = function(name) {
    const inp = qs('gh-repo-name');
    if (inp) { inp.value = name; inp.focus(); }
    showToast('선택: ' + name);
  };

  // VS Code OAuth 버튼: 클릭 → sidebarProvider._handleGhLogin() 트리거
  on('btn-gh-login', 'click', () => {
    const btn = qs('btn-gh-login'); if (btn) btn.disabled = true;
    const prog = qs('gh-login-progress'); if (prog) prog.textContent = 'GitHub 인증 창을 엽니다...';
    vscode.postMessage({ type:'gh_login' });
  });
  on('btn-gh-logout', 'click', () => {
    vscode.postMessage({ type:'gh_logout' });
    hide('gh-ship-card'); hide('gh-ship-progress'); show('gh-login-card');
    // 로그아웃 즉시 레포/브랜치 표시 초기화
    _ghUser = ''; _ghRepo = '';
    const descEl = qs('fc-github-desc'); if (descEl) descEl.textContent = '연결 안됨';
    const nameEl = qs('git-account-name'); if (nameEl) nameEl.textContent = '연결 안됨';
    const acctEl = qs('git-account'); if (acctEl) acctEl.classList.remove('ok');
    setChip('wb-chip-github', 'fail'); setChip('sc-github', 'fail');
    showToast('로그아웃 중...');
  });
  on('btn-project-scan', 'click', () => {
    vscode.postMessage({ type:'scan_project' });
    const statusEl = qs('gh-project-status');
    if (statusEl) statusEl.textContent = '프로젝트 스캔 중...';
    showToast('프로젝트 스캔 중...');
    addLog('gha', '프로젝트 스캔 시작', 'ai');
  });
  on('btn-gh-refresh-branches', 'click', () => vscode.postMessage({ type:'git_branches_request' }));
  on('btn-gh-refresh-repos', 'click', () => vscode.postMessage({ type:'gh_repos_request' }));
  // 배포 완료/실패 후 → 계정 설정 화면으로 돌아가기
  on('btn-ship-back', 'click', () => {
    hide('gh-ship-progress'); show('gh-ship-card');
    const backBtn = qs('btn-ship-back'); if (backBtn) backBtn.style.display = 'none';
    const spinner = qs('gh-ship-spinner'); if (spinner) spinner.style.display = '';
    const titleTxt = qs('gh-ship-title-text'); if (titleTxt) { titleTxt.textContent = 'GitHub 배포 진행 중'; titleTxt.style.color = ''; }
    const current = qs('gh-ship-current'); if (current) current.textContent = 'init';
    hide('gh-ship-result');
  });
  on('btn-ship-go', 'click', () => {
    const repoName = qs('gh-repo-name')?.value.trim();
    if (!repoName) { showToast('repo 이름을 입력하세요'); return; }
    const isPrivate = qs('gh-private')?.checked ?? true;
    const includeInfra = qs('gh-include-infra')?.checked ?? false;
    hide('gh-ship-card'); show('gh-ship-progress');
    vscode.postMessage({ type:'ship_github', repo_name:repoName, private:isPrivate, include_dockerfile:includeInfra, include_compose:includeInfra, include_actions:includeInfra, include_dockerignore:includeInfra });
    addLog('gha', 'GitHub ship 시작: ' + repoName, 'ai');
    addActivity('blue', 'GitHub ship 시작: ' + repoName);
    showToast('GitHub 배포 시작 중...');
  });

  function renderShipProgress(data) {
    show('gh-ship-progress'); hide('gh-ship-card');
    const stepsEl = qs('gh-ship-steps');
    const currentEl = qs('gh-ship-current');
    if (currentEl) currentEl.textContent = data.current || '';

    // 스텝 아이콘 — skipped·warn·failed 포함
    if (stepsEl && data.steps) {
      stepsEl.innerHTML = data.steps.map(s => {
        let icon;
        if (s.status === 'done')    icon = '✅';
        else if (s.status === 'skipped') icon = '⏭';
        else if (s.status === 'warn')    icon = '⚠️';
        else if (s.status === 'error' || s.status === 'failed') icon = '❌';
        else                             icon = '⏳';
        return `<div class="ship-step"><span style="width:18px;text-align:center">${icon}</span><span style="flex:1">${s.label}</span><span style="font-size:10px;color:var(--t3)">${s.message||''}</span></div>`;
      }).join('');
    }

    // 완료 시 헤더 스피너 제거 + 타이틀 텍스트 변경 + 돌아가기 버튼 표시
    if (!data.running) {
      const spinner  = qs('gh-ship-spinner');
      const titleTxt = qs('gh-ship-title-text');
      const current  = qs('gh-ship-current');
      const backBtn  = qs('btn-ship-back');
      if (spinner)  spinner.style.display = 'none';
      if (current)  current.textContent = '';
      if (backBtn)  backBtn.style.display = '';   // 돌아가기 버튼 표시
      if (titleTxt) {
        if (data.error) {
          titleTxt.textContent = 'GitHub 배포 실패';
          titleTxt.style.color = 'var(--red)';
        } else {
          titleTxt.textContent = 'GitHub 배포 완료 ✅';
          titleTxt.style.color = 'var(--green)';
        }
      }
    }

    const resultEl = qs('gh-ship-result');
    if (data.repo_url && !data.running) {
      show('gh-ship-result');
      if (resultEl) { resultEl.style.background='var(--green-bg)'; resultEl.style.border='1px solid rgba(63,185,80,.3)'; resultEl.innerHTML = `✅ 완료! <a href="${data.repo_url}" style="color:var(--blue)">${data.repo_url}</a>`; }
      addLog('gha', 'Ship 완료: ' + data.repo_url, 'ok');
      addActivity('ok', 'GitHub 배포 완료: ' + data.repo_url);
    } else if (data.error && !data.running) {
      show('gh-ship-result');
      if (resultEl) { resultEl.style.background='var(--red-bg)'; resultEl.style.border='1px solid rgba(248,81,73,.3)'; resultEl.textContent='❌ 오류: '+data.error; }
      addLog('gha', 'Ship 오류: ' + data.error, 'err');
    }
  }

  // ── Deploy Center ─────────────────────────────────────────────────
  window.generateInfra = function(fileType) {
    hide('dockerfile-card'); showToast(fileType + ' 생성 중...');
    vscode.postMessage({ type:'generate_dockerfile', file_type: fileType });
    addLog('docker', fileType + ' 생성 요청', 'ai');
  };

  on('btn-gen-dockerfile', 'click', () => generateInfra('Dockerfile'));
  on('btn-gen-compose', 'click', () => generateInfra('docker-compose'));
  on('btn-gen-gha', 'click', () => generateInfra('github-actions'));
  on('btn-approve-infra', 'click', () => {
    if (!_currentInfraProposal) return;
    vscode.postMessage({ type:'approve_infra', proposal_id:_currentInfraProposal.proposal_id });
    hide('dockerfile-card'); showToast('인프라 파일 저장 중...');
    addLog('docker', '인프라 파일 저장: ' + _currentInfraProposal.target_path);
  });
  on('btn-security-scan', 'click', () => {
    vscode.postMessage({ type:'run_security_scan' });
    showToast('보안 스캔 중...');
    addLog('deploy', '보안 스캔 시작', 'ai');
  });
  on('btn-deploy-local', 'click', () => {
    if (!_currentDeployPlan) return;
    hide('deploy-section'); hide('deploy-idle-card'); show('deploy-progress-card');
    setDeployProgress('building', 0);
    vscode.postMessage({ type:'deploy_local', plan_id:_currentDeployPlan.plan_id });
    if (_deployPollTimer) clearInterval(_deployPollTimer);
    _deployPollTimer = setInterval(() => vscode.postMessage({ type:'deploy_status_poll' }), 1500);
    addLog('deploy', 'Docker 빌드 시작: ' + (_currentDeployPlan.image||''), 'ai');
    addActivity('blue', 'Docker 배포 시작');
  });
  on('btn-rollback', 'click', () => {
    if (!_currentDeployPlan) return;
    vscode.postMessage({ type:'deploy_rollback', plan_id:_currentDeployPlan.plan_id });
    showToast('롤백 중...');
    addLog('deploy', '롤백 시작', 'warn');
  });

  function setDeployProgress(stage, pct) {
    const bar = qs('deploy-progress-bar'); if (bar) bar.style.width = pct + '%';
    const stageEl = qs('deploy-stage'); if (stageEl) stageEl.textContent = stage;
    const stageMap = { building:'dstep-build', running:'dstep-run', health:'dstep-health' };
    document.querySelectorAll('.deploy-step').forEach(el => el.classList.remove('done','running'));
    const order = ['building','running','health'];
    const idx = order.indexOf(stage);
    order.forEach((s, i) => {
      const el = qs(stageMap[s]); if (!el) return;
      if (i < idx) el.classList.add('done');
      else if (i === idx) el.classList.add('running');
    });
  }

  function renderDeployStatus(ds) {
    if (!ds) return;
    const prog = { idle:0, building:25, running:60, health:80, done:100, failed:100 };
    setDeployProgress(ds.stage, prog[ds.stage]||0);
    const logEl = qs('deploy-log-tail');
    if (logEl && ds.log_tail) {
      logEl.textContent = ds.log_tail.join('\n');
      ds.log_tail.forEach(l => addLog('deploy', l));
    }
    if (ds.finished || ds.stage === 'done' || ds.stage === 'failed') {
      if (_deployPollTimer) { clearInterval(_deployPollTimer); _deployPollTimer = null; }
      if (ds.stage === 'done') {
        setTimeout(() => {
          hide('deploy-progress-card'); show('health-card');
          const hEl = qs('health-result');
          if (hEl) hEl.innerHTML = ds.health ? '✅ Health Check 통과' : '⚠ Health Check 실패';
          addLog('health', ds.health ? 'Health Check 통과' : 'Health Check 실패', ds.health ? 'ok' : 'err');
          addActivity(ds.health ? 'ok' : 'err', '배포 ' + (ds.health ? '성공' : '실패'));
          updateDeployCard(ds);
        }, 800);
      }
    }
  }

  // ── Feature cards + Quick actions: document-level 이벤트 위임 ─────
  // ID 기반 on() 대신 document.addEventListener 사용 → 타이밍 무관하게 동작
  document.addEventListener('click', function(e) {
    const t = e.target;
    // Feature cards — 카드 자체 또는 내부 버튼 클릭
    const card = t.closest && t.closest('.feature-card');
    if (card) {
      if (card.id === 'fc-card-error'  || t.closest('#fc-btn-error'))  { e.stopPropagation(); switchPage('error');  return; }
      if (card.id === 'fc-card-github' || t.closest('#fc-btn-github')) { e.stopPropagation(); switchPage('github'); return; }
      if (card.id === 'fc-card-deploy' || t.closest('#fc-btn-deploy')) { e.stopPropagation(); switchPage('deploy'); return; }
    }
    // Quick action buttons
    const bid = t.id || (t.closest && t.closest('button') && t.closest('button').id);
    if (bid === 'cmd-qbtn-error')      { switchPage('error'); return; }
    if (bid === 'cmd-qbtn-dockerfile') { generateInfra('Dockerfile'); return; }
    if (bid === 'cmd-qbtn-gha')        { generateInfra('github-actions'); return; }
    if (bid === 'cmd-qbtn-dashboard')  { vscode.postMessage({ type: 'open_dashboard' }); return; }
  });
  on('cmd-auto-detect', 'change', function() {
    vscode.postMessage({ type:'toggle_auto_detect', enabled:this.checked });
    showToast(this.checked ? '자동 감지 켜짐' : '자동 감지 꺼짐');
  });
  on('cmd-health-btn', 'click', () => { showToast('헬스 체크 요청 중...'); addLog('health', '헬스 체크 요청', 'ai'); });
  on('cmd-log-btn', 'click', () => { switchPage('deploy'); });

  // ── Message receiver ──────────────────────────────────────────────
  window.addEventListener('message', event => {
    const msg = event.data;
    if (!msg?.type) return;

    switch (msg.type) {
      case 'navigate_to_page':
        if (msg.page) switchPage(msg.page);
        break;

      case 'ready_state':
      case 'ready_update':
        renderReady(msg.data || msg.ready || {});
        break;

      case 'analyze_result':
        hide('analyzing-state');
        if (msg.data?.patches?.length > 0) {
          renderPatchProposal(msg.data);
          switchPage('error');
        } else {
          show('no-error-state');
          updateErrorCard(null);
          addLog('ai', '분석 완료 — 이슈 없음', 'ok');
        }
        break;

      case 'auto_detected':
        showToast('터미널 오류 감지됨! Error Center 확인');
        addLog('ai', '터미널 오류 자동 감지', 'warn');
        addActivity('err', '터미널 오류 자동 감지');
        switchPage('error');
        break;

      case 'patch_approved': {
        const r = msg.data;
        const resultEl = qs('patch-approved-result');
        if (resultEl) {
          resultEl.textContent = r?.error ? '오류: '+r.error : '✅ 적용됨: '+(r?.applied_files||[]).join(', ');
          resultEl.classList.remove('hidden');
        }
        show('patch-card');
        addLog('ai', '패치 적용: ' + (r?.applied_files||[]).join(', '), 'ok');
        addActivity('ok', '코드 패치 적용 완료');
        break;
      }

      case 'infra_generated':
        _currentInfraProposal = msg.data;
        show('dockerfile-card');
        const targetEl = qs('infra-target'); if (targetEl) targetEl.textContent = msg.data?.target_path || 'Dockerfile';
        const contentEl = qs('dockerfile-content'); if (contentEl) contentEl.textContent = msg.data?.content || '';
        setStageBar('infra');
        hide('deploy-idle-card');
        addLog('docker', 'Dockerfile 생성 완료: ' + (msg.data?.target_path||''), 'ok');
        addActivity('blue', 'Dockerfile 생성 완료');
        break;

      case 'infra_approved': {
        const d = msg.data || {};
        if (d.plan) {
          _currentDeployPlan = d.plan;
          const previewEl = qs('deploy-command-preview');
          const ports = d.plan.ports || [{host:8080,container:8080}];
          if (previewEl) previewEl.textContent = `docker run -p ${ports[0]?.host}:${ports[0]?.container} ${d.plan.image||''}`;
          show('deploy-section'); hide('deploy-idle-card'); setStageBar('deploy');
        }
        hide('dockerfile-card');
        showToast('인프라 파일 저장 완료');
        addLog('docker', '인프라 파일 저장 완료', 'ok');
        break;
      }

      case 'deploy_status':
        renderDeployStatus(msg.data);
        break;

      case 'security_scan_result': {
        const scanEl = qs('scan-result');
        if (!scanEl) break;
        const ok = msg.data?.passed;
        scanEl.innerHTML = `<div style="margin-top:6px;padding:6px 10px;border-radius:var(--radius-sm);font-size:11px;background:${ok?'var(--green-bg)':'var(--red-bg)'};border:1px solid ${ok?'rgba(63,185,80,.3)':'rgba(248,81,73,.3)'}">${ok?'✅ 보안 스캔 통과':'⚠ 보안 이슈 발견'}</div>`;
        addLog('deploy', '보안 스캔: ' + (ok?'통과':'이슈 발견'), ok?'ok':'err');
        break;
      }

      case 'project_scan_started': {
        const statusEl = qs('gh-project-status');
        if (statusEl) statusEl.textContent = '프로젝트 스캔 중...';
        addLog('gha', '프로젝트 스캔 시작', 'ai');
        break;
      }

      case 'project_scanned': {
        const d = msg.data || {};
        const statusEl = qs('gh-project-status');
        const label = d.stack || d.package_manager || '등록됨';
        if (statusEl) statusEl.textContent = '프로젝트 등록됨: ' + label;
        showToast('프로젝트 스캔 완료');
        addLog('gha', '프로젝트 스캔 완료: ' + label, 'ok');
        addActivity('ok', '프로젝트 등록 완료');
        break;
      }

      case 'git_set_remote_result': {
        const d = msg.data || {};
        if (d.status === 'ok') {
          showToast('원격 저장소 변경 완료 ✅');
          addLog('gha', '원격 저장소 변경: ' + d.remote_url, 'ok');
          addActivity('ok', '원격 저장소 변경: ' + d.remote_url);
          // GitHub 탭 정보 갱신
          vscode.postMessage({ type: 'git_info_request', force: true });
          vscode.postMessage({ type: 'git_branches_request' });
        } else {
          showToast('원격 저장소 변경 실패: ' + (d.message || ''), 4000);
          addLog('gha', '원격 저장소 변경 실패: ' + (d.message || ''), 'err');
        }
        break;
      }

      case 'gh_status_result':
        renderGhStatus(msg.data || {});
        break;

      case 'gh_login_progress': {
        // VS Code OAuth 진행 상태 표시
        const prog = qs('gh-login-progress');
        if (prog) prog.textContent = msg.message || '';
        if (!msg.message) {
          const btn = qs('btn-gh-login'); if (btn) btn.disabled = false;
        }
        break;
      }

      case 'error': {
        // 2026-05-11: error 핸들러 누락으로 GitHub 연결 실패 시 사용자에게 피드백이 없던
        // 문제를 수정 (P0). 토스트 + 로그 + 진행 표시 클리어 + 관련 버튼 재활성화.
        const errMsg = msg.message || '오류 발생';
        showToast(errMsg, 4000);
        addLog('ai', errMsg, 'err');
        addActivity('err', errMsg);
        if (errMsg.includes('Ship 실행 실패') || errMsg.includes('프로젝트 스캔 실패')) {
          hide('gh-ship-progress');
          show('gh-ship-card');
        }
        const prog = qs('gh-login-progress'); if (prog) prog.textContent = '';
        const btn = qs('btn-gh-login'); if (btn) btn.disabled = false;
        break;
      }

      case 'ship_github_status':
        renderShipProgress(msg.data || {});
        break;

      case 'git_info_result':
        renderGitInfo(msg.data);
        break;

      case 'git_branches_result':
        renderGitBranches(msg.data);
        break;

      case 'gh_repos_result':
        renderGhRepos(msg.data);
        break;

      case 'git_checkout_result':
      case 'git_branch_create_result':
      case 'git_push_result': {
        const d = msg.data || {};
        const st = qs('git-commit-status'); if (st) st.textContent = d.message||'';
        showToast(d.message || '완료');
        if (msg.type === 'git_push_result') addActivity('ok', 'Push 완료: ' + _gitCurrentBranch);
        vscode.postMessage({ type:'git_info_request' });
        vscode.postMessage({ type:'git_branches_request' });
        break;
      }

      case 'git_commit_result': {
        const d = msg.data || {};
        const st = qs('git-commit-status');
        if (d.status === 'ok' || d.status === 'committed') {
          const sha = d.commit_hash ? ' (' + d.commit_hash.slice(0,7) + ')' : '';
          if (st) st.textContent = '✅ 커밋 완료' + sha;
          showToast('커밋 완료' + sha);
          if (qs('git-commit-msg')) qs('git-commit-msg').value = '';
          addActivity('ok', '커밋 완료' + sha);
          addLog('ai', 'git commit 완료' + sha, 'ok');
        } else {
          if (st) st.textContent = '❌ ' + (d.message||'커밋 실패');
          showToast('커밋 실패');
        }
        vscode.postMessage({ type:'git_info_request' });
        break;
      }

      case 'cost_update': {
        const d = msg.data || {};
        const costVal = qs('wb-cost-val'); if (costVal) costVal.textContent = '$' + (d.daily||0).toFixed(4);
        const costBig = qs('cmd-cost-big'); if (costBig) costBig.textContent = '$' + (d.daily||0).toFixed(2);
        break;
      }
    }
  });

  // ── Init ─────────────────────────────────────────────────────────
  vscode.postMessage({ type: 'ready' });
  // gh_status 는 sidebarProvider 가 서버 사이드에서 관리.
  // 여기서 추가로 gh_status 를 보내면 _autoDetectGhSession 이 동시에 여러 번 실행되어
  // "Connect to GitHub" 팝업이 중복 표시되는 원인이 됨 → 제거.
  vscode.postMessage({ type: 'git_info_request' });

  // git info 만 주기적으로 갱신 (브랜치, 변경사항 등)
  setInterval(() => {
    vscode.postMessage({ type: 'git_info_request' });
  }, 15000);

})();
