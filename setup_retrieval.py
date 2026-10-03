"""Explicit, resumable setup. Never called from a conversation request."""
import argparse
from pathlib import Path
from huggingface_hub import snapshot_download
from research_agent.retrieval import MODEL, REVISION

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-dir',type=Path,default=Path('data/models/bge-m3'))
    args=parser.parse_args()
    snapshot_download(MODEL,revision=REVISION,local_dir=str(args.model_dir),
        allow_patterns=['config.json','config_sentence_transformers.json','modules.json','sentence_bert_config.json',
            'sentencepiece.bpe.model','special_tokens_map.json','tokenizer.json','tokenizer_config.json',
            '1_Pooling/*','2_Normalize/*','model.safetensors','pytorch_model.bin'])
    (args.model_dir/'researchagent-revision.txt').write_text(REVISION,encoding='utf-8')
    print('Prepared pinned BGE-M3:',args.model_dir)
