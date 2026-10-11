"""Regressions for candidate acquisition, evidence retention and bounded delivery."""
import json
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from http.client import IncompleteRead

from research_agent.contracts import Evidence, ModelDecision, ReadResponse, SearchResponse
from research_agent.context import _compact_tool_pair, build_context, ContextCheckpoint
from research_agent.library import MaterialReader
from research_agent.loop import ResearchAgent, _tool_messages
from research_agent.materials import read_material
from research_agent.models import OpenAICompatibleModel
from research_agent.paper_relations import discovery_progress
from research_agent.search import FixtureReader, _text_response
from research_agent.verify import answer_blocks, apply_answer_patch, repair_message


class DiscoveryRecoveryTests(unittest.TestCase):
    def test_final_only_composition_uses_originals_without_tool_transcripts(self):
        calls = []
        class Model:
            name = 'composition-boundary-fixture'
            usage_purpose = 'answer'
            def complete(model, messages, tools):
                calls.append(messages)
                if len(calls) == 1:
                    return ModelDecision('tool_call', tool_name='search', query='fact')
                if len(calls) == 2:
                    return ModelDecision('tool_call', tool_name='read', url='https://example.org/paper')
                self.assertFalse(tools)
                self.assertEqual([m['role'] for m in messages], ['system', 'user'])
                self.assertNotIn('tool_calls', json.dumps(messages))
                data=json.loads(messages[-1]['content'].split('\n', 1)[1])
                self.assertEqual([e['evidence_id'] for e in data['evidence']], ['E2'])
                self.assertEqual(data['evidence'][0]['content'], 'Original verified fact.')
                return ModelDecision('final', content='Original verified fact. [E2]')
        search=Mock();search.search.return_value=SearchResponse(True, 'fact', [
            {'title':'Paper', 'url':'https://example.org/paper', 'snippet':'Unverified preview'}])
        reader=Mock();reader.read.return_value=ReadResponse(True, 'https://example.org/paper', 'Original verified fact.')
        with tempfile.TemporaryDirectory() as folder, patch('research_agent.loop.check_answer', return_value={'ready':True, 'claims':[]}):
            result=ResearchAgent(search, Model(), folder, reader=reader, max_tool_calls=2,
                                 max_rounds=4, answer_verification=True).run('Explain the fact')
        self.assertEqual(result.termination, 'model_final')
        self.assertEqual(result.tool_attempts, 2)

    def test_resume_rejected_tool_markup_composes_again_without_replaying_it(self):
        states=[]
        model=Mock(name='markup-resume-fixture')
        model.name='markup-resume-fixture';model.request_deadline=None
        model.complete.return_value=ModelDecision('final',content='<tool_call>invalid</tool_call>')
        with tempfile.TemporaryDirectory() as folder:
            first=ResearchAgent(Mock(),model,Path(folder)/'first',max_tool_calls=0,max_rounds=3,
                                answer_verification=True,state_callback=states.append).run('Explain the method')
            self.assertEqual(first.termination,'invalid_answer_format')
            model.complete.reset_mock();model.complete.return_value=ModelDecision('final',content='Available evidence is insufficient.')
            resumed=ResearchAgent(Mock(),model,Path(folder)/'resumed',max_tool_calls=0,max_rounds=3,answer_verification=True)
            resumed.resume_state=states[-1]
            with patch('research_agent.loop.check_answer',return_value={'ready':True,'claims':[]}):
                result=resumed.run('Explain the method')
        self.assertEqual(result.termination,'model_final')
        self.assertEqual(model.complete.call_count,1)
        self.assertEqual(result.tool_attempts,0)

    def test_tool_markup_final_gets_one_correction_without_execution_or_verification(self):
        markup='<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="search">invented call</｜｜DSML｜｜ invoke></｜｜DSML｜｜ calls>'
        for corrected in (True, False):
            with self.subTest(corrected=corrected), tempfile.TemporaryDirectory() as folder:
                model=Mock()
                model.name='format-fixture'
                model.request_deadline=None
                model.complete.side_effect=[ModelDecision('final',content=markup),
                    ModelDecision('final',content='Available evidence is insufficient.' if corrected else markup)]
                search=Mock()
                with patch('research_agent.loop.check_answer',return_value={'ready':True,'claims':[]}) as checker:
                    result=ResearchAgent(search,model,folder,max_tool_calls=0,max_rounds=3,answer_verification=True).run('Verify the papers')
                self.assertNotIn('DSML',result.answer)
                self.assertEqual(model.complete.call_count,2)
                self.assertEqual(checker.call_count,1 if corrected else 0)
                self.assertFalse(search.mock_calls)
                self.assertEqual(result.termination,'model_final' if corrected else 'invalid_answer_format')

    def test_optional_compaction_transport_failure_does_not_multiply_retries(self):
        model = Mock()
        model.complete.side_effect = IncompleteRead(b'partial', 100)
        events = []
        checkpoint = ContextCheckpoint(model, 1500, events.append)
        messages = [{'role': 'user', 'content': 'Read original papers'}]
        for i in range(4):
            messages += _tool_messages(ModelDecision('tool_call', tool_name='read'), str(i), {},
                                      {'kind': 'read', 'ok': True, 'content': 'original ' * 500})
        output = checkpoint.project('system', messages, [], 'Read original papers', [])
        self.assertEqual(output[1:], messages)
        checkpoint.project('system', messages * 2, [], 'Read original papers', [])
        self.assertEqual(model.complete.call_count, 1)
        self.assertEqual(events[0]['kind'], 'compression_failed')

    def test_repair_transport_failure_delivers_verified_partial_and_keeps_resume(self):
        url = 'https://example.org/source'
        search = Mock()
        search.search.return_value = SearchResponse(True, 'fact', [{'url': url, 'title': 'Original', 'snippet': 'preview'}])
        reader = Mock()
        reader.read.return_value = ReadResponse(True, url, 'Known fact.')
        calls, states = [], []
        class Model:
            name = 'repair-interruption-fixture'
            supports_answer_verification = True
            usage_purpose = 'answer'
            def complete(self, messages, tools):
                if self.usage_purpose == 'answer_verification':
                    data = json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                    return ModelDecision('final', content=json.dumps({'requirements': [{'id': 'R1', 'requirement': 'Mechanism',
                        'addressed': False, 'abstention_supported': False, 'reason': 'Read mechanism next',
                        'block_ids': [b['block_id'] for b in data['blocks']]}], 'blocks': [
                        {'block_id': b['block_id'], 'kind': 'uncertainty' if 'unknown' in b['text'] else 'fact',
                         'supported': True, 'reason': 'checked', 'evidence_ids': re.findall(r'\[(E\d+)\]', b['text'])}
                        for b in data['blocks']]}))
                calls.append(True)
                if len(calls) == 1:return ModelDecision('tool_call', tool_name='search', query='fact')
                if len(calls) == 2:return ModelDecision('tool_call', tool_name='read', url=url)
                if len(calls) == 3:return ModelDecision('final', content='Known fact. [E2]\n\nMechanism unknown.')
                raise ConnectionError('interrupted')
        with tempfile.TemporaryDirectory() as folder:
            result = ResearchAgent(search, Model(), folder, reader=reader, max_tool_calls=4, state_callback=states.append).run('Explain mechanism')
        self.assertEqual(result.termination, 'model_error')
        self.assertEqual(result.status, 'insufficient')
        self.assertIn('Known fact. [E2]', result.answer)
        self.assertIn('尚未完成', result.answer)
        self.assertTrue(states[-1]['revision_pending'])
        self.assertEqual(len(states[-1]['evidence']), 2)

    def test_truncated_model_http_body_retries_with_usage_and_finite_limit(self):
        model = OpenAICompatibleModel('https://example.org', 'test', 'test')
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.read.side_effect = IncompleteRead(b'partial', 625)
        good = io.BytesIO(b'{"choices":[{"message":{"content":"complete"}}],"usage":{"total_tokens":12}}')
        with patch('research_agent.models.urlopen', side_effect=[broken, good]) as transport, patch('research_agent.models.time.sleep'):
            self.assertEqual(model.complete([], []).content, 'complete')
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(len(model.usage_records), 2)
        with patch('research_agent.models.urlopen', return_value=broken) as transport, patch('research_agent.models.time.sleep'):
            with self.assertRaises(RuntimeError):
                model.complete([], [])
        self.assertEqual(transport.call_count, 3)

    def test_truncated_verifier_response_preserves_unverified_draft(self):
        model = OpenAICompatibleModel('https://example.org', 'test', 'test')
        draft = io.BytesIO(b'{"choices":[{"message":{"content":"A saved draft."}}]}')
        broken = Mock()
        broken.__enter__ = Mock(return_value=broken)
        broken.__exit__ = Mock(return_value=False)
        broken.read.side_effect = IncompleteRead(b'partial', 100)
        with tempfile.TemporaryDirectory() as folder, patch('research_agent.models.urlopen', side_effect=[draft, broken, broken]), patch('research_agent.models.time.sleep'):
            result = ResearchAgent(Mock(), model, folder, max_tool_calls=0).run('Explain the method')
        self.assertEqual(result.termination, 'answer_verification_unavailable')
        self.assertIn('A saved draft.', result.answer)
        self.assertIn('待核验草稿', result.answer)
        self.assertFalse(result.claims)
        self.assertEqual(len(model.usage_records), 3)

    def test_challenge_shell_never_becomes_read_evidence_or_saved_material(self):
        url = 'https://example.org/article'
        for title, body in [("Making sure you're not a bot!", 'Loading...'),
                            ('Just a moment...', 'Enable JavaScript and cookies to continue')]:
            html = f'<title>{title}</title><main>{body}</main>'
            response = _text_response(url, url, html, 'text/html', 2, False)
            self.assertFalse(response.ok)
            self.assertEqual(response.error['code'], 'access_challenge')
            self.assertEqual(response.network_requests, 2)
            self.assertFalse(response.content)
            reader = MaterialReader(FixtureReader([{'url': url, 'content': html, 'media_type': 'text/html'}]), Mock(), Mock())
            self.assertFalse(reader.read(url).ok)
            self.assertEqual(reader.records, [])
            with self.assertRaisesRegex(ValueError, 'access_challenge'):
                read_material(url, lambda *a, **k: (html.encode(), 'text/html', url, 'utf-8'))
        article = '<title>CAPTCHA detection in computer vision</title><p>We study CAPTCHA and browser verification methods.</p>'
        self.assertTrue(_text_response(url, url, article, 'text/html', 1, False).ok)

    def test_metadata_only_landing_page_can_still_follow_observed_pdf(self):
        url = 'https://example.org/article'
        html = '<meta name="citation_title" content="A Paper"><meta name="citation_pdf_url" content="/paper.pdf">'
        fetch = Mock(side_effect=[(html.encode(), 'text/html', url, 'utf-8'),
                                  (b'%PDF-test', 'application/pdf', 'https://example.org/paper.pdf', 'utf-8')])
        with patch('research_agent.materials.parse_pdf', return_value={'authors': [], 'page_count': 1,
                'title': 'A Paper', 'pages': [(1, 'An actual original.')], 'warnings': [], 'boundary': 'one page'}):
            self.assertEqual(read_material(url, fetch)['kind'], 'paper')
        self.assertEqual(fetch.call_count, 2)

    def test_progress_keeps_failure_alternatives_and_never_promotes_candidate_identity(self):
        candidate = {'title': 'A Paper', 'doi': '10.1/p', 'urls': ['https://example.org/a', 'https://example.org/a.pdf']}
        cache = {('discover_author_papers', '{}'): {'results': [candidate]},
                 ('read', 'a'): {'url': candidate['urls'][0], 'ok': False, 'error': {'code': 'access_challenge'}}}
        row = discovery_progress(cache)[0]
        self.assertEqual(row['reading_status'], 'original_missing')
        self.assertEqual(row['next_urls'], [candidate['urls'][1]])
        restored = {(a, b): p for a, b, p in json.loads(json.dumps([[*k, v] for k, v in cache.items()]))}
        self.assertEqual(discovery_progress(restored), [row])
        cache[('read', 'pdf')] = {'url': candidate['urls'][1], 'ok': True, 'evidence_id': 'E3'}
        read = discovery_progress(cache)[0]
        self.assertEqual(read['reading_status'], 'read_requires_verification')
        self.assertEqual(read['original_evidence_ids'], ['E3'])

    def test_compacted_discovery_retains_urls_identity_and_readback_references(self):
        row = {'work_id': 'W1', 'title': 'A Paper', 'urls': ['https://example.org/a.pdf'],
               'identity_check': 'provider_name_conflict', 'shared_coauthors': ['Alex Lee'], 'snippet': 'x' * 10000}
        decision = ModelDecision('tool_call', tool_name='discover_author_papers')
        pair = _tool_messages(decision, 'd1', {}, {'kind': 'discover_author_papers', 'results': [row]})
        compacted = _compact_tool_pair(pair, 'paper', 300)
        result = json.loads(compacted[1]['content'])['UNTRUSTED_TOOL_DATA']['results'][0]
        for key in ('urls', 'work_id', 'identity_check', 'shared_coauthors'):
            self.assertEqual(result[key], row[key])
        self.assertLess(len(compacted[1]['content']), len(pair[1]['content']))

    def test_compacted_author_lookup_keeps_read_locations_and_complete_page(self):
        rows = [{'artifact_id':str(i),'title':'Paper '+str(i),
            'authors':[{'name':'Alex Lee','evidence':[{'ref_id':f'D{i}-0'}]}],
            'author':{'name':'Alex Lee','evidence':[{'ref_id':f'D{i}-0'}]},
            'identity_match':'unresolved_same_name','shared_paper_affiliations':['Example University'],
            'unneeded_verbose_field':'x'*2000} for i in range(12)]
        pair=_tool_messages(ModelDecision('tool_call',tool_name='lookup_papers'),'lookup',{},
            {'kind':'lookup_papers','total':12,'next_offset':None,'results':rows})
        compacted=_compact_tool_pair(pair,'find same-author papers',300)
        payload=json.loads(compacted[1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertEqual(len(payload['results']),12)
        self.assertIsNone(payload['next_offset'])
        for original,row in zip(rows,payload['results']):
            for key in ('authors','author','identity_match','shared_paper_affiliations'):
                self.assertEqual(row[key],original[key])
        self.assertLess(len(compacted[1]['content']),len(pair[1]['content']))

    def repair_fixture(self):
        blocks = answer_blocks('Known fact. [E1]\n\nMechanism unknown.')
        return {'blocks': blocks, 'claims': [dict(blocks[0], supported=True, kind='fact', reason='checked'),
                dict(blocks[1], supported=True, kind='uncertainty', reason='mechanism missing')],
                'requirements': [{'id': 'R1', 'requirement': 'Explain the PairRecovery mechanism',
                    'addressed': False, 'block_ids': [blocks[1]['block_id']], 'reason': 'Original contains PairRecovery'}], 'gaps': ['mechanism missing']}

    def test_repair_gets_original_gap_window_after_context_compaction(self):
        checked = self.repair_fixture()
        fact = 'PairRecovery computes missing pairs by nearest neighbours.'
        original = 'Generic introduction. ' * 900 + fact + '\n' + 'Other experiments. ' * 900
        evidence = [Evidence('E2', 'S2', original, 'page'), Evidence('E3', 'S3', 'Invented snippet detail.', 'snippet')]
        prompt = repair_message(checked, evidence, 1600)
        payload = json.loads(prompt.split('\n', 1)[1])
        self.assertIn(fact, payload['repair_evidence'][0]['content'])
        self.assertEqual([e['evidence_id'] for e in payload['repair_evidence']], ['E2'])
        context = build_context([{'role': 'system', 'content': prompt}, {'role': 'user', 'content': 'Explain mechanism'},
                                 {'role': 'assistant', 'content': 'old history ' * 10000}],
                                max_context_tokens=6000, output_reserve_tokens=500, safety_margin_tokens=100)
        self.assertFalse(context.error)
        self.assertIn(fact, context.messages[0]['content'])
        self.assertEqual(evidence[0].content, original)

    def test_large_paper_catalog_does_not_overflow_protected_repair_context(self):
        checked = self.repair_fixture()
        title = 'Long multi-document publication title ' * 30
        evidence = [Evidence(f'E{i}', f'S{i}', 'PairRecovery original method. '*50, f'local:D{i}-0',title=title)
                    for i in range(1, 161)]
        checked['original_evidence'] = [{'evidence_id':e.evidence_id, 'title':e.title, 'kind':e.kind, 'evidence_role':'document'} for e in evidence]
        prompt = repair_message(checked, evidence)
        payload = json.loads(prompt.split('\n',1)[1])
        self.assertEqual(len(payload['original_evidence']), 160)
        self.assertEqual(payload['repair_evidence'][0]['title'], title)
        context = build_context([{'role':'system','content':prompt}, {'role':'user','content':'Explain PairRecovery'}],
            max_context_tokens=32768,output_reserve_tokens=2048,safety_margin_tokens=256)
        self.assertFalse(context.error)

    def test_patch_format_normalization_still_locks_verified_facts(self):
        checked = self.repair_fixture()
        patch_data = {'type': 'json_object', 'replace': [{'block_id': checked['blocks'][1]['block_id'], 'text': 'Mechanism. [E2]'}], 'append': []}
        self.assertIn('Known fact. [E1]', apply_answer_patch(json.dumps(patch_data), checked))
        patch_data['replace'][0]['block_id'] = checked['blocks'][0]['block_id']
        with self.assertRaisesRegex(ValueError, 'verified'):
            apply_answer_patch(json.dumps(patch_data), checked)
        for bad in ({'note': 'read E2', 'ref_id': 'E2'}, {'type': 'tool_call', 'replace': [], 'append': []}):
            with self.assertRaises(ValueError):
                apply_answer_patch(json.dumps(bad), checked)

    def test_repair_can_read_only_from_the_newly_returned_tool_location(self):
        for tool in ('search', 'retrieve', 'lookup_papers'):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as folder:
                calls, reads = [], []
                location = 'https://example.org/unique-repair-original'
                reference = 'D777-0'
                search, retriever, reader = Mock(), Mock(), Mock()
                search.search.return_value = SearchResponse(True, 'PairRecovery', [
                    {'title':'Newly located paper', 'url':location, 'snippet':'A retrieval clue only.'}])
                retriever.retrieve.return_value = {'ok':True, 'kind':'retrieve', 'results':[
                    {'ref_id':reference, 'corpus':'documents', 'owner_id':'paper', 'chunk_id':777, 'title':'Newly located paper',
                     'url':location, 'snippet':'A retrieval clue only.'}]}
                retriever.lookup_papers.return_value = {'ok':True, 'kind':'lookup_papers', 'results':[
                    {'source_id':'paper', 'target_id':'other', 'evidence':[{'ref_id':reference}]}]}
                def read_local(space_id, ref_id, **kwargs):
                    reads.append(ref_id)
                    return {'ok':True, 'kind':'read_evidence', 'ref_id':ref_id, 'url':location,
                        'title':'Newly located paper', 'corpus':'documents', 'artifact_id':'paper',
                        'content':'PairRecovery computes missing pairs.', 'content_hash':'original'}
                retriever.read.side_effect = read_local
                def read_web(url):
                    reads.append(url)
                    return ReadResponse(True,url,'PairRecovery computes missing pairs.')
                reader.read.side_effect = read_web
                owner = self
                class Model:
                    name = 'repair-tool-context-fixture'
                    usage_purpose = 'answer'
                    def complete(model, messages, tools):
                        if model.usage_purpose == 'answer_verification':
                            data = json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                            complete = 'computes' in data['blocks'][0]['text']
                            return ModelDecision('final',content=json.dumps({'requirements':[{'id':'R1',
                                'requirement':'Explain PairRecovery','addressed':complete,'abstention_supported':False,
                                'block_ids':['P1'],'reason':'Need the mechanism'}], 'blocks':[{
                                'block_id':'P1','kind':'fact' if complete else 'uncertainty','supported':True,
                                'evidence_ids':re.findall(r'\[(E\d+)\]',data['blocks'][0]['text']),
                                'reason':'checked','citation_relations':[]}]}))
                        calls.append(messages)
                        if len(calls) == 1:
                            return ModelDecision('final',content='Mechanism unknown.')
                        if len(calls) == 2:
                            return ModelDecision('tool_call',tool_name=tool,query='PairRecovery',
                                arguments={'mode':'references','artifact_ids':['paper']} if tool=='lookup_papers'
                                else {'query':'PairRecovery'})
                        if len(calls) == 3:
                            results = [json.loads(m['content'])['UNTRUSTED_TOOL_DATA']
                                       for m in messages if m['role']=='tool']
                            owner.assertTrue(results, 'New tool results must reach the repair model')
                            hit = results[-1]['results'][0]
                            if tool == 'search':
                                return ModelDecision('tool_call',tool_name='read',url=hit['url'])
                            ref = hit['ref_id'] if tool=='retrieve' else hit['evidence'][0]['ref_id']
                            return ModelDecision('tool_call',tool_name='read_evidence',arguments={'ref_id':ref})
                        data = json.loads(messages[0]['content'].split('\n',1)[1])
                        original = next(e for e in data['repair_evidence'] if 'computes' in e['content'])
                        return ModelDecision('final',content=json.dumps({'replace':[{
                            'block_id':data['editable'][0]['block_id'],
                            'text':original['content']+' ['+original['evidence_id']+']'}]}))
                result = ResearchAgent(search,Model(),folder,reader=reader,retrieval=retriever,space_id='s',
                    max_tool_calls=3,max_rounds=8,answer_verification=True).run('Explain PairRecovery')
                self.assertEqual(result.termination,'model_final')
                self.assertEqual(reads,[location if tool=='search' else reference])
                self.assertEqual(result.tool_calls,2)

    def test_online_discovery_reserves_a_read_for_verified_patch(self):
        requests, events, states = [], [], []
        urls = ['https://example.org/' + name for name in 'abc']
        retriever = Mock()
        retriever.discover_author_papers.return_value = {'ok': True, 'kind': 'discover_author_papers',
            'results': [{'title': name, 'urls': [url]} for name, url in zip('ABC', urls)],
            'sources': [{'title': name, 'url': url, 'snippet': name + ' candidate'} for name, url in zip('ABC', urls)]}
        reader = Mock()
        reader.read.side_effect = lambda url: ReadResponse(True, url, content=url[-1].upper() + ' original.', network_requests=1)
        owner = self
        class Model:
            name = 'bounded-discovery-fixture'
            supports_answer_verification = True
            usage_purpose = 'answer'
            def complete(self, messages, tools):
                if self.usage_purpose == 'answer_verification':
                    data = json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                    complete = any('C original.' in b['text'] for b in data['blocks'])
                    return ModelDecision('final', content=json.dumps({'requirements': [{'id': 'R1', 'requirement': 'Read three originals',
                        'addressed': complete, 'abstention_supported': False, 'reason': 'C must be read',
                        'block_ids': [b['block_id'] for b in data['blocks']]}], 'blocks': [
                        {'block_id': b['block_id'], 'kind': 'uncertainty' if 'unknown' in b['text'] else 'fact',
                         'supported': True, 'reason': 'checked', 'evidence_ids': re.findall(r'\[(E\d+)\]', b['text'])}
                        for b in data['blocks'] if not b['cached']]}))
                requests.append((messages, tools))
                n = len(requests)
                if n == 1:return ModelDecision('tool_call', tool_name='discover_author_papers', arguments={'seed_artifact_id': 'seed', 'author': 'Alex Lee'})
                if n in (2, 3):return ModelDecision('tool_call', tool_name='read', url=urls[n-2])
                if n == 4:
                    owner.assertFalse(tools)
                    return ModelDecision('final', content='A original. [E4]\n\nB original. [E5]\n\nC unknown.')
                if n == 5:
                    owner.assertTrue(tools)
                    state = json.loads(next(m['content'] for m in messages if m['role']=='user'))
                    pending = next(p for p in state['paper_acquisition'] if p['title']=='C')
                    return ModelDecision('tool_call', tool_name='read', url=pending['next_urls'][0])
                return ModelDecision('final', content=json.dumps({'type': 'json_object', 'replace': [
                    {'block_id': answer_blocks('C unknown.')[0]['block_id'], 'text': 'C original. [E6]'}], 'append': []}))
        with tempfile.TemporaryDirectory() as folder:
            result = ResearchAgent(Mock(), Model(), Path(folder), reader=reader, retrieval=retriever, space_id='s',
                max_tool_calls=4, max_rounds=10, on_event=events.append, state_callback=states.append).run('Read three originals')
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.tool_attempts, 4)
        self.assertEqual(reader.read.call_count, 3)
        self.assertEqual(next(e for e in events if e['event'] == 'answer_repair')['remaining_tool_attempts'], 1)
        self.assertNotIn('unknown', result.answer)
        self.assertTrue(states[-1]['answer_check']['ready'])


if __name__ == '__main__':
    unittest.main()
