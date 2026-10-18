"""Offline numerical, cascade and default-routing contract checks; no model downloads."""
import json
from contextlib import closing
import math
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_agent.contracts import ModelDecision
from research_agent.decision_router import (LABELS, DecisionRouter, OptionScorer, complete_route_fields,
    option_logprobs, probabilities, select_router, state_text, prompt_hash, policy, validate_config)
from research_agent.workbench import _parse_route, route_intent, Workbench
from research_agent.workbench_store import WorkbenchStore
from evals.calibrate_decision_router import distribution_metrics, fit_temperature, fit_thresholds, paired_comparison

ROOT = Path(__file__).resolve().parents[1]
HISTORY = [{'role': 'user', 'content': '请帮我下载这篇论文。'}]
SPACE = {'name': '路由测试', 'description': '科研资料', 'local_material_count': 2}


def route(label='CHAT'):
    return {'intent': label, 'reply': '请补充主题。' if label == 'CHAT' else '',
            'brief': '比较两个明确的方法。' if label in {'RESEARCH', 'AUTO_RESEARCH'} else '',
            'assumptions': [], 'effort': 'quick' if label == 'RESEARCH' else 'none'}


class LLM:
    name = 'fake'
    def __init__(self, *labels):
        self.labels, self.requests = iter(labels or ('CHAT',)), []

    def complete(self, messages, tools):
        self.requests.append((messages, tools))
        return ModelDecision('final', content=json.dumps(route(next(self.labels))))


class Scorer:
    weights_sha256, adapter_sha256, prompt_sha256 = 'weights', None, prompt_hash()
    def __init__(self, label='DOWNLOAD', confidence=.99):
        self.label, self.confidence = label, confidence

    def score(self, *args, **kwargs):
        other = (1-self.confidence)/6
        return {'intent': self.label, 'probabilities': {k: self.confidence if k == self.label else other for k in LABELS},
                'p_max': self.confidence, 'margin': self.confidence-other, 'intent_seconds': .001}


def config(**values):
    return {'schema_version': 1, 'calibrated': True, 'order': 'forward', 'temperature': 1., 'max_chars': 6000,
            'tau_intent': .9, 'tau_margin': .5, 'tau_auto': .999, 'score_timeout_seconds': 1.,
            'weights_sha256': 'weights', 'adapter_sha256': None, 'prompt_sha256': Scorer.prompt_sha256, **values}


