"""Print connection readiness without printing secrets or contacting Discord."""
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).with_name('.env'))
    missing = []
    for key in ('DISCORD_BOT_TOKEN', 'DISCORD_CLIENT_ID', 'DISCORD_CLIENT_SECRET'):
        present = bool(os.getenv(key, '').strip())
        print(f"{'OK' if present else 'MISSING'} {key}")
        if not present:
            missing.append(key)
    base = os.getenv('DISCORD_PUBLIC_URL', 'http://127.0.0.1:8765').rstrip('/')
    uri = urlsplit(base)
    valid = (uri.scheme == 'https' or (uri.scheme == 'http' and uri.hostname in ('127.0.0.1', 'localhost', '::1'))) and not (uri.username or uri.password or uri.query or uri.fragment or uri.path not in ('', '/'))
    print('OK DISCORD_PUBLIC_URL' if valid else 'INVALID DISCORD_PUBLIC_URL (HTTPS 또는 localhost HTTP 주소 필요)')
    if valid:
        print(f'Discord OAuth2 Redirect: {base}/api/v1/connect/callback')
        print(f'VS Code recoder.discord.serverUrl: {base}')
    print('로컬 설정만 확인했습니다. 실제 Discord 로그인 및 채널 권한은 연결 후 검증합니다.')
    return 1 if missing or not valid else 0


if __name__ == '__main__':
    sys.exit(main())
