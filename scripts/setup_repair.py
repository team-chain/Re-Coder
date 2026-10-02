"""Explicitly download the MIT multilingual E5 model and configure local hybrid retrieval.
Usage: python scripts/setup_repair.py --assets /path/to/assets --config /path/to/repair.json
Point VS Code recoder.repair.configPath at that config; restart the Core afterwards.
"""
import argparse
import json
from pathlib import Path
from huggingface_hub import snapshot_download

ROOT=Path(__file__).resolve().parents[1]
MODEL='intfloat/multilingual-e5-small'
# Pin resolved model revision so repeated setup uses identical weights.
REVISION='614241f622f53c4eeff9890bdc4f31cfecc418b3'

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--assets',type=Path,required=True);p.add_argument('--config',type=Path,required=True);args=p.parse_args()
 root=args.assets.expanduser().resolve();root.mkdir(parents=True,exist_ok=True)
 model=root/'multilingual-e5-small'
 snapshot_download(MODEL,revision=REVISION,local_dir=model,cache_dir=root/'hub-cache',
                   allow_patterns=['*.json','*.safetensors','*.txt','*.model','README.md'],ignore_patterns=['onnx/*','openvino/*'])
 (model/'recoder-embedding.json').write_text(json.dumps({'model':MODEL,'revision':REVISION,'query_prefix':'query: ','passage_prefix':'passage: ','min_cosine':0.7,'license':'MIT'}))
 # The checked-in corpus has a reviewed, pinned source and notices per passage.
 config={'embedding_model':str(model),'corpus':str(ROOT/'benchmarks/repair/corpus.json'),
         'prices':json.loads((ROOT/'benchmarks/repair/prices.json').read_text())['models']}
 args.config.parent.mkdir(parents=True,exist_ok=True);args.config.write_text(json.dumps(config,indent=2))
 print('Hybrid retrieval configured:',args.config.resolve())

if __name__=='__main__':main()
