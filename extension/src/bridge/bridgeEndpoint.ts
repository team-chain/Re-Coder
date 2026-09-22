import { isIP } from 'net';

/** True only for hosts whose traffic stays on the local machine. */
export function isLoopbackBridgeHost(rawHost: string): boolean {
    const host = String(rawHost || '').trim().toLowerCase().replace(/^\[|\]$/g, '');
    if (host === 'localhost' || host === '::1' || host === '0:0:0:0:0:0:0:1') {
        return true;
    }
    if (isIP(host) === 4) {
        const firstOctet = Number(host.split('.')[0]);
        return firstOctet === 127;
    }
    return false;
}

/** Build a TLS-protected URL for every non-loopback bridge. */
export function buildBridgeWebSocketUrl(
    rawHost: string,
    port: number,
    params: URLSearchParams,
): string {
    const host = String(rawHost || '').trim();
    if (!host || host.includes('/') || host.includes('\\')) {
        throw new Error('recoder.bridge.host must be a hostname or IP address.');
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error('recoder.bridge.port must be between 1 and 65535.');
    }
    const protocol = isLoopbackBridgeHost(host) ? 'ws' : 'wss';
    const urlHost = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
    const query = params.toString();
    return `${protocol}://${urlHost}:${port}/ws${query ? `?${query}` : ''}`;
}
