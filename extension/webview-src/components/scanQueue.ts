export type ScanKind = 'trivy' | 'hadolint' | 'gitleaks';
export interface ScanRequest { scanType: ScanKind; requestId: string; workspacePath: string }
let serial = 0;

/** Only our matching response may advance a sequence; other panels also run scans. */
export class ScanQueue {
  current: ScanRequest | null = null;
  private remaining: ScanKind[] = [];
  start(kinds: ScanKind[]): ScanRequest | null {
    if (this.current) return null;
    this.remaining = [...new Set(kinds)];
    return this.advance();
  }
  matches(result: { requestId?: string; scan_type?: string }): boolean {
    return Boolean(this.current && result.requestId === this.current.requestId && result.scan_type === this.current.scanType);
  }
  complete(): ScanRequest | null { return this.advance(); }
  stop() { this.current = null; this.remaining = []; }
  private advance(): ScanRequest | null {
    const scanType = this.remaining.shift();
    this.current = scanType ? { scanType, requestId: `security-${Date.now()}-${++serial}`, workspacePath: '' } : null;
    return this.current;
  }
}
