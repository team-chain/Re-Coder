/**
 * 공통 부품(2.0.46) — 설정 시트 · 값 상자 · 설정 줄 · 나눔 버튼 · 접기 · 칩 · 기본 버튼.
 *
 *  · 색은 모두 VS Code 테마 색에서 온다(밝은 테마에서도 맞게). 면은 글자색을 옅게 섞어 만든다.
 *  · 모서리는 10~14px, 테두리 대신 밝기 차이와 얇은 선으로 나눈다.
 *  · 스타일은 문서에 한 번만 넣는다. 서버 렌더(테스트)에서는 <style> 로 함께 낸다.
 */
import React, { useState } from "react";
import { Icon, IconName } from "./icons";

const STYLE_ID = "rc-kit-styles";
export const KIT_CSS = `
.rk{--rk-fg:var(--vscode-foreground,#e9ebef);--rk-fg2:var(--vscode-descriptionForeground,#9aa1ad);--rk-fg3:color-mix(in srgb,var(--rk-fg2) 70%,transparent);
  --rk-surface:color-mix(in srgb,var(--rk-fg) 5%,transparent);--rk-surface2:color-mix(in srgb,var(--rk-fg) 9%,transparent);--rk-hair:color-mix(in srgb,var(--rk-fg) 8%,transparent);
  --rk-accent:var(--vscode-button-background,#3d8bff);--rk-accent-fg:var(--vscode-button-foreground,#fff);--rk-accent-soft:color-mix(in srgb,var(--rk-accent) 15%,transparent);
  --rk-ok:var(--vscode-testing-iconPassed,#3fb950);--rk-warn:var(--vscode-editorWarning-foreground,#cca700);--rk-bad:var(--vscode-errorForeground,#f48771);
  color:var(--rk-fg);font-size:13px;line-height:1.5}
.rk *{box-sizing:border-box}
.rk .rk-head{display:flex;align-items:center;gap:12px;margin:0 0 18px}
.rk .rk-head .ic{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;flex:none;overflow:hidden;background:var(--rk-surface2);color:var(--rk-fg)}
.rk .rk-head .ic svg{width:22px;height:22px}.rk .rk-head .ic svg.rc-logo-aws{width:38px;height:38px;border-radius:0;box-shadow:none}
.rk .rk-head h3{margin:0!important;font-size:15.5px;font-weight:600;line-height:1.3}
.rk .rk-head p{margin:2px 0 0!important;color:var(--rk-fg2);font-size:12.5px}
.rk .rk-sec{margin:20px 0 0}.rk .rk-head+.rk-sec{margin-top:0}
.rk .rk-sec>.t{font-size:12px;font-weight:500;color:var(--rk-fg2);margin:0 2px 8px;display:flex;justify-content:space-between;gap:8px}
.rk .rk-box{background:var(--rk-surface);border-radius:12px;padding:2px 14px}
.rk .rk-kv{display:flex;align-items:center;gap:10px;min-height:44px;padding:4px 0}
.rk .rk-kv+.rk-kv{border-top:1px solid var(--rk-hair)}
.rk .rk-kv .k{width:58px;flex:none;color:var(--rk-fg2)}
.rk .rk-kv .v{flex:1;min-width:0;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rk .rk-kv .v small{font-weight:400;color:var(--rk-fg3);margin-left:6px}
.rk .rk-kv .v input,.rk .rk-kv .v select{width:100%;height:30px;border-radius:7px;border:1px solid var(--vscode-focusBorder,#3d8bff);background:var(--vscode-input-background,#1e1e1e);color:var(--vscode-input-foreground,var(--rk-fg));padding:0 8px;font:inherit;font-weight:400}
.rk .rk-link{all:unset;cursor:pointer;color:var(--vscode-textLink-foreground,#3d8bff);font-size:12.5px;white-space:nowrap}
.rk .rk-link:hover{text-decoration:underline}.rk .rk-link:focus-visible{outline:2px solid var(--vscode-focusBorder,#3d8bff);outline-offset:2px;border-radius:3px}
.rk .rk-link[aria-disabled=true]{opacity:.5;cursor:default;text-decoration:none}
.rk .rk-note{color:var(--rk-fg3);font-size:12px;white-space:nowrap}
.rk .rk-row{display:flex;align-items:center;gap:12px;min-height:54px;padding:8px 0}
.rk .rk-row+.rk-row,.rk .rk-sub+.rk-row{border-top:1px solid var(--rk-hair)}
.rk .rk-row .ri{width:28px;height:28px;border-radius:8px;background:var(--rk-surface2);display:grid;place-items:center;color:var(--rk-fg2);flex:none}
.rk .rk-row .tx{flex:1;min-width:0}.rk .rk-row .tx>div{font-weight:500}
.rk .rk-row .tx small{display:block;color:var(--rk-fg2);font-size:12px;line-height:1.45}
.rk .rk-row .cost{font-size:12px;color:var(--rk-fg3);white-space:nowrap}
.rk .rk-sub{padding:0 0 12px 40px;display:flex;flex-direction:column;gap:8px}
.rk .rk-sub .rk-help{font-size:12px;color:var(--rk-fg2);line-height:1.5;margin:0}
.rk .rk-costnote{font-size:12px;color:var(--rk-fg2);line-height:1.5}
.rk .rk-costnote summary{list-style:none;cursor:pointer;display:flex;gap:8px;align-items:baseline}
.rk .rk-costnote summary::-webkit-details-marker{display:none}
.rk .rk-costnote summary em{font-style:normal;color:var(--rk-accent);white-space:nowrap;margin-left:auto}
.rk .rk-costnote[open] summary em{display:none}
.rk .rk-costnote p{margin:6px 0 0;color:var(--rk-fg3)}
.rk .rk-input{width:100%;height:34px;border-radius:9px;border:1px solid var(--vscode-input-border,var(--rk-hair));background:var(--vscode-input-background,var(--rk-surface));color:var(--vscode-input-foreground,var(--rk-fg));padding:0 11px;font:inherit}
.rk .rk-input:focus{outline:none;border-color:var(--vscode-focusBorder,#3d8bff)}
.rk .rk-seg{display:flex;gap:3px;padding:3px;border-radius:9px;background:var(--rk-surface)}
.rk .rk-seg label{flex:1;position:relative;cursor:pointer}
.rk .rk-seg input{position:absolute;opacity:0;inset:0;margin:0;cursor:pointer}
.rk .rk-seg input+span{display:block;text-align:center;height:28px;line-height:28px;border-radius:7px;font-size:12px;color:var(--rk-fg2);white-space:nowrap}
.rk .rk-seg input:checked+span{background:var(--rk-surface2);color:var(--rk-fg);font-weight:500;box-shadow:0 1px 2px rgba(0,0,0,.2)}
.rk .rk-seg input:focus-visible+span{outline:2px solid var(--vscode-focusBorder,#3d8bff)}
.rk .rk-collapse{margin-top:10px;border-radius:12px;background:var(--rk-surface)}
.rk .rk-collapse>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px;min-height:42px;padding:0 14px;color:var(--rk-fg2);font-size:12.5px;border-radius:12px}
.rk .rk-collapse>summary::-webkit-details-marker{display:none}
.rk .rk-collapse>summary .sp{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rk .rk-collapse>summary svg{transition:transform .15s}.rk .rk-collapse[open]>summary svg{transform:rotate(90deg)}
.rk .rk-collapse>summary:focus-visible{outline:2px solid var(--vscode-focusBorder,#3d8bff)}
.rk .rk-collapse>.in{padding:0 14px 6px}
.rk .rk-chips{display:flex;flex-wrap:wrap;gap:6px}
.rk .rk-chip{display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 10px;border-radius:8px;background:var(--rk-surface);font-size:12px;color:var(--rk-fg2);max-width:100%}
.rk .rk-chip code{font-family:var(--vscode-editor-font-family,monospace);font-size:11.5px;color:var(--rk-fg2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rk .rk-chip.ok{background:color-mix(in srgb,var(--rk-ok) 14%,transparent);color:var(--rk-ok)}
.rk .rk-line{display:flex;align-items:flex-start;gap:8px;font-size:12px;color:var(--rk-fg2);margin:10px 2px 0}
.rk .rk-line svg{margin-top:2px}.rk .rk-line.ok svg{color:var(--rk-ok)}.rk .rk-line.warn{color:var(--rk-warn)}.rk .rk-line.bad{color:var(--rk-bad)}
.rk .rk-foot{margin:22px 0 0;padding:16px 0 0;border-top:1px solid var(--rk-hair)}
.rk .rk-cost{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;color:var(--rk-fg2);margin-bottom:10px}.rk .rk-cost b{color:var(--rk-fg);font-weight:500;text-align:right}
.rk .rk-btn{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;width:100%;height:40px;border-radius:11px;background:var(--rk-accent);color:var(--rk-accent-fg);font-weight:600;font-size:13.5px}
.rk .rk-btn:hover{filter:brightness(1.08)}.rk .rk-btn:focus-visible{outline:2px solid var(--vscode-focusBorder,#3d8bff);outline-offset:2px}
.rk .rk-btn[aria-disabled=true]{opacity:.55;cursor:default;filter:none}
.rk .rk-btn.ghost{background:var(--rk-surface2);color:var(--rk-fg);font-weight:500}
.rk .rk-btn.sm{width:auto;height:30px;padding:0 12px;border-radius:9px;font-size:12.5px}
.rk .rk-hint{text-align:center;font-size:12px;color:var(--rk-fg3);margin-top:8px}
.rk .rk-err{font-size:12px;color:var(--rk-bad);margin:6px 2px 0}
.rk .rk-list{display:flex;flex-direction:column;gap:2px}
.rk .rk-item{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;gap:10px;min-height:44px;padding:6px 12px;border-radius:10px}
.rk .rk-item:hover{background:var(--rk-surface)}
.rk .rk-item[aria-checked=true]{background:var(--rk-accent-soft);box-shadow:inset 0 0 0 1.5px var(--rk-accent)}
.rk .rk-item:focus-visible{outline:2px solid var(--vscode-focusBorder,#3d8bff)}
.rk .rk-item .av{width:26px;height:26px;border-radius:7px;background:var(--rk-surface2);display:grid;place-items:center;font-size:11.5px;font-weight:700;color:var(--rk-fg2);flex:none}
.rk .rk-item .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}.rk .rk-item .n span{color:var(--rk-fg3);font-weight:400}
.rk .rk-item small{color:var(--rk-fg3);font-size:11.5px}
.rk .rk-item.err{box-shadow:inset 0 0 0 1.5px var(--rk-bad)}
.rk .rk-search{display:flex;align-items:center;gap:8px;height:36px;border-radius:10px;background:var(--rk-surface);padding:0 12px;color:var(--rk-fg3);margin-bottom:6px}
.rk .rk-search input{all:unset;flex:1;min-width:0;color:var(--rk-fg);font-size:13px}
.rk .rk-search input::placeholder{color:var(--rk-fg3)}
.rk .rk-search:focus-within{box-shadow:inset 0 0 0 1px var(--vscode-focusBorder,#3d8bff)}
.rk .rk-search.err{box-shadow:inset 0 0 0 1.5px var(--rk-bad)}
@media (prefers-reduced-motion: reduce){.rk .rk-collapse>summary svg{transition:none}}
`;