class DecisionRouterTests(unittest.TestCase):
    def test_softmax_temperature_and_input_validation(self):
        p = probabilities([1, 0, 0, 0, 0, 0, 0], 2)
        self.assertAlmostEqual(sum(p), 1)
        self.assertAlmostEqual(p[0]/p[1], math.exp(.5))
        for logits, temperature in [([1]*6, 1), ([math.nan]*7, 1), ([0]*7, 0)]:
            with self.assertRaises(ValueError):
                probabilities(logits, temperature)

    def test_exact_multitoken_sums_and_single_prefill(self):
        import torch
        class Cache:
            def batch_repeat_interleave(self, n):
                self.n = n
        class Model:
            def __init__(self):
                self.calls = []
            def __call__(self, input_ids, past_key_values=None, **kwargs):
                self.calls.append((input_ids.clone(), past_key_values))
                if past_key_values is None:
                    logits = torch.arange(10, dtype=torch.float).reshape(1, 1, 10)
                    return SimpleNamespace(logits=logits, past_key_values=Cache())
                logits = torch.stack([torch.arange(10, dtype=torch.float)*(-1 if int(t) % 2 else 1) for t in input_ids[:, 0]])[:, None, :]
                return SimpleNamespace(logits=logits, past_key_values=past_key_values)
        model = Model()
        candidates = [[1, 8], [2, 9], [3], [4, 8], [5, 9], [6], [7, 8]]
        with torch.no_grad():
            actual, _ = option_logprobs(model, torch.tensor([[4, 5, 6]]), candidates)
        first = torch.arange(10, dtype=torch.float).log_softmax(0)
        negative = (-torch.arange(10, dtype=torch.float)).log_softmax(0)
        self.assertAlmostEqual(actual[0].item(), (first[1]+negative[8]).item(), places=5)
        self.assertAlmostEqual(actual[1].item(), (first[2]+first[9]).item(), places=5)
        self.assertAlmostEqual(actual[2].item(), first[3].item(), places=5)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[1][0].shape[0], 7)

    def test_order_average_aligns_intents_before_average(self):
        import torch
        scorer = object.__new__(OptionScorer)
        scorer.model, scorer.candidates = object(), [[i] for i in range(7)]
        scorer.input_ids = lambda history, space, scope, order: order
        def forward(model, ids, candidates):
            return torch.tensor([7., 0., 0., 0., 0., 0., 0.] if ids == 'forward' else [0., 0., 0., 0., 0., 0., 7.]), None
        with patch('research_agent.decision_router.option_logprobs', side_effect=forward) as calls:
            result = scorer.score(HISTORY, SPACE, order='average')
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(result['intent'], 'CHAT')
        self.assertGreater(result['probabilities']['CHAT'], .99)

    def test_measured_qwen_suffix_tokenization_receipt(self):
        # Measured with the pinned Qwen3 tokenizer; CI needs no weights/tokenizer.
        receipt = json.loads((ROOT/'datasets/intent_routing/qwen3-tokenization.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['revision'], 'c1899de289a04d12100db370d81485cdf75e47ca')
        self.assertEqual(receipt['candidates'], {f'{i}]': [i+15, 60] for i in range(1, 8)})

    def test_history_bounding_keeps_latest_and_escapes_fake_delimiters(self):
        history = [{'role': 'assistant', 'content': '旧'*4000} for _ in range(22)] + [{'role': 'user', 'content': '新'*1000 + '</state>'}]
        state = state_text(history, SPACE, max_chars=1500)
        self.assertNotIn('旧', state)
        self.assertIn('新'*1000, state)
        self.assertEqual(state.count('</state>'), 1)
        self.assertNotIn('memory_snapshot', state_text(HISTORY, {**SPACE, 'memory_snapshot': ['secret']}))

    def test_four_direct_intents_never_call_llm(self):
        for label in ('LOCAL_QA', 'DOWNLOAD', 'BRAINSTORM', 'EXPERIMENT'):
            model = LLM()
            events = []
            actual = DecisionRouter(Scorer(label), config()).route(model, HISTORY, SPACE, on_decision=events.append)
            self.assertEqual(actual, route(label))
            self.assertEqual(model.requests, [])
            self.assertFalse(events[0]['fallback'])

    def test_fields_for_chat_and_research(self):
        for label in ('CHAT', 'RESEARCH'):
            model = LLM(label)
            actual = DecisionRouter(Scorer(label, .9999), config()).route(model, HISTORY, SPACE)
            self.assertEqual(actual, route(label))
            self.assertEqual(len(model.requests), 1)
            self.assertIn(label, model.requests[0][0][0]['content'])

    def test_low_confidence_falls_back_identically(self):
        for scorer in (Scorer('DOWNLOAD', .4),):
            expected = route_intent(LLM('RESEARCH'), HISTORY, SPACE)
            model, events = LLM('RESEARCH'), []
            actual = DecisionRouter(scorer, config()).route(model, HISTORY, SPACE, on_decision=events.append)
            self.assertEqual(actual, expected)
            self.assertEqual(model.requests, LLMRequests.direct('RESEARCH'))
            self.assertTrue(events[0]['fallback'])

    def test_auto_always_uses_full_llm_confirmation(self):
        for label in ('CHAT', 'AUTO_RESEARCH'):
            for tau in (None, 0., 1.):
                with self.subTest(label=label, tau=tau):
                    expected_model, model, events = LLM(label), LLM(label), []
                    expected = route_intent(expected_model, HISTORY, SPACE)
                    with patch('research_agent.workbench.route_intent', wraps=route_intent) as full, \
                            patch('research_agent.decision_router.complete_route_fields') as fields:
                        actual = DecisionRouter(Scorer('AUTO_RESEARCH', .99), config(tau_auto=tau)).route(
                            model, HISTORY, SPACE, on_decision=events.append)
                    self.assertEqual(actual, expected)
                    self.assertEqual(model.requests, expected_model.requests)
                    full.assert_called_once_with(model, HISTORY, SPACE, 'auto')
                    fields.assert_not_called()
                    self.assertTrue(events[0]['fallback'])
                    self.assertEqual(events[0]['fallback_reason'], 'auto_research_confirm')
                    self.assertFalse(events[0]['field_completion'])

    def test_auto_confirmation_precedes_confidence_and_never_retries_failed_route(self):
        events = []
        with patch('research_agent.workbench.route_intent', side_effect=RuntimeError('remote failed')) as full:
            with self.assertRaisesRegex(RuntimeError, 'remote failed'):
                DecisionRouter(Scorer('AUTO_RESEARCH', .2), config(reject_all=True)).route(
                    LLM(), HISTORY, SPACE, on_decision=events.append)
        full.assert_called_once()
        self.assertEqual(events[0]['fallback_reason'], 'auto_research_confirm')
        self.assertGreaterEqual(events[0]['route_seconds'], 0)

    def test_auto_decision_mode_preserves_fixed_intent_and_policy_validation(self):
        model, events = LLM('AUTO_RESEARCH'), []
        with patch('research_agent.workbench.route_intent') as full:
            result = DecisionRouter(Scorer('AUTO_RESEARCH'), config(), mode='decision').route(
                model, HISTORY, SPACE, on_decision=events.append)
        self.assertEqual(result, route('AUTO_RESEARCH'))
        full.assert_not_called()
        self.assertTrue(events[0]['field_completion'])
        self.assertFalse(events[0]['fallback'])
        validate_config(config(auto_research_policy='llm_confirm'))
        validate_config(config())  # Archived schema-v1 configs remain readable.
        with self.assertRaisesRegex(ValueError, 'auto_research_policy'):
            validate_config(config(auto_research_policy='local'))

    def test_field_intent_mutation_rejected_then_full_fallback(self):
        model, events = LLM('AUTO_RESEARCH', 'CHAT'), []
        result = DecisionRouter(Scorer('RESEARCH'), config()).route(model, HISTORY, SPACE, on_decision=events.append)
        self.assertEqual(result['intent'], 'CHAT')
        self.assertEqual(len(model.requests), 2)
        self.assertIn('changed intent', events[0]['fallback_reason'])

    def test_scorer_failure_timeout_and_identity_mismatch(self):
        bad = Scorer()
        bad.score = lambda *a, **k: (_ for _ in ()).throw(MemoryError('oom'))
        slow = Scorer()
        slow.score = lambda *a, **k: time.sleep(.04)
        for scorer, settings in [(bad, config()), (slow, config(score_timeout_seconds=.005)),
                                  (Scorer(), config(weights_sha256='different')), (Scorer(), config(calibrated=False))]:
            events = []
            result = DecisionRouter(scorer, settings).route(LLM(), HISTORY, SPACE, on_decision=events.append)
            self.assertEqual(result, route())
            self.assertTrue(events[0]['fallback_reason'])

    def test_low_confidence_decision_mode_does_not_fall_back(self):
        model = LLM()
        result = DecisionRouter(Scorer('EXPERIMENT', .4), config(calibrated=False), mode='decision').route(model, HISTORY, SPACE)
        self.assertEqual(result['intent'], 'EXPERIMENT')
        self.assertFalse(model.requests)

    def test_changed_live_policy_cannot_use_cached_calibration(self):
        router, events = DecisionRouter(Scorer('DOWNLOAD'), config()), []
        changed = {**policy(), 'DOWNLOAD': 'changed definition during deployment'}
        with patch('research_agent.decision_router.policy', return_value=changed):
            result = router.route(LLM('CHAT'), HISTORY, SPACE, on_decision=events.append)
        self.assertEqual(result['intent'], 'CHAT')
        self.assertIn('prompt changed', events[0]['fallback_reason'])

    def test_invalid_completion_uses_production_validation(self):
        class Invalid:
            def complete(self, *args):
                return ModelDecision('final', content=json.dumps({**route('RESEARCH'), 'effort': 'infinite'}))
        with self.assertRaises(ValueError):
            complete_route_fields(Invalid(), 'RESEARCH', HISTORY, SPACE)
        with self.assertRaises(ValueError):
            _parse_route(ModelDecision('final', content=json.dumps({**route(), 'assumptions': [42]})))

    def test_default_dispatch_is_exact_original_request(self):
        before, after = LLM('CHAT'), LLM('CHAT')
        expected = route_intent(before, HISTORY, SPACE)
        with patch.dict(os.environ, {}, clear=True), patch('research_agent.decision_router._router', side_effect=AssertionError('default must not load local model')):
            self.assertEqual(select_router(after, HISTORY, SPACE), expected)
        self.assertEqual(before.requests, after.requests)

    def test_workbench_sync_and_pending_use_selected_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ROUTER_MODE': 'llm'}):
            store = WorkbenchStore(Path(tmp)/'state.db')
            space = store.save_space({'name': 'test'})['id']
            conversation = store.create_conversation(space, 'test')['id']
            model = LLM('CHAT', 'CHAT')
            app = Workbench(store, lambda: model, lambda observer: None, Path(tmp)/'traces', start_worker=False)
            try:
                with patch('research_agent.workbench.select_router', wraps=select_router) as dispatch:
                    self.assertEqual(app.send(space, conversation, '你好')['intent'], 'CHAT')
                    app.send(space, conversation, '你好', background=True)
                    app.execute(store.claim_next())
                    self.assertEqual(dispatch.call_count, 2)
                    self.assertEqual(len(model.requests), 2)
            finally:
                app.close()

    def test_pending_cascade_writes_decision_to_trace_and_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ROUTER_MODE': 'cascade'}):
            store = WorkbenchStore(Path(tmp)/'state.db')
            space = store.save_space({'name': 'test'})['id']
            conversation = store.create_conversation(space, 'test')['id']
            model = LLM('CHAT')
            app = Workbench(store, lambda: model, lambda observer: None, Path(tmp)/'traces', start_worker=False)
            try:
                with patch('research_agent.decision_router._router', return_value=DecisionRouter(Scorer('CHAT'), config())):
                    pending = app.send(space, conversation, '你好', background=True)
                    app.execute(store.claim_next())
                path = Path(tmp)/'traces'/('intent-pending-'+pending['task_id']+'.jsonl')
                events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
                self.assertEqual(events[-1]['event'], 'intent_decision')
                self.assertTrue(events[-1]['field_completion'])
                with closing(store._connect()) as db:
                    saved = db.execute("SELECT payload FROM conversation_events WHERE kind='intent_decision'").fetchall()
                self.assertEqual(len(saved), 1)
            finally:
                app.close()


class LLMRequests:
    @staticmethod
    def direct(label):
        model = LLM(label)
        route_intent(model, HISTORY, SPACE)
        return model.requests


class CalibrationTests(unittest.TestCase):
    def cases(self):
        return [{'id': str(i), 'expected_intent': label} for i, label in enumerate(LABELS)]

    def test_temperature_reduces_nll_and_thresholds_bound_risk(self):
        cases = self.cases()
        rows = [{'id': str(i), 'logits': [3 if j == i else 0 for j in range(7)]} for i in range(7)]
        t = fit_temperature(cases, rows)
        self.assertLess(t, 1)
        self.assertLess(distribution_metrics(cases, rows, t)['nll'], distribution_metrics(cases, rows)['nll'])
        rows[0]['logits'] = [0]*6 + [8]
        selected, _ = fit_thresholds(cases, rows, 1)
        self.assertLessEqual(selected['risk'], .02)
        self.assertGreater(selected['tau_auto'], probabilities(rows[0]['logits'])[-1])
        self.assertLess(selected['coverage'], 1)

    def test_perfect_uniform_metrics_and_pairing(self):
        cases = self.cases()
        uniform = [{'id': str(i), 'logits': [0]*7} for i in range(7)]
        metrics = distribution_metrics(cases, uniform)
        self.assertAlmostEqual(metrics['nll'], math.log(7))
        self.assertAlmostEqual(metrics['brier'], 6/7)
        self.assertAlmostEqual(metrics['ece'], 0.)
        self.assertEqual(metrics['risk_coverage'][0]['accepted'], 7)
        rows = [{'id': str(i), 'response': {'intent': label}} for i, label in enumerate(LABELS)]
        paired = paired_comparison(cases, rows, rows)
        self.assertEqual(paired['paired_bootstrap_95'], [0., 0.])
        self.assertEqual(paired['mcnemar_exact_two_sided_p'], 1.)

    def test_frozen_data_integrity_and_provenance(self):
        from evals.build_intent_routing_v2 import verify_frozen
        panel = verify_frozen()
        ids = {split: {c['id'] for c in panel['cases'] if c['split'] == split} for split in ('train_v2', 'dev_v2', 'test_v2')}
        self.assertEqual(len(ids['dev_v2']), 70)
        self.assertEqual(len(ids['test_v2']), 140)
        self.assertFalse(ids['train_v2'] & ids['test_v2'])
        self.assertGreaterEqual(len(ids['train_v2']), 840)

    def test_training_ece_gate_uses_raw_logits_even_after_baseline_calibration(self):
        from evals.train_decision_router import train
        from evals.build_intent_routing_v2 import write
        cases = [{**c, 'split': 'dev_v2'} for c in self.cases()]
        rows = [{'id': str(i), 'logits': [1 if j == i else 0 for j in range(7)]} for i in range(7)]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            write(folder/'base'/'protocol.json', {'split': 'dev_v2', 'group': 'C1', 'panel_sha256': 'panel', 'temperature': .01, 'order': 'average', 'max_chars': 6000})
            write(folder/'base'/'results.json', rows)
            write(folder/'base'/'metrics.json', {'calibration': {'ece': .0001}})
            args = SimpleNamespace(baseline_run=folder/'base', out=folder/'train', learning_rates=[5e-5], seed=17,
                                   rank=16, modules=['q_proj'], kl_weight=1., anchor_positions=4, model=folder/'model', device='cpu')
            with patch.dict('sys.modules', {'peft': SimpleNamespace(LoraConfig=Mock(), get_peft_model=Mock()), 'torch': SimpleNamespace(manual_seed=lambda seed: None)}), \
                    patch('evals.train_decision_router.verify_frozen', return_value={'cases': cases}), \
                    patch('evals.train_decision_router.sha', return_value='panel'), \
                    patch('evals.train_decision_router.source_manifest', return_value={}), \
                    patch('evals.train_decision_router.hardware', return_value={}), \
                    patch('evals.train_decision_router.OptionScorer', side_effect=RuntimeError('stop before model loading')):
                with self.assertRaisesRegex(RuntimeError, 'stop before model loading'):
                    train(args)
            saved = json.loads((folder/'train'/'protocol.json').read_text(encoding='utf-8'))
            self.assertAlmostEqual(saved['baseline_ece'], distribution_metrics(cases, rows)['ece'])


class EvaluationContractTests(unittest.TestCase):
    def test_concurrent_groups_only_observe_complete_plan(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from evals.run_decision_router_eval import reserve_holdout
        entered, release = threading.Event(), threading.Event()
        original = json.dump
        def paused(data, stream, *args, **kwargs):
            if 'plan' in data and threading.current_thread().name.startswith('slow-plan'):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release first writer')
            return original(data, stream, *args, **kwargs)
        with tempfile.TemporaryDirectory() as temporary, patch('evals.run_decision_router_eval.ROOT', Path(temporary)), \
                patch('evals.run_decision_router_eval.json.dump', side_effect=paused), \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='slow-plan') as pool:
            first = pool.submit(reserve_holdout, 'panel', 'A', Path(temporary)/'a', {'same': True}, 'same-hash')
            try:
                self.assertTrue(entered.wait(5))
                reserve_holdout('panel', 'B', Path(temporary)/'b', {'same': True}, 'same-hash')
            finally:
                release.set()
            first.result()

    def test_formal_slot_is_consumed_even_without_results(self):
        from evals.run_decision_router_eval import reserve_holdout
        with tempfile.TemporaryDirectory() as temporary, patch('evals.run_decision_router_eval.ROOT', Path(temporary)):
            reserve_holdout('frozen-panel', 'C1', Path(temporary)/'first', {'selected': 'original'}, 'first-plan')
            with self.assertRaises(FileExistsError):
                reserve_holdout('frozen-panel', 'C1', Path(temporary)/'retry', {'selected': 'original'}, 'first-plan')
            with self.assertRaisesRegex(ValueError, 'plan changed'):
                reserve_holdout('frozen-panel', 'D', Path(temporary)/'changed', {'selected': 'changed'}, 'new-plan')
            reserve_holdout('frozen-panel', 'D', Path(temporary)/'different-method', {'selected': 'original'}, 'first-plan')

    def test_calibration_mismatch_rejected_before_consuming_formal_slot(self):
        from evals.run_decision_router_eval import run
        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(group='C1', split='test_v2', adapter=None, config=Path(temporary)/'config.json',
                                   out=Path(temporary)/'result', order='forward', max_chars=6000)
            for changed in ({'order': 'average'}, {'max_chars': 1000}):
                with patch('evals.run_decision_router_eval.verify_frozen', return_value={'cases': []}), \
                        patch('evals.run_decision_router_eval.read', return_value=config(**changed)), \
                        patch('evals.run_decision_router_eval.reserve_holdout') as reserve:
                    with self.assertRaisesRegex(ValueError, 'calibration identity mismatch'):
                        run(args)
                    reserve.assert_not_called()
                    self.assertFalse(args.out.exists())

    def test_evaluation_identity_binds_model_and_encoder_weights(self):
        from evals.run_decision_router_eval import evaluation_identity
        args = SimpleNamespace(group='C0', split='test_v2', adapter=None, config=None, model=Path('first'),
                               encoder=Path('encoder'), order='forward', max_chars=6000, remote_model='remote')
        with patch('evals.run_decision_router_eval.weight_identity', side_effect=str):
            first = evaluation_identity(args, None)
            args.model = Path('different')
            self.assertNotEqual(first, evaluation_identity(args, None))
            args.group = 'B'
            self.assertEqual(evaluation_identity(args, None)['weights_sha256'], 'encoder')

    def test_embedding_default_device_uses_library_auto_selection(self):
        from evals.run_decision_router_eval import EmbeddingClassifier
        encoder, classifier = Mock(), Mock()
        with patch.dict('sys.modules', {'sentence_transformers': SimpleNamespace(SentenceTransformer=encoder),
                                       'sklearn.linear_model': SimpleNamespace(LogisticRegression=classifier)}):
            EmbeddingClassifier({'cases': []}, Path('encoder'), 'auto')
        self.assertIsNone(encoder.call_args.kwargs['device'])
        classifier.return_value.fit.assert_called_once()

    def test_embedding_context_keeps_entire_latest_message(self):
        from evals.run_decision_router_eval import EmbeddingClassifier
        scorer = object.__new__(EmbeddingClassifier)
        scorer.max_chars = 6000
        scorer.encoder = SimpleNamespace(max_seq_length=400, tokenizer=lambda value, **kwargs: {'input_ids': list(value)})
        case = {'history': [{'role': 'assistant', 'content': 'old'*150}, {'role': 'user', 'content': 'current'*15}],
                'space': SPACE, 'search_scope': 'auto'}
        value = scorer.input_text(case)
        self.assertIn('current'*15, value)
        self.assertNotIn('old', value)
        case['history'][-1]['content'] = 'current'*100
        with self.assertRaisesRegex(ValueError, 'exceed encoder context'):
            scorer.input_text(case)

    def test_missing_predictions_stay_in_denominator_and_calibration_reports_missing(self):
        from evals.run_decision_router_eval import summarize
        cases = [{'id': str(i), 'expected_intent': label, 'origin': {'kind': 'authored'}, 'tags': [],
                  'history': [{'role': 'user', 'content': '稳定问题'}]} for i, label in enumerate(LABELS)]
        rows = [{'id': str(i), 'response': {'intent': label}, 'logits': [3 if j == i else 0 for j in range(7)],
                 'error': None, 'intent_seconds': .1, 'route_seconds': None, 'attempts': 0, 'usage': []}
                for i, label in enumerate(LABELS[:-1])]
        metrics = summarize(cases, rows, .5)
        self.assertEqual((metrics['n'], metrics['correct'], metrics['errors_or_missing']), (7, 6, 1))
        self.assertEqual(metrics['calibration']['missing_probability_cases'], 1)
        self.assertIsNone(metrics['route_seconds']['p50'])
        self.assertLess(metrics['calibration']['nll'], summarize(cases, rows)['calibration']['nll'])
        missing = summarize(cases, [], cascade=True)
        self.assertEqual(missing['errors_or_missing'], 7)
        self.assertEqual(missing['cascade']['unobserved_cases'], 7)
        self.assertIsNone(missing['cascade']['fallback_rate'])
        self.assertIsNone(missing['cascade']['accepted'])


if __name__ == '__main__':
    unittest.main()
