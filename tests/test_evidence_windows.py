"""Original reading must reach the model intact or expose exact smaller offsets."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from research_agent.context import build_context, _tool_pairs
from research_agent.contracts import ModelDecision, ReadResponse, SearchResponse
from research_agent.library import Library
from research_agent.loop import ResearchAgent, _tool_messages
from research_agent.retrieval import Retriever, TOOLS
from research_agent.workbench_store import WorkbenchStore


def payload(messages):
    return json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))['UNTRUSTED_TOOL_DATA']


def call(name, **args):
    return ModelDecision('tool_call', tool_name=name, arguments=args, query=args.get('query', ''), url=args.get('url', ''))


class EvidenceWindowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_web_unicode_windows_reach_model_and_page_to_exact_end(self):
        original = ''.join(f'原文方法{i:04d}。' for i in range(1000))
        digest = hashlib.sha256(original.encode()).hexdigest()
        url = 'https://example.org/paper'
        search = Mock()
        search.search.return_value = SearchResponse(True, 'paper', [{'title': 'Paper', 'url': url, 'snippet': 'Preview'}])
        reader = Mock()
        reader.read.return_value = ReadResponse(True, url, original, title='Paper', content_hash=digest)
        step = 0
        def complete(messages, tools):
            nonlocal step
            step += 1
            if step == 1:
                return call('search', query='paper')
            if step == 2:
                return call('read', url=url)
            data = payload(messages)
            if step == 3:
                self.assertTrue(data['context_excerpted'])
                return call('read_evidence', ref_id=data['evidence_id'], offset=100, max_chars=6000)
            start, end = (100, 6100) if step == 4 else (6100, len(original))
            self.assertEqual(data['content'], original[start:end])
            self.assertEqual((data['start_offset'], data['end_offset']), (start, end))
            self.assertEqual(data['content_hash'], digest)
            self.assertNotIn('context_excerpted', data)
            if step == 4:
                self.assertEqual(data['next_offset'], 6100)
                return call('read_evidence', ref_id=data['evidence_id'], offset=data['next_offset'], max_chars=12000)
            self.assertIsNone(data['next_offset'])
            return ModelDecision('final', content='INSUFFICIENT: window contract checked.')
        model = SimpleNamespace(name='window-test', complete=complete)
        result = ResearchAgent(search, model, self.root, reader=reader, max_tool_calls=5,
                               max_context_tokens=32768).run('Read the methods')
        self.assertEqual(step, 5, (result.termination, result.answer))
        self.assertEqual(result.evidence[-1].content, original)
        reader.read.assert_called_once_with(url)

    def test_local_windows_preserve_pages_hashes_and_bounded_neighbors(self):
        env = patch.dict(os.environ, {'RETRIEVAL_MODE': 'lexical'})
        env.start(); self.addCleanup(env.stop)
        store = WorkbenchStore(self.root/'local.db')
        sid = store.save_space({'name': 'Originals'})['id']
        library = Library(store)
        original = 'uniqueneedle ' + '模型原文🙂' * 1800
        artifact, _ = library.save(sid, {'kind': 'paper', 'title': 'Paper', 'url': '', 'canonical_id': 'test',
            'metadata': {}, 'data': None, 'warnings': [], 'boundary': 'fixture', 'chunks': [
                {'text': text, 'page': page, 'section': 'methods', 'line_start': 1, 'line_end': 20}
                for page, text in enumerate(['neighbor ' * 600, original, 'neighbor ' * 600], 1)]})
        retriever = Retriever(store, library)
        retriever.sync(sid)
        step = 0
        def complete(messages, tools):
            nonlocal step
            step += 1
            if step == 1:
                return call('retrieve', query='uniqueneedle', corpus='documents', top_k=1)
            data = payload(messages)
            if step == 2:
                return call('read_evidence', ref_id=data['results'][0]['evidence_id'], offset=200, max_chars=7000)
            if step == 3:
                self.assertEqual(data['content'], original[200:7200])
                self.assertEqual(data['page'], 2)
                self.assertEqual(data['content_hash'], hashlib.sha256(original.encode()).hexdigest())
                self.assertEqual({p['page'] for p in data['neighbors']}, {1, 3})
                self.assertTrue(all(len(p['content']) == 1800 for p in data['neighbors']))
                return call('read_evidence', ref_id=data['evidence_id'], offset=7200, adjacent=0)
            self.assertEqual(data['content'], original[7200:9000])
            self.assertEqual(data['next_offset'], 9000)
            self.assertEqual(data['neighbors'], [])
            return ModelDecision('final', content='INSUFFICIENT: local contract checked.')
        result = ResearchAgent(None, SimpleNamespace(name='local-window', complete=complete), self.root/'traces',
            retrieval=retriever, space_id=sid, allow_external=False, max_tool_calls=4, max_context_tokens=32768).run('Read methods')
        self.assertEqual(step, 4)
        self.assertEqual(result.network_requests, 0)
        self.assertIn(original, [e.content for e in result.evidence])

    def test_invalid_window_sizes_fail_before_reading(self):
        for value in [True, 0, -1, 12001, 1.5, '6000', None]:
            with self.subTest(value=value):
                retriever = Mock()
                decisions = iter([call('read_evidence', ref_id='D1', max_chars=value),
                                  ModelDecision('final', content='INSUFFICIENT')])
                inputs = []
                def complete(messages, tools):
                    inputs.append(messages)
                    return next(decisions)
                ResearchAgent(None, SimpleNamespace(name='invalid-window', complete=complete), self.root,
                    retrieval=retriever, space_id='space', allow_external=False, max_tool_calls=2).run('Read')
                self.assertEqual(payload(inputs[-1])['error']['code'], 'invalid_retrieval_request')
                retriever.read.assert_not_called()
        schema = TOOLS[1]['function']['parameters']['properties']['max_chars']
        self.assertEqual((schema['minimum'], schema['maximum']), (1, 12000))

    def test_context_pressure_preserves_contiguous_visible_offsets_and_neighbors(self):
        source = '原文🙂内容' * 3000
        main = {'ok': True, 'kind': 'read_evidence', 'evidence_id': 'E1', 'source_id': 'S1',
                'content_hash': 'unchanged', 'content': source[100:12100], 'start_offset': 100,
                'end_offset': 12100, 'next_offset': 12100, 'total_chars': len(source),
                'neighbors': [{'evidence_id': 'E2', 'source_id': 'S1', 'content_hash': 'neighbor',
                               'content': source[:1800], 'start_offset': 0, 'end_offset': 1800,
                               'next_offset': 1800, 'total_chars': len(source)}]}
        decision = call('read_evidence', ref_id='E1', offset=100, max_chars=12000)
        messages = [{'role': 'system', 'content': 'Rules'}, {'role': 'user', 'content': 'Read'}]
        messages += _tool_messages(decision, 'read-1', decision.arguments, main)
        original = copy.deepcopy(messages)
        built = build_context(messages, max_context_tokens=1600, output_reserve_tokens=200,
                              safety_margin_tokens=100, question='Read')
        self.assertFalse(built.error)
        self.assertLessEqual(built.estimated_input_tokens, 1300)
        self.assertFalse(_tool_pairs(built.messages)[1])
        got = payload(built.messages)
        for part in [got, *got['neighbors']]:
            self.assertTrue(part['context_excerpted'])
            self.assertGreater(len(part['content']), 0)
            self.assertEqual(part['end_offset'], part['start_offset'] + len(part['content']))
            self.assertEqual(part['content'], source[part['start_offset']:part['end_offset']])
            self.assertEqual(part['next_offset'], part['end_offset'])
        self.assertIn('read_content_excerpt:read-1', built.removed)
        self.assertEqual(got['content_hash'], 'unchanged')
        self.assertEqual(messages, original)


if __name__ == '__main__':
    unittest.main()
