"""Bounded research fan-out exercises SQLite, real agent loops and recovery."""
import json
import threading
import time
import unittest
from contextlib import closing
from dataclasses import asdict

from tests import test_auto_research as support
from research_agent import OfflineModel, ModelDecision
from research_agent.auto_research import authorization, AutoResearch
from research_agent.workbench_store import Conflict


TASKS = [dict(question='Research ReAct methods', mode='web', effort='quick'),
         dict(question='Research ReAct evaluation limits', mode='web', effort='quick')]


class ParallelResearchTests(unittest.TestCase):
    setUp = support.AutoResearchTests.setUp
    agent = support.AutoResearchTests.agent
    start = support.AutoResearchTests.start
    step = support.AutoResearchTests.step
    state = support.AutoResearchTests.state

    def dispatch_batch(self, budget=2):
        self.start(budget={'research_calls': budget})
        job = self.store.claim_next()
        state = self.state(job)
        state['context'] = {'history': [{'role': 'user', 'content': 'Shared prior constraint'}], 'memory': []}
        decision = asdict(support.call('research_parallel', {'tasks': TASKS}))
        state['pending'] = decision
        self.app.auto_research.dispatch(job, state, decision, json.loads(job['payload'])['auto_research'])
        return job, state, decision

    def test_batch_is_atomic_budgeted_idempotent_and_keeps_frozen_context(self):
        job, state, decision = self.dispatch_batch()
        ids = state['research'][:]
        self.assertEqual(len(ids), 2)
        # Simulate a crash after SQLite commit but before saving the wait state.
        recovered = self.state(job)
        recovered['context'] = state['context']
        self.app.auto_research.dispatch(job, recovered, decision, authorization({'research_calls': 2}))
        self.assertEqual(recovered['research'], ids)
        self.assertEqual(recovered['waiting']['ids'], ids)
        for child_id in ids:
            child = self.store.job(self.space, child_id)
            payload = json.loads(child['payload'])
            self.assertEqual(payload['auto_context_turn'], max(0, job['turn_seq'] - 1))
            self.assertEqual(payload['auto_context_revision'], 0)
            self.assertEqual(child['conversation_id'], self.chat)
            self.assertEqual(child['parent_message_id'], job['parent_message_id'])
        with self.assertRaises(Conflict):
            self.app.auto_research.dispatch(job, state, {**decision, 'call_id': 'second'}, authorization({'research_calls': 3}))
        self.assertEqual(len(self.store.jobs(self.space)), 3)

    def test_invalid_or_oversized_batch_creates_no_partial_jobs(self):
        self.start()
        job = self.store.claim_next()
        state = self.state(job)
        state['context'] = {}
        for tasks in ([TASKS[0]], TASKS + [TASKS[0]], [TASKS[0], {**TASKS[1], 'mode': 'shell'}], [TASKS[0], TASKS[0]]):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                self.app.auto_research.dispatch(job, state, asdict(support.call('research_parallel', {'tasks': tasks})), authorization())
        self.assertEqual(len(self.store.jobs(self.space)), 1)

    def test_siblings_overlap_but_later_turn_cannot_overtake_parent(self):
        job, state, _ = self.dispatch_batch()
        # A helper may not start while its controller is still running.
        self.assertIsNone(self.store.claim_next(parallel_only=True))
        self.app.auto_research.yield_job(job, state, waiting=True)
        first = self.store.claim_next()
        second = self.store.claim_next(parallel_only=True)
        self.assertEqual({first['id'], second['id']}, set(state['research']))
        self.assertIsNone(self.store.claim_next(parallel_only=True))
        self.store.message(self.space, self.chat, 'user', 'Later question')
        later = self.store.enqueue(self.space, self.chat, 'Later question', '', [])
        self.store.finish(first['id'], 'completed', 'First result')
        self.app.auto_research.tick()
        self.assertEqual(self.store.job(self.space, job['id'])['stage'], 'auto_waiting')
        self.assertIsNone(self.store.claim_next())
        self.store.finish(second['id'], 'failed', '', 'Upstream unavailable')
        self.app.auto_research.tick()
        self.assertEqual(self.store.claim_next()['id'], job['id'])
        self.store.finish(job['id'], 'completed', 'Partial result with failure')
        self.assertEqual(self.store.claim_next()['id'], later['id'])

    def test_cancelled_parent_cancels_every_child_and_no_new_calls(self):
        job, state, _ = self.dispatch_batch()
        self.app.auto_research.yield_job(job, state, waiting=True)
        self.store.claim_next(parallel_only=True)
        self.store.cancel(self.space, job['id'])
        self.app.auto_research.tick()
        self.assertTrue(all(self.store.job(self.space, i)['status'] == 'cancelled' for i in state['research']))
        self.model.complete.assert_not_called()

    def test_queued_child_cannot_send_forgotten_snapshot_before_parent_tick(self):
        job, state, _ = self.dispatch_batch()
        self.app.auto_research.yield_job(job, state, waiting=True)
        with closing(self.store._connect()) as db, db:
            self.app.memory.invalidate(db, self.space)
        child = self.store.claim_next(parallel_only=True)
        self.assertIsNotNone(child)
        # The helper can claim before the controller worker's next tick.
        self.app.execute(child)
        self.assertEqual(self.store.job(self.space, child['id'])['status'], 'cancelled')
        self.model.complete.assert_not_called()
        self.assertFalse(list((self.root / 'traces').glob('*.jsonl')))

    def test_cancel_running_parent_stops_both_children_before_another_model_call(self):
        gate = threading.Barrier(3)
        release = threading.Event()
        models = []
        original = self.app.agent_factory

        class PausedModel(OfflineModel):
            calls = 0

            def complete(model, messages, tools):
                model.calls += 1
                if model.calls == 1:
                    gate.wait(timeout=8)
                    release.wait(timeout=8)
                return super().complete(messages, tools)

        def factory(observer):
            agent = original(observer)
            agent.model = PausedModel()
            models.append(agent.model)
            return agent

        self.app.agent_factory = factory
        parent, state, _ = self.dispatch_batch()
        self.app.auto_research.yield_job(parent, state, waiting=True)
        children = [self.store.claim_next(), self.store.claim_next(parallel_only=True)]
        workers = [threading.Thread(target=self.app.execute, args=(job,)) for job in children]
        try:
            for worker in workers:
                worker.start()
            gate.wait(timeout=8)
            self.store.cancel(self.space, parent['id'])
        finally:
            release.set()
            for worker in workers:
                worker.join(timeout=8)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual([model.calls for model in models], [1, 1])
        self.assertTrue(all(self.store.job(self.space, job['id'])['status'] == 'cancelled' for job in children))

    def test_forget_after_claim_preserves_interrupted_status_without_killing_worker(self):
        job, state, _ = self.dispatch_batch()
        self.app.auto_research.yield_job(job, state, waiting=True)
        child = self.store.claim_next(parallel_only=True)
        with closing(self.store._connect()) as db, db:
            self.app.memory.invalidate(db, self.space)
        self.app.execute(child)
        self.assertEqual(self.store.job(self.space, child['id'])['status'], 'interrupted')
        self.model.complete.assert_not_called()

    def test_quick_child_compresses_long_prior_history_before_its_first_research_call(self):
        for _ in range(6):
            self.store.message(self.space, self.chat, 'assistant', '既往研究资料' * 380)
        original = self.app.agent_factory
        compressions = []
        class CompressingModel(OfflineModel):
            def complete(model, messages, tools):
                if '压缩会话供后续研究使用' in messages[0].get('content', ''):
                    compressions.append(True)
                    return ModelDecision('final', content=json.dumps({'goal': 'Research ReAct', 'constraints': [],
                        'completed': [], 'pending': [], 'findings': [], 'conflicts': [], 'next_steps': []}))
                return super().complete(messages, tools)
        def factory(observer):
            agent = original(observer)
            agent.model = CompressingModel()
            return agent
        self.app.agent_factory = factory
        job, state, _ = self.dispatch_batch()
        self.app.auto_research.yield_job(job, state, waiting=True)
        child = self.store.claim_next()
        self.app.execute(child)
        self.assertTrue(compressions)
        self.assertEqual(self.store.job(self.space, child['id'])['status'], 'completed')

    def test_join_delivers_failed_branch_and_scoped_evidence_without_polling_model(self):
        job, state, _ = self.dispatch_batch()
        self.app.auto_research.yield_job(job, state, waiting=True)
        first = self.store.claim_next()
        second = self.store.claim_next(parallel_only=True)
        self.store.finish(first['id'], 'completed', 'Evidence-backed finding')
        for _ in range(3):
            self.app.auto_research.tick()
        self.model.complete.assert_not_called()
        self.store.finish(second['id'], 'failed', '', 'Model overloaded')
        self.model.complete.return_value = support.call('finish_research', {'outcome': 'reported', 'summary': 'Partial findings; second branch failed', 'limitations': 'Missing second branch'}, 2)
        self.step()
        messages = self.model.complete.call_args.args[0]
        tool = next(json.loads(m['content'])['UNTRUSTED_TOOL_DATA'] for m in messages if m['role'] == 'tool')
        self.assertEqual(tool['failed_count'], 1)
        self.assertEqual([r['job_id'] for r in tool['results']], state['research'])
        self.assertIn('Model overloaded', json.dumps(tool))
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'completed')

    def test_real_worker_runs_two_independent_agent_loops_and_joins(self):
        barrier = threading.Barrier(2)
        contexts, intervals = {}, {}
        original = self.app.agent_factory

        def agent_factory(observer):
            agent = original(observer)
            run = agent.run
            def run_together(brief):
                key = threading.get_ident()
                contexts[key] = agent.initial_context
                intervals[key] = [time.monotonic(), None]
                barrier.wait(timeout=8)
                result = run(brief)
                intervals[key][1] = time.monotonic()
                return result
            agent.run = run_together
            return agent

        self.app.agent_factory = agent_factory
        self.model.complete.side_effect = [support.call('research_parallel', {'tasks': TASKS}),
            support.call('finish_research', {'outcome': 'reported', 'summary': 'Joined independent research', 'limitations': 'Offline fixture'}, 2)]
        job = self.start(budget={'research_calls': 2, 'coding_calls': 0})
        self.app.worker.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.store.job(self.space, job['id'])['status'] not in {'completed', 'failed'}:
            time.sleep(.02)
        detail = self.app.auto_research.detail(self.space, job['id'])
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'completed')
        self.assertEqual(len(intervals), 2)
        values = list(intervals.values())
        self.assertTrue(all(v[1] is not None for v in values))
        self.assertLess(max(v[0] for v in values), min(v[1] for v in values))
        self.assertEqual(*contexts.values())
        self.assertEqual(len(detail['research']), 2)
        self.assertTrue(all(r['run_id'] for r in detail['research']))
        self.assertEqual(detail['coding'], [])

    def test_large_join_keeps_both_branch_ids_errors_and_evidence_catalog(self):
        results = [dict(job_id=str(i), id=str(i), status='failed', summary='x' * 16000,
                        error='failure-' + str(i), evidence=[dict(evidence_id='E'+str(n), source_id='S1', excerpt='y'*4000) for n in range(30)],
                        sources=[dict(source_id='S1', title='A paper', url='https://example.com/paper')]) for i in range(2)]
        compact = AutoResearch.context_result(dict(kind='research_parallel', status='completed', failed_count=2, results=results))
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), 7000)
        self.assertEqual([r['job_id'] for r in compact['results']], ['0', '1'])
        self.assertTrue(all(r['error'].startswith('failure-') and r['evidence'] for r in compact['results']))


if __name__ == '__main__':
    unittest.main()
