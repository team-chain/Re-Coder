"""Customer-account scheduled refresh. Keeps the previous corpus if refresh fails."""
import json
import os
import hashlib

from grounded_repair.refresh import refresh, fetch


def handler(event, context):
    import boto3
    s3 = boto3.client("s3")
    bucket = os.environ["DOCUMENT_BUCKET"]
    source = s3.get_object(Bucket=bucket, Key="sources/manifest.json")["Body"].read(1_000_001)
    if len(source) > 1_000_000:
        raise ValueError("Source manifest exceeds size limit")
    manifest = json.loads(source)
    if not isinstance(manifest, list) or len(manifest) > 20:
        raise ValueError("At most 20 reviewed sources per refresh")
    documents = refresh(manifest, track=True)
    # Retain redistribution notices alongside the published dataset. Content
    # addressed objects are immutable; publish the new corpus only after them.
    notices = {}
    for entry in manifest:
        source = entry.get("license_source_url")
        digest = entry.get("license_sha256")
        if entry.get("fulltext_status") == "approved" and source and digest:
            if digest not in notices:
                text = fetch(source)
                if hashlib.sha256(text.encode()).hexdigest() != digest:
                    raise ValueError("Reviewed license content changed")
                notices[digest] = text
    for digest, text in notices.items():
        s3.put_object(Bucket=bucket, Key=f"index/licenses/{digest}.txt",
                      Body=text.encode(), ContentType="text/plain; charset=utf-8")
    # A single PUT atomically replaces the object. Versioning retains the old one.
    s3.put_object(Bucket=bucket, Key="index/corpus.json",
                  Body=json.dumps(documents, ensure_ascii=False).encode(), ContentType="application/json")
    return {"documents": len(documents)}
