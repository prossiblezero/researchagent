"""Separately cited list paragraphs must be independently checked and repaired."""
import json
import unittest
from unittest.mock import Mock

from research_agent.contracts import Evidence, ModelDecision, Source
from research_agent.verify import answer_blocks, apply_answer_patch, check_answer


class ListItemVerificationTests(unittest.TestCase):
    def verifier(self):
        def complete(messages, tools):
            data = json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
            blocks = data['blocks']
            return ModelDecision('final', content=json.dumps({
                'requirements': [{'id': 'R1', 'requirement': 'methods', 'addressed': True,
                                  'block_ids': [b['block_id'] for b in blocks], 'reason': 'fixture'}],
                'blocks': [{'block_id': b['block_id'], 'kind': 'fact', 'supported': True,
                            'evidence_ids': ['E2'] if '[E2]' in b['text'] else ['E1'],
                            'reason': 'fixture semantic verdict'} for b in blocks if not b['cached']]}))
        return Mock(complete=complete, usage_purpose='answer')

    def test_cited_list_items_are_separate_and_uncited_sibling_cannot_borrow(self):
        for marker in ('1.', '1)', '-', '*', '+'):
            with self.subTest(marker=marker):
                first = f'{marker} First fact. [E1]'
                missing = f'{marker} An unrelated uncited fact.'
                last = f'{marker} Last fact. [E2]'
                self.assertEqual([b['text'] for b in answer_blocks('\n\n'.join((first, missing, last)))],
                                 [first, missing, last])
        sources = [Source('S1', 'Paper', 'https://example.org/paper', '')]
        evidence = [Evidence('E1', 'S1', 'First fact and unrelated fact.', 'page')]
        check = check_answer(self.verifier(), 'methods?', '1. First fact. [E1]\n\n2. Uncited fact.', evidence, sources)
        self.assertTrue(check['claims'][0]['supported'])
        self.assertFalse(check['claims'][1]['supported'])

    def test_repair_locks_valid_item_and_v4_cache_is_not_reused(self):
        first = '1. First method is described on 第4页. [E1]'
        second = '2. Second method is described on 第5页. [E2]'
        sources = [Source('S1', 'Paper', 'https://example.org/paper', '')]
        evidence = [Evidence('E1', 'S1', 'First method.', 'local:D1-0', provenance={'page': 4}),
                    Evidence('E2', 'S1', 'Second method.', 'local-snippet:D2-0')]
        model = self.verifier()
        check = check_answer(model, 'methods?', first + '\n\n' + second, evidence, sources)
        self.assertEqual(len(check['claims']), 2)
        self.assertTrue(check['claims'][0]['supported'])
        self.assertFalse(check['claims'][1]['supported'])
        self.assertEqual(check['unread_citations'], [{'evidence_id': 'E2', 'read_tool': 'read_evidence', 'ref_id': 'E2'}])
        locked, editable = (c['block_id'] for c in check['claims'])
        updated = apply_answer_patch(json.dumps({'replace': [{'block_id': editable, 'text': '2. Second method remains unknown.'}]}), check)
        self.assertEqual(updated, first + '\n\n2. Second method remains unknown.')
        with self.assertRaisesRegex(ValueError, 'verified or unknown'):
            apply_answer_patch(json.dumps({'replace': [{'block_id': locked, 'text': 'Changed'}]}), check)
        check['binding_version'] = 4
        repeated = check_answer(model, 'methods?', first + '\n\n' + second, evidence, sources, previous=check)
        self.assertEqual(repeated['reused_blocks'], 0)

    def test_uncited_list_still_owns_a_shared_source_note(self):
        for marker in ('1.', '1)', '-', '*', '+'):
            with self.subTest(marker=marker):
                shared = f'{marker} First\n\n{marker} Second\n\n来源：[E1]'
                self.assertEqual([b['text'] for b in answer_blocks(shared)], [shared])
                isolated = shared + f'\n\n{marker} New assertion. [E2]'
                self.assertEqual(len(answer_blocks(isolated)), 2)


    def test_resume_old_repair_rechecks_draft_before_any_patch_or_fallback(self):
        from dataclasses import asdict
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from research_agent.loop import ResearchAgent
        draft = '1. Unsupported first fact. [E1]\n\n2. Unknown second fact.'
        evidence = [Evidence('E1', 'S1', 'Unrelated original.', 'page')]
        sources = [Source('S1', 'Paper', 'https://example.org/paper', '')]
        previous = check_answer(self.verifier(), 'methods?', draft, evidence, sources)
        previous['binding_version'] = 4
        for unavailable in (False, True):
            for pending in (None, asdict(ModelDecision('final', content='invalid patch'))):
                with self.subTest(unavailable=unavailable, pending=bool(pending)), TemporaryDirectory() as directory:
                    calls = []
                    def complete(messages, tools):
                        calls.append(model.usage_purpose)
                        if model.usage_purpose != 'answer_verification':
                            return ModelDecision('final', content='invalid patch')
                        if unavailable:
                            raise TimeoutError('fixture verification outage')
                        data = json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                        self.assertEqual([b['text'] for b in data['blocks']], [b['text'] for b in answer_blocks(draft)])
                        self.assertFalse(any(b['cached'] for b in data['blocks']))
                        return ModelDecision('final', content=json.dumps({
                            'requirements': [{'id': 'R1', 'requirement': 'methods', 'addressed': False,
                                              'block_ids': [], 'reason': 'unsupported'}],
                            'blocks': [{'block_id': b['block_id'], 'kind': 'fact', 'supported': False,
                                        'evidence_ids': [], 'reason': 'unsupported'} for b in data['blocks']]}))
                    model = Mock(complete=complete, usage_purpose='answer', supports_answer_verification=True,
                                 name='fixture', request_deadline=None)
                    state = {'question': 'methods?', 'messages': [{'role': 'user', 'content': 'methods?'}],
                             'sources': [asdict(s) for s in sources], 'evidence': [asdict(e) for e in evidence],
                             'counters': [0, 0, 0, 0, 0], 'cache': [], 'next_iteration': 1,
                             'pending_decision': pending, 'answer_check': previous, 'checked_draft': 'old',
                             'revision_pending': True, 'draft_answer': draft, 'patch_errors': 1,
                             'answer_repair_rounds': 2}
                    result = ResearchAgent(Mock(), model, Path(directory), max_rounds=1,
                                           resume_state=state).run('methods?')
                    self.assertEqual(calls, ['answer_verification'])
                    self.assertFalse(any(c.status == 'SUPPORTED' for c in result.claims))
                    self.assertEqual(result.termination, 'answer_verification_unavailable' if unavailable
                                     else 'answer_verification_failed')


if __name__ == '__main__':
    unittest.main()
