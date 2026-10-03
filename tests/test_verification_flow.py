import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from research_agent.contracts import Evidence,Source,ModelDecision
from research_agent.verify import check_answer,answer_blocks,apply_answer_patch,partial_answer,repair_message
from research_agent.models import OpenAICompatibleModel
from research_agent.loop import ResearchAgent
from research_agent.workbench import route_intent
from research_agent.storage import RunStore


class VerificationFlowTests(unittest.TestCase):
    def test_replay_resume_rejects_changed_windows_manifest(self):
        from evals.run_verification_recovery import assert_same_product
        before={'research_agent\\verify.py':'original','evals\\runner.py':'old runner'}
        assert_same_product(before,{'research_agent/verify.py':'original','evals/runner.py':'new runner'})
        for current in ({'research_agent/verify.py':'changed'},{'evals/runner.py':'only runner'}):
            with self.assertRaises(ValueError):assert_same_product(before,current)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.sources=[Source('S1','Report','https://example.org/report','')]
        self.evidence=[Evidence('E1','S1','The layout model uses RT-DETR. Runtime is ONNX.','local:D1-0',provenance={'page':3,'artifact_id':'paper','chunk_id':1})]

    def verifier(self,verdicts,covered=True):
        captured=[]
        def complete(messages,tools):
            data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA'];captured.append(data)
            checks=[{'block_id':b['block_id'],'kind':kind,'supported':yes,'evidence_ids':ids,'reason':reason}
                for b,(kind,yes,ids,reason) in zip(data['blocks'],verdicts) if not b['cached']]
            req={'id':'R1','requirement':'architecture','addressed':covered,'block_ids':[b['block_id'] for b in data['blocks']],'reason':'coverage'}
            return ModelDecision('final',content=json.dumps({'requirements':[req],'blocks':checks}))
        model=Mock(complete=complete,usage_purpose='answer');return model,captured

    def test_patch_repairs_only_invalid_math_escapes_and_preserves_locked_text(self):
        model, _ = self.verifier([('fact', True, ['E1'], 'supported'), ('fact', False, [], 'unsupported')])
        previous = check_answer(model, 'architecture?', 'RT-DETR. [E1]\n\nWrong extension.', self.evidence, self.sources)
        editable = next(c['block_id'] for c in previous['claims'] if not c['supported'])
        replacement = r'Formula \(\alpha\) remains a hypothesis. [E1]'
        valid = json.dumps({'replace': [{'block_id': editable, 'text': replacement}]})
        malformed = valid.replace(r'\\(', r'\(').replace(r'\\)', r'\)')
        with self.assertRaises(json.JSONDecodeError):
            json.loads(malformed)
        result = apply_answer_patch(malformed, previous)
        self.assertEqual(result, 'RT-DETR. [E1]\n\n' + replacement)
        locked = next(c['block_id'] for c in previous['claims'] if c['supported'])
        with self.assertRaisesRegex(ValueError, 'verified or unknown'):
            apply_answer_patch(malformed.replace(editable, locked), previous)
        with self.assertRaises(json.JSONDecodeError):
            apply_answer_patch('<tool-call>not JSON</tool-call>', previous)

    def test_repair_context_exposes_locked_text_and_existing_length_limit(self):
        model, _ = self.verifier([('fact', True, ['E1'], 'supported'), ('fact', False, [], 'unsupported')])
        previous = check_answer(model, 'architecture?', 'RT-DETR. [E1]\n\nWrong extension.', self.evidence, self.sources)
        message = repair_message(previous)
        data = json.JSONDecoder().raw_decode(message[message.index('{"requirements"'):])[0]
        self.assertEqual(data['verified'][0]['text'], 'RT-DETR. [E1]')
        self.assertEqual(data['editable'][0]['text'], 'Wrong extension.')
        self.assertEqual(data['max_answer_chars'], 12000)
        self.assertEqual(data['current_answer_chars'], len('RT-DETR. [E1]\n\nWrong extension.'))
        with self.assertRaisesRegex(ValueError, 'answer length'):
            apply_answer_patch(json.dumps({'append': ['x' * 12000]}), previous)

    def test_verifier_keeps_complete_original_when_context_budget_allows(self):
        original = 'Introduction. ' * 700 + 'Dataset has 809 training claims and 5,183 abstracts. ' + 'Methods. ' * 1600
        evidence = [Evidence('E1', 'S1', original, 'page')]
        model, captured = self.verifier([('fact', True, ['E1'], 'supported')])
        check = check_answer(model, 'Describe the dataset', 'There are 809 training claims. [E1]',
                             evidence, self.sources, input_limit=63000)
        self.assertTrue(check['ready'])
        sent = captured[0]['evidence'][0]
        self.assertEqual(sent['content'], original)
        self.assertFalse(sent['excerpted'])
        self.assertEqual(sent['original_chars'], len(original))

    def test_verifier_still_bounds_large_originals_and_marks_omissions(self):
        from research_agent.verify import ANSWER_CHECK_PROMPT
        original = '文档内容：The model uses RT-DETR. ' * 1500
        evidence = [Evidence('E1', 'S1', original, 'page')]
        model, captured = self.verifier([('fact', True, ['E1'], 'supported')])
        check_answer(model, 'architecture?', 'RT-DETR. [E1]', evidence, self.sources, input_limit=5000)
        sent = captured[0]['evidence'][0]
        self.assertTrue(sent['excerpted'])
        self.assertLess(len(sent['content']), len(original))
        self.assertEqual(sent['original_chars'], len(original))
        self.assertEqual(evidence[0].content, original)
        limit = 5000 * 3 - len(ANSWER_CHECK_PROMPT.encode()) - 1500
        self.assertLessEqual(len(json.dumps(captured[0], ensure_ascii=False).encode()), limit)

    def test_changed_block_keeps_its_original_window_ahead_of_cached_claims(self):
        facts = [f'ArchivedSection{chr(65+i)} uses a separate retrieval method.' for i in range(20)]
        target = 'UniqueTarget definition requires all gold rationale sentences.'
        original = ''.join(('Background material. ' * 700) + fact + '\n' for fact in [*facts, target])
        evidence = [Evidence('E1', 'S1', original, 'page')]
        answer = '\n\n'.join(fact + ' [E1]' for fact in facts)
        model, _ = self.verifier([('fact', True, ['E1'], 'supported')] * len(facts))
        previous = check_answer(model, 'Define the named methods', answer, evidence, self.sources, input_limit=200000)
        model, captured = self.verifier([('fact', True, ['E1'], 'supported')] * (len(facts)+1))
        checked = check_answer(model, 'Define the named methods', answer+'\n\n'+target+' [E1]',
                               evidence, self.sources, input_limit=3500, previous=previous)
        self.assertEqual(checked['reused_blocks'], len(facts))
        self.assertIn(target, captured[0]['evidence'][0]['content'])
        self.assertEqual(evidence[0].content, original)

    def test_verification_budget_keeps_evidence_for_each_cited_claim(self):
        facts = ['ZephyrAlpha dataset contains 809 claims.', 'BorealisBeta uses TF-IDF retrieval.',
                 'CobaltGamma uses 5183 abstracts.', 'DeltaNova splits 2593 questions.',
                 'EpsilonQuartz measures Evidence-F1.']
        original = ''.join(('Unrelated background. ' * 800) + fact + '\n' for fact in facts)
        evidence = [Evidence('E1', 'S1', original, 'page')]
        model, captured = self.verifier([('fact', True, ['E1'], 'supported')] * len(facts))
        check_answer(model, 'Describe each named method and its setting',
                     '\n\n'.join(fact + ' [E1]' for fact in facts), evidence, self.sources, input_limit=5000)
        sent = captured[0]['evidence'][0]
        self.assertTrue(sent['excerpted'])
        for fact in facts:
            self.assertIn(fact, sent['content'])
        self.assertEqual(evidence[0].content, original)

    def test_parser_page_metadata_reaches_checker_and_survives_store(self):
        model,captured=self.verifier([('fact',True,['E1'],'supported')])
        answer='架构 **RT-DETR**，使用 ONNX；第 3 页。[E1]'
        checked=check_answer(model,'architecture?',answer,self.evidence,self.sources)
        self.assertTrue(checked['ready']);self.assertEqual(captured[0]['evidence'][0]['provenance']['page'],3)
        wrong=check_answer(model,'architecture?',answer.replace('第 3 页','第 9 页'),self.evidence,self.sources)
        self.assertFalse(wrong['ready'])
        from research_agent.contracts import RunResult
        result=RunResult(answer,self.sources,str(self.root/'run.jsonl'),'ok','model_final',1,[],[],evidence=self.evidence)
        store=RunStore(self.root/'db.sqlite');key=store.save(result,'q','now')
        self.assertEqual(store.get(key)['evidence'][0]['provenance'],self.evidence[0].provenance)

    def test_cross_area_document_originals_are_valid_but_history_requires_recall_basis(self):
        doc=Evidence('E1','S1','The layout model uses RT-DETR.','local:x:'+'a'*32+':-:D7-0')
        model,data=self.verifier([('fact',True,['E1'],'supported')])
        self.assertTrue(check_answer(model,'architecture?','RT-DETR. [E1]',[doc],self.sources)['ready'])
        self.assertEqual(data[0]['evidence'][0]['evidence_role'],'document')
        history=Evidence('E1','S1','Previously we chose SQLite.','local:x:'+'a'*32+':'+'b'*32+':H7-0-'+'b'*32)
        self.assertFalse(check_answer(model,'architecture?','SQLite. [E1]',[history],self.sources)['ready'])
        original=model.complete
        def recall(messages,tools):
            decision=original(messages,tools);result=json.loads(decision.content)
            for block in result['blocks']:block['basis']='historical_recall'
            return ModelDecision('final',content=json.dumps(result))
        model.complete=recall
        self.assertTrue(check_answer(model,'What did we decide?','Previously we chose SQLite. [E1]',[history],self.sources)['ready'])
        preview=Evidence('E1','S1','Previously we chose SQLite.','local-snippet:x:'+'a'*32+':-:D7-0')
        self.assertFalse(check_answer(model,'What did we decide?','Previously we chose SQLite. [E1]',[preview],self.sources)['ready'])

    def test_mixed_history_citation_reports_role_conflict_without_granting_support(self):
        answer = '论文采用 RT-DETR。[E1] 此前会话只计划核对实现。[E9]'
        for kind in ('local:H9', 'local:M9'):
            with self.subTest(kind=kind):
                history = Evidence('E9', 'S9', '此前计划核对实现。', kind)
                model, _ = self.verifier([('fact', True, ['E1', 'E9'], 'fixture')])
                check = check_answer(model, '核对论文方法与项目实现', answer,
                                     [*self.evidence, history], [*self.sources, Source('S9', '历史', 'local:history', '')])
                claim = check['claims'][0]
                self.assertFalse(check['ready'])
                self.assertEqual(claim['evidence_ids'], ['E1'])
                self.assertEqual(claim.get('suggested_evidence_ids'), [])
                self.assertIn('E9', claim['reason'])
                self.assertIn('历史或记忆', claim['reason'])
                self.assertIn('独立段落', claim['reason'])
                feedback = json.loads(repair_message(check).split('\n', 1)[1])
                self.assertEqual(feedback['editable'][0]['reason'], claim['reason'])
                self.assertIn('两个换行', repair_message(check))
                split = '论文采用 RT-DETR。[E1]\n\n此前会话只计划核对实现。[E9]'
                patched = apply_answer_patch(json.dumps({'replace': [{'block_id': claim['block_id'], 'text': split}]}), check)
                self.assertEqual([b['text'] for b in answer_blocks(patched)], split.split('\n\n'))

    def test_citation_in_other_paragraph_does_not_ground_uncited_fact(self):
        model,_=self.verifier([('fact',True,['E1'],'supported'),('fact',True,['E1'],'supported')])
        check=check_answer(model,'architecture?','RT-DETR. [E1]\n\nRuntime is ONNX.',self.evidence,self.sources)
        self.assertFalse(check['ready']);self.assertTrue(check['claims'][0]['supported']);self.assertFalse(check['claims'][1]['supported'])

    def test_table_source_note_binds_without_granting_citations_to_unrelated_prose(self):
        answer='| Model | Runtime |\n|---|---|\n| RT-DETR | ONNX |\n\n以上说明见原文。[E1]\n\nAn unrelated uncited claim.'
        blocks=answer_blocks(answer)
        self.assertEqual(len(blocks),2);self.assertIn('[E1]',blocks[0]['text']);self.assertNotIn('[E1]',blocks[1]['text'])
        model,_=self.verifier([('fact',True,['E1'],'supported'),('fact',True,['E1'],'incorrect binding')])
        check=check_answer(model,'runtime?',answer,self.evidence,self.sources)
        self.assertTrue(check['claims'][0]['supported']);self.assertFalse(check['claims'][1]['supported'])

    def test_saved_list_repairs_keep_their_trailing_citations(self):
        path=Path(__file__).parent/'fixtures/list-repair-drafts.json'
        for item in json.loads(path.read_text(encoding='utf-8')):
            with self.subTest(case=item['case'],arm=item['arm']):
                blocks=answer_blocks(item['draft'])
                self.assertEqual(len(blocks),2)
                self.assertIn('[E7]',blocks[0]['text'])
                self.assertTrue(all(name in blocks[0]['text'] for name in ('HotPotQA','FEVER','ALFWorld','WebShop')))

    def test_list_source_scope_and_invalid_evidence_are_still_checked(self):
        for marker in ('1.','1)','-','*','+'):
            answer=f'Model details:\n\n{marker} RT-DETR\n\n{marker} ONNX\n\n来源：[E1]\n\nUnrelated uncited fact.'
            blocks=answer_blocks(answer)
            self.assertEqual(len(blocks),2)
            model,_=self.verifier([('fact',True,['E1'],'supported')]*2)
            result=check_answer(model,'details?',answer,self.evidence,self.sources)
            self.assertTrue(result['claims'][0]['supported']);self.assertFalse(result['claims'][1]['supported'])
        for tail in ('Unrelated assertion. [E1]','# Other topic\n\n[E1]'):
            self.assertNotIn('[E1]',answer_blocks('1. RT-DETR\n2. ONNX\n\n'+tail)[0]['text'])
        for other in ('- ONNX.','1. ONNX.'):
            blocks=answer_blocks('1. RT-DETR.\n\n[E1]\n\n'+other)
            self.assertEqual(len(blocks),2);self.assertNotIn('[E1]',blocks[1]['text'])
        self.assertEqual(len(answer_blocks('1. RT-DETR. [E1]\n\n- ONNX.')),2)
        for citation,evidence in [('[E999]',self.evidence),('[E1]',[Evidence('E1','S1','RT-DETR','snippet')])]:
            model,_=self.verifier([('fact',True,['E1'],'asserted support')])
            self.assertFalse(check_answer(model,'details?','- RT-DETR\n- ONNX\n\n'+citation,evidence,self.sources)['ready'])

    def test_requirement_whitespace_is_normalized_with_exact_wire_id(self):
        def complete(messages,tools):
            data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA'];bid=data['blocks'][0]['block_id']
            return ModelDecision('final',content=json.dumps({'requirements':[{'id':'R1','requirement':'archi tecture','addressed':True,'block_ids':[bid],'reason':'covered'}],
                'blocks':[{'block_id':bid,'kind':'fact','supported':True,'evidence_ids':['E1'],'reason':'supported'}]}))
        base,_=self.verifier([('fact',True,['E1'],'supported')])
        first=check_answer(base,'architecture?','RT-DETR. [E1]',self.evidence,self.sources)
        model=Mock(complete=complete,usage_purpose='answer')
        checked=check_answer(model,'architecture?','Architecture: RT-DETR. [E1]',self.evidence,self.sources,previous=first)
        self.assertTrue(checked['ready']);self.assertEqual(checked['requirements'][0]['requirement'],'architecture')
        self.assertEqual(checked['requirements'][0]['block_ids'],[checked['blocks'][0]['block_id']])

    def test_verification_preserves_context_next_to_a_selected_fact(self):
        original='Research report. '+('intro '*300)+'The experiment used 240 images. Runtime was 12 minutes.'+(' detail'*1600)
        evidence=[Evidence('E1','S1',original,'page')]
        model,data=self.verifier([('fact',True,['E1'],'supported')])
        check_answer(model,'How many images and what runtime?','240 images; 12 minutes. [E1]',evidence,self.sources)
        self.assertIn('240 images. Runtime was 12 minutes.',data[0]['evidence'][0]['content'])
        self.assertEqual(data[0]['evidence'][0]['content'],original)

    def test_unbound_original_is_a_repair_hint_without_granting_support(self):
        model, _ = self.verifier([('fact', True, ['E1'], 'The original supports RT-DETR.')])
        check = check_answer(model, 'architecture?', '- RT-DETR.', self.evidence, self.sources)
        self.assertFalse(check['ready'])
        self.assertEqual(check['claims'][0]['evidence_ids'], [])
        self.assertEqual(check['claims'][0].get('suggested_evidence_ids'), ['E1'])
        feedback = json.loads(repair_message(check).split('\n', 1)[1])
        self.assertEqual(feedback['editable'][0]['suggested_evidence_ids'], ['E1'])
        for candidate in (Evidence('E2', 'S1', 'RT-DETR.', 'snippet'),
                          Evidence('E2', 'S1', 'RT-DETR.', 'local:H1')):
            model, _ = self.verifier([('fact', True, ['E2', 'E999'], 'Try these references.')])
            invalid = check_answer(model, 'architecture?', '- RT-DETR.', [candidate], self.sources)
            self.assertFalse(invalid['ready'])
            self.assertEqual(invalid['claims'][0].get('suggested_evidence_ids', []), [])

    def test_read_original_source_does_not_request_its_preview_again(self):
        for kind in ('snippet', 'local-snippet:D14-0'):
            with self.subTest(kind=kind):
                preview = Evidence('E2', 'S1', 'A separate preview from this source.', kind)
                model, _ = self.verifier([('fact', False, ['E1'], 'Use only E IDs as requested.')])
                result = check_answer(model, 'architecture?', 'RT-DETR. [E1] [S1]',
                                      [*self.evidence, preview], self.sources)
                self.assertFalse(result['ready'])
                self.assertEqual(result['claims'][0]['evidence_ids'], ['E1'])
                self.assertEqual(result['unread_citations'], [])

    def test_explicit_preview_stays_unread_until_covered_by_explicit_original(self):
        for kind in ('snippet', 'local-snippet:D14-0'):
            with self.subTest(kind=kind):
                preview = Evidence('E2', 'S1', 'Runtime is ONNX.', kind)
                model, _ = self.verifier([('fact', False, ['E1'], 'Different semantic failure.')])
                duplicate = check_answer(model, 'architecture?', 'RT-DETR. [E1] [E2] [S1]',
                                         [*self.evidence, preview], self.sources)
                self.assertFalse(duplicate['ready'])
                self.assertEqual(duplicate['unread_citations'], [])
                preview.content = 'An additional unread fact.'
                distinct = check_answer(model, 'architecture?', 'RT-DETR and extra fact. [E1] [E2] [S1]',
                                        [*self.evidence, preview], self.sources)
                self.assertEqual([e['evidence_id'] for e in distinct['unread_citations']], ['E2'])
                preview.content = 'Runtime is ONNX.'
                source_only = check_answer(model, 'architecture?', 'RT-DETR. [E2] [S1]',
                                           [*self.evidence, preview], self.sources)
                self.assertEqual([e['evidence_id'] for e in source_only['unread_citations']], ['E2'])

    def test_local_preview_repair_identifies_the_local_reader(self):
        preview = Evidence('E2', 'S1', 'The model uses RT-DETR.', 'local-snippet:D14-0')
        model, _ = self.verifier([('fact', True, ['E2'], 'preview only')])
        result = check_answer(model, 'architecture?', 'RT-DETR. [E2]', [preview], self.sources)
        self.assertFalse(result['ready'])
        self.assertEqual(result['claims'][0]['evidence_ids'], [])
        self.assertEqual(result['unread_citations'], [
            {'evidence_id': 'E2', 'read_tool': 'read_evidence', 'ref_id': 'E2'}])
        feedback = repair_message(result)
        self.assertIn('read_evidence', feedback)
        self.assertIn('ref_id', feedback)
        preview.kind = 'local-snippet:x:' + 'a' * 32 + ':-:D14-0'
        second = check_answer(model, 'architecture?', 'RT-DETR. [S1]', [preview], self.sources)
        self.assertEqual(second['unread_citations'], result['unread_citations'])

    def test_page_diagnostic_preserves_binding_and_semantic_errors(self):
        preview = Evidence('E2', 'S1', 'Unread distinct detail.', 'local-snippet:D14-0')
        model, _ = self.verifier([('fact', True, ['E1', 'E2'], 'supported')])
        result = check_answer(model, 'architecture?', 'RT-DETR and unread detail on 第9页. [E1] [E2]',
                              [*self.evidence, preview], self.sources)
        self.assertFalse(result['ready'])
        self.assertIn('未绑定', result['claims'][0]['reason'])
        self.assertIn('页码', result['claims'][0]['reason'])
        model, _ = self.verifier([('fact', False, ['E1'], 'The original contradicts the claim.')])
        result = check_answer(model, 'architecture?', 'Wrong architecture on 第9页. [E1]',
                              self.evidence, self.sources)
        self.assertIn('contradicts', result['claims'][0]['reason'])
        self.assertIn('页码', result['claims'][0]['reason'])

    def test_repair_distinguishes_unread_snippets_from_originals(self):
        model,_=self.verifier([('fact',True,['E2'],'snippet only')])
        evidence=self.evidence+[Evidence('E2','S1','The layout model uses RT-DETR.','snippet')]
        check=check_answer(model,'architecture?','RT-DETR. [E2]',evidence,self.sources)
        self.assertFalse(check['ready'])
        self.assertEqual([e['evidence_id'] for e in check['original_evidence']],['E1'])
        self.assertEqual(check['unread_citations'],[{'evidence_id':'E2','read_url':'https://example.org/report'}])
        self.assertIn('https://example.org/report',repair_message(check))

    def test_exact_local_alias_is_resolved_without_accepting_unknown_evidence(self):
        model,_=self.verifier([('fact',True,['local:D1-0'],'supported')])
        check=check_answer(model,'architecture?','RT-DETR. [E1]',self.evidence,self.sources)
        self.assertTrue(check['ready']);self.assertEqual(check['claims'][0]['evidence_ids'],['E1'])
        model,_=self.verifier([('fact',True,['local:D999-0'],'invented ref')])
        self.assertFalse(check_answer(model,'architecture?','RT-DETR. [E1]',self.evidence,self.sources)['ready'])

    def test_supported_paragraph_is_immutable_and_markdown_is_preserved(self):
        answer='**Architecture:** RT-DETR. [E1]\n\nRuntime is invented. [E1]'
        model,_=self.verifier([('fact',True,['E1'],'supported'),('fact',False,['E1'],'wrong runtime')])
        check=check_answer(model,'architecture?',answer,self.evidence,self.sources)
        good,bad=check['blocks']
        with self.assertRaises(ValueError):apply_answer_patch(json.dumps({'replace':[{'block_id':good['block_id'],'text':'deleted'}],'append':[]}),check)
        merged=apply_answer_patch(json.dumps({'replace':[{'block_id':bad['block_id'],'text':'Runtime is ONNX. [E1]'}],'append':[]}),check)
        self.assertIn(good['text'],merged);self.assertNotIn('invented',merged)
        self.assertIn(good['text'],partial_answer(answer,check));self.assertNotIn('invented',partial_answer(answer,check))
        model2,data=self.verifier([('fact',True,['E1'],'supported')]*2)
        checked=check_answer(model2,'architecture?',merged,self.evidence,self.sources,previous=check)
        self.assertTrue(checked['ready']);self.assertEqual(checked['reused_blocks'],1)
        self.assertEqual(data[0]['requirements'],[{'id':'R1','requirement':'architecture'}])

    def test_replace_only_patch_is_valid_but_cannot_modify_verified_content(self):
        answer='Verified. [E1]\n\nUnsupported. [E1]'
        model,_=self.verifier([('fact',True,['E1'],'supported'),('fact',False,['E1'],'unsupported')])
        check=check_answer(model,'architecture?',answer,self.evidence,self.sources)
        good,bad=check['blocks']
        merged=apply_answer_patch(json.dumps({'replace':[{'block_id':bad['block_id'],'text':'Unknown.'}]}),check)
        self.assertIn(good['text'],merged)
        self.assertIn('Unknown.',merged)
        with self.assertRaises(ValueError):
            apply_answer_patch(json.dumps({'replace':[{'block_id':good['block_id'],'text':'Corrupted'}]}),check)
        with self.assertRaises(ValueError):
            apply_answer_patch(json.dumps({'replace':[], 'append':None}),check)

    def test_checker_cannot_drop_requirement_or_paragraph(self):
        model,_=self.verifier([('fact',True,['E1'],'supported')])
        previous=check_answer(model,'architecture?','RT-DETR. [E1]',self.evidence,self.sources)
        previous['requirements'][0]['requirement']='original user requirement'
        with self.assertRaisesRegex(ValueError,'fixed user requirements'):
            check_answer(model,'architecture?','RT-DETR. [E1]',self.evidence,self.sources,previous=previous)
        missing=Mock(usage_purpose='answer',complete=lambda *a:ModelDecision('final',content=json.dumps({'requirements':[{'id':'R1','requirement':'q','addressed':True,'block_ids':[],'reason':''}],'blocks':[]})))
        with self.assertRaisesRegex(ValueError,'omitted'):
            check_answer(missing,'q','Unverified. [E1]',self.evidence,self.sources)

    def test_unavailable_check_never_rewrites_and_resume_rechecks_saved_draft(self):
        calls=[];states=[]
        class Model:
            name='failure';supports_answer_verification=True;usage_purpose='answer'
            def complete(inner,messages,tools):
                calls.append(inner.usage_purpose)
                if inner.usage_purpose=='answer_verification':raise TimeoutError('gateway timed out')
                return ModelDecision('final',content='现有证据不足以确定。')
        agent=ResearchAgent(Mock(),Model(),self.root/'traces',max_tool_calls=2,state_callback=lambda s:states.append(json.loads(json.dumps(s))))
        result=agent.run('Can the evidence determine it?')
        self.assertEqual(calls,['answer','answer_verification']);self.assertEqual(result.termination,'answer_verification_unavailable')
        self.assertIn('待核验草稿',result.answer);self.assertEqual(states[-1]['answer_repair_rounds'],0)
        good,captured=self.verifier([('uncertainty',True,[],'reasonable uncertainty')]);good.supports_answer_verification=True;good.name='resumed';good.request_deadline=None
        # Saved final decisions need only the checker context, even with huge old history.
        states[-1]['messages'].append({'role':'user','content':'old history '*20000})
        with patch('research_agent.loop.ContextCheckpoint.project',side_effect=AssertionError('unneeded compression')) as compact:
            resumed=ResearchAgent(Mock(),good,self.root/'traces',max_tool_calls=2,resume_state=states[-1],semantic_compaction=True).run('q')
        compact.assert_not_called();self.assertEqual(len(captured),1)
        self.assertEqual(resumed.status,'ok');self.assertEqual(resumed.answer,'现有证据不足以确定。')

    def test_timeout_after_patch_keeps_verified_blocks_and_resumes_merged_draft(self):
        from dataclasses import asdict
        draft='Architecture: RT-DETR. [E1]\n\nRuntime is invented. [E1]'
        state={'question':'architecture?','messages':[],'sources':[asdict(x) for x in self.sources],
            'evidence':[asdict(x) for x in self.evidence],'counters':[2,2,0,0,2],'cache':[],
            'next_iteration':2,'pending_decision':asdict(ModelDecision('final',content=draft))}
        base,_=self.verifier([('fact',True,['E1'],'supported'),('fact',False,['E1'],'wrong runtime')])
        calls=[];states=[]
        def complete(messages,tools):
            calls.append(model.usage_purpose)
            if calls==['answer_verification']:return base.complete(messages,tools)
            if model.usage_purpose=='answer_verification':raise TimeoutError('after patch')
            return ModelDecision('final',content=json.dumps({'replace':[{'block_id':answer_blocks(draft)[1]['block_id'],'text':'Runtime is ONNX. [E1]'}],'append':[]}))
        model=Mock(complete=complete,usage_purpose='answer',supports_answer_verification=True,name='repair',request_deadline=None)
        result=ResearchAgent(Mock(),model,self.root/'traces',max_tool_calls=2,resume_state=state,state_callback=states.append).run('architecture?')
        self.assertEqual(result.termination,'answer_verification_unavailable')
        saved=states[-1]
        self.assertEqual(saved['answer_repair_rounds'],1);self.assertFalse(saved['revision_pending'])
        self.assertIn('Runtime is ONNX.',saved['pending_decision']['content'])
        self.assertTrue(saved['answer_check']['claims'][0]['supported'])
        good,captured=self.verifier([('fact',True,['E1'],'supported')]*2)
        good.name='resumed';good.request_deadline=None;good.supports_answer_verification=True
        provider=Mock();events=[]
        with patch('research_agent.loop.ContextCheckpoint.project',side_effect=AssertionError('unneeded compression')):
            resumed=ResearchAgent(provider,good,self.root/'traces',max_tool_calls=2,resume_state=saved,semantic_compaction=True,on_event=events.append).run('architecture?')
        self.assertEqual(resumed.status,'ok');self.assertEqual(len(captured),1)
        self.assertFalse(provider.mock_calls);self.assertTrue(captured[0]['blocks'][0]['cached'])
        self.assertEqual(resumed.tool_calls,2);self.assertNotIn('invented',resumed.answer)
        self.assertFalse(any(e['event']=='model_request' for e in events))

    def test_irrelevant_route_brief_never_turns_local_question_into_web_search(self):
        payload={'intent':'LOCAL_QA','reply':'','brief':'local research plan','assumptions':[],'effort':'none'}
        model=Mock();model.complete.return_value=ModelDecision('final',content=json.dumps(payload))
        result=route_intent(model,[{'role':'user','content':'只依据本地原文，不联网'}],{'name':'test','description':''})
        self.assertEqual(result['intent'],'LOCAL_QA');self.assertEqual(result['brief'],'');self.assertEqual(model.complete.call_count,1)
        payload['effort']='deep'
        model.complete.side_effect=[ModelDecision('final',content=json.dumps(payload)),ModelDecision('final',content=json.dumps({**payload,'intent':'RESEARCH'}))]
        with self.assertRaisesRegex(ValueError,'扩大工具权限'):
            route_intent(model,[{'role':'user','content':'不要联网'}],{'name':'test','description':''})

    def test_verifier_retries_share_deadline_instead_of_three_full_timeouts(self):
        model=OpenAICompatibleModel('https://example.org','test','test',timeout=180);model.usage_purpose='answer_verification'
        model.request_deadline=time.monotonic()+.05
        def request(*args,**kwargs):
            self.assertLessEqual(kwargs['timeout'],.050001)
            raise TimeoutError('timeout')
        with patch('research_agent.models.urlopen',side_effect=request) as network,patch('research_agent.models.time.sleep') as pause:
            with self.assertRaises(RuntimeError):model.complete([],[])
        self.assertEqual(network.call_count,1);pause.assert_not_called();self.assertEqual(len(model.usage_records),1)

    def test_long_verification_uses_configured_timeout_with_a_shared_retry_budget(self):
        model=OpenAICompatibleModel('https://example.org','test','test',timeout=180)
        model.usage_purpose='answer_verification'
        # The first request consumes 101 seconds; a retry gets only the remainder.
        with patch('research_agent.models.time.monotonic',side_effect=[100,100,201,202]), \
             patch('research_agent.models.urlopen',side_effect=TimeoutError('timeout')) as network, \
             patch('research_agent.models.time.sleep'):
            with self.assertRaises(RuntimeError):model.complete([],[])
        self.assertEqual([call.kwargs['timeout'] for call in network.call_args_list],[180,78])
        self.assertEqual(len(model.usage_records),2)

    def test_patch_contract_survives_compaction_and_final_only(self):
        owner=self;requests=[]
        class Model:
            name='patch';supports_answer_verification=True;usage_purpose='answer'
            def complete(inner,messages,tools):
                if inner.usage_purpose=='answer_verification':
                    data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA'];block=data['blocks'][0]
                    good='无法确定' in block['text']
                    return ModelDecision('final',content=json.dumps({'requirements':[{'id':'R1','requirement':'cost','addressed':good,'block_ids':[block['block_id']],'reason':'checked'}],
                      'blocks':[{'block_id':block['block_id'],'kind':'uncertainty' if good else 'fact','supported':good,'evidence_ids':[],'reason':'no cost evidence'}]}))
                requests.append(messages)
                if len(requests)==1:return ModelDecision('final',content='The cost was $100.')
                owner.assertIn('段落补丁 JSON',messages[0]['content']);owner.assertFalse(tools)
                owner.assertIn('FINAL_ONLY', messages[0]['content'])
                return ModelDecision('final',content=json.dumps({'replace':[{'block_id':answer_blocks('The cost was $100.')[0]['block_id'],'text':'现有资料无法确定费用。'}],'append':[]}))
        def compact(system,messages,*args):return [{'role':'system','content':system},{'role':'user','content':'What was the cost?'}]
        with patch('research_agent.loop.ContextCheckpoint.project',side_effect=compact):
            result=ResearchAgent(Mock(),Model(),self.root/'traces',max_tool_calls=0,semantic_compaction=True).run('cost?')
        self.assertEqual(result.status,'ok');self.assertEqual(result.answer,'现有资料无法确定费用。')


if __name__=='__main__':unittest.main()
