"""Explicit, resumable setup. Never called from a conversation request."""
import argparse
from pathlib import Path
from huggingface_hub import snapshot_download
from research_agent.retrieval import MODEL, REVISION, RERANK_MODEL, RERANK_REVISION, RERANK_FOLDER

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-dir',type=Path)
    parser.add_argument('--reranker',action='store_true',help='Prepare the optional local passage reranker')
    args=parser.parse_args()
    model,revision=(RERANK_MODEL,RERANK_REVISION) if args.reranker else (MODEL,REVISION)
    directory=args.model_dir or Path('data/models')/(RERANK_FOLDER if args.reranker else 'bge-m3')
    patterns=['config.json','config_sentence_transformers.json','modules.json','sentence_bert_config.json',
            'sentencepiece.bpe.model','special_tokens_map.json','tokenizer.json','tokenizer_config.json',
            '1_Pooling/*','2_Normalize/*','model.safetensors','pytorch_model.bin']
    if args.reranker:
        patterns=['README.md','config.json','model.safetensors','sentencepiece.bpe.model',
            'special_tokens_map.json','tokenizer.json','tokenizer_config.json']
    snapshot_download(model,revision=revision,local_dir=str(directory),allow_patterns=patterns)
    (directory/'researchagent-revision.txt').write_text(revision,encoding='utf-8')
    print('Prepared pinned model:',model,directory)
