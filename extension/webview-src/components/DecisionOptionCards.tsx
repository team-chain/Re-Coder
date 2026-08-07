import React from "react";

export type DecisionOptionCard = {
  key: string;
  label: string;
  summary?: string;
  detail?: string;
  pros?: string[];
  cons?: string[];
  recommended?: boolean;
};

export const DecisionOptionCards: React.FC<{
  options: DecisionOptionCard[];
  selectedKey?: string;
  onSelect: (key: string) => void;
  disabled?: boolean;
  radioName?: string;
}> = ({ options, selectedKey, onSelect, disabled = false, radioName }) => (
  <div style={{ display: "grid", gap: 8 }}>
    {options.map((option) => {
      const selected = selectedKey === option.key;
      return <button key={option.key} type="button" disabled={disabled} onClick={() => onSelect(option.key)} style={{ textAlign: "left", width: "100%", padding: "12px 13px", borderRadius: 8, border: `1px solid ${selected || option.recommended ? "var(--vscode-focusBorder, #3794ff)" : "var(--vscode-panel-border, #3f3f3f)"}`, background: selected ? "var(--vscode-list-activeSelectionBackground, rgba(55,148,255,.18))" : option.recommended ? "rgba(55,148,255,.12)" : "var(--vscode-editor-background, #1e1e1e)", color: "var(--vscode-foreground, #eee)", cursor: disabled ? "wait" : "pointer" }}>
        <span style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
          {radioName && <input type="radio" name={radioName} checked={selected} readOnly style={{ marginTop: 3, accentColor: "var(--vscode-focusBorder, #3794ff)" }} />}
          <span style={{ minWidth: 0, flex: 1 }}>
            <span style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13.5, fontWeight: 650 }}>
              {option.label}
              {option.recommended && <span style={{ borderRadius: 99, padding: "2px 6px", background: "rgba(81,188,120,.18)", color: "#78d89b", fontSize: 10, fontWeight: 700 }}>추천</span>}
            </span>
            {option.summary && <span style={{ display: "block", marginTop: 4, color: "var(--vscode-descriptionForeground, #aaa)", fontSize: 11.5, lineHeight: 1.45 }}>{option.summary}</span>}
            {option.detail && <span style={{ display: "block", marginTop: 3, color: "var(--vscode-descriptionForeground, #888)", fontSize: 10.5, lineHeight: 1.45 }}>{option.detail}</span>}
            {(option.pros?.length || option.cons?.length) && <span style={{ display: "block", marginTop: 6, color: "var(--vscode-descriptionForeground, #888)", fontSize: 10.5, lineHeight: 1.5 }}>{[...(option.pros ?? []).map(text => `+ ${text}`), ...(option.cons ?? []).map(text => `− ${text}`)].join("  ·  ")}</span>}
          </span>
        </span>
      </button>;
    })}
  </div>
);
