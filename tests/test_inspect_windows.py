"""A controller must be able to read every character using advertised offsets."""
import copy
from dataclasses import asdict
import json
import unittest

from research_agent.auto_research import AutoResearch, MODEL_TOOL_RESULT_LIMIT
import tests.test_auto_research as fixtures


class InspectWindowTests(unittest.TestCase):
    setUp = fixtures.AutoResearchTests.setUp
    agent = fixtures.AutoResearchTests.agent

    def test_file_windows_reach_sqlite_and_model_without_gaps(self):
        job = self.app.auto_research.enqueue(self.space, self.chat, 'Inspect this method')
        job = self.store.claim_next()
        config = json.loads(job['payload'])['auto_research']
        workspace = self.app.coding.work_root / job['id']
        workspace.mkdir(parents=True, exist_ok=True)
        content = ''.join(f'{i}: method 原文\n' for i in range(2100))
        (workspace / 'method.py').write_bytes(content.encode('utf-8'))
        offset = 0
        visible = []
        while offset is not None:
            self.assertLess(len(visible), 30, 'A continuation must make progress')
            state = self.app.auto_research.state(job)
            decision = fixtures.call('inspect', {'kind': 'file', 'id': 'current',
                                                'path': 'method.py', 'offset': offset}, len(visible) + 1)
            state['pending'] = asdict(decision)
            raw = self.app.auto_research.dispatch(job, state, asdict(decision), config)
            before = copy.deepcopy(raw)
            self.app.auto_research.complete_tool(job, state, raw)
            saved = self.app.auto_research.state(job)
            shown = json.loads(saved['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
            self.assertEqual(raw, before)
            self.assertEqual(shown['content'], content[offset:offset + len(shown['content'])])
            expected = offset + len(shown['content'])
            self.assertEqual(shown['next_offset'], expected if expected < len(content) else None)
            self.assertGreater(len(shown['content']), 0)
            visible.append(shown['content'])
            offset = shown['next_offset']
        self.assertEqual(''.join(visible), content)
        self.model.complete.return_value = fixtures.call('inspect', {'kind': 'files', 'id': 'current'}, 99)
        self.app.auto_research.decide(job, saved, config, lambda *a, **kw: None)
        messages = self.model.complete.call_args.args[0]
        last = next(m for m in messages if m.get('tool_call_id') == decision.call_id)
        self.assertEqual(json.loads(last['content'])['UNTRUSTED_TOOL_DATA'], shown)

    def test_evidence_and_escaped_windows_preserve_offsets_and_metadata(self):
        metadata = {'evidence_id': 'E3', 'source_id': 'S1',
                    'provenance': {'page': 4, 'artifact_kind': 'paper', 'chunk_id': 12}}
        for content in ('x' * 5000, '\x00\\"\n中文' * 2300, 'short', ''):
            with self.subTest(length=len(content)):
                offset = 0
                visible = []
                while offset is not None:
                    self.assertLess(len(visible), 60)
                    raw = AutoResearch.window(content, {'offset': offset}, metadata)
                    before = copy.deepcopy(raw)
                    shown = AutoResearch.context_result(raw)
                    self.assertEqual(raw, before)
                    self.assertEqual(shown['content'], content[offset:offset + len(shown['content'])])
                    self.assertEqual(shown['provenance'], metadata['provenance'])
                    self.assertEqual(shown['evidence_id'], 'E3')
                    self.assertEqual(shown['total_chars'], len(content))
                    self.assertLessEqual(len(json.dumps(shown, ensure_ascii=False)), MODEL_TOOL_RESULT_LIMIT + 1000)
                    self.assertEqual(AutoResearch.context_result(shown), shown)
                    end = offset + len(shown['content'])
                    self.assertEqual(shown['next_offset'], end if end < len(content) else None)
                    if offset < len(content): self.assertGreater(end, offset)
                    visible.append(shown['content'])
                    offset = shown['next_offset']
                self.assertEqual(''.join(visible), content)

    def test_literal_search_reads_target_and_keeps_unread_prefix(self):
        job = self.app.auto_research.enqueue(self.space, self.chat, 'Inspect the restore function')
        job = self.store.claim_next()
        config = json.loads(job['payload'])['auto_research']
        workspace = self.app.coding.work_root / job['id']
        workspace.mkdir(parents=True, exist_ok=True)
        text = '# irrelevant\n' * 1000 + 'def restore(x):\n' + '    pass\n' * 500 + 'def restore(x):\n    return x\n'
        (workspace / 'method.py').write_bytes(text.encode())
        args = {'kind': 'file', 'id': 'current', 'path': 'method.py', 'find_text': 'def restore(x):'}
        state = self.app.auto_research.state(job)
        decision = asdict(fixtures.call('inspect', args))
        state['pending'] = decision
        result = self.app.auto_research.dispatch(job, state, decision, config)
        self.app.auto_research.complete_tool(job, state, result)
        shown = json.loads(state['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertTrue(shown['found'])
        self.assertEqual(shown['offset'], text.index(args['find_text']))
        self.assertEqual(shown['content'], text[shown['offset']:shown['next_offset']])
        progress = self.app.auto_research.reading_progress(state['messages'])['files'][0]
        self.assertEqual(progress['next_unread_offset'], 0)
        self.assertFalse(progress['complete'])
        later = self.app.auto_research.window(text, {**args, 'offset': shown['next_offset']}, {})
        self.assertEqual(later['offset'], text.rindex(args['find_text']))
        for path in ('../private.txt', '.env'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.app.auto_research.dispatch(job, state, {**decision, 'arguments': {**args, 'path': path}}, config)

    def test_literal_absence_is_not_a_read_or_regex_and_bad_arguments_fail(self):
        result = AutoResearch.window('abc def', {'find_text': '.*', 'offset': 2}, {'evidence_id': 'E1'})
        self.assertFalse(result['found'])
        self.assertEqual(result['search_offset'], 2)
        self.assertEqual(result['total_chars'], 7)
        self.assertNotIn('content', result)
        self.assertEqual(result['evidence_id'], 'E1')
        for value in ('', 'x' * 201, None, 3):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AutoResearch.window('abc', {'find_text': value}, {})
        for offset in (-1, 4, True):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                AutoResearch.window('abc', {'find_text': 'a', 'offset': offset}, {})


if __name__ == '__main__':
    unittest.main()
