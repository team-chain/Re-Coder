/**
 * 체크박스 · 켜기/끄기 스위치 — 브라우저 기본 모양 대신 VS Code 테마 색을 따르는 공통 부품.
 *
 *  · 실제 <input type="checkbox"> 를 그대로 쓴다(키보드·스크린리더·폼 동작 유지). 모양만 CSS 로 바꾼다.
 *  · 스타일은 문서에 한 번만 넣는다. 테스트(서버 렌더)에서는 document 가 없어 건너뛴다.
 */
import React from "react";

const STYLE_ID = "rc-check-styles";
export const CHECK_CSS = `
label.rc-ck.rc-ck{display:flex;flex-direction:row;gap:9px;align-items:flex-start;padding:5px 7px;margin:0 -7px;border-radius:7px;cursor:pointer;transition:background .15s;line-height:1.45}
label.rc-ck.rc-ck:hover{background:var(--vscode-list-hoverBackground,rgba(255,255,255,.04))}
label.rc-ck.rc-ck.compact{padding:2px 4px;margin:0 -4px;gap:6px}
label.rc-ck.rc-ck.inline{display:inline-flex;margin:0}
label.rc-ck.rc-ck input,label.rc-sw.rc-sw input{appearance:none;-webkit-appearance:none;margin:0;padding:0;min-width:0;flex:none;cursor:pointer;font:inherit;box-sizing:border-box}
label.rc-ck.rc-ck input{width:16px;height:16px;margin-top:1px;border-radius:5px;border:1.5px solid var(--vscode-checkbox-border,#6b7480);background:var(--vscode-checkbox-background,#1a1c20);display:grid;place-content:center;transition:background .15s,border-color .15s,box-shadow .15s}
label.rc-ck.rc-ck input::before{content:"";width:9px;height:9px;transform:scale(0);transition:transform .15s cubic-bezier(.3,1.6,.5,1);clip-path:polygon(14% 44%,0 65%,50% 100%,100% 16%,80% 0%,43% 62%);background:var(--vscode-button-foreground,#fff)}
label.rc-ck.rc-ck input:checked{background:var(--vscode-button-background,#0e639c);border-color:var(--vscode-button-background,#0e639c)}
label.rc-ck.rc-ck input:checked::before{transform:scale(1)}
label.rc-ck.rc-ck.warn input:checked{background:var(--vscode-editorWarning-foreground,#cca700);border-color:var(--vscode-editorWarning-foreground,#cca700)}
label.rc-ck.rc-ck input:focus-visible,label.rc-sw.rc-sw input:focus-visible{outline:none;box-shadow:0 0 0 3px color-mix(in srgb,var(--vscode-focusBorder,#3794ff) 45%,transparent)}
label.rc-ck.rc-ck.disabled,label.rc-sw.rc-sw.disabled{opacity:.5;cursor:not-allowed}
label.rc-ck.rc-ck.disabled input,label.rc-sw.rc-sw.disabled input{cursor:not-allowed}
.rc-ck-t{min-width:0}
.rc-ck-d{display:block;font-size:.92em;color:var(--vscode-descriptionForeground,#9da5b0);margin-top:1px}
label.rc-sw.rc-sw{display:inline-flex;flex-direction:row;align-items:center;gap:8px;cursor:pointer;line-height:1.4}
label.rc-sw.rc-sw input{width:32px;height:18px;border-radius:99px;background:var(--vscode-input-border,#4a4f57);position:relative;transition:background .2s;border:0}
label.rc-sw.rc-sw input::after{content:"";position:absolute;left:2px;top:2px;width:14px;height:14px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.35);transition:left .2s cubic-bezier(.3,1.4,.5,1)}
label.rc-sw.rc-sw input:checked{background:var(--vscode-button-background,#0e639c)}
label.rc-sw.rc-sw input:checked::after{left:16px}
label.rc-sw.rc-sw .rc-ck-d{display:block}
@media (prefers-reduced-motion: reduce){label.rc-ck.rc-ck input,label.rc-ck.rc-ck input::before,label.rc-sw.rc-sw input,label.rc-sw.rc-sw input::after{transition:none}}
`;

function ensureStyles(): void {
  if (typeof document === "undefined" || document.getElementById(STYLE_ID)) { return; }
  const el = document.createElement("style");
  el.id = STYLE_ID;
  el.textContent = CHECK_CSS;
  document.head.appendChild(el);
}

interface BaseProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  /** 보이는 이름. 없으면 ariaLabel 이 필요하다. */
  label?: React.ReactNode;
  /** 이름 아래 한 줄 설명. */
  description?: React.ReactNode;
  ariaLabel?: string;
  title?: string;
  className?: string;
  style?: React.CSSProperties;
}

export const Checkbox: React.FC<BaseProps & { tone?: "default" | "warn"; compact?: boolean; inline?: boolean }> = ({
  checked, onChange, disabled, label, description, ariaLabel, title, tone = "default", compact, inline, className, style,
}) => {
  ensureStyles();
  const cls = ["rc-ck", tone === "warn" ? "warn" : "", compact ? "compact" : "", inline ? "inline" : "", disabled ? "disabled" : "", className ?? ""].filter(Boolean).join(" ");
  return (
    <label className={cls} title={title} style={style}>
      <input type="checkbox" checked={checked} disabled={disabled} aria-label={ariaLabel}
        onChange={e => onChange(e.target.checked)} />
      {(label || description) && (
        <span className="rc-ck-t">
          {label}
          {description && <span className="rc-ck-d">{description}</span>}
        </span>
      )}
    </label>
  );
};

export const Switch: React.FC<BaseProps> = ({ checked, onChange, disabled, label, description, ariaLabel, title, className, style }) => {
  ensureStyles();
  const cls = ["rc-sw", disabled ? "disabled" : "", className ?? ""].filter(Boolean).join(" ");
  return (
    <label className={cls} title={title} style={style}>
      <input type="checkbox" role="switch" aria-checked={checked} checked={checked} disabled={disabled} aria-label={ariaLabel}
        onChange={e => onChange(e.target.checked)} />
      {(label || description) && (
        <span className="rc-ck-t">
          {label}
          {description && <span className="rc-ck-d">{description}</span>}
        </span>
      )}
    </label>
  );
};
