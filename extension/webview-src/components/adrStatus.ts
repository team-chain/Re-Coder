/** Read the decision recorded in the ADR; a missing reviewer is not a rejection. */
export function adrStatus(content: string): { label: string; accepted: boolean } {
  const text = content.replace(/\*\*/g, '');
  const status = /^\s*(?:[-*|]\s*)?(?:상태|status)\s*[:：|]\s*([^\n|]+)/im.exec(text)?.[1].trim().toLowerCase();
  if (status && /^(승인됨|승인|확정|accepted|approved)(?:\s|$)/.test(status)) return { label: '설계 확정', accepted: true };
  if (status && /^(폐기|거부|rejected|deprecated|superseded)/.test(status)) return { label: status, accepted: false };
  const reviewer = /검토자\s*[:：]\s*([^\n]+)/.exec(text)?.[1].trim();
  if (reviewer && !/^(\(|미검토|tbd|-)/i.test(reviewer)) return { label: '검토 완료', accepted: true };
  if (!content) return { label: '불러오는 중', accepted: false };
  return { label: status || '검토 대기', accepted: false };
}
