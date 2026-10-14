import type { SecretStorage } from 'vscode';
import { createHash } from 'crypto';

/**
 * "내가 만든 Discord 봇" 으로 알림 보내기 — 별도 봇 프로그램 없이.
 *
 * 예전에는 봇 연동이 저장소의 discord-bot 프로그램(127.0.0.1:8765)을 직접 띄워야만
 * 됐다. 마켓플레이스에서 확장만 설치한 사용자에게는 그 프로그램이 없어서, Developer
 * Portal 에서 봇을 만들어도 ReCoder 와 이을 방법이 없었다.
 *
 * 알림은 Discord REST API(채널에 메시지 쓰기)만으로 충분하다 — 상시 접속(게이트웨이)이
 * 필요 없다. 그래서 확장이 사용자의 봇 토큰으로 직접 호출한다.
 *
 * - 토큰은 VS Code 보안 저장소(SecretStorage)에만 둔다. 화면·로그·오류 문구에 싣지 않는다.
 * - 알림 채널은 프로젝트(워크스페이스 폴더)마다 따로 고른다.
 * - 멘션은 보내지 않는다(allowed_mentions 비움).
 */
const API = 'https://discord.com/api/v10';
const TOKEN_KEY = 'recoder.discord.botToken';
//: 채널 보기(1024) + 메시지 보내기(2048) + 링크 임베드(16384)
export const BOT_INVITE_PERMISSIONS = 1024 + 2048 + 16384;

export interface BotChannelState { channel_id: string; channel_name: string; guild_id: string; guild_name: string }

export function normalizeBotToken(raw: string): string {
  const token = raw.trim().replace(/^Bot\s+/i, '');
  if (!token) throw new Error('봇 토큰을 붙여넣으세요.');
  if (/\s/.test(token)) throw new Error('토큰에 공백이나 줄바꿈이 있습니다. 토큰만 다시 복사하세요.');
  if (/^https?:/i.test(token)) throw new Error('웹후크 URL이 아니라 Developer Portal → Bot 의 토큰이 필요합니다.');
  if (!/^[\w-]{20,}\.[\w-]{4,}\.[\w-]{20,}$/.test(token)) {
    throw new Error('Discord 봇 토큰 형식이 아닙니다. Developer Portal → 내 앱 → Bot → Reset Token 으로 받은 값을 붙여넣으세요.');
  }
  return token;
}

export function botInviteUrl(clientId: string): string {
  if (!/^\d{15,22}$/.test(clientId)) throw new Error('봇 ID를 확인하지 못했습니다. 봇 토큰을 다시 연결하세요.');
  return `https://discord.com/oauth2/authorize?client_id=${clientId}&permissions=${BOT_INVITE_PERMISSIONS}&scope=bot`;
}

export class DiscordBotToken {
  constructor(private secrets: Pick<SecretStorage, 'get' | 'store' | 'delete'>, private request: typeof fetch = fetch, private version = '') {}

  private channelKey(workspace: string) {
    return 'recoder.discord.botChannel.' + createHash('sha256').update(process.platform === 'win32' ? workspace.toLowerCase() : workspace).digest('hex');
  }

  async hasToken(): Promise<boolean> { return Boolean(await this.secrets.get(TOKEN_KEY)); }

