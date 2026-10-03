"""Regression cases discovered in the frozen first QA run."""
import json
import hashlib
import tempfile
import unittest
from dataclasses import asdict
from unittest.mock import patch
from research_agent.contracts import Evidence, Source, ModelDecision
from research_agent.evidence import normalize_citation_format, validate_evidence_citations
from research_agent.verify import check_answer, answer_blocks
from research_agent.loop import ResearchAgent


class FormatRecoveryTests(unittest.TestCase):
    def test_fullwidth_explicit_citations_preserve_unknown_ids_and_do_not_guess_prose(self):
        text=normalize_citation_format('事实【E2】【S1】和［E999，E3］。范围【E1-E3】，说明【见 E2】。')
        self.assertIn('[E2][S1]',text)
        self.assertIn('[E999] [E3]',text)
        self.assertIn('【E1-E3】',text)
        self.assertIn('【见 E2】',text)
        known=[Evidence('E2','S1','source'),Evidence('E3','S1','source')]
        self.assertEqual(validate_evidence_citations(text,known)[1],['E999'])

    def test_fullwidth_locations_bind_separate_facts_without_guessing_ranges(self):
        text = normalize_citation_format(
            '- First method.【E22；E28，第 2、6 页】\n\n'
            '- First limit.【E23，第 1 页；E30，第 6 页】\n\n'
            '- Second method.［E17，第 6–7 页；E999，第 8 页］')
        self.assertEqual(len(answer_blocks(text)), 3)
        self.assertIn('[E22] [E28]（第 2、6 页）', text)
        self.assertIn('[E23] [E30]', text)
        self.assertIn('第 1 页', text)
        self.assertIn('第 6 页', text)
        self.assertIn('[E17] [E999]', text)
        self.assertIn('第 6–7 页', text)
        self.assertIn('第 8 页', text)
        known = [Evidence('E'+str(n), 'S1', 'read original') for n in (22,28,23,30,17)]
        self.assertEqual(validate_evidence_citations(text, known)[1], ['E999'])
        self.assertEqual(normalize_citation_format(text), text)
        for invalid in ('【E1-E3，第 2 页】', '［E1–3，第 2 页］', '【见 E2，第 3 页】', '【E1abc，第 3 页】', '【E1，第 2 页］'):
            with self.subTest(invalid=invalid):
                self.assertEqual(normalize_citation_format(invalid), invalid)

    def checked(self, transform):
        text='The gain was 17 percent. [E1]'
        def complete(messages,tools):
            data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
            key=data['blocks'][0]['block_id']
            value={'requirements':[{'id':'R1','requirement':'gain','addressed':True,'block_ids':[key],'reason':'covered'}],
                'blocks':[{'block_id':transform(key),'kind':'fact','supported':True,'basis':'external_fact',
                    'evidence_ids':['E1'],'reason':'The cited original supports 17 percent.'}]}
            return ModelDecision('final',content=json.dumps(value))
        class Model:
            usage_purpose='answer'
        model=Model();model.complete=complete
        return check_answer(model,'gain?',text,[Evidence('E1','S1','The gain was 17 percent.','local:D1-0')],
                            [Source('S1','paper','https://example.org','')])

    def test_mistyped_transport_identifier_is_not_guessed(self):
        with self.assertRaises(ValueError):
            self.checked(lambda key:key[:-1]+('a' if key[-1]!='a' else 'b'))
        with self.assertRaises(ValueError):
            self.checked(lambda key:key[:-1])

    def test_short_wire_ids_avoid_recorded_hash_collision_and_preserve_cache(self):
        # Actual failure shape: the first hash was mistyped into a one-edit neighbor
        # of the second, so the former fuzzy repair selected the wrong paragraph.
        fixed=[{'block_id':'B350fb1203e44','text':'First fact. [E1]'},
               {'block_id':'B3545b9798c6c','text':'Second fact. [E1]'}]
        captured=[]
        class Model:
            usage_purpose='answer'
            def complete(self,messages,tools):
                data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA'];captured.append(data)
                rows=[]
                for block in data['blocks']:
                    if block['cached']:continue
                    bid=block['block_id']
                    if bid=='B350fb1203e44':bid='B3505b9798c6c'
                    rows.append({'block_id':bid,'kind':'fact','supported':True,'evidence_ids':['E1'],'reason':'supported'})
                return ModelDecision('final',content=json.dumps({'requirements':[
                    {'id':'R1','requirement':'both facts','addressed':True,
                     'block_ids':[b['block_id'] for b in data['blocks']],'reason':'covered'}],'blocks':rows}))
        evidence=[Evidence('E1','S1','First fact. Second fact.','page')]
        with patch('research_agent.verify.answer_blocks',side_effect=lambda _: [dict(b) for b in fixed]):
            first=check_answer(Model(),'both facts','draft',evidence,[])
            again=check_answer(Model(),'both facts','draft',evidence,[],previous=first)
        self.assertTrue(first['ready'])
        self.assertTrue(again['ready'])
        self.assertEqual([b['block_id'] for b in captured[0]['blocks']],['P1','P2'])
        self.assertEqual([c['block_id'] for c in first['claims']],[b['block_id'] for b in fixed])
        self.assertEqual(first['requirements'][0]['block_ids'],[b['block_id'] for b in fixed])
        self.assertEqual(again['reused_blocks'],2)
        self.assertEqual(first['block_id_repairs'],{})

    def test_short_wire_ids_still_require_all_distinct_paragraphs(self):
        fixed=[{'block_id':'B350fb1203e44','text':'First fact. [E1]'},
               {'block_id':'B3545b9798c6c','text':'Second fact. [E1]'}]
        # Reusing an existing short ID cannot hide an omitted paragraph.
        with patch('research_agent.verify.answer_blocks',return_value=fixed):
            with self.assertRaises(ValueError):self.checked(lambda _: 'P1')

    def test_unrelated_or_ambiguous_internal_identifiers_still_fail(self):
        with self.assertRaises(ValueError):self.checked(lambda key:'B000000000000')
        with self.assertRaises(ValueError):self.checked(lambda key:key[:-2]+'zz')
        # Near matches must not select between two possible paragraphs.
        blocks=[{'block_id':'B00000000000a','text':'First. [E1]'},
                {'block_id':'B00000000000b','text':'Second. [E1]'}]
        with patch('research_agent.verify.answer_blocks',return_value=blocks):
            with self.assertRaises(ValueError):self.checked(lambda key:'B00000000000c')

    def test_partial_intersection_cannot_hide_a_borrowed_uncited_original(self):
        answer='First is 17 and second is 25. [E1]'
        evidence=[Evidence('E1','S1','First is 17.','local:D1-0'),
                  Evidence('E2','S2','Second is 25.','local:D2-0')]
        sources=[Source('S1','first','https://example.org/first',''),
                 Source('S2','second','https://example.org/second','')]
        def complete(messages,tools):
            data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
            key=data['blocks'][0]['block_id']
            return ModelDecision('final',content=json.dumps({
                'requirements':[{'id':'R1','requirement':'both values','addressed':True,'block_ids':[key],'reason':'covered'}],
                'blocks':[{'block_id':key,'kind':'fact','supported':True,
                           'evidence_ids':['E1','E2'],'reason':'Each value has support in a different original.'}]}))
        class Model:
            usage_purpose='answer'
        model=Model();model.complete=complete
        checked=check_answer(model,'both values?',answer,evidence,sources)
        self.assertFalse(checked['ready'])
        # Source-level citations remain supported when explicitly written in this block.
        repaired=check_answer(model,'both values?',answer+' [S2]',evidence,sources)
        self.assertTrue(repaired['ready'])
        # A checkpoint produced before this binding fix must be checked again.
        checked['claims'][0]['supported']=True
        checked.pop('binding_version',None)
        replay=check_answer(model,'both values?',answer,evidence,sources,previous=checked)
        self.assertFalse(replay['ready'])
        self.assertEqual(replay['reused_blocks'],0)

    def test_loop_does_not_skip_new_binding_rules_on_an_old_checked_draft(self):
        answer='The gain was 17 percent. [E1]'
        evidence=[Evidence('E1','S1','The gain was 17 percent.','local:D1-0')]
        blocks=answer_blocks(answer)
        claim={**blocks[0],'statement':answer,'supported':True,'kind':'fact','reason':'old check','evidence_ids':['E1']}
        old={'ready':True,'claims':[claim],'blocks':blocks,'requirements':[],'gaps':[]}
        saved={'question':'gain?','messages':[{'role':'user','content':'gain?'}],
            'sources':[asdict(Source('S1','paper','https://example.org',''))],
            'evidence':[asdict(e) for e in evidence],'counters':[1,1,0,0,1],
            'cache':[],'next_iteration':1,'pending_decision':asdict(ModelDecision('final',content=answer)),
            'draft_answer':answer,'answer_check':old,'checked_draft':hashlib.sha256(json.dumps(
                [answer,[(e.evidence_id,e.content_hash,e.kind,e.provenance) for e in evidence]],
                ensure_ascii=False).encode()).hexdigest()}
        class Model:
            name='fixture';supports_answer_verification=True
            def complete(self,messages,tools):raise AssertionError('No answer regeneration is needed')
        with tempfile.TemporaryDirectory() as folder, patch('research_agent.loop.check_answer',return_value=old) as checker:
            result=ResearchAgent(None,Model(),folder,max_rounds=1,resume_state=saved,allow_external=False).run('gain?')
        self.assertEqual(result.status,'ok')
        checker.assert_called_once()

    def test_extra_preview_cannot_add_a_fact_absent_from_the_bound_original(self):
        answer='The gain was 17 percent and cost 25 dollars. [E1] [E2]'
        class Model:
            def complete(self,messages,tools):
                key=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']['blocks'][0]['block_id']
                return ModelDecision('final',content=json.dumps({
                    'requirements':[{'id':'R1','requirement':'gain and cost','addressed':True,'block_ids':[key],'reason':'covered'}],
                    'blocks':[{'block_id':key,'kind':'fact','supported':True,'evidence_ids':['E1','E2'],'reason':'supported'}]}))
        evidence=[Evidence('E1','S1','The gain was 17 percent and cost 25 dollars.','snippet'),
                  Evidence('E2','S1','The gain was 17 percent.','page')]
        result=check_answer(Model(),'gain and cost?',answer,evidence,[])
        self.assertFalse(result['ready'])


    def code_checked(self, answer):
        class Model:
            def complete(self,messages,tools):
                data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                ids=[b['block_id'] for b in data['blocks']]
                return ModelDecision('final',content=json.dumps({
                    'requirements':[{'id':'R1','requirement':'code','addressed':True,'block_ids':ids,'reason':'fixture'}],
                    'blocks':[{'block_id':key,'kind':'fact','supported':True,'evidence_ids':['E1'],'reason':'fixture'} for key in ids]}))
        return check_answer(Model(),'code?',answer,
            [Evidence('E1','S1','def f():\n\n    return 1','page'),
             Evidence('E2','S2','Unrelated document','page')],[])

    def test_closed_code_fence_and_adjacent_citation_form_one_scope(self):
        for opening,closing in [('```python','```'),('~~~python','~~~~'),('````python','`````')]:
            with self.subTest(opening=opening):
                answer=opening+'\ndef f():\n\n    return 1\n'+closing+'\n\n[E1]'
                self.assertEqual(len(answer_blocks(answer)),1)
                self.assertTrue(self.code_checked(answer)['ready'])

    def test_code_citation_cannot_be_borrowed_from_new_assertions_or_wrong_source(self):
        code='```python\ndef f():\n    return 1\n```'
        literal_table='```markdown\n| A | B |\n| --- | --- |\n\n| 1 | 2 |'
        for answer in (code+'\n\n[E2]',code+'\n\nLatency is 3 ms. [E1]',
                       code+'\n\n[E1]\n\nLatency is 3 ms.',
                       literal_table+'\n```\n\n来源：Latency is 3 ms. [E1]',
                       literal_table+'\n\n来源：Latency is 3 ms. [E1]'):
            with self.subTest(answer=answer):
                self.assertFalse(self.code_checked(answer)['ready'])

    def test_unclosed_fence_does_not_acquire_a_following_citation_note(self):
        for last_line in ('    return 1','    if value:'):
            answer='```python\ndef f():\n'+last_line+'\n\n[E1]'
            with self.subTest(last_line=last_line):
                blocks=answer_blocks(answer)
                self.assertEqual(len(blocks),2)
                self.assertNotIn('[E1]',blocks[0]['text'])
                self.assertFalse(self.code_checked(answer)['ready'])


if __name__=='__main__':unittest.main()