let injected = false;
function ensure(): boolean {
  if (typeof document === "undefined") return false;
  if (!injected && !document.getElementById(STYLE_ID)) {
    const el = document.createElement("style");
    el.id = STYLE_ID;
    el.textContent = KIT_CSS;
    document.head.appendChild(el);
  }
  injected = true;
  return true;
}

/** 부품을 쓰는 영역 — 스타일을 한 번 넣고 테마 색 변수를 연다. */
export function Kit({ children, className = "", style, ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  const live = ensure();
  return <div className={`rk ${className}`.trim()} style={style} {...rest}>{!live && <style>{KIT_CSS}</style>}{children}</div>;
}

export function SheetHead({ icon, title, sub }: { icon: React.ReactNode; title: React.ReactNode; sub?: React.ReactNode }) {
  return <div className="rk-head"><span className="ic">{icon}</span><div style={{ minWidth: 0 }}><h3>{title}</h3>{sub && <p>{sub}</p>}</div></div>;
}

export function Section({ title, aside, children, testId }: { title?: React.ReactNode; aside?: React.ReactNode; children: React.ReactNode; testId?: string }) {
  return <section className="rk-sec" data-testid={testId}>{title && <div className="t"><span>{title}</span>{aside && <span>{aside}</span>}</div>}{children}</section>;
}

export function Box({ children }: { children: React.ReactNode }) {
  return <div className="rk-box">{children}</div>;
}

/** 값 줄 — 기본은 값만 보이고 [변경]을 누르면 그 자리에서 고친다(Enter·바깥 누름으로 끝). */
export function KvRow({ label, value, note, editor, editLabel = "변경", disabled, testId }: {
  label: string; value: React.ReactNode; note?: React.ReactNode;
  /** 있으면 [변경]으로 연다. 받은 close 를 부르면 닫힌다. */
  editor?: (close: () => void) => React.ReactNode; editLabel?: string; disabled?: boolean; testId?: string;
}) {
  const [editing, setEditing] = useState(false);
  return (
    <div className="rk-kv" data-testid={testId}>
      <span className="k">{label}</span>
      <span className="v">{editing && editor ? editor(() => setEditing(false)) : <>{value}{note && editor && <small>{note}</small>}</>}</span>
      {editor && !editing && <button type="button" className="rk-link" aria-disabled={disabled || undefined} aria-label={`${label} ${editLabel}`}
        onClick={() => { if (!disabled) setEditing(true); }}>{editLabel}</button>}
      {!editor && note && <span className="rk-note">{note}</span>}
    </div>
  );
}

export function SettingRow({ icon, title, desc, trailing, cost, children, testId }: {
  icon?: IconName; title: React.ReactNode; desc?: React.ReactNode; trailing?: React.ReactNode; cost?: React.ReactNode;
  /** 켰을 때 아래에 붙는 내용(강도·메일 등). */
  children?: React.ReactNode; testId?: string;
}) {
  return (
    <div data-testid={testId}>
      <div className="rk-row">
        {icon && <span className="ri"><Icon name={icon} size={15} /></span>}
        <div className="tx"><div>{title}</div>{desc && <small>{desc}</small>}</div>
        {cost && <span className="cost">{cost}</span>}
        {trailing}
      </div>
      {children && <div className="rk-sub">{children}</div>}
    </div>
  );
}

export function Segmented<T extends string>({ name, value, options, onChange, disabled, label }: {
  name: string; value: T; options: Array<{ value: T; label: React.ReactNode; title?: string }>; onChange: (v: T) => void; disabled?: boolean; label: string;
}) {
  return (
    <div className="rk-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <label key={o.value} title={o.title} data-level={o.value}>
          <input type="radio" name={name} checked={value === o.value} value={o.value} disabled={disabled} onChange={() => onChange(o.value)} />
          <span>{o.label}</span>
        </label>
      ))}
    </div>
  );
}

