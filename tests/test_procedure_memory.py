"""Procedural learning reuses real local tool feedback across tasks."""
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research_agent.contracts import Evidence, ModelDecision, RunResult
from research_agent.experience import distill_procedure
from research_agent.library import Library
from research_agent.memory import Memory
from research_agent.retrieval import Retriever
from research_agent.sessions import Sessions
from research_agent.workbench import Workbench
from research_agent.workbench_store import WorkbenchStore


LESSON = {'when': '比较实验指标时', 'lesson': '先读原文核对实验条件，再比较指标。'}


class ExtractionModel:
    usage_purpose = 'answer'

    def __init__(self, response=LESSON):
        self.response = response
        self.inputs = []

    def complete(self, messages, tools):
        self.inputs.append(messages)
        return ModelDecision('final', content=json.dumps(self.response, ensure_ascii=False))


class ProcedureMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {'OFFLINE_MODE': '1', 'RETRIEVAL_MODE': 'lexical'})
        env.start()
        self.addCleanup(env.stop)
        self.store = WorkbenchStore(self.root / 'state.db')
        self.sid = self.store.save_space({'name': 'Research'})['id']
        self.memory = Memory(self.store)
        self.retrieval = Retriever(self.store, Library(self.store))
        self.job = self.job_for('比较实验指标')
        self.store.claim_next()
        self.store.finish(self.job['id'], 'failed', '缺少实验条件，未完成比较。')
        self.trace = self.root / 'trace.jsonl'
        records = [
            {'event': 'model_response', 'reasoning_content': 'PRIVATE_REASONING'},
            {'event': 'tool_call_requested', 'name': 'read_evidence', 'args': {'ref_id': 'D1-0'}},
            {'event': 'tool_result', 'name': 'read_evidence', 'ok': True, 'truncated': True},
            {'event': 'answer_checked', 'ready': False, 'gaps': ['缺少实验条件，需要继续读原文。']},
        ]
        self.trace.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in records), encoding='utf-8')
        self.result = RunResult('尚缺实验条件。', [], str(self.trace), 'insufficient',
                                'answer_verification_failed', 1, [], [])

    def job_for(self, question, sid=None):
        sid = sid or self.sid
        cid = self.store.create_conversation(sid, question)['id']
        self.store.message(sid, cid, 'user', question)
        return self.store.enqueue(sid, cid, question, question, [], kind='LOCAL_QA', research_effort='quick')

    def test_failed_local_trajectory_is_distilled_without_private_reasoning(self):
        model = ExtractionModel()
        key = self.memory.extract_procedure(self.job, self.result, model)
        item = self.memory.list(self.sid)[0]
        self.assertEqual(item['id'], key)
        self.assertEqual(item['source_refs'][0]['job_id'], self.job['id'])
        self.assertEqual(item['source_refs'][0]['outcome'], 'answer_verification_failed')
        prompt = json.dumps(model.inputs, ensure_ascii=False)
        self.assertIn('缺少实验条件', prompt)
        self.assertNotIn('PRIVATE_REASONING', prompt)
        self.assertEqual(model.usage_purpose, 'answer')

    def test_selected_lessons_are_bounded_and_separate_from_factual_evidence(self):
        for suffix in ('先读原文', '核对划分', '核对预算'):
            self.memory.add(self.sid, '比较实验指标：' + suffix, 'procedure',
                            [{'ref': suffix, 'job_id': self.job['id']}], scope='workspace')
        next_job = self.job_for('比较实验指标')
        lessons = self.memory.procedures(next_job, self.retrieval)
        self.assertEqual(len(lessons), 2)
        self.assertEqual(len({i['id'] for i in lessons}), 2)
        self.memory.update(self.sid, lessons[0]['id'], {'status': 'confirmed'})
        self.assertEqual(self.memory.snapshot(self.sid, next_job['conversation_id']), [])
        self.assertEqual(self.retrieval.retrieve(self.sid, '比较实验指标', corpus='memory')['results'], [])
        other = self.store.save_space({'name': 'Other'})['id']
        self.assertEqual(self.memory.procedures(self.job_for('比较实验指标', other), self.retrieval), [])
        self.assertEqual(self.memory.procedures({**next_job, 'question': '不要使用历史经验，比较实验指标'}, self.retrieval), [])

    def test_rejected_forgotten_and_deleted_source_lessons_are_not_reused(self):
        key = self.memory.extract_procedure(self.job, self.result, ExtractionModel())
        next_job = self.job_for('比较实验指标')
        self.assertTrue(self.memory.procedures(next_job, self.retrieval))
        self.memory.update(self.sid, key, {'status': 'rejected'})
        self.assertEqual(self.memory.procedures(next_job, self.retrieval), [])
        self.memory.forget(self.sid, key)
        self.assertIsNone(self.memory.extract_procedure(self.job, self.result, ExtractionModel()))
        other_key = self.memory.add(self.sid, '比较实验指标时核对条件与指标定义。', 'procedure',
                                    [{'ref': 'separate-observation', 'job_id': self.job['id']}], scope='workspace')
        self.assertTrue(other_key)
        Sessions(self.store, self.memory).delete_branch(self.sid, self.job['conversation_id'])
        self.assertEqual(self.memory.procedures(next_job, self.retrieval), [])

    def test_skip_and_invalid_extraction_restore_usage_purpose(self):
        self.assertIsNone(distill_procedure('question', self.result, ExtractionModel({'skip': True})))
        model = ExtractionModel({'when': 'missing lesson'})
        with self.assertRaises(ValueError):
            distill_procedure('question', self.result, model)
        self.assertEqual(model.usage_purpose, 'answer')

    def test_relevant_fact_from_previous_task_keeps_readable_original(self):
        text = 'The laboratory measured a temperature of 280 kelvin in the experiment.'
        library = Library(self.store)
        paper, _ = library.save(self.sid, {'kind': 'paper', 'title': 'Temperature experiment',
            'url': '', 'canonical_id': 'temperature', 'metadata': {}, 'data': None,
            'warnings': [], 'boundary': 'test', 'chunks': [{'text': text, 'section': 'Results',
                'page': 1, 'line_start': 1, 'line_end': 1}]})
        chunk = library.chunks(self.sid, paper['id'])[0]
        ref = f'D{chunk["id"]}-0'
        evidence = Evidence('E1', 'S1', text, 'local:' + ref, title='Temperature experiment',
                            provenance={'chunk_id': chunk['id'], 'artifact_id': paper['id']})
        class FactModel(ExtractionModel):
            def complete(model, messages, tools):
                if model.usage_purpose == 'memory_extraction':
                    value = {'items': [{'content': text, 'evidence_id': 'E1', 'quote': text}]}
                else:
                    data = json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                    value = {'checks': [{'id': c['id'], 'supported': True} for c in data['candidates']]}
                return ModelDecision('final', content=json.dumps(value))
        self.memory.extract(self.job, SimpleNamespace(evidence=[evidence], claims=[True], answer=text), FactModel())
        self.memory.add(self.sid, 'Temperature uncertain candidate', 'finding', [{'ref': 'candidate'}], scope='workspace')
        next_job = self.job_for('What temperature did the laboratory measure?')
        selected = self.memory.relevant(next_job, self.retrieval, 'finding')
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['sources'][0]['read_ref'], ref)
        self.retrieval.sync(self.sid)
        self.assertEqual(self.retrieval.read(self.sid, ref)['content'], text)
        other = self.store.save_space({'name': 'Other'})['id']
        self.assertEqual(self.memory.relevant(self.job_for('temperature', other), self.retrieval, 'finding'), [])
        self.memory.forget(self.sid, selected[0]['id'])
        self.assertEqual(self.memory.relevant(next_job, self.retrieval, 'finding'), [])

    def test_short_lesson_request_allows_a_modest_english_length_overrun(self):
        lesson = {'when': ('A comparison question about measured results and experimental settings. ' * 2).strip(),
                  'lesson': ('Read the experiment section around the returned match and inspect the neighboring paragraph for the dataset split. ' * 3).strip()}
        self.assertGreater(len(lesson['when']), 100)
        self.assertGreater(len(lesson['lesson']), 300)
        self.assertEqual(distill_procedure('Compare results', self.result, ExtractionModel(lesson)), lesson)

    def test_many_procedures_do_not_crowd_facts_out_of_vector_candidates(self):
        refs = [{'ref': 'observed', 'job_id': self.job['id']}]
        fact = self.memory.add(self.sid, 'The laboratory records a temperature of 280 kelvin.',
                               'finding', refs, status='evidence_checked', scope='workspace')
        for i in range(45):
            self.memory.add(self.sid, 'Inspect laboratory equipment before measuring: ' + str(i),
                            'procedure', refs, scope='workspace')
        rows = self.retrieval.sync(self.sid, 'memory')
        # Simulate a vector backend whose 40 closest items would otherwise all be procedures.
        ranked = sorted(rows.values(), key=lambda r: r['owner_id'] == fact)
        def query(**kwargs):
            filters = kwargs['where']['$and']
            owners = next((f['owner_id']['$in'] for f in filters if 'owner_id' in f), None)
            candidates = [r for r in ranked if owners is None or r['owner_id'] in owners]
            return {'metadatas': [[{'ref_id': r['ref_id'], 'content_hash': r['content_hash']}
                                   for r in candidates[:kwargs['n_results']]]]}
        self.retrieval.dense = SimpleNamespace(encode=lambda texts: [[0.0]],
                                               collection=SimpleNamespace(query=query))
        self.retrieval.error = ''
        with patch.object(self.retrieval, 'sync', return_value=rows):
            hits = self.retrieval.retrieve(self.sid, 'thermal reading', corpus='memory')['results']
        self.assertEqual([h['owner_id'] for h in hits], [fact])

    def test_workbench_saves_then_loads_experience_and_tolerates_extraction_failure(self):
        models = []
        class LocalModel(ExtractionModel):
            name = 'scripted-local-model'
            supports_answer_verification = False
            def __init__(model):
                super().__init__()
                model.usage_records = []
            def complete(model, messages, tools):
                model.inputs.append(messages)
                if model.usage_purpose == 'experience_extraction':
                    if len(models) > 1:
                        raise RuntimeError('unavailable extraction')
                    return ModelDecision('final', content=json.dumps(LESSON))
                if model.usage_purpose in ('memory_extraction', 'memory_verification'):
                    return ModelDecision('final', content='{"items":[]}')
                data = next((json.loads(m['content'])['UNTRUSTED_TOOL_DATA']
                             for m in reversed(messages) if m['role'] == 'tool'), None)
                if not data:
                    return ModelDecision('tool_call', tool_name='retrieve',
                        arguments={'query': '实验指标', 'corpus': 'documents'}, call_id='retrieve')
                if data['kind'] == 'retrieve':
                    return ModelDecision('tool_call', tool_name='read_evidence',
                        arguments={'ref_id': data['results'][0]['ref_id']}, call_id='read')
                return ModelDecision('final', content='实验指标在相同条件下测量。[' + data['evidence_id'] + ']')
        def factory():
            model = LocalModel()
            models.append(model)
            return model
        work = Workbench(self.store, factory, None, self.root / 'traces', start_worker=False)
        work.library.save(self.sid, {'kind': 'paper', 'title': '实验指标', 'url': '', 'canonical_id': 'experiment',
            'metadata': {}, 'data': None, 'warnings': [], 'boundary': 'test',
            'chunks': [{'text': '实验指标在相同条件下测量。所有实验使用同一数据划分与计算预算，并报告独立测试集结果。', 'page': 1, 'section': 'method', 'line_start': 1, 'line_end': 1}]})
        first = self.job_for('实验指标如何测量？')
        work.execute(self.store.claim_next())
        self.assertEqual(self.store.job(self.sid, first['id'])['status'], 'completed', self.store.job(self.sid, first['id'])['error'])
        second = self.job_for('比较实验指标如何测量？')
        work.execute(self.store.claim_next())
        self.assertEqual(self.store.job(self.sid, second['id'])['status'], 'completed')
        self.assertIn('procedure_memory', json.dumps(models[1].inputs[0], ensure_ascii=False))
        with closing(self.store._connect()) as db:
            kinds = {r[0] for r in db.execute('SELECT kind FROM conversation_events WHERE job_id=?', (second['id'],))}
        self.assertIn('procedure_memory_loaded', kinds)
        self.assertIn('procedure_extraction_failed', kinds)


if __name__ == '__main__':
    unittest.main()
