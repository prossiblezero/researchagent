"""Optional experiment generation controls reach HTTP and stay out of ordinary calls."""
import io
import json
from unittest.mock import patch
import unittest

from research_agent.coding_tool import CodingTool
from research_agent.models import OpenAICompatibleModel
from research_agent.workbench_store import Conflict
import tests.test_coding_tool as fixtures


class ExperimentGenerationTests(unittest.TestCase):
    setUp = fixtures.CodingToolTests.setUp

    def test_old_request_unchanged_and_bad_overrides_rejected(self):
        old = {'id': 'r', 'messages': [{'role': 'user', 'content': 'memory turn'}]}
        self.assertEqual(CodingTool._model_request(old), old)
        for value in [0, .3, .7, 2]:
            self.assertEqual(CodingTool._model_request({**old, 'temperature': value})['temperature'], value)
        self.assertEqual(CodingTool._model_request({**old, 'max_tokens': 1000})['max_tokens'], 1000)
        for field, value in [('temperature', True), ('temperature', None), ('temperature', -.1),
                             ('temperature', 2.1), ('temperature', 10**400), ('temperature', float('nan')), ('temperature', float('inf')),
                             ('max_tokens', True), ('max_tokens', 0), ('max_tokens', 65537),
                             ('max_tokens', 1.0), ('max_tokens', None), ('base_url', 'https://attacker.invalid')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                CodingTool._model_request({**old, field: value})

    def test_real_client_payload_receipt_restore_and_parameter_bound_recovery(self):
        model = OpenAICompatibleModel('https://example.invalid/v1', 'test-placeholder', 'experiment', temperature=None)
        self.tool.app.model_factory = lambda: model
        seen = []
        def response(request, **kwargs):
            seen.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'READY'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5}}).encode())
        output = self.root / 'generation-receipts'; output.mkdir()
        spec = {'mode': 'stdio', 'model_id': 'default', 'max_requests': 3, 'seconds': 30}
        item = CodingTool._model_request({'id': 'r1', 'messages': [{'role': 'user', 'content': 'test'}],
                                          'temperature': .3, 'max_tokens': 1000})
        state = {}
        with patch('research_agent.models.urlopen', side_effect=response):
            self.tool._call_host_model(output, state, spec, item, lambda: None, lambda: False)
            model.complete([{'role': 'user', 'content': 'ordinary chat'}], [])
        self.assertEqual(seen[0]['temperature'], .3)
        self.assertEqual(seen[0]['max_tokens'], 1000)
        self.assertNotIn('temperature', seen[1])
        self.assertNotIn('max_tokens', seen[1])
        self.assertEqual(seen[0]['messages'][-1]['content'], 'test')
        receipt = json.loads((output / 'model-request-00.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['generation_parameters'], {'temperature': .3, 'max_tokens': 1000})
        self.assertEqual(receipt['request'], item)
        self.assertEqual(state['model_requests'][0]['generation_parameters'], receipt['generation_parameters'])
        with patch('research_agent.models.urlopen', side_effect=AssertionError('must reuse receipt')):
            self.tool._call_host_model(output, {}, spec, item, lambda: None, lambda: False)
        with self.assertRaisesRegex(Conflict, '不匹配'):
            self.tool._call_host_model(output, {}, spec, {**item, 'temperature': .7}, lambda: None, lambda: False)
        with self.assertRaisesRegex(Conflict, '不匹配'):
            self.tool._call_host_model(output, {}, spec, {**item, 'max_tokens': 500}, lambda: None, lambda: False)

    def test_provider_rejection_is_preserved_and_options_restored(self):
        from urllib.error import HTTPError
        model = OpenAICompatibleModel('https://example.invalid/v1', 'test-placeholder', 'experiment', temperature=None)
        self.tool.app.model_factory = lambda: model
        output = self.root / 'rejected'; output.mkdir()
        item = CodingTool._model_request({'id': 'r1', 'messages': [{'role': 'user', 'content': 'test'}],
                                          'temperature': .7, 'max_tokens': 1000})
        error = HTTPError('https://example.invalid/v1', 400, 'unsupported', {}, io.BytesIO(b'unsupported parameter'))
        with patch('research_agent.models.urlopen', side_effect=error) as send, self.assertRaisesRegex(RuntimeError, 'HTTP 400'):
            self.tool._call_host_model(output, {}, {'model_id': 'default', 'max_requests': 1, 'seconds': 30},
                                      item, lambda: None, lambda: False)
        self.assertEqual(send.call_count, 1)
        self.assertIsNone(model.temperature)
        self.assertIsNone(model.max_tokens)
        receipt = json.loads((output / 'model-request-00.json').read_text(encoding='utf-8'))
        self.assertFalse(receipt['valid'])
        self.assertIn('HTTP 400', receipt['error'])
        self.assertEqual(receipt['generation_parameters']['max_tokens'], 1000)

    def test_unsupported_client_fails_without_mutating_shared_callbacks(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        guard, stream = lambda: None, lambda *a: None
        model = SimpleNamespace(request_guard=guard, stream_callback=stream, last_usage={'total_tokens': 999},
                                usage_records=[{'total_tokens': 999}], complete=Mock())
        self.tool.app.model_factory = lambda: model
        output = self.root / 'unsupported'; output.mkdir()
        item = CodingTool._model_request({'id': 'r1', 'messages': [{'role': 'user', 'content': 'test'}], 'temperature': .7})
        with self.assertRaisesRegex(RuntimeError, '不支持'):
            self.tool._call_host_model(output, {}, {'model_id': 'default', 'max_requests': 1, 'seconds': 30},
                                      item, lambda: None, lambda: False)
        model.complete.assert_not_called()
        self.assertIs(model.request_guard, guard)
        self.assertIs(model.stream_callback, stream)
        receipt = json.loads((output / 'model-request-00.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['usage'], {})
        self.assertEqual(receipt['usage_records'], [])
        self.assertEqual(model.last_usage, {'total_tokens': 999})
        self.assertEqual(model.usage_records, [{'total_tokens': 999}])

    def test_rejection_inside_real_client_does_not_reuse_historical_usage(self):
        model = OpenAICompatibleModel('https://example.invalid/v1', 'test-placeholder', 'experiment', temperature=None)
        model.last_usage = {'total_tokens': 999}
        model.usage_records = [{'total_tokens': 999}]
        checks = []
        def guard():
            checks.append(True)
            if len(checks) == 2:
                raise RuntimeError('cancelled before HTTP')
        model.request_guard = guard
        self.tool.app.model_factory = lambda: model
        output = self.root / 'pre-http-rejection'; output.mkdir()
        item = CodingTool._model_request({'id': 'r1', 'messages': [{'role': 'user', 'content': 'test'}], 'temperature': .7})
        with patch('research_agent.models.urlopen') as send, self.assertRaisesRegex(RuntimeError, 'cancelled before HTTP'):
            self.tool._call_host_model(output, {}, {'model_id': 'default', 'max_requests': 1, 'seconds': 30},
                                      item, lambda: None, lambda: False)
        send.assert_not_called()
        receipt = json.loads((output / 'model-request-00.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['usage'], {})
        self.assertEqual(receipt['usage_records'], [])
        self.assertIs(model.request_guard, guard)
        self.assertIsNone(model.temperature)


if __name__ == '__main__':
    unittest.main()
