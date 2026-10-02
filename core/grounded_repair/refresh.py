"""Refresh explicitly reviewed official sources, usable locally or in Lambda.

The shipped catalog contains original summaries, not copied documentation. Full
text is ingested only from an operator-reviewed manifest with attribution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from grounded_repair.knowledge import official_url


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
    if not official_url(url):
        raise ValueError("Only allowlisted official HTTPS documentation may be fetched")
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "ReCoder-Docs/1.0", "Accept": "text/html"})
    with opener.open(request, timeout=20) as response:
        if "text/html" not in response.headers.get("Content-Type", ""):
            raise ValueError("Expected HTML documentation")
        data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("Document exceeds 2 MB limit")
        return data.decode("utf-8", "replace")


def refresh(entries: list[dict], fetcher=fetch) -> list[dict]:
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
        html = fetcher(entry["url"])
        parser = Paragraphs()
        parser.feed(html)
        paragraphs = [s for p in parser.items for s in (p[i:i+1600] for i in range(0, len(p), 1600))]
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
    args = parser.parse_args()
    if args.manifest.resolve() == args.output.resolve():
        raise SystemExit("Use separate source manifest and generated corpus paths")
    data = refresh(json.loads(args.manifest.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(args.output)


if __name__ == "__main__":
    main()
