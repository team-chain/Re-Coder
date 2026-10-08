"""Refresh explicitly reviewed official sources, usable locally or in Lambda.

The shipped catalog contains original summaries, not copied documentation. Full
text is ingested only from an operator-reviewed manifest with attribution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from grounded_repair.knowledge import official_url

SOURCE_REPOS = {"docker/docs", "npm/cli", "nodejs/node", "vitejs/vite",
                "expressjs/expressjs.com", "awsdocs/amazon-ecs-developer-guide",
                "awsdocs/amazon-s3-userguide", "awsdocs/iam-user-guide",
                "awsdocs/amazon-ecr-user-guide"}


def approved_source(url):
    if official_url(url):
        return True
    p = urlparse(url)
    parts = p.path.strip("/").split("/")
    return (p.scheme == "https" and p.hostname == "raw.githubusercontent.com"
            and p.port in (None, 443) and not p.username and not p.query
            and len(parts) > 3 and "/".join(parts[:2]) in SOURCE_REPOS
            and re.fullmatch(r"[a-f0-9]{40}", parts[2]) is not None
            and ".." not in parts)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Redirect rejected; review and use the canonical URL")


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.depth = 0
        self.current = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer", "header"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "pre", "li", "h1", "h2", "h3", "h4"}:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer", "header"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "pre", "li", "h1", "h2", "h3", "h4"}:
            self.depth = max(0, self.depth - 1)
            if not self.depth:
                text = " ".join("".join(self.current).split())
                if text:
                    self.items.append(text)
                self.current = []

    def handle_data(self, data):
        if self.depth and not self.hidden:
            self.current.append(data)


def fetch(url):
    if not approved_source(url):
        raise ValueError("Only allowlisted official HTTPS documentation may be fetched")
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, headers={"User-Agent": "ReCoder-Docs/1.0", "Accept": "text/html"})
    with opener.open(request, timeout=20) as response:
        if not any(t in response.headers.get("Content-Type", "") for t in ("text/html", "text/plain")):
            raise ValueError("Expected text documentation")
        data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("Document exceeds 2 MB limit")
        return data.decode("utf-8", "replace")


def update_revision(entry: dict) -> dict:
    """Advance only reviewed repository/ref pairs while their license is unchanged."""
    if not entry.get("tracking_ref"):
        return entry
    import requests
    repo, ref = entry["repository"], entry["tracking_ref"]
    if repo not in SOURCE_REPOS or not re.fullmatch(r"[\w.-]+", ref):
        raise ValueError("Unapproved tracked repository/ref")
    response = requests.get(f"https://api.github.com/repos/{repo}/commits/{ref}", timeout=20,
                            allow_redirects=False)
    response.raise_for_status()
    metadata = response.json()
    sha = metadata["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Invalid repository revision")
    license_source = f"https://raw.githubusercontent.com/{repo}/{sha}/{entry['license_path']}"
    license_text = fetch(license_source)
    if hashlib.sha256(license_text.encode()).hexdigest() != entry["license_sha256"]:
        raise ValueError("Source license changed; review required: " + repo)
    return {**entry, "source_url": f"https://raw.githubusercontent.com/{repo}/{sha}/{entry['source_path']}",
            "version": sha, "source_date": metadata["commit"]["committer"]["date"],
            "license_url": f"https://github.com/{repo}/blob/{sha}/{entry['license_path']}",
            "license_source_url": license_source}


def refresh(entries: list[dict], fetcher=fetch, *, track=False) -> list[dict]:
    output = []
    for entry in entries:
        if entry.get("fulltext_status") != "approved":
            output.append(entry)
            continue
        if (entry.get("license_status") != "reviewed" or not entry.get("license_url")
            or not entry.get("attribution") or not entry.get("reviewed_at")):
            raise ValueError("Full-text ingestion requires license review and attribution: " + entry["id"])
        if not official_url(entry["url"]):
            raise ValueError("Not an official source")
        if track:
            entry = update_revision(entry)
        source = entry.get("source_url", entry["url"])
        if not approved_source(source):
            raise ValueError("Unapproved source snapshot")
        html = fetcher(source)
        if entry.get("format") == "markdown":
            sections = re.split(r"(?m)(?=^#{1,6} )", html)
            selectors = entry.get("sections", [])
            if selectors:
                sections = [s for s in sections if s.strip() and any(k.lower() in s.splitlines()[0].lower() for k in selectors)]
            items = [p.strip() for s in sections for p in re.split(r"\n\s*\n", s) if p.strip()]
        else:
            parser = Paragraphs()
            parser.feed(html)
            items = parser.items
        paragraphs = []
        chunk = ""
        for item in items:
            for piece in (item[i:i+1400] for i in range(0, len(item), 1400)):
                if chunk and len(chunk) + len(piece) + 2 > 1600:
                    paragraphs.append(chunk)
                    chunk = ""
                chunk += ("\n\n" if chunk else "") + piece
        if chunk:
            paragraphs.append(chunk)
        if not paragraphs:
            raise ValueError("Document has no extractable paragraphs: " + entry["id"])
        revision = hashlib.sha256(html.encode()).hexdigest()
        for i, text in enumerate(paragraphs):
            output.append({**entry, "id": f"{entry['id']}:{i}", "text": text, "kind": "excerpt",
                           "version": revision, "fetched_at": datetime.now(timezone.utc).isoformat()})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--update", action="store_true", help="Advance tracked sources if their reviewed license is unchanged")
    args = parser.parse_args()
    if args.manifest.resolve() == args.output.resolve():
        raise SystemExit("Use separate source manifest and generated corpus paths")
    data = refresh(json.loads(args.manifest.read_text(encoding="utf-8")), track=args.update)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(args.output)


if __name__ == "__main__":
    main()
