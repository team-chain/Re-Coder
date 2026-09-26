/**
 * 새 GitHub 저장소 이름 미리보기 — 확장 호스트의 githubRepoName 과 같은 규칙.
 * 공백·한글 등은 GitHub 웹과 같이 '-' 로 바꾼다. 실제 검증은 호스트가 다시 한다.
 */
export function previewRepoName(raw: string): { name: string; error: string } {
  const last = raw.trim().replace(/\/+$/, '').split('/').pop() || '';
  const name = last.replace(/\.git$/i, '').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/-{2,}/g, '-').replace(/^-+|-+$/g, '').slice(0, 100);
  if (!raw.trim()) return { name: '', error: '' };
  if (!name || name === '.' || name === '..') return { name: '', error: '저장소 이름은 영문·숫자로 입력하세요. 한글·기호만으로는 만들 수 없습니다 (예: lunch-vote).' };
  return { name, error: '' };
}

/** 프로젝트 폴더 이름으로 만든 기본 저장소 이름. 쓸 수 없는 이름이면 ''. */
export function defaultRepoName(workspace: string): string {
  const folder = workspace.split(/[\\/]/).filter(Boolean).pop() || '';
  return previewRepoName(folder).name;
}
