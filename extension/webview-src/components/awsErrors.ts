/**
 * AWS 오류 원문을 사람이 읽는 한 줄로 — 실사용(2026-11-20) Operate 화면에 IAM 영문 원문이 두 번씩 그대로 나왔다.
 * 권한 오류면 "어떤 권한이 필요한지"만 말한다. 계정 번호·ARN 은 보여 주지 않는다. 모르는 오류는 원문을 그대로 둔다.
 */
export function awsErrorText(raw: string): string {
  const text = String(raw || "").trim();
  const denied = /not authorized to perform:?\s*([a-z0-9-]+:[A-Za-z0-9*]+)/i.exec(text);
  if (denied) {
    const who = /:(user|role|assumed-role)\/([^\s/]+)/.exec(text)?.[2];
    return `AWS 권한이 없어요 — ${who ? `${who} 에 ` : ""}${denied[1]} 권한을 추가하세요`;
  }
  if (/ExpiredToken|security token included in the request is expired/i.test(text)) return "AWS 로그인이 만료됐어요 — AWS 연결을 다시 하세요";
  if (/InvalidClientTokenId|UnrecognizedClient|SignatureDoesNotMatch/i.test(text)) return "AWS 키가 맞지 않아요 — AWS 연결에서 키를 다시 확인하세요";
  if (/Could not connect to the endpoint|EndpointConnectionError|getaddrinfo|ENOTFOUND/i.test(text)) return "AWS 에 연결하지 못했어요 — 인터넷 연결을 확인하세요";
  if (/ResourceNotFoundException|ServiceNotFoundException|ClusterNotFoundException/i.test(text)) return "AWS 에 해당 리소스가 없어요 — 지워졌거나 다른 리전일 수 있어요";
  return text;
}

/** "앞말: 원문" 형태의 안내를 앞말은 두고 원문만 바꾼다. 같은 문장은 한 번만. */
export function awsNotes(lines: string[], hide: (line: string) => boolean = () => false): string[] {
  const out: string[] = [];
  for (const line of lines) {
    if (hide(line)) continue;
    const m = /^([^:]{2,40}?):\s+(.+)$/s.exec(line);
    const text = m ? `${m[1].replace(/습니다$/, "어요")} · ${awsErrorText(m[2])}` : awsErrorText(line);
    if (!out.includes(text)) out.push(text);
  }
  return out;
}
