/** Build the Discord ownership-verification command from the one-time token. */
export function buildDiscordLinkCommand(rawToken: string): string {
    const token = String(rawToken || '').trim();
    const parts = token.split('_', 3);
    if (!token.startsWith('rcdr_') || parts.length !== 3 || !parts[1] || !parts[2]) {
        throw new Error('Gateway returned an invalid ReCoder enrollment token.');
    }
    return `/recoder link ${token}`;
}
