"""
ReCoder Gateway — /deploy/s3 핸들러.

학생이 본인 토큰으로 정적 파일(HTML/JS/CSS…)을 운영자 S3 정적 버킷에 올리고
공개 URL을 돌려받는다. 학생은 AWS 자격증명이 전혀 필요 없다(Bedrock 게이트웨이와 동일 패턴).

요청 (POST):
  헤더: Authorization: Bearer <학생토큰>
  바디: { "project": "tetris",
          "files": [ {"path": "index.html", "content": "<html>…"} , ... ] }
        또는 단일 파일: { "project": "tetris", "filename": "tetris.html", "content": "..." }
응답: { "url": "http://<bucket>.s3-website.<region>.amazonaws.com/<student>/<project>/",
        "files": ["index.html", ...], "count": N }
"""
from __future__ import annotations

import json
import os
import re

import boto3

try:
    import common
except ImportError:
    from . import common  # type: ignore

DEPLOY_BUCKET = os.environ.get("GW_DEPLOY_BUCKET", "")
MAX_FILES = 30
MAX_BYTES = 3_000_000   # 파일당 3MB

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".json": "application/json", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".wasm": "application/wasm",
}

_s3 = None
def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=common.REGION)
    return _s3


def _slug(name: str, fallback: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    return s[:40] or fallback


def _safe_path(p: str) -> str:
    p = (p or "").replace("\\", "/").lstrip("/")
    parts = [seg for seg in p.split("/") if seg and seg != ".."]
    return "/".join(parts)


def _content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _bearer(event) -> str:
    h = event.get("headers") or {}
    auth = h.get("authorization") or h.get("Authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()


def handler(event, context):
    try:
        if not DEPLOY_BUCKET:
            return common.resp(500, {"error": "no_bucket", "message": "배포 버킷이 설정되지 않았습니다."})

        token = _bearer(event)
        item = common.authenticate(token)
        sid = item["pk"].split("#", 1)[1]
        common.check_rate(sid, int(item.get("rpm", common.DEF_RPM)))

        body = json.loads(event.get("body") or "{}")
        project = _slug(body.get("project", ""), "site")

        # 파일 수집: files[] 우선, 없으면 단일 filename/content
        files = body.get("files")
        if not files:
            fn = body.get("filename") or "index.html"
            files = [{"path": fn, "content": body.get("content", "")}]
        if not isinstance(files, list) or not files:
            return common.resp(400, {"error": "no_files", "message": "업로드할 파일이 없습니다."})
        if len(files) > MAX_FILES:
            return common.resp(400, {"error": "too_many", "message": f"파일은 최대 {MAX_FILES}개까지."})

        # 진입 파일(index.html) 결정 — 없고 단일 .html 이면 그걸 index.html 로도 올린다.
        norm = []
        html_paths = []
        for f in files:
            path = _safe_path(f.get("path") or f.get("filename") or "")
            content = f.get("content")
            if not path or content is None:
                continue
            data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            if len(data) > MAX_BYTES:
                return common.resp(400, {"error": "too_large", "message": f"{path} 가 너무 큽니다."})
            norm.append((path, data))
            if path.lower().endswith((".html", ".htm")):
                html_paths.append(path)
        if not norm:
            return common.resp(400, {"error": "no_files", "message": "유효한 파일이 없습니다."})

        prefix = f"{sid}/{project}"
        client = _s3_client()
        uploaded = []
        has_index = any(p.lower() == "index.html" for p, _ in norm)
        for path, data in norm:
            key = f"{prefix}/{path}"
            client.put_object(Bucket=DEPLOY_BUCKET, Key=key, Body=data,
                              ContentType=_content_type(path), CacheControl="no-cache")
            uploaded.append(path)
        # index.html 이 없으면 단일/첫 HTML 을 index.html 로 복제해 폴더 URL 이 바로 열리게.
        if not has_index and html_paths:
            src = next((d for p, d in norm if p == html_paths[0]), None)
            if src is not None:
                client.put_object(Bucket=DEPLOY_BUCKET, Key=f"{prefix}/index.html",
                                  Body=src, ContentType="text/html; charset=utf-8",
                                  CacheControl="no-cache")
                uploaded.append("index.html")

        url = f"http://{DEPLOY_BUCKET}.s3-website.{common.REGION}.amazonaws.com/{prefix}/"
        return common.resp(200, {"url": url, "project": project,
                                 "files": uploaded, "count": len(uploaded)})

    except common.QuotaError as qe:
        code = 429 if qe.code in ("rate_limited",) else 401
        return common.resp(code, {"error": qe.code, "message": qe.message})
    except Exception as exc:  # noqa: BLE001
        return common.resp(500, {"error": "internal", "message": str(exc)})