  private async call(token: string, route: string, method = 'GET', body?: unknown): Promise<any> {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    let response: Response;
    try {
      response = await this.request(`${API}${route}`, {
        method, redirect: 'error', signal: controller.signal,
        headers: {
          Authorization: `Bot ${token}`,
          'Content-Type': 'application/json',
          'User-Agent': `DiscordBot (https://github.com/team-chain/Re-Coder, ${this.version || '1'})`,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      // fetch 오류 문구에는 요청 정보가 섞일 수 있다 — 그대로 올리지 않는다.
      throw new Error('Discord에 연결하지 못했습니다. 인터넷 연결(방화벽·프록시)을 확인하고 다시 시도하세요.');
    } finally { clearTimeout(timer); }
    if (response.status === 401) throw new Error('봇 토큰이 올바르지 않거나 재발급되었습니다. Developer Portal → Bot → Reset Token 으로 새 토큰을 받아 다시 연결하세요.');
    if (response.status === 403) throw new Error('봇에 이 채널을 보거나 메시지를 보낼 권한이 없습니다. 서버 설정 → 채널 권한에서 봇 역할에 "채널 보기·메시지 보내기·링크 첨부"를 허용하세요.');
    if (response.status === 404) throw new Error('채널이나 서버를 찾지 못했습니다. 봇이 그 서버에 초대돼 있는지 확인하세요.');
    if (response.status === 429) throw new Error('Discord 요청이 많습니다. 잠시 후 다시 시도하세요.');
    if (!response.ok) throw new Error(`Discord 응답 오류 (${response.status}). 잠시 후 다시 시도하세요.`);
    return response.status === 204 ? {} : await response.json();
  }

  private async token(): Promise<string> {
    const token = await this.secrets.get(TOKEN_KEY);
    if (!token) throw new Error('먼저 내 Discord 봇 토큰을 연결하세요.');
    return token;
  }

  /** 토큰을 확인하고 저장한다. 메시지는 보내지 않는다. */
  async connect(raw: string): Promise<{ bot_user: string; bot_id: string }> {
    const token = normalizeBotToken(raw);
    const me = await this.call(token, '/users/@me');
    if (!me?.bot) throw new Error('사용자 계정 토큰은 쓸 수 없습니다. Developer Portal 에서 만든 봇의 토큰을 붙여넣으세요.');
    await this.secrets.store(TOKEN_KEY, token);
    return { bot_user: String(me.username || '봇'), bot_id: String(me.id || '') };
  }

  async disconnect(workspace: string): Promise<void> {
    await this.secrets.delete(TOKEN_KEY);
    await this.secrets.delete(this.channelKey(workspace));
  }

  async status(workspace: string): Promise<Record<string, unknown>> {
    const token = await this.secrets.get(TOKEN_KEY);
    if (!token) return { mode: 'token', active_channel_id: '', bot_user: '' };
    const me = await this.call(token, '/users/@me');
    const saved = await this.secrets.get(this.channelKey(workspace));
    const channel: Partial<BotChannelState> = saved ? JSON.parse(saved) : {};
    return {
      mode: 'token', bot_user: String(me.username || ''), bot_id: String(me.id || ''),
      active_channel_id: channel.channel_id || '', channel_name: channel.channel_name || '',
      guild_id: channel.guild_id || '', guild_name: channel.guild_name || '',
    };
  }

  async guilds(): Promise<Array<{ id: string; name: string }>> {
    const list = await this.call(await this.token(), '/users/@me/guilds');
    return (Array.isArray(list) ? list : []).map((g: any) => ({ id: String(g.id), name: String(g.name || g.id) }));
  }

  async channels(guildId: string): Promise<Array<{ id: string; name: string }>> {
    if (!/^\d{15,22}$/.test(guildId)) throw new Error('서버를 다시 선택하세요.');
    const list = await this.call(await this.token(), `/guilds/${guildId}/channels`);
    //: 0 = 텍스트 채널, 5 = 공지 채널. 음성·카테고리·포럼은 메시지를 바로 쓸 수 없다.
    return (Array.isArray(list) ? list : [])
      .filter((c: any) => c.type === 0 || c.type === 5)
      .sort((a: any, b: any) => (a.position ?? 0) - (b.position ?? 0))
      .map((c: any) => ({ id: String(c.id), name: String(c.name || c.id) }));
  }

  async setChannel(workspace: string, channelId: string): Promise<Record<string, unknown>> {
    if (!channelId) { await this.secrets.delete(this.channelKey(workspace)); return this.status(workspace); }
    if (!/^\d{15,22}$/.test(channelId)) throw new Error('채널을 다시 선택하세요.');
    const token = await this.token();
    const channel = await this.call(token, `/channels/${channelId}`);
    if (channel.type !== 0 && channel.type !== 5) throw new Error('텍스트 채널을 선택하세요.');
    let guildName = '';
    try { guildName = String((await this.call(token, `/guilds/${channel.guild_id}`)).name || ''); } catch { /* 이름은 표시용 */ }
    const state: BotChannelState = { channel_id: String(channel.id), channel_name: String(channel.name || ''), guild_id: String(channel.guild_id || ''), guild_name: guildName };
    await this.secrets.store(this.channelKey(workspace), JSON.stringify(state));
    return this.status(workspace);
  }

  async send(workspace: string, title: string, detail: string): Promise<void> {
    const saved = await this.secrets.get(this.channelKey(workspace));
    const channel: Partial<BotChannelState> = saved ? JSON.parse(saved) : {};
    if (!channel.channel_id) throw new Error('알림 받을 채널을 먼저 고르세요.');
    const result = await this.call(await this.token(), `/channels/${channel.channel_id}/messages`, 'POST', {
      allowed_mentions: { parse: [] },
      embeds: [{ title: title.slice(0, 160), description: detail.slice(0, 1000), color: 5793266 }],
    });
    if (!result?.id) throw new Error('Discord 전송 확인을 받지 못했습니다. 채널에서 수신 여부를 확인하세요.');
  }

  async inviteUrl(): Promise<string> {
    const me = await this.call(await this.token(), '/users/@me');
    return botInviteUrl(String(me.id || ''));
  }
}
