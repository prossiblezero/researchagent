import json
import unittest
from evals.postprocess_evidence_qa import reading_audit


class EvidenceQAWindowTests(unittest.TestCase):
    def test_repeated_windows_use_verified_offsets_and_reject_false_offsets(self):
        original = 'same original.\n' * 240
        case = {'facts': [{'evidence': [{'document': 'paper', 'ordinal': 0, 'quote': original}]}]}
        def request(text, offset):
            return {'attempt_id': offset, 'purpose': 'answer', 'response': {'kind': 'final'},
                    'messages': [{'role': 'tool', 'content': json.dumps({'UNTRUSTED_TOOL_DATA': {
                        'kind': 'read_evidence', 'ok': True, 'corpus': 'documents', 'chunk_id': 1,
                        'content': text, 'start_offset': offset, 'total_chars': len(original), 'evidence_id': 'E1'}})}]}
        complete = reading_audit(case, [request(original[:1800], 0), request(original[1800:], 1800)], {'1': ['paper', 0]})
        self.assertEqual(complete['gold_fully_read_passage_rate'], 1)
        self.assertEqual(complete['gold_character_coverage'], 1)
        wrong = reading_audit(case, [request(original[:1800], 1)], {'1': ['paper', 0]})
        self.assertEqual(wrong['gold_character_coverage'], 0)
        self.assertEqual(wrong['passages'][0]['unmapped_windows'][0]['reason'], 'offset_content_mismatch')

    def test_compacted_excerpt_search_stays_inside_the_original_read_window(self):
        text = 'BEGIN' + 'middle ' * 10 + 'END'
        original = text + '_' * (1800-len(text)) + text
        case = {'facts': [{'evidence': [{'document': 'paper', 'ordinal': 0, 'quote': original}]}]}
        request = {'attempt_id': 1, 'purpose': 'answer', 'response': {'kind': 'final'},
            'messages': [{'role': 'tool', 'content': json.dumps({'UNTRUSTED_TOOL_DATA': {
                'kind': 'read_evidence', 'ok': True, 'corpus': 'documents', 'chunk_id': 1,
                'content': 'BEGIN\n[... omitted ...]\nEND', 'start_offset': 1800,
                'total_chars': len(original), 'content_chars': len(text), 'context_excerpted': True,
                'evidence_id': 'E1'}})}]}
        result = reading_audit(case, [request], {'1': ['paper', 0]})
        self.assertEqual(result['passages'][0]['seen_chars'], 8)
        self.assertTrue(all(x['start'] >= 1800 for x in result['passages'][0]['windows']))

    def test_only_submitted_answer_windows_count_and_omitted_text_does_not(self):
        original = 'A' * 1800 + 'B' * 100
        case = {'facts': [{'evidence': [{'document': 'paper', 'ordinal': 0, 'quote': original}]}]}
        def request(text, purpose='answer', response=True):
            value = {'attempt_id': 1, 'purpose': purpose, 'messages': [{'role': 'tool', 'content': json.dumps({
                'UNTRUSTED_TOOL_DATA': {'kind': 'read_evidence', 'ok': True, 'corpus': 'documents',
                                       'chunk_id': 1, 'content': text, 'evidence_id': 'E1'}})}]}
            if response:
                value['response'] = {'kind': 'final'}
            return value
        first = reading_audit(case, [request(original[:1800]), request(original, 'answer_verification'),
            request(original, 'evidence_rerank'), request(original, response=False)], {'1': ['paper', 0]})
        self.assertEqual(first['gold_chunk_seen_rate'], 1)
        self.assertEqual(first['gold_fully_read_passage_rate'], 0)
        self.assertAlmostEqual(first['gold_character_coverage'], 1800 / 1900)
        second = reading_audit(case, [request(original[:1800]), request(original[1800:])], {'1': ['paper', 0]})
        self.assertEqual(second['gold_fully_read_passage_rate'], 1)
        omitted = reading_audit(case, [request('A' * 100 + '\n[... omitted ...]\n' + 'B' * 100)], {'1': ['paper', 0]})
        self.assertAlmostEqual(omitted['gold_character_coverage'], 100 / 1900)
        self.assertEqual(omitted['passages'][0]['unmapped_windows'][0]['reason'], 'ambiguous_repeated_excerpt')


if __name__ == '__main__':
    unittest.main()
