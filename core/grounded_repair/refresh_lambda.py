"""Customer-account scheduled refresh. Keeps the previous corpus if refresh fails."""
import json
import os

from grounded_repair.refresh import refresh


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
    documents = refresh(manifest)
    # A single PUT atomically replaces the object. Versioning retains the old one.
    s3.put_object(Bucket=bucket, Key="index/corpus.json",
                  Body=json.dumps(documents, ensure_ascii=False).encode(), ContentType="application/json")
    return {"documents": len(documents)}
