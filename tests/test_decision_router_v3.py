"""Offline v3 freeze, first-run and reporting contracts; no model downloads."""
import copy
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evals.build_intent_routing_v3 import cases_hash, validate
from evals.report_decision_router_v3 import confirmation_impact, paired_no_bootstrap, summarize_parts
from research_agent.decision_router import LABELS


def panel():
    cases = []
    specs = [(label, 'regular', None) for label in LABELS for _ in range(15)]
    specs += [('CHAT', 'auto_boundary', 'missing_topic')]*15
    specs += [('AUTO_RESEARCH', 'auto_boundary', 'resolvable_topic')]*10
    specs += [('RESEARCH', 'auto_boundary', 'research_only')]*10
    specs += [(LABELS[i % 7], 'colloquial', None) for i in range(20)]
    counts = Counter()
    for i, (label, part, kind) in enumerate(specs):
        scope = ('auto', 'web', 'arxiv')[counts[label] % 3]
        counts[label] += 1
        c = {'id': f'v3-fixture-{i}', 'split': 'test_v3', 'part': part, 'expected_intent': label,
             'rationale': 'Fixture for structural validation only', 'tags': ['fixture'], 'origin': {'kind': 'authored'},
             'search_scope': scope, 'space': {'name': 'fixture', 'description': 'fixture', 'local_material_count': 0},
             'history': [{'role': 'user', 'content': f'Unique fixture question {i}'}],
             'annotation': {'generator': 'author', 'reviewer': 'reviewer', 'gold_labels_visible': True,
                            'model_predictions_read': False, 'prior_dataset_examples_read': False, 'human_annotated': False}}
        if kind:
            c['boundary_kind'] = kind
        cases.append(c)
    return {'cases': cases, 'label_audit': {'reviewer': 'reviewer', 'model_predictions_read': False,
            'human_annotated': False, 'gold_labels_visible': True, 'reviewed_cases_sha256': cases_hash(cases),
            'case_reviews': [{'id': c['id']} for c in cases]}}


