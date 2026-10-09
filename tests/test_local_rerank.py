"""Local rank fusion: bounded authorized inputs, offline loading, and safe fallback."""
from contextlib import ExitStack
import os
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_agent.library import Library
from research_agent.retrieval import Retriever, RERANK_REVISION, RERANK_MODEL, MODEL, REVISION
from research_agent.workbench_store import WorkbenchStore


class LocalRerank(unittest.TestCase):
    def test_bounded_authorized_ranking_and_failed_model_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root=Path(tmp);weights=root/'ranker';weights.mkdir()
            stack.enter_context(patch.dict(os.environ,{'RETRIEVAL_MODE':'lexical','RETRIEVAL_RERANKER_PATH':str(weights),'BGE_DEVICE':'cpu'}))
            store=WorkbenchStore(root/'state.db');lib=Library(store)
            sid=store.save_space({'name':'Current'})['id'];other=store.save_space({'name':'Other'})['id']
            def save(space,title,texts):
                return lib.save(space,{'kind':'document','title':title,'url':'','canonical_id':title,
                    'metadata':{},'data':None,'warnings':[],'boundary':'test original',
                    'chunks':[{'text':text,'page':1,'section':'method','line_start':1,'line_end':1} for text in texts]})[0]
            paper=save(sid,'Requested paper',[f'Authorized needle paragraph {i}: the method reads original evidence before answering.' for i in range(45)])
            save(other,'Forbidden paper',['Forbidden needle content from another research area must remain inaccessible.'])
            engine=Retriever(store,lib)
            query=lambda:engine.retrieve(sid,'needle',artifact_ids=[paper['id']],top_k=5)
            baseline=query();self.assertNotIn('reranking',baseline)
            refs=lambda value:[h['ref_id'] for h in value['results']]
            (weights/'researchagent-revision.txt').write_text(RERANK_REVISION,encoding='utf-8')
            (weights/'config.json').write_text('{}',encoding='utf-8')
            model=Mock();model.predict.side_effect=lambda pairs,**kw:list(range(len(pairs)))
            constructor=Mock(return_value=model)
            stack.enter_context(patch.dict(sys.modules,{'sentence_transformers':SimpleNamespace(CrossEncoder=constructor),'torch':SimpleNamespace(set_num_threads=Mock())}))
            ranked=query()
            self.assertEqual(ranked['reranking']['status'],'ok')
            self.assertEqual(ranked['reranking']['candidates'],40)
            self.assertNotEqual(refs(ranked),refs(baseline))
            pairs=model.predict.call_args.args[0]
            self.assertEqual(len(pairs),40)
            self.assertTrue(all(q=='needle' and 'Authorized needle' in text and 'Forbidden' not in text for q,text in pairs))
            self.assertTrue(constructor.call_args.kwargs['local_files_only'])
            self.assertEqual({h['owner_id'] for h in ranked['results']},{paper['id']})
            self.assertIn('Authorized needle',engine.read(sid,ranked['results'][0]['ref_id'],adjacent=0)['content'])
            model.predict.side_effect=RuntimeError('inference unavailable')
            fallback=query()
            self.assertEqual(fallback['reranking']['status'],'fallback')
            self.assertEqual(refs(fallback),refs(baseline))

    def test_setup_selects_pinned_model_without_runtime_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            for flag,model,revision in [([],MODEL,REVISION),(['--reranker'],RERANK_MODEL,RERANK_REVISION)]:
                folder=Path(tmp)/('reranker' if flag else 'encoder');folder.mkdir()
                download=Mock()
                with patch.dict(sys.modules,{'huggingface_hub':SimpleNamespace(snapshot_download=download)}),patch.object(sys,'argv',['setup_retrieval.py','--model-dir',str(folder),*flag]):
                    runpy.run_path(str(Path(__file__).resolve().parents[1]/'setup_retrieval.py'),run_name='__main__')
                self.assertEqual(download.call_args.args[0],model)
                self.assertEqual(download.call_args.kwargs['revision'],revision)
                self.assertEqual((folder/'researchagent-revision.txt').read_text(encoding='utf-8'),revision)
                if flag:self.assertNotIn('pytorch_model.bin',download.call_args.kwargs['allow_patterns'])


if __name__=='__main__':
    unittest.main()
