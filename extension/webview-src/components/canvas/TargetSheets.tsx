/**
 * 배포 설정 시트(2.0.46) — 캔버스에서 대상(ECS·S3)에 프로젝트를 놓으면 오른쪽에 열린다.
 *
 *  · 값은 미리 채워 두고 [변경]으로만 고친다(이름·지역·버전).
 *  · 보호는 두 줄(기본 방패·감지 알림), 요금이 드는 것은 접어 둔다(EcsProtection).
 *  · 사양·포트·환경변수 같은 것은 「고급」 안에. 맨 아래에 예상 추가 요금과 버튼 하나.
 */
import React from "react";
import { Box, Button, Collapse, KvRow, Section, SheetHead, AWS_REGIONS, regionName } from "../ui/kit";
import { EcsProtection, monthlyExtra, type Protection } from "../EcsProtection";
import { Logo } from "./Scene";
import type { Config } from "./ApprovalCard";

type Field = "ecs_service" | "tag" | "aws_region" | "dir";

/** 배포된 이미지 이름에서 버전(태그) — "…/app:v3" → "v3". */
export function deployedTag(image: string | undefined | null): string {
  const m = /:([A-Za-z0-9._-]{1,64})$/.exec((image || "").split("@")[0]);
  return m ? m[1] : "";
}

/** 2.0.46 다음 버전 — 지난 배포가 v3 이면 v4. 숫자 버전이 아니면 ""(지금 값을 그대로 둔다). */
export function nextTag(image: string | undefined | null): string {
  const m = /^v(\d{1,6})$/.exec(deployedTag(image));
  return m ? `v${Number(m[1]) + 1}` : "";
}

function TextEditor({ value, onDone, placeholder, label }: { value: string; onDone: (v: string) => void; placeholder?: string; label: string }) {
  return <input autoFocus aria-label={label} defaultValue={value} placeholder={placeholder}
    onBlur={(e) => onDone(e.currentTarget.value.trim() || value)}
    onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); onDone(e.currentTarget.value.trim() || value); } if (e.key === "Escape") { e.stopPropagation(); onDone(value); } }} />;
}

function RegionEditor({ value, onDone }: { value: string; onDone: (v: string) => void }) {
  const known = AWS_REGIONS.some((r) => r.id === value);
  return (
    <select autoFocus aria-label="지역" defaultValue={value || "ap-northeast-2"} onBlur={(e) => onDone(e.currentTarget.value)} onChange={(e) => onDone(e.currentTarget.value)}
      onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onDone(value); } }}>
      {!known && value && <option value={value}>{value}</option>}
      {AWS_REGIONS.map((r) => <option key={r.id} value={r.id}>{r.name} ({r.id})</option>)}
    </select>
  );
}

const regionText = (id: string) => id ? <>{regionName(id) || id}{regionName(id) && <small>{id}</small>}</> : <span style={{ color: "var(--rk-fg3)" }}>AWS 연결 지역</span>;

export interface SheetCommon {
  config: Config;
  update: (key: Field, value: string) => void;
  running: boolean;
  busy: boolean;
  ready: boolean;
  onPrepare: () => void;
}

export function EcsSheet({ config, update, running, busy, ready, onPrepare, protection, onProtection, deployedImage, advanced }: SheetCommon & {
  protection: Protection; onProtection: (p: Protection) => void; deployedImage?: string;
  /** 사양·포트·환경변수·실행 역할 — 원래 입력칸들을 그대로 넣는다. */
  advanced: React.ReactNode;
}) {
  const extra = monthlyExtra(protection);
  const prev = deployedTag(deployedImage);
  return (
    <div data-testid="ecs-sheet">
      <SheetHead icon={<Logo kind="ecs" />} title="ECS 로 배포" sub="서버까지 · 승인 전에는 아무것도 바뀌지 않아요" />
      <Section>
        <Box>
          <KvRow label="이름" testId="kv-service" value={config.ecs_service} disabled={running}
            editor={(close) => <TextEditor label="서비스 이름" value={config.ecs_service} onDone={(v) => { update("ecs_service", v); close(); }} />} />
          <KvRow label="지역" testId="kv-region" value={regionText(config.aws_region)} disabled={running}
            editor={(close) => <RegionEditor value={config.aws_region} onDone={(v) => { update("aws_region", v); close(); }} />} />
          <KvRow label="버전" testId="kv-tag" value={config.tag} note={prev ? `지난 배포 ${prev}` : undefined} disabled={running}
            editor={(close) => <TextEditor label="이미지 태그" value={config.tag} onDone={(v) => { update("tag", v); close(); }} />} />
        </Box>
      </Section>
      <EcsProtection value={protection} onChange={onProtection} disabled={running} />
      <Collapse summary="고급 · 사양 · 포트 · 환경변수" aside={`${Number(config.cpu) / 1024} vCPU · ${Number(config.memory) / 1024} GB · ${config.container_port}`} testId="ecs-advanced">
        <div className="rc-advanced-body">{advanced}</div>
      </Collapse>
      <div className="rk-foot">
        <div className="rk-cost"><span>예상 추가 요금</span><b data-testid="ecs-extra">{extra ? `월 약 $${extra}+ · Fargate 실행비 별도` : "없음 · Fargate 실행비만"}</b></div>
        <Button testId="ecs-review" onClick={onPrepare} disabled={running || !ready}>{busy ? "확인 중…" : "배포 내용 확인"}</Button>
        <p className="rk-hint">다음 단계에서 내용을 검토하고 승인합니다</p>
      </div>
    </div>
  );
}

export function S3Sheet({ config, update, running, busy, ready, onPrepare, autoDir }: SheetCommon & { autoDir: boolean }) {
  return (
    <div data-testid="s3-sheet">
      <SheetHead icon={<Logo kind="s3" />} title="S3 로 배포" sub="화면만 · 정적 사이트 · 승인 전에는 그대로예요" />
      <Section>
        <Box>
          <KvRow label="폴더" value={config.dir || (autoDir ? "자동으로 찾기" : "")} disabled={running}
            editor={(close) => <TextEditor label="빌드 산출물 폴더" value={config.dir} placeholder="dist · build · public" onDone={(v) => { update("dir", v); close(); }} />} />
          <KvRow label="지역" value={regionText(config.aws_region)} disabled={running}
            editor={(close) => <RegionEditor value={config.aws_region} onDone={(v) => { update("aws_region", v); close(); }} />} />
        </Box>
      </Section>
      <div className="rk-foot">
        <div className="rk-cost"><span>예상 추가 요금</span><b>거의 없음 · 저장·전송량만큼</b></div>
        <Button testId="s3-review" onClick={onPrepare} disabled={running || !ready}>{busy ? "확인 중…" : "배포 내용 확인"}</Button>
        <p className="rk-hint">다음 단계에서 올릴 파일을 확인하고 승인합니다</p>
      </div>
    </div>
  );
}
