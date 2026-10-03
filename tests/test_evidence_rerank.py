"""Source-grounded reranking contracts; deterministic fixtures, no model/network calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_agent import evidence_rerank as r
from research_agent import experiment_evidence as e
from research_agent import experiment_feedback as f
from research_agent.contracts import ModelDecision
from research_agent.library import quote_spans


def corpus():
    return [
        {'id': 'paper:0', 'title': 'Alpha study', 'section': 'method',
         'content': 'alpha evidence 中文原文。 ' * 40, 'url': 'https://example.org/alpha', 'page': 1},
        {'id': 'paper:1', 'title': 'Beta study', 'section': 'results',
         'content': 'beta exact original evidence.', 'url': 'https://example.org/beta', 'page': 2},
    ]


def fixture():
    questions = [
        {'id': 'dev-visible', 'category': 'direct', 'split': 'dev', 'query': 'alpha',
         'gold': [{'document': 'paper', 'ordinal': 0}], 'answer': 'PRIVATE_DEV_LABEL'},
        {'id': 'held-private', 'category': 'semantic', 'split': 'held_out', 'query': 'beta private held-out query',
         'gold': [{'document': 'paper', 'ordinal': 1}], 'answer': 'PRIVATE_HELD_LABEL'},
    ]
    signals = [{'text': q['query'], 'dense': [{'id': 'paper:0', 'score': .8},
                                            {'id': 'paper:1', 'score': .7}]} for q in questions]
    return {'corpus': corpus(), 'questions': questions, 'signals': signals,
            'features': {'signals_hash': 'fixture-dense'}}


class FakeModel:
    name = 'fixture-luna'
    base_url = 'https://example.invalid/v1'
    temperature = 0

    def __init__(self, response=None, *, error=None, calls=None):
        self.response = response if response is not None else ModelDecision(
            kind='final', content='{"ranking":[{"id":"paper:1","span":1}]}')
        self.error = error
        self.calls = calls if calls is not None else []
        self.request_deadline = None
        self.usage_purpose = 'answer'
        self.usage_records = []

    def complete(self, messages, tools):
        self.calls.append({'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools),
                           'deadline': self.request_deadline, 'purpose': self.usage_purpose})
        self.usage_records.append({'input_tokens': 17, 'output_tokens': 3, 'total_tokens': 20})
        if self.error:
            raise self.error
        return self.response


class EvidenceRerankTests(unittest.TestCase):
    def test_original_span_binding_and_request_excludes_labels_or_arbitrary_metadata(self):
        docs = corpus()
        docs[0].update(gold='DO_NOT_SEND_GOLD', answer='DO_NOT_SEND_ANSWER',
                       instructions='DO_NOT_SEND_METADATA')
        response = {'ranking': [{'id': 'paper:0', 'span': 2, 'quote': 'fabricated quote'},
                                {'id': 'paper:1', 'span': 1}]}
        model = FakeModel(ModelDecision(kind='final', content=json.dumps(response)))
        result = r.rerank('中文 alpha query', docs, model)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['ranking'], ['paper:0', 'paper:1'])
        self.assertEqual(result['evidence'][0]['quote'], quote_spans(docs[0]['content'])[1])
        self.assertNotIn('fabricated', json.dumps(result['evidence']))
        sent = model.calls[0]
        self.assertEqual(sent['tools'], [])
        payload = json.loads(sent['messages'][1]['content'])['UNTRUSTED_DOCUMENT_DATA']
        self.assertEqual(set(payload), {'question', 'passages'})
        self.assertEqual(set(payload['passages'][0]), {'id', 'title', 'section', 'spans'})
        self.assertEqual(payload['passages'][0]['spans'],
                         [{'span': i, 'text': text} for i, text in enumerate(quote_spans(docs[0]['content']), 1)])
        self.assertEqual(result['full_original_characters'], sum(len(p['content']) for p in docs))
        for secret in ('DO_NOT_SEND_GOLD', 'DO_NOT_SEND_ANSWER', 'DO_NOT_SEND_METADATA'):
            self.assertNotIn(secret, json.dumps(sent))

    def test_empty_ranking_is_valid_and_never_padded_with_baseline(self):
        model = FakeModel(ModelDecision(kind='final', content='{"ranking":[]}'))
        result = r.rerank('unsupported question', corpus(), model)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['ranking'], [])
        self.assertEqual(result['evidence'], [])

    def test_invalid_ranking_and_tool_response_fall_back_to_original_order(self):
        invalid = [
            '{"ranking":[{"id":"unknown","span":1}]}',
            '{"ranking":[{"id":"paper:1","span":1},{"id":"paper:1","span":999}]}',
            '{"ranking":[{"id":"paper:1","span":1},{"id":"unknown","span":1}]}',
            '{"ranking":[{"id":"paper:0","span":0}]}',
            '{"ranking":[{"id":"paper:0","span":999}]}',
            '{"ranking":[{"id":"paper:0","span":true}]}',
            '{"ranking":[{"id":"paper:0","span":"1"}]}',
            '{"ranking":[{"id":"paper:0"}]}',
            '{"ranking":[{"id":1,"span":1}]}',
            '{"ranking":["paper:0"]}',
            '{"ranking":{}}', '{}', '[]', 'not JSON',
        ]
        decisions = [ModelDecision(kind='final', content=body) for body in invalid]
        decisions.append(ModelDecision(kind='tool_call', content='{}', tool_name='search'))
        for decision in decisions:
            with self.subTest(content=decision.content, kind=decision.kind):
                result = r.rerank('alpha', corpus(), FakeModel(decision))
                self.assertEqual(result['status'], 'fallback')
                self.assertEqual(result['ranking'], ['paper:0', 'paper:1'])
                self.assertEqual(result['evidence'], [])
                self.assertEqual(result['raw_response'], decision.content)


    def test_multiple_valid_spans_share_one_ranked_passage_and_keep_all_evidence(self):
        docs = corpus()
        response = {'ranking': [
            {'id': 'paper:1', 'span': 1},
            {'id': 'paper:0', 'span': 2},
            {'id': 'paper:1', 'span': 1},
            {'id': 'paper:0', 'span': 1},
        ]}
        result = r.rerank('multi-part question', docs,
                          FakeModel(ModelDecision(kind='final', content=json.dumps(response))))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['ranking'], ['paper:1', 'paper:0'])
        self.assertEqual(result['duplicate_passage_rows'], 2)
        self.assertEqual([(item['id'], item['span']) for item in result['evidence']],
                         [('paper:1', 1), ('paper:0', 2), ('paper:1', 1), ('paper:0', 1)])
        originals = {doc['id']: quote_spans(doc['content']) for doc in docs}
        for item in result['evidence']:
            self.assertEqual(item['quote'], originals[item['id']][item['span'] - 1])

    def test_ranking_limit_is_ten_and_fallback_preserves_input_order(self):
        docs = [dict(id=f'p:{i}', title='fixture', section='', content='evidence') for i in range(12)]
        response = {'ranking': [{'id': p['id'], 'span': 1} for p in docs[:11]]}
        result = r.rerank('evidence', docs, FakeModel(ModelDecision(kind='final', content=json.dumps(response))))
        self.assertEqual(result['status'], 'fallback')
        self.assertEqual(result['ranking'], [p['id'] for p in docs[:10]])

    def test_deadline_is_bounded_and_existing_usage_and_purpose_are_restored(self):
        model = FakeModel()
        model.request_deadline = 95
        model.usage_purpose = 'verification'
        earlier = {'input_tokens': 99, 'output_tokens': 1, 'total_tokens': 100}
        model.usage_records.append(earlier)
        with patch.object(r.time, 'monotonic', return_value=80):
            result = r.rerank('alpha', corpus(), model, seconds=30)
        self.assertEqual(model.calls[0]['deadline'], 95)
        self.assertEqual(model.calls[0]['purpose'], 'evidence_rerank')
        self.assertEqual(model.request_deadline, 95)
        self.assertEqual(model.usage_purpose, 'verification')
        self.assertEqual(result['usage']['requests'], 1)
        self.assertEqual(result['usage']['total_tokens'], 20)
        self.assertEqual(model.usage_records[0], earlier)
        model.request_deadline = None
        with patch.object(r.time, 'monotonic', return_value=80):
            r.rerank('alpha', corpus(), model, seconds=30)
        self.assertEqual(model.calls[-1]['deadline'], 110)
        self.assertIsNone(model.request_deadline)

    def test_transport_failure_restores_model_and_does_not_expose_private_error(self):
        private = 'https://private.invalid/?api_key=never-publish'
        model = FakeModel(error=RuntimeError(private))
        model.request_deadline, model.usage_purpose = 12345, 'existing'
        result = r.rerank('alpha', corpus(), model)
        self.assertEqual(result['status'], 'fallback')
        self.assertEqual(result['error'], 'RuntimeError')
        self.assertEqual(result['ranking'], ['paper:0', 'paper:1'])
        self.assertNotIn(private, json.dumps(result))
        self.assertEqual((model.request_deadline, model.usage_purpose), (12345, 'existing'))
        self.assertEqual(result['usage']['requests'], 1)

    def test_cancel_before_and_after_model_restores_budget_without_returning_success(self):
        for checks, expected_calls in (([True], 0), ([False, True], 1)):
            with self.subTest(checks=checks):
                model = FakeModel()
                model.request_deadline, model.usage_purpose = 4000, 'existing'
                answers = iter(checks)
                with self.assertRaises(InterruptedError):
                    r.rerank('alpha', corpus(), model, cancelled=lambda: next(answers))
                self.assertEqual(len(model.calls), expected_calls)
                self.assertEqual((model.request_deadline, model.usage_purpose), (4000, 'existing'))

    def test_invalid_inputs_do_not_call_model(self):
        cases = [
            ('', corpus()), ('x' * 8001, corpus()),
            ('query', [corpus()[0], corpus()[0]]),
            ('query', [dict(id=f'p:{i}', content='x') for i in range(41)]),
            ('query', [dict(id='p:0', content='x' * 80001)]),
        ]
        for query, docs in cases:
            with self.subTest(query_length=len(query), docs=len(docs)):
                model = FakeModel()
                with self.assertRaises(ValueError):
                    r.rerank(query, docs, model)
                self.assertEqual(model.calls, [])


class EvidenceFeatureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = fixture()
        self.calls = []
        self.model_options = {}
        self.patch_root = patch.object(e, 'ROOT', self.root)
        self.patch_workers = patch.object(e, 'WORKERS', 1)
        self.patch_model = patch.object(e, 'model_from_env', side_effect=self.model)
        for patcher in (self.patch_root, self.patch_workers, self.patch_model):
            patcher.start()
            self.addCleanup(patcher.stop)

    def model(self, profile):
        self.assertEqual(profile, e.MODEL_PROFILE)
        model = FakeModel(calls=self.calls, **self.model_options)
        return model

    def test_label_free_model_requests_and_private_signals_never_enter_development_files(self):
        prepared = e.prepare_evidence(self.data)
        self.assertEqual(len(self.calls), 2)
        for call in self.calls:
            self.assertEqual(call['tools'], [])
            serialized = json.dumps(call)
            for private in ('PRIVATE_DEV_LABEL', 'PRIVATE_HELD_LABEL', '"gold"', '"split"', '"answer"'):
                self.assertNotIn(private, serialized)
        rows = f.score(self.data['corpus'], self.data['questions'], [['paper:0'], ['paper:1']])
        files = f.development_files(prepared, {'rows': rows})
        visible = json.dumps(files)
        self.assertNotIn('held-private', visible)
        self.assertNotIn('private held-out query', visible)
        self.assertNotIn('PRIVATE_HELD_LABEL', visible)
        inputs = files['dev-inputs.json']
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]['evidence_ranking'], prepared['signals'][0]['evidence_ranking'])
        self.assertEqual(inputs[0]['evidence_status'], 'ok')
        self.assertEqual(prepared['features']['evidence_rerank']['current_usage']['requests'], 2)

    def test_cached_failures_are_replayed_without_new_model_calls(self):
        self.model_options['error'] = RuntimeError('transient failure')
        first = e.prepare_evidence(self.data)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(first['features']['evidence_rerank']['fallbacks'], 2)
        self.model_options.clear()
        second = e.prepare_evidence(self.data)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(second['signals'], first['signals'])
        stats = second['features']['evidence_rerank']
        self.assertEqual(stats['cache_hits'], 2)
        self.assertEqual(stats['fallbacks'], 2)
        self.assertEqual(stats['current_usage']['requests'], 0)
        self.assertEqual(stats['original_usage']['requests'], 2)

    def test_cache_identity_changes_with_model_source_query_and_implementation(self):
        first = e.prepare_evidence(self.data)
        original_keys = [item['cache_key'] for item in first['evidence_records']]
        e.prepare_evidence(self.data)
        self.assertEqual(len(self.calls), 2)
        # Labels are not part of generation identity and cannot trigger extra attempts.
        relabeled = copy.deepcopy(self.data)
        relabeled['questions'][0]['gold'] = []
        relabeled['questions'][1]['answer'] = 'new label only'
        same = e.prepare_evidence(relabeled)
        self.assertEqual([item['cache_key'] for item in same['evidence_records']], original_keys)
        self.assertEqual(len(self.calls), 2)
        for name, value in (('base_url', 'https://different.invalid/v1'),
                            ('name', 'different-model'), ('temperature', .5)):
            with self.subTest(model_field=name), patch.object(FakeModel, name, value):
                changed = e.prepare_evidence(self.data)
                self.assertNotEqual([item['cache_key'] for item in changed['evidence_records']], original_keys)
        changed_data = copy.deepcopy(self.data)
        changed_data['corpus'][0]['content'] += ' revised original'
        changed = e.prepare_evidence(changed_data)
        self.assertNotEqual(changed['evidence_records'][0]['cache_key'], original_keys[0])
        changed_data = copy.deepcopy(self.data)
        changed_data['questions'][0]['query'] += ' revised query'
        changed = e.prepare_evidence(changed_data)
        self.assertNotEqual(changed['evidence_records'][0]['cache_key'], original_keys[0])
        with patch.object(e, 'fingerprint', return_value='new-implementation'):
            changed = e.prepare_evidence(self.data)
        self.assertNotEqual(changed['evidence_records'][0]['cache_key'], original_keys[0])

    def test_corrupted_cache_result_is_rejected_without_retry(self):
        prepared = e.prepare_evidence(self.data)
        target = self.root / 'data/v4-evidence-signals' / (prepared['evidence_records'][0]['cache_key'] + '.json')
        saved = json.loads(target.read_text(encoding='utf-8'))
        saved['result']['ranking'] = ['unknown']
        target.write_text(json.dumps(saved), encoding='utf-8')
        before = len(self.calls)
        with self.assertRaisesRegex(ValueError, 'cache hash mismatch'):
            e.prepare_evidence(self.data)
        self.assertEqual(len(self.calls), before)


    def test_duplicate_query_in_parallel_uses_one_call_and_a_shared_cache_entry(self):
        import threading
        data = copy.deepcopy(self.data)
        data['questions'][1]['query'] = data['questions'][0]['query']
        data['signals'][1] = copy.deepcopy(data['signals'][0])
        original_complete = FakeModel.complete

        def delayed_complete(model, messages, tools):
            # Keep one request live long enough for the duplicate worker to overlap.
            threading.Event().wait(.03)
            return original_complete(model, messages, tools)

        with patch.object(e, 'WORKERS', 4), patch.object(FakeModel, 'complete', delayed_complete):
            prepared = e.prepare_evidence(data)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(prepared['signals'][0], prepared['signals'][1])
        self.assertEqual(prepared['evidence_records'][0]['cache_key'],
                         prepared['evidence_records'][1]['cache_key'])
        self.assertEqual(prepared['features']['evidence_rerank']['cache_hits'], 1)
        self.assertEqual(prepared['features']['evidence_rerank']['current_usage']['requests'], 1)


    def test_inflight_response_is_cached_before_cancellation_and_resume_spends_no_new_call(self):
        import threading
        data = copy.deepcopy(self.data)
        data['questions'] = data['questions'][:1]
        data['signals'] = data['signals'][:1]
        stop = threading.Event()
        original_complete = FakeModel.complete

        def finish_then_cancel(model, messages, tools):
            result = original_complete(model, messages, tools)
            stop.set()
            return result

        with patch.object(FakeModel, 'complete', finish_then_cancel):
            with self.assertRaises(InterruptedError):
                e.prepare_evidence(data, cancelled=stop.is_set)
        self.assertEqual(len(self.calls), 1)
        cache_files = list((self.root / 'data/v4-evidence-signals').glob('*.json'))
        self.assertEqual(len(cache_files), 1)
        saved = json.loads(cache_files[0].read_text(encoding='utf-8'))
        self.assertEqual(saved['result']['status'], 'ok')
        self.assertEqual(saved['result']['usage']['requests'], 1)
        stop.clear()
        restored = e.prepare_evidence(data, cancelled=stop.is_set)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(restored['signals'][0]['evidence_ranking'], saved['result']['ranking'])
        self.assertEqual(restored['features']['evidence_rerank']['cache_hits'], 1)
        self.assertEqual(restored['features']['evidence_rerank']['current_usage']['requests'], 0)
        self.assertEqual(restored['features']['evidence_rerank']['original_usage']['requests'], 1)

    def test_cancelled_feature_preparation_never_calls_model(self):
        with self.assertRaises(InterruptedError):
            e.prepare_evidence(self.data, cancelled=lambda: True)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
