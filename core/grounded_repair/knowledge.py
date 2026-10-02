"""BM25 + local dense retrieval. No implicit downloads or paid embeddings."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

HOSTS = {"docs.docker.com", "docs.npmjs.com", "nodejs.org", "vite.dev",
         "expressjs.com", "docs.aws.amazon.com"}
STOPWORDS = set("a an the to of in on for from and or with by is are was be not this that it at as".split())


@dataclass(frozen=True)
class Passage:
    id: str
    title: str
    url: str
    text: str
    version: str
    reviewed_at: str
    kind: str = "authored_summary"
    license_url: str = ""
    attribution: str = ""
    fetched_at: str = ""
    source_url: str = ""
    source_date: str = ""
    license: str = ""

    def to_dict(self):
        return asdict(self)


def official_url(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and p.hostname in HOSTS and not p.username and p.port in (None, 443)


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w]+(?:[.:@/-][\w]+)*", text.lower()) if t not in STOPWORDS]


class LocalEmbedding:
    """Supply a predownloaded sentence-transformers model directory explicitly."""
    def __init__(self, path: str):
        from sentence_transformers import SentenceTransformer
        root = Path(path).resolve()
        if not root.is_dir():
            raise ValueError("Embedding model must be an existing local directory")
        self.identity = str(root) + ":" + hashlib.sha256(json.dumps([
            (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()
        ]).encode()).hexdigest()
        options = root / "recoder-embedding.json"
        config = json.loads(options.read_text()) if options.exists() else {}
        self.query_prefix = config.get("query_prefix", "")
        self.passage_prefix = config.get("passage_prefix", "")
        self.min_cosine = float(config.get("min_cosine", .35))
        self.model = SentenceTransformer(path, local_files_only=True, trust_remote_code=False,
                                        device=os.getenv("RECODER_REPAIR_EMBEDDING_DEVICE", "cpu"))

    def __call__(self, texts: list[str]):
        return self.model.encode([self.passage_prefix + t for t in texts], normalize_embeddings=True).tolist()

    def query(self, text: str):
        return self.model.encode([self.query_prefix + text], normalize_embeddings=True).tolist()[0]


class KnowledgeIndex:
    def __init__(self, passages: list[Passage], embed=None):
        if any(not official_url(p.url) or not p.text or len(p.text) > 2400 for p in passages):
            raise ValueError("Invalid official document passage")
        if len({p.id for p in passages}) != len(passages):
            raise ValueError("Duplicate passage id")
        self.passages = passages
        self.embed = embed
        self.mode = "hybrid" if embed else "keyword_only"
        self.version = hashlib.sha256(json.dumps({"passages": [p.to_dict() for p in passages],
            "embedding": getattr(embed, "identity", "injected" if embed else "none"),
            "retriever": "bm25-rrf-exact-summary-anchor-v3"}, sort_keys=True).encode()).hexdigest()
        self.terms = [Counter(tokens(p.title + " " + p.text)) for p in passages]
        self.vectors = embed([p.title + "\n" + p.text for p in passages]) if embed and passages else []

    @classmethod
    def load(cls, path: Path | None = None, model_path: str | None = None, *, include_summaries=False):
        path = path or Path(__file__).with_name("sources.json")
        entries = json.loads(path.read_text(encoding="utf-8"))
        if include_summaries:
            ids = {entry["id"] for entry in entries}
            entries.extend(entry for entry in json.loads(Path(__file__).with_name("sources.json").read_text(encoding="utf-8"))
                           if entry["id"] not in ids)
        passages = []
        for entry in entries:
            # Authored summaries are our text. Copied excerpts need a recorded review.
            if entry.get("kind") != "authored_summary" and (
                entry.get("license_status") != "reviewed" or not entry.get("license_url")
                or not entry.get("attribution")
            ):
                raise ValueError("Unreviewed document license: " + entry["id"])
            passages.append(Passage(**{k: entry[k] for k in Passage.__dataclass_fields__ if k in entry}))
        model = model_path if model_path is not None else os.getenv("RECODER_REPAIR_EMBEDDING_MODEL", "")
        return cls(passages, LocalEmbedding(model) if model else None)

    def search(self, query: str, keywords: list[str], top_k: int = 4) -> list[Passage]:
        if not self.passages:
            return []
        query_terms = set(tokens(query))
        n = len(self.terms)
        avg = sum(sum(t.values()) for t in self.terms) / n or 1
        df = {t: sum(t in d for d in self.terms) for t in query_terms}
        lexical = []
        for i, d in enumerate(self.terms):
            score = 0.0
            for term in query_terms:
                f = d[term]
                score += math.log(1 + (n - df[term] + .5) / (df[term] + .5)) * f * 2.2 / (f + 1.2 * (.25 + .75 * sum(d.values()) / avg))
            # Error codes must match whole tokens; no substring collisions.
            exact = sum(k.lower() in d for k in keywords)
            if score > 0:
                lexical.append((i, score + exact * 10))
        lexical.sort(key=lambda x: (-x[1], self.passages[x[0]].id))
        scores = {i: 1 / (60 + rank) for rank, (i, _) in enumerate(lexical, 1)}
        if self.embed:
            q = self.embed.query(query) if hasattr(self.embed, "query") else self.embed([query])[0]
            dense = []
            for i, v in enumerate(self.vectors):
                if len(q) != len(v):
                    raise ValueError("Embedding dimensions changed")
                norm = math.sqrt(sum(x*x for x in q) * sum(x*x for x in v))
                cosine = sum(a*b for a, b in zip(q, v)) / norm if norm else 0
                if cosine >= getattr(self.embed, "min_cosine", .35):
                    dense.append((i, cosine))
            for rank, (i, _) in enumerate(sorted(dense, key=lambda x: -x[1]), 1):
                scores[i] = scores.get(i, 0) + 1 / (60 + rank)
            # Short reviewed topic summaries provide a stable document-level
            # anchor for cross-language symptoms; code-heavy excerpts otherwise
            # crowd them out. Reserve one of the four evidence slots for the
            # strongest semantic summary, leaving literal error codes first.
            anchors = [(i, similarity) for i, similarity in dense
                       if self.passages[i].kind == "authored_summary"]
            if anchors:
                anchor = max(anchors, key=lambda item: item[1])[0]
                scores[anchor] = scores.get(anchor, 0) + 1 / 30
        # A literal error-code match must not lose to generic paragraphs that
        # merely appear in both retrieval lists (RRF alone can do that).
        ranked = sorted(scores, key=lambda i: (
            -sum(k.lower() in self.terms[i] for k in keywords), -scores[i], self.passages[i].id))
        return [self.passages[i] for i in ranked[:top_k]]
