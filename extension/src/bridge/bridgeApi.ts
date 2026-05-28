/**
 * extension/src/bridge/bridgeApi.ts
 *
 * 봇 내장 HTTP API(기본 0.0.0.0:8765)와 통신해 ReCoder Bridge 설정을
 * 조회/저장하는 헬퍼 함수들. SidebarProvider와 WorkbenchPanel 양쪽에서
 * 동일하게 import해 쓴다.
 *
 * 인증은 기존 BOT_REGISTRATION_KEY와 동일한 헤더(X-Registration-Key)를 그대로 재사용.
 */

import * as vscode from 'vscode';

export interface BridgeStatusResponse {
    ok: boolean;
    error?: string;
    active_channel_id?: string;
    channel_name?: string | null;
    guild_name?: string | null;
    connected_clients?: number;
    settings?: Record<string, unknown>;
}

export interface BridgeInviteResponse {
    ok: boolean;
    error?: string;
    invite_url?: string;
    client_id?: string;
    bot_name?: string | null;
    bot_avatar?: string | null;
}

function _getBotApiBase(): string {
    const cfg = vscode.workspace.getConfiguration('recoder.bridge');
    return cfg.get<string>('botApiUrl', 'http://127.0.0.1:8765');
}

function _getRegistrationKey(): string {
    const cfg = vscode.workspace.getConfiguration('recoder.bridge');
    return cfg.get<string>('botRegistrationKey', '');
}

export async function fetchBridgeStatus(): Promise<BridgeStatusResponse> {
    const url = `${_getBotApiBase().replace(/\/+$/, '')}/api/v1/bridge/status`;
    try {
        const headers: Record<string, string> = {};
        const key = _getRegistrationKey();
        if (key) headers['X-Registration-Key'] = key;
        const res = await fetch(url, { method: 'GET', headers });
        if (!res.ok) {
            return { ok: false, error: `봇 API ${res.status}` };
        }
        const data = await res.json() as Partial<BridgeStatusResponse>;
        return { ok: true, ...data };
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return { ok: false, error: `봇에 연결 실패: ${msg}` };
    }
}

export async function fetchInviteUrl(): Promise<BridgeInviteResponse> {
    const url = `${_getBotApiBase().replace(/\/+$/, '')}/api/v1/bridge/invite-url`;
    try {
        const headers: Record<string, string> = {};
        const key = _getRegistrationKey();
        if (key) headers['X-Registration-Key'] = key;
        const res = await fetch(url, { method: 'GET', headers });
        const data = await res.json().catch(() => ({})) as Partial<BridgeInviteResponse>;
        if (!res.ok) {
            return { ok: false, error: data.error || `봇 API ${res.status}` };
        }
        return { ok: true, ...data };
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return { ok: false, error: `봇에 연결 실패: ${msg}` };
    }
}

export async function setBridgeChannel(channelId: string): Promise<BridgeStatusResponse> {
    const url = `${_getBotApiBase().replace(/\/+$/, '')}/api/v1/bridge/channel`;
    try {
        const headers: Record<string, string> = { 'Content-Type': 'application/json' };
        const key = _getRegistrationKey();
        if (key) headers['X-Registration-Key'] = key;
        const res = await fetch(url, {
            method: 'PUT',
            headers,
            body: JSON.stringify({ channel_id: channelId }),
        });
        const data = await res.json().catch(() => ({})) as Partial<BridgeStatusResponse>;
        if (!res.ok) {
            return { ok: false, error: data.error || `봇 API ${res.status}` };
        }
        // PUT 응답 후 최신 상태를 한 번 더 조회해서 channel_name/guild_name 채움
        const status = await fetchBridgeStatus();
        return { ...status, ok: true };
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return { ok: false, error: `봇에 연결 실패: ${msg}` };
    }
}
