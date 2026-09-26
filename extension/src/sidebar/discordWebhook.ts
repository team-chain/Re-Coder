import type { SecretStorage } from 'vscode';
import { createHash } from 'crypto';

export function webhookUrl(raw: string): string {
  let url: URL;
  try { url = new URL(raw.trim()); } catch { throw new Error('Discord 웹후크 URL을 붙여넣으세요.'); }
  if (url.protocol !== 'https:' || !['discord.com', 'discordapp.com'].includes(url.hostname) || url.port || url.username || url.password || url.search || url.hash || !/^\/api(?:\/v\d+)?\/webhooks\/\d{15,22}\/[\w-]{30,200}\/?$/.test(url.pathname)) throw new Error('Discord 채널 설정에서 복사한 웹후크 URL이 필요합니다.');
  return `https://discord.com${url.pathname.replace(/\/$/, '')}`;
}

/** Credentials stay in VS Code SecretStorage, scoped to this workspace. */
export class DiscordWebhook {
  constructor(private secrets: Pick<SecretStorage, 'get' | 'store' | 'delete'>, private request: typeof fetch = fetch) {}
  private key(workspace: string) { return 'recoder.discord.webhook.' + createHash('sha256').update(process.platform === 'win32' ? workspace.toLowerCase() : workspace).digest('hex'); }
  async configured(workspace: string) { return Boolean(await this.secrets.get(this.key(workspace))); }
  async mode(workspace: string) { return (await this.secrets.get(this.key(workspace) + '.mode')) || 'webhook'; }
  async setMode(workspace: string, mode: string) { await this.secrets.store(this.key(workspace) + '.mode', mode); }
  private async call(url: string, body?: unknown): Promise<any> {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await this.request(url + (body ? '?wait=true' : ''), {
        method: body ? 'POST' : 'GET', redirect: 'error', signal: controller.signal,
        headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
      });
      if (!response.ok) throw new Error(response.status === 404 || response.status === 401
        ? '웹후크가 삭제되었거나 유효하지 않습니다. 새 URL로 다시 연결하세요.'
        : response.status === 429 ? 'Discord 요청이 많습니다. 잠시 후 다시 시도하세요.' : `Discord 응답 오류 (${response.status}). 채널 권한과 웹후크 설정을 확인하세요.`);
      return await response.json();
    } catch (error: any) {
      // Fetch errors can contain the secret URL. Never propagate their text.
      if (error?.message?.startsWith('Discord ') || error?.message?.startsWith('웹후크가 ')) throw error;
      throw new Error('Discord에 연결하지 못했습니다. 인터넷 연결을 확인하고 다시 시도하세요.');
    } finally { clearTimeout(timer); }
  }
  private publicState(data: any) {
    if (data.type !== 1 || !data.channel_id || !data.guild_id) throw new Error('서버의 텍스트 채널에서 만든 수신 웹후크가 필요합니다.');
    return { mode: 'webhook', active_channel_id: String(data.channel_id), channel_name: String(data.name || '배포 알림'), guild_id: String(data.guild_id) };
  }
  async connect(workspace: string, raw: string) {
    const url = webhookUrl(raw), state = this.publicState(await this.call(url));
    await this.secrets.store(this.key(workspace), url);
    await this.setMode(workspace, 'webhook');
    return state;
  }
  async status(workspace: string) {
    const saved = await this.secrets.get(this.key(workspace));
    return saved ? this.publicState(await this.call(webhookUrl(saved))) : { mode: 'webhook', active_channel_id: '' };
  }
  async disconnect(workspace: string) { await this.secrets.delete(this.key(workspace)); await this.setMode(workspace, 'webhook'); }
  async send(workspace: string, title: string, detail: string) {
    const saved = await this.secrets.get(this.key(workspace));
    if (!saved) throw new Error('Discord 알림 채널을 먼저 연결하세요.');
    const result = await this.call(webhookUrl(saved), { username: 'ReCoder', allowed_mentions: { parse: [] }, embeds: [{ title: title.slice(0, 160), description: detail.slice(0, 1000), color: 5793266 }] });
    if (!result.id) throw new Error('Discord 전송 확인을 받지 못했습니다. 채널에서 수신 여부를 확인하세요.');
  }
}
