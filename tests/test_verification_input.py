import json
import unittest
from dataclasses import asdict

from research_agent.context import _size
from research_agent.contracts import Evidence, ModelDecision, Source
from research_agent.verify import check_answer


class RecordingVerifier:
    usage_purpose = 'answer'

    def __init__(self, refs, basis='external_fact'):
        self.refs = refs
        self.basis = basis

    def complete(self, messages, tools):
        self.data = json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
        self.input_tokens = _size(messages, tools)[1]
        checks = [{'block_id': b['block_id'], 'kind': 'fact', 'supported': True,
                   'basis': self.basis, 'evidence_ids': self.refs, 'reason': 'scripted verdict'}
                  for b in self.data['blocks'] if not b['cached']]
        requirements = self.data['requirements'] or [{'id': 'R1', 'requirement': 'Answer the question'}]
        return ModelDecision('final', content=json.dumps({
            'requirements': [{**r, 'addressed': True, 'block_ids': [b['block_id'] for b in self.data['blocks']],
                              'reason': 'scripted coverage'} for r in requirements],
            'blocks': checks}))


class VerificationInputTests(unittest.TestCase):
    def test_changed_source_scope_rechecks_cached_claims(self):
        evidence = [Evidence('E1', 'S1', 'The original describes the dataset.', 'local:D1-0')]
        sources = [Source('S1', 'Paper', 'https://example.org/paper', '')]
        model = RecordingVerifier(['E1'])
        args = ('Describe the dataset', 'The original describes the dataset. [E1]', evidence, sources)
        first = check_answer(model, *args, allow_external=True)
        cached = check_answer(model, *args, previous=first, allow_external=True)
        self.assertEqual(cached['reused_blocks'], 1)
        restricted = check_answer(model, *args, previous=cached, allow_external=False)
        self.assertIs(model.data['external_sources_allowed'], False)
        self.assertEqual(restricted['reused_blocks'], 0)
        self.assertFalse(any(b['cached'] for b in model.data['blocks']))

    def test_unread_source_catalog_does_not_displace_original_or_exhaust_budget(self):
        original = 'MAP improves from 0.696 to 0.734. ' + 'Original context. ' * 150
        evidence = [Evidence('E1', 'S1', original, 'local:D1-0')]
        sources = [Source('S1', 'Original paper', 'https://example.org/paper', '')] + [
            Source(f'S{i}', 'Unrelated search result ' * 12, f'https://example.org/unread/{i}', '')
            for i in range(2, 102)]
        saved = ([asdict(e) for e in evidence], [asdict(s) for s in sources])
        model = RecordingVerifier(['E1'])
        result = check_answer(model, 'What is the MAP improvement?',
                              'MAP improves from 0.696 to 0.734. [E1]', evidence, sources, input_limit=3500)
        self.assertTrue(result['ready'])
        self.assertEqual(model.data['evidence'][0]['content'], original)
        self.assertFalse(model.data['evidence'][0]['excerpted'])
        self.assertLessEqual(model.input_tokens, 3500)
        self.assertEqual([s['source_id'] for s in model.data['sources']], ['S1'])
        self.assertEqual(saved, ([asdict(e) for e in evidence], [asdict(s) for s in sources]))

    def test_source_selection_keeps_reading_leads_without_weakening_citation_checks(self):
        evidence = [Evidence('E1', 'S1', 'The document reports MAP.', 'local:D1-0'),
                    Evidence('E2', 'S2', 'An unread preview.', 'local-snippet:D2-0'),
                    Evidence('E3', 'S3', 'Previously we discussed MAP.', 'local:H3-0'),
                    Evidence('E4', 'S4', 'Other original context.', 'page')]
        sources = [Source(f'S{i}', f'Source {i}', f'https://example.org/{i}', '') for i in range(1, 6)]
        cases = [('[E1]', ['E1'], 'external_fact', True),
                 ('[S1]', ['E1'], 'external_fact', True),
                 ('[E2]', ['E2'], 'external_fact', False),
                 ('[S2]', ['E2'], 'external_fact', False),
                 ('[E1] [S999]', ['E1'], 'external_fact', False),
                 ('[E3]', ['E3'], 'external_fact', False),
                 ('[E3]', ['E3'], 'historical_recall', True)]
        for citation, refs, basis, ready in cases:
            with self.subTest(citation=citation, basis=basis):
                model = RecordingVerifier(refs, basis)
                question = 'What did we discuss?' if basis == 'historical_recall' else 'What does the document report?'
                result = check_answer(model, question, f'MAP. {citation}', evidence, sources)
                self.assertEqual(result['ready'], ready)
                sent_sources = {s['source_id'] for s in model.data['sources']}
                self.assertEqual({e['evidence_id'] for e in model.data['evidence']}, {'E1', 'E3', 'E4'})
                self.assertNotIn('S5', sent_sources)
                if refs == ['E2']:
                    self.assertIn('S2', sent_sources)
                    self.assertEqual(result['unread_citations'], [
                        {'evidence_id': 'E2', 'read_tool': 'read_evidence', 'ref_id': 'E2'}])

    def test_cached_paragraph_keeps_original_for_conflict_checks(self):
        evidence = [Evidence(f'E{i}', f'S{i}', 'MAP evidence' if i < 6 else 'Archived fact', f'local:D{i}-0')
                    for i in range(1, 7)]
        sources = [Source(f'S{i}', f'Source {i}', f'https://example.org/{i}', '') for i in range(1, 8)]
        previous = check_answer(RecordingVerifier(['E6']), 'MAP?', 'Archived fact. [E6]', evidence, sources)
        model = RecordingVerifier(['E1'])
        result = check_answer(model, 'MAP?', 'Archived fact. [E6]\n\nMAP evidence. [E1]',
                              evidence, sources, previous=previous)
        self.assertTrue(result['ready'])
        self.assertEqual(result['reused_blocks'], 1)
        self.assertIn('E6', {e['evidence_id'] for e in model.data['evidence']})
        self.assertIn('S6', {s['source_id'] for s in model.data['sources']})
        self.assertNotIn('S7', {s['source_id'] for s in model.data['sources']})


if __name__ == '__main__':
    unittest.main()
