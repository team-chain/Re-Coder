"""Explicit local retrieval assets and distinct repair tiers, without downloads at runtime."""
import json
import os
from functools import lru_cache
from pathlib import Path


def settings():
    path = Path(os.getenv('RECODER_REPAIR_CONFIG', str(Path(__file__).with_name('runtime.json'))))
    return json.loads(path.read_text()) if path.exists() else {}


@lru_cache(maxsize=2)
def _index(corpus, corpus_stamp, model):
    from grounded_repair.knowledge import KnowledgeIndex, LocalEmbedding
    index = KnowledgeIndex.load(Path(corpus) if corpus else None, model_path=model, include_summaries=True)
    return index


def load_index():
    config = settings()
    corpus = os.getenv('RECODER_REPAIR_CORPUS', config.get('corpus', ''))
    model = os.getenv('RECODER_REPAIR_EMBEDDING_MODEL', config.get('embedding_model', ''))
    p = Path(corpus) if corpus else Path(__file__).with_name('sources.json')
    return _index(str(p), (p.stat().st_mtime_ns, p.stat().st_size), model)


def repair_router():
    from llm.provider_router import LLMProviderRouter
    from llm.bedrock_provider import BedrockProvider
    router = LLMProviderRouter()
    config = settings()
    for tier, attribute in [('primary', '_bedrock_sonnet'), ('fast', '_bedrock_haiku')]:
        provider = getattr(router, attribute)
        model = os.getenv('RECODER_REPAIR_' + tier.upper() + '_MODEL', config.get(tier + '_model', ''))
        if not model and isinstance(provider, BedrockProvider):
            model = ('global.anthropic.claude-sonnet-4-5-20250929-v1:0' if tier == 'primary'
                     else 'global.anthropic.claude-haiku-4-5-20251001-v1:0')
        if model:
            # Preserve explicitly selected authentication/provider and AWS region.
            provider.model_id = model
    if 'RECODER_REPAIR_PRICES' not in os.environ and config.get('prices'):
        os.environ['RECODER_REPAIR_PRICES'] = json.dumps(config['prices'])
    return router