class V3Contracts(unittest.TestCase):
    def test_frozen_json_publication_has_one_winner_and_no_partial_file(self):
        from evals.build_intent_routing_v2 import write_atomic, read
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)/'plan.json'
            def publish(i):
                try:
                    write_atomic(destination, {'writer': i, 'complete': list(range(100))})
                    return i
                except FileExistsError:
                    return None
            with ThreadPoolExecutor(max_workers=8) as pool:
                winners = [i for i in pool.map(publish, range(8)) if i is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(read(destination), {'writer': winners[0], 'complete': list(range(100))})
            self.assertFalse(list(Path(tmp).glob('*.tmp')))
            write_atomic(destination, {'manifest': 'updated'}, replace=True)
            self.assertEqual(read(destination), {'manifest': 'updated'})

    def test_missing_cost_receipts_are_unknown_and_not_zero(self):
        from evals.report_decision_router_v3 import cost_summary
        protocol = {'run_error': 'write failed', 'budget_final': {'attempts_admitted': 7,
            'accounted_tokens_including_unknown_reservations': 7000, 'pending_reserved_tokens': 0, 'unknown_usage_attempts': 0}}
        cost = cost_summary(protocol, [], remote=True)
        self.assertIsNone(cost['requests'])
        self.assertEqual(cost['admissions_without_receipts'], 7)
        self.assertFalse(cost['reported_tokens_complete'])
        protocol['budget_final']['attempts_admitted'] = 1
        with self.assertRaisesRegex(ValueError, 'token receipts disagree'):
            cost_summary(protocol, [{'attempts': 1, 'usage': [{'total_tokens': 1}]}], remote=True)

    def test_four_b_selection_rejects_mislabeled_protocol(self):
        from evals.report_decision_router_v3 import select_four_b
        from research_agent.decision_router import prompt_hash
        protocol = {'split': 'dev_v2', 'order': 'forward', 'group': 'C1', 'model': 'Qwen3-4B',
                    'temperature': 1., 'panel_sha256': 'hash', 'prompt_sha256': prompt_hash(), 'weights_sha256': 'weights'}
        with tempfile.TemporaryDirectory() as tmp:
            for changes in ({'order': 'reverse'}, {'model': 'Qwen3-0.6B'}, {'adapter_path': 'adapter'}, {'split': 'test_v2'}):
                def fake_read(path):
                    return {**protocol, **changes} if Path(path).name == 'protocol.json' else {'macro_f1': .9}
                with patch('evals.build_intent_routing_v2.verify_frozen', return_value={'cases': []}), \
                        patch('evals.report_decision_router_v3.read', side_effect=fake_read), \
                        patch('evals.report_decision_router_v3.sha', return_value='hash'):
                    with self.assertRaisesRegex(ValueError, 'identity or order mismatch'):
                        select_four_b(Path(tmp), Path(tmp)/'config.json')

    def test_worker_write_failure_retains_other_rows_and_logged_cost(self):
        from evals.build_intent_routing_v2 import write, read
        from evals.run_decision_router_eval import run
        from evals.report_decision_router_v3 import cost_summary
        cases = panel()['cases'][:2]
        with tempfile.TemporaryDirectory() as tmp:
            out, plan_path = Path(tmp)/'run', Path(tmp)/'plan.json'
            write(plan_path, {'panel_sha256': 'hash', 'source_files': {}, 'four_b_selection': {'include_cascade': False},
                             'groups': dict.fromkeys(('A', 'D', 'E2', 'C1-4B'), {})})
            args = SimpleNamespace(group='A', split='test_v3', adapter=None, config=None, out=out, plan=plan_path,
                order='forward', max_chars=6000, model=Path('fake'), remote_model='fake')
            def fake_remote(case, folder, model_id, budget, router=None):
                reserved = budget.reserve([], [])
                budget.finish(reserved, {'total_tokens': 10})
                if case['id'] in {cases[0]['id'], 'warmup-0'}:
                    write(folder/'cases'/case['id']/'requests/001.json', {'usage': {'total_tokens': 10}})
                    raise OSError('result write failed')
                return {'id': case['id'], 'response': {'intent': 'CHAT'}, 'error': None, 'seconds': 1.,
                        'intent_seconds': 1., 'route_seconds': 1., 'attempts': 1, 'usage': [{'total_tokens': 10}]}
            with patch('evals.build_intent_routing_v3.verify_frozen', return_value={'cases': cases}), \
                    patch('evals.run_decision_router_eval.evaluation_identity', return_value={}), \
                    patch('evals.run_decision_router_eval.source_manifest', return_value={}), \
                    patch('evals.run_decision_router_eval.sha', return_value='hash'), \
                    patch('evals.run_decision_router_eval.hardware', return_value={}), \
                    patch('evals.run_decision_router_eval.reserve_holdout'), \
                    patch('evals.run_decision_router_eval.model_from_env', return_value=SimpleNamespace(name='fake', base_url='https://example.invalid')), \
                    patch('evals.run_decision_router_eval.load_dotenv'), \
                    patch('evals.run_decision_router_eval.guarded_remote', side_effect=fake_remote), \
                    patch('torch.cuda.is_available', return_value=False):
                run(args)
            rows, protocol = read(out/'results.json'), read(out/'protocol.json')
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]['error'])
            self.assertIsNone(rows[1]['error'])
            self.assertEqual(len(protocol['remote_warmup_results']), 5)
            cost = cost_summary(protocol, rows, remote=True)
            self.assertEqual((cost['requests'], cost['total_tokens']), (7, 70))
            self.assertTrue(cost['reported_tokens_complete'])

    def test_frozen_v3_manifest_and_review_integrity(self):
        from evals.build_intent_routing_v3 import verify_frozen
        frozen = verify_frozen()
        self.assertEqual(len(frozen['cases']), 160)
        self.assertEqual(len(frozen['label_audit']['case_reviews']), 160)
        self.assertFalse(frozen['label_audit']['model_predictions_read'])
        self.assertLess(frozen['dedup']['semantic']['max_cosine'], .92)

    def test_failed_initialization_still_produces_complete_report(self):
        from evals.report_decision_router_v3 import report
        data = panel()
        data['dedup'] = {'passed': True}
        rows = [{'id': c['id'], 'response': None, 'error': 'initialization failed',
                 'attempts': 0, 'usage': [], 'seconds': 0} for c in data['cases']]
        groups = dict.fromkeys(('A', 'D', 'E2', 'C1-4B'), {})
        plan = {'panel_sha256': 'hash', 'source_files': {}, 'groups': groups, 'selection_artifacts': {},
                'four_b_selection': {'include_cascade': False, 'selected': {'macro_f1': .6}, 'd_macro_f1': .9}}
        protocol = {'evaluation_identity': {}, 'source_files': {}, 'plan_sha256': 'hash', 'panel_sha256': 'hash',
                    'results_sha256': 'hash', 'temperature': 1., 'run_error': 'initialization failed',
                    'remote_warmup_results': [{'id': 'warmup-0', 'error': 'write failed', 'attempts': 0,
                                               'usage': [], 'collection_failed': True}]}
        old = {'groups': {'E': {'rows': []}}, 'regression_v1': {'E': {'rows': []}}}
        with tempfile.TemporaryDirectory() as tmp:
            study = Path(tmp)/'study'
            def fake_read(path):
                path = Path(path)
                if path.name == 'test-plan.json':
                    return plan
                if path.name == 'plan.json':
                    return {'plan_sha256': 'hash', 'plan': plan}
                if path.stem in groups:
                    return {'plan_sha256': 'hash', 'out': str(study/('test-'+path.stem))}
                if path.name == 'protocol.json':
                    return protocol
                if path.name == 'results.json':
                    return rows
                if path.name == 'decision-router-results-20261018.json':
                    return old
                if path.name == 'decision_router_c1_4b.json':
                    return {'order': 'reverse'}
                return {}
            with patch('evals.build_intent_routing_v3.verify_frozen', return_value=data), \
                    patch('evals.run_decision_router_eval.source_manifest', return_value={}), \
                    patch('evals.report_decision_router_v3.selection_artifacts', return_value={}), \
                    patch('evals.report_decision_router_v3.sha', return_value='hash'), \
                    patch('evals.report_decision_router_v3.read', side_effect=fake_read):
                out = Path(tmp)/'report'
                report(study, out)
                result = json.loads(out.with_suffix('.json').read_text(encoding='utf-8'))
                metrics = result['groups']['E2']['metrics']
                self.assertEqual((metrics['n'], metrics['correct'], metrics['errors_or_missing']), (160, 0, 160))
                self.assertEqual(result['groups']['E2']['cost_including_warmup']['requests'], 0)
                self.assertIn('无观测', out.with_suffix('.md').read_text(encoding='utf-8'))
                plan['selection_artifacts'] = {'changed': 'receipt'}
                with self.assertRaisesRegex(ValueError, 'selection artifacts changed'):
                    report(study, Path(tmp)/'changed')
                plan['selection_artifacts'] = {}
                rows[0].update(attempts=1, usage=[{'total_tokens': 3}])
                with self.assertRaisesRegex(ValueError, 'receipts require a budget'):
                    report(study, Path(tmp)/'unaccounted')

    def test_final_review_binds_inputs_and_balanced_partition(self):
        data = panel()
        validate(data)
        changed = copy.deepcopy(data)
        changed['cases'][0]['history'][0]['content'] += ' unreviewed edit'
        with self.assertRaisesRegex(ValueError, 'review receipt'):
            validate(changed)
        for mutation in (lambda p: p['cases'].pop(),
                         lambda p: p['cases'][0]['annotation'].update(model_predictions_read=True),
                         lambda p: p['cases'][0]['annotation'].update(reviewer='author'),
                         lambda p: p['cases'][105].update(expected_intent='AUTO_RESEARCH')):
            changed = copy.deepcopy(data)
            mutation(changed)
            with self.assertRaises(ValueError):
                validate(changed)
        for mutate in (lambda c: c.pop('origin'), lambda c: c['space'].pop('name'),
                       lambda c: c['space'].pop('description'), lambda c: c.update(tags='string-not-list')):
            changed = copy.deepcopy(data)
            mutate(changed['cases'][0])
            changed['label_audit']['reviewed_cases_sha256'] = cases_hash(changed['cases'])
            with self.assertRaisesRegex(ValueError, 'metadata'):
                validate(changed)
        changed = copy.deepcopy(data)
        for c in changed['cases']:
            if c['expected_intent'] == 'RESEARCH':
                c['search_scope'] = 'web'
        with self.assertRaisesRegex(ValueError, 'imbalance'):
            validate(changed)

    def test_wrong_groups_cannot_consume_v3_or_alias_old_slots(self):
        from evals.run_decision_router_eval import run
        for group, split in [('E', 'test_v3'), ('B', 'test_v3'), ('C0', 'test_v3'), ('E2', 'test_v2')]:
            with patch('evals.run_decision_router_eval.reserve_holdout') as reserve:
                with self.assertRaisesRegex(ValueError, 'Group not permitted|names require'):
                    run(SimpleNamespace(group=group, split=split))
                reserve.assert_not_called()

    def test_v3_plan_source_drift_rejected_before_slot_or_output(self):
        from evals.run_decision_router_eval import run
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(group='A', split='test_v3', adapter=None, config=None, plan=Path(tmp)/'plan',
                                   out=Path(tmp)/'out')
            frozen = {'panel_sha256': 'hash', 'four_b_selection': {'include_cascade': False},
                      'groups': dict.fromkeys(('A', 'D', 'E2', 'C1-4B'), {}), 'source_files': {'old': 'sha'}}
            with patch('evals.build_intent_routing_v3.verify_frozen', return_value=panel()), \
                    patch('evals.run_decision_router_eval.evaluation_identity', return_value={}), \
                    patch('evals.run_decision_router_eval.read', return_value=frozen), \
                    patch('evals.run_decision_router_eval.sha', return_value='hash'), \
                    patch('evals.run_decision_router_eval.source_manifest', return_value={'new': 'sha'}), \
                    patch('evals.run_decision_router_eval.reserve_holdout') as reserve:
                with self.assertRaisesRegex(ValueError, 'source files changed'):
                    run(args)
                reserve.assert_not_called()
                self.assertFalse(args.out.exists())

    def test_confirmation_impact_distinguishes_field_failure_from_final_acceptance(self):
        rows = [{'id': str(i), 'decision': {'intent': label, 'fallback': fallback, 'field_completion': fields}}
                for i, (label, fallback, fields) in enumerate([
                    ('AUTO_RESEARCH', False, True), ('AUTO_RESEARCH', True, True),
                    ('AUTO_RESEARCH', True, False), ('CHAT', False, True)])]
        bundle = {'groups': {'E': {'rows': rows}}, 'regression_v1': {'E': {'rows': rows}}}
        result = confirmation_impact(bundle)['splits']['test_v2']
        self.assertEqual(result['affected_cases'], 2)
        self.assertEqual(result['finally_accepted_cases'], 1)
        self.assertEqual(result['expected_remote_request_delta_without_retries'], -1)
        self.assertNotIn('accuracy', result)

    def test_pairing_and_part_scores_keep_errors_and_missing_in_denominator(self):
        cases = panel()['cases']
        first = [{'id': c['id'], 'response': {'intent': c['expected_intent']}, 'error': None,
                  'seconds': 1, 'attempts': 0, 'usage': []} for c in cases]
        second = copy.deepcopy(first)
        first[0]['error'] = 'parse failure'
        second[1]['response']['intent'] = 'EXPERIMENT'
        second.pop()
        paired = paired_no_bootstrap(cases, first, second)
        self.assertEqual(paired['n'], 160)
        self.assertAlmostEqual(paired['accuracy_difference_e2_minus_a'], 1/160)
        self.assertEqual((paired['e2_only_correct'], paired['a_only_correct']), (2, 1))
        self.assertFalse(any('bootstrap' in k for k in paired))
        parts = summarize_parts(cases, second)
        self.assertEqual(parts['colloquial']['n'], 20)
        self.assertEqual(parts['colloquial']['errors_or_missing'], 1)
        self.assertEqual(parts['missing_topic']['outcomes'], {'CHAT': 15})


if __name__ == '__main__':
    unittest.main()
