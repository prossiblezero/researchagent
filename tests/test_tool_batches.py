"""Provider batches must survive serial execution, checkpoint resume and budgets."""
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from research_agent.contracts import Evidence, ModelDecision, SearchResponse, Source
from research_agent.context import _tool_pairs
from research_agent.loop import ResearchAgent
from research_agent.models import OpenAICompatibleModel
from research_agent.verify import answer_blocks, check_answer


class ToolBatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def batch(self, ids=('a', 'b')):
        payload = {'choices': [{'message': {'reasoning_content': 'Plan two lookups', 'tool_calls': [
            {'id': value, 'type': 'function', 'function': {'name': 'search', 'arguments': json.dumps({'query': value})}}
            for value in ids]}, 'finish_reason': 'tool_calls'}]}
        model = OpenAICompatibleModel('https://example.org/v1', 'test', 'test')
        with patch('research_agent.models.urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
            return model.complete([], [{'type': 'function', 'function': {'name': 'search'}}])

    def model(self, decisions):
        return SimpleNamespace(name='batch-test', supports_answer_verification=False,
                               usage_callback=None, complete=Mock(side_effect=decisions))

    def search(self):
        provider = Mock()
        provider.search.side_effect = lambda query: SearchResponse(True, query, [
            {'title': query, 'url': 'https://example.org/' + query, 'snippet': 'A verified result.'}])
        return provider

    def test_mixed_provider_tool_history_has_reasoning_field_without_mutating_checkpoint(self):
        messages = [{'role': 'user', 'content': 'Continue the saved research'},
                    {'role': 'assistant', 'tool_calls': [{'id': 'old', 'type': 'function',
                     'function': {'name': 'read', 'arguments': '{}'}}]},
                    {'role': 'tool', 'tool_call_id': 'old', 'content': 'Original paper'},
                    {'role': 'assistant', 'reasoning_content': 'Saved provider reasoning',
                     'tool_calls': [{'id': 'new', 'type': 'function',
                     'function': {'name': 'read', 'arguments': '{}'}}]},
                    {'role': 'tool', 'tool_call_id': 'new', 'content': 'Another original'}]
        snapshot = json.dumps(messages)
        response = io.BytesIO(b'{"choices":[{"message":{"content":"done"},"finish_reason":"stop"}]}')
        model = OpenAICompatibleModel('https://example.org/v1', 'test', 'test')
        with patch('research_agent.models.urlopen', return_value=response) as transport:
            model.complete(messages, [])
        sent = json.loads(transport.call_args.args[0].data)['messages']
        self.assertEqual(sent[1].get('reasoning_content'), '')
        self.assertEqual(sent[3]['reasoning_content'], 'Saved provider reasoning')
        self.assertEqual(json.dumps(messages), snapshot)

    def test_serial_batch_and_resume_reuse_first_committed_result(self):
        saved = []
        provider = self.search()
        model = self.model([self.batch(), ModelDecision('final', content='A verified result. [S1] [S2]')])
        result = ResearchAgent(provider, model, self.root, max_tool_calls=3,
                               state_callback=lambda state: saved.append(json.loads(json.dumps(state)))).run('Compare a and b')
        self.assertEqual(result.status, 'ok')
        self.assertEqual([call.args[0] for call in provider.search.call_args_list], ['a', 'b'])
        self.assertEqual(model.complete.call_count, 2)
        self.assertFalse(_tool_pairs(model.complete.call_args.args[0])[1])
        checkpoint = next(s for s in saved if s['next_iteration'] == 1 and s['pending_decision'] is None)
        self.assertEqual(checkpoint['queued_tools'][0]['call_id'], 'b')
        resumed_provider = self.search()
        resumed_model = self.model([ModelDecision('final', content='A verified result. [S1] [S2]')])
        resumed = ResearchAgent(resumed_provider, resumed_model, self.root, max_tool_calls=3,
                                resume_state=checkpoint).run('Compare a and b')
        self.assertEqual(resumed.status, 'ok')
        resumed_provider.search.assert_called_once_with('b')
        self.assertEqual(resumed_model.complete.call_count, 1)

    def test_batch_budget_stops_remaining_calls_then_requests_summary(self):
        provider = self.search()
        model = self.model([self.batch(), ModelDecision('final', content='A verified result. [S1]')])
        result = ResearchAgent(provider, model, self.root, max_tool_calls=1).run('Compare a and b')
        self.assertEqual(result.tool_calls, 1)
        provider.search.assert_called_once_with('a')
        self.assertEqual(model.complete.call_args.args[1], [])
        self.assertFalse(_tool_pairs(model.complete.call_args.args[0])[1])

    def test_batch_cannot_bypass_per_tool_permission(self):
        first = self.batch()
        first.queued_tool_calls = [asdict(ModelDecision('tool_call', tool_name='shell', call_id='unsafe', arguments={'cmd': 'echo x'}))]
        model = self.model([first])
        result = ResearchAgent(self.search(), model, self.root, max_tool_calls=3).run('Look up a')
        self.assertEqual(result.termination, 'policy_denied')
        self.assertEqual(result.tool_calls, 1)

    def test_duplicate_or_unbounded_provider_ids_fail_before_any_execution(self):
        for ids in [('a', 'a'), ('a', ''), tuple(str(i) for i in range(17))]:
            with self.subTest(ids=ids), self.assertRaises(RuntimeError):
                self.batch(ids)

    def test_source_citation_does_not_hide_explicit_preview_citation(self):
        sources = [Source('S1', 'Reference', 'https://example.org', '')]
        evidence = [Evidence('E1', 'S1', 'Parameter preview', 'local-snippet:D1'),
                    Evidence('E7', 'S1', 'The parameter is 60.', 'local:D1')]
        for answer, expected in [('The parameter is 60. [E1] [S1]', False),
                                 ('The parameter is 60. [E7] [S1]', True)]:
            block = 'P1'  # The single paragraph's request-local identifier.
            judgment = {'requirements': [{'id': 'R1', 'requirement': 'parameter', 'addressed': True,
                         'block_ids': [block], 'reason': 'answered'}],
                        'blocks': [{'block_id': block, 'kind': 'fact', 'supported': True,
                                    'evidence_ids': ['E7'], 'reason': 'original supports answer'}]}
            with patch('research_agent.library.model_json', return_value=judgment):
                result = check_answer(Mock(), 'What is the parameter?', answer, evidence, sources)
            self.assertEqual(result['ready'], expected)


if __name__ == '__main__':
    unittest.main()
