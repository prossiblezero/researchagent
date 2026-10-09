import json
from pathlib import Path
import tempfile
import unittest

from research_agent.context import _compact_read_pair, _tool_pairs, build_context
from research_agent.contracts import Evidence, ModelDecision, Source
from research_agent.library import Library
from research_agent.loop import ResearchAgent, _tool_messages
from research_agent.retrieval import Retriever
from research_agent.workbench_store import WorkbenchStore


class ReadingWindowTests(unittest.TestCase):
    def test_many_document_paragraphs_fit_without_duplicate_catalog_metadata(self):
        title = 'A saved paper with many short paragraphs and a long descriptive academic title ' * 2
        base = '/api/spaces/' + 'a' * 32 + '/materials/' + 'b' * 32 + '/reader#local-L'
        sources = [Source(f'S{i}', title, base + str(i), '') for i in range(1, 62)]
        evidence = [Evidence(f'E{i}', f'S{i}', 'Source sentence. ' * 24,
                             kind=f'local:D{i}-0', title=title, content_hash=str(i).zfill(64))
                    for i in range(1, 62)]
        parts = [{'ref_id': f'D{i}-0', 'evidence_id': f'E{i}', 'source_id': f'S{i}',
                  'section': 'Methods', 'page': i, 'content': evidence[i-1].content}
                 for i in range(1, 61)]
        payload = {'kind': 'read_evidence', 'ok': True, 'document_read': True,
                   'document_complete': True, 'title': title, 'url': sources[0].url,
                   **parts[0], 'document_passages': parts[1:]}
        pair = _tool_messages(ModelDecision('tool_call', tool_name='read_evidence', call_id='doc'),
                              'doc', {'ref_id': 'D1-0', 'document': True}, payload)
        messages = [{'role': 'system', 'content': 'Read original paragraphs and cite their E IDs.'},
                    {'role': 'user', 'content': 'How were the data obtained?'}, *pair]
        original = json.dumps(messages)
        context = build_context(messages, max_context_tokens=9000, output_reserve_tokens=2048,
                                safety_margin_tokens=256, sources=sources, evidence=evidence)
        self.assertFalse(context.error)
        self.assertLessEqual(context.estimated_input_tokens, context.input_limit_tokens)
        self.assertFalse(_tool_pairs(context.messages)[1])
        visible = json.loads(next(m['content'] for m in context.messages if m['role'] == 'tool'))['UNTRUSTED_TOOL_DATA']
        self.assertEqual({p['evidence_id'] for p in [visible, *visible['document_passages']]},
                         {f'E{i}' for i in range(1, 61)})
        self.assertIn('E61', json.dumps(context.messages))  # Older evidence remains usable.
        self.assertFalse(visible['document_complete'])
        self.assertTrue(all(p['content'].startswith('Source sentence.') for p in [visible, *visible['document_passages']]))
        self.assertEqual(json.dumps(messages), original)

        # Subsequent passage reads move the document exchange out of the recent
        # window; its sixty durable bindings must still fit and remain readable.
        for index in (1, 2):
            messages += _tool_messages(ModelDecision('tool_call', tool_name='read_evidence', call_id=f'r{index}'),
                                       f'r{index}', {'ref_id': f'D{index}-0'},
                                       {'kind': 'read_evidence', 'ok': True, **parts[index-1]})
        context = build_context(messages, max_context_tokens=9000, output_reserve_tokens=2048,
                                safety_margin_tokens=256, sources=sources, evidence=evidence)
        self.assertFalse(context.error)
        state = next(json.loads(m['content'])['CONTEXT_STATE'] for m in context.messages
                     if m.get('content', '').startswith('{"CONTEXT_STATE"'))
        self.assertEqual({e['evidence_id'] for e in state['EVIDENCE_MAP']}, {e.evidence_id for e in evidence})
        source_map = {s['source_id']: s for s in state['SOURCE_MAP']}
        for source in sources:
            projected = source_map[source.source_id]
            if 'same_document_as' in projected:
                anchor = source_map[projected['same_document_as']]
                restored_url = anchor['url'].split('#', 1)[0] + '#' + projected['fragment']
                self.assertEqual(restored_url, source.url)
        self.assertFalse(_tool_pairs(context.messages)[1])

    def test_document_read_covers_later_experiments_and_keeps_access_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=WorkbenchStore(root/'db.sqlite')
            space=store.save_space({'name':'Docs'})['id'];other=store.save_space({'name':'Other'})['id']
            library=Library(store)
            texts=['Introduction: three preprocessing levels.','Methods description.',
                   'Additional experiments: level four uses TextRank.','Conclusion: four levels.']
            record={'kind':'paper','title':'Levels','url':'','canonical_id':'levels','metadata':{},'data':None,
                    'chunks':[{'text':t,'page':i+1,'section':str(i),'line_start':1,'line_end':2} for i,t in enumerate(texts)],'warnings':[],'boundary':'test'}
            library.save(space,record);retriever=Retriever(store,library);retriever._dense_attempted=True
            rows=retriever.sync(space);ref=next(iter(rows))
            p=retriever.read(space,ref,document=True)
            self.assertTrue(p['document_complete'])
            self.assertEqual({x['content'] for x in [p,*p['document_passages']]},set(texts))
            with self.assertRaises(ValueError):retriever.read(other,'x:'+space+':-:'+ref,document=True)
            cross=retriever.read(other,'x:'+space+':-:'+ref,document=True,allow_cross=True)
            self.assertTrue(all(x['ref_id'].startswith('x:'+space+':-:') for x in [cross,*cross['document_passages']]))
            test=self
            class Model:
                name='document-fixture'
                def complete(self,messages,tools):
                    payload=next((json.loads(m['content'])['UNTRUSTED_TOOL_DATA'] for m in reversed(messages) if m['role']=='tool'),None)
                    if not payload:return ModelDecision('tool_call',tool_name='read_evidence',call_id='r',arguments={'ref_id':ref,'document':True})
                    parts=[payload,*payload['document_passages']]
                    test.assertEqual({x['content'] for x in parts},set(texts))
                    test.assertEqual(len({x['evidence_id'] for x in parts}),4)
                    last=next(x for x in parts if 'TextRank' in x['content'])
                    return ModelDecision('final',content='Fourth level uses TextRank. ['+last['evidence_id']+']')
            run=ResearchAgent(None,Model(),root/'traces',retrieval=retriever,space_id=space,allow_external=False).run('What levels?')
            self.assertEqual(run.status,'ok');self.assertEqual(run.tool_calls,1)
            pair=_tool_messages(ModelDecision('tool_call',tool_name='read_evidence',call_id='r'),'r',{},
                 {'kind':'read_evidence','document_read':True,'document_complete':True,'content':'short',
                  'document_passages':[{'content':'longer original '*100,'evidence_id':'E4'}]})
            compact=json.loads(_compact_read_pair(pair,'q',6)[-1]['content'])['UNTRUSTED_TOOL_DATA']
            self.assertFalse(compact['document_complete'])
            self.assertEqual(compact['document_passages'][0]['next_offset'],6)

    def test_default_read_opens_hit_and_explicit_offsets_still_control_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkbenchStore(root / 'state.sqlite')
            space = store.save_space({'name': 'Documents'})['id']
            other = store.save_space({'name': 'Other'})['id']
            library = Library(store)
            original = 'Background. ' * 240 + 'ZXMarker measures 0.734 MAP. ' + 'Following context. ' * 200
            library.save(space, {'kind': 'paper', 'title': 'Marker paper', 'url': '', 'canonical_id': 'marker',
                         'metadata': {}, 'data': None, 'chunks': [{'text': original, 'page': 2, 'section': 'Results', 'line_start': 1, 'line_end': 3}],
                         'warnings': [], 'boundary': 'test'})
            retriever = Retriever(store, library)
            retriever._dense_attempted = True
            rows = retriever.sync(space)
            hit = next(row for row in rows.values() if row['start_offset'] == 2800)
            ref = hit['ref_id']
            for style in ('ref', 'preview', 'cross-space'):
                with self.subTest(style=style):
                    test = self
                    class Reader:
                        name = 'scripted-read-contract'
                        count = 0
                        continuation = None

                        def complete(model, messages, tools):
                            payload = next((json.loads(m['content'])['UNTRUSTED_TOOL_DATA']
                                            for m in reversed(messages) if m['role'] == 'tool'), None)
                            if not payload and style == 'preview':
                                return ModelDecision('tool_call', tool_name='retrieve',
                                                     arguments={'query': 'ZXMarker', 'top_k': 8}, call_id='retrieve')
                            if payload is None or payload['kind'] == 'retrieve':
                                selected = ('x:' + space + ':-:' + ref) if style == 'cross-space' else ref
                                if style == 'preview':
                                    selected = next(h['evidence_id'] for h in payload['results'] if h['ref_id'] == ref)
                                return ModelDecision('tool_call', tool_name='read_evidence',
                                                     arguments={'ref_id': selected, 'adjacent': 0}, call_id='matched')
                            test.assertTrue(payload['ok'])
                            model.count += 1
                            if model.count == 1:
                                test.assertEqual(payload['start_offset'], 2800)
                                test.assertEqual(payload['content'], original[2800:4600])
                                test.assertIn('ZXMarker', payload['content'])
                                model.continuation = payload['next_offset']
                                return ModelDecision('tool_call', tool_name='read_evidence', call_id='head',
                                                     arguments={'ref_id': payload['evidence_id'], 'offset': 0,
                                                                'max_chars': 64, 'adjacent': 0})
                            if model.count == 2:
                                test.assertEqual(payload['start_offset'], 0)
                                test.assertEqual(payload['content'], original[:64])
                                return ModelDecision('tool_call', tool_name='read_evidence', call_id='continue',
                                                     arguments={'ref_id': payload['evidence_id'], 'offset': model.continuation,
                                                                'max_chars': 300, 'adjacent': 0})
                            test.assertEqual(payload['start_offset'], model.continuation)
                            test.assertEqual(payload['content'], original[model.continuation:model.continuation + 300])
                            return ModelDecision('final', content='INSUFFICIENT: offline contract only.')
                    model = Reader()
                    result = ResearchAgent(None, model, root / 'traces', retrieval=retriever,
                                           space_id=other if style == 'cross-space' else space,
                                           allow_external=False, max_tool_calls=5).run('Open the matching passage.')
                    self.assertEqual(model.count, 3)
                    self.assertEqual(result.network_requests, 0)

    def test_long_document_pages_are_contiguous_and_never_claim_full_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=WorkbenchStore(root/'state.sqlite')
            space=store.save_space({'name':'Long document'})['id'];library=Library(store)
            texts=[str(i)+': '+('paragraph context '*700) for i in range(5)]
            library.save(space,{'kind':'paper','title':'Long','url':'','canonical_id':'long','metadata':{},'data':None,
                'chunks':[{'text':t,'page':i+1,'section':'Body','line_start':1,'line_end':2} for i,t in enumerate(texts)],
                'warnings':[],'boundary':'fixture'})
            retriever=Retriever(store,library);retriever._dense_attempted=True
            rows=retriever.sync(space);ref=next(iter(rows));seen=[];previous=None
            while ref:
                result=retriever.read(space,ref,document=True)
                self.assertFalse(result['document_complete'])
                self.assertEqual(result['document_previous_ref'],previous)
                parts=[result,*result['document_passages']]
                self.assertLessEqual(sum(len(p['content']) for p in parts),24000)
                seen.extend(p['content'] for p in parts)
                previous=ref;ref=result['document_next_ref']
                self.assertLessEqual(len(seen),5)
            self.assertEqual(seen,texts)

    def test_compaction_keeps_continuation_relative_to_the_matched_offset(self):
        decision = ModelDecision('tool_call', tool_name='read_evidence', call_id='r')
        payload = {'ok': True, 'kind': 'read_evidence', 'content': 'x' * 1800,
                   'start_offset': 2800, 'end_offset': 4600, 'next_offset': 4600, 'total_chars': 7000}
        pair = _tool_messages(decision, 'r', {'ref_id': 'D1-2'}, payload)
        compact = _compact_read_pair(pair, 'question', 60)
        visible = json.loads(compact[-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertEqual(visible['start_offset'], 2800)
        self.assertEqual(visible['end_offset'], 2860)
        self.assertEqual(visible['next_offset'], 2860)
        self.assertEqual(len(visible['content']), 60)


if __name__ == '__main__':
    unittest.main()