export function Collapse({ summary, aside, open, children, testId }: { summary: React.ReactNode; aside?: React.ReactNode; open?: boolean; children: React.ReactNode; testId?: string }) {
  return (
    <details className="rk-collapse" open={open} data-testid={testId}>
      <summary><span className="sp">{summary}</span>{aside && <span className="rk-note">{aside}</span>}<Icon name="chevron" size={14} /></summary>
      <div className="in">{children}</div>
    </details>
  );
}

export function Button({ children, onClick, disabled, variant = "primary", small, title, testId, type = "button" }: {
  children: React.ReactNode; onClick?: () => void; disabled?: boolean; variant?: "primary" | "ghost"; small?: boolean; title?: string; testId?: string; type?: "button" | "submit";
}) {
  //: 잠긴 버튼도 누를 수 있게 둔다(aria-disabled) — 누르면 무엇이 모자란지 알려 주는 쪽은 부르는 곳이 정한다.
  return (
    <button type={type} className={`rk-btn${variant === "ghost" ? " ghost" : ""}${small ? " sm" : ""}`} aria-disabled={disabled || undefined} title={title}
      data-testid={testId} onClick={() => { if (!disabled) onClick?.(); }}>{children}</button>
  );
}

export function Line({ icon = "check", tone, children }: { icon?: IconName; tone?: "ok" | "warn" | "bad"; children: React.ReactNode }) {
  return <div className={`rk-line${tone ? ` ${tone}` : ""}`}><Icon name={icon} size={14} /><span>{children}</span></div>;
}

/** AWS 지역 — 사람이 읽는 이름(한국 사용자 기준 자주 쓰는 곳 먼저). */
export const AWS_REGIONS: Array<{ id: string; name: string }> = [
  { id: "ap-northeast-2", name: "서울" }, { id: "ap-northeast-1", name: "도쿄" }, { id: "ap-northeast-3", name: "오사카" },
  { id: "ap-southeast-1", name: "싱가포르" }, { id: "ap-southeast-2", name: "시드니" }, { id: "ap-south-1", name: "뭄바이" },
  { id: "us-east-1", name: "버지니아 북부" }, { id: "us-east-2", name: "오하이오" }, { id: "us-west-1", name: "캘리포니아" }, { id: "us-west-2", name: "오리건" },
  { id: "eu-central-1", name: "프랑크푸르트" }, { id: "eu-west-1", name: "아일랜드" }, { id: "eu-west-2", name: "런던" }, { id: "ca-central-1", name: "캐나다 중부" },
  { id: "sa-east-1", name: "상파울루" },
];
export const regionName = (id: string): string => AWS_REGIONS.find((r) => r.id === id)?.name ?? "";
