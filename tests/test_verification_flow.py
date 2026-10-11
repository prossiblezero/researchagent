import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from research_agent.contracts import Evidence,Source,ModelDecision
from research_agent.verify import check_answer,answer_blocks,apply_answer_patch,partial_answer,repair_message,original_citations
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
            checks=[{'block_id':b['block_id'],'kind':kind,'supported':yes,'evidence_ids':ids,'reason':reason,'citation_relations':[]}
                for b,(kind,yes,ids,reason) in zip(data['blocks'],verdicts) if not b['cached']]
            req={'id':'R1','requirement':'architecture','addressed':covered,'block_ids':[b['block_id'] for b in data['blocks']],'reason':'coverage'}
            return ModelDecision('final',content=json.dumps({'requirements':[req],'blocks':checks,
                'required_document_ids':[d['artifact_id'] for d in data.get('paper_context',{}).get('documents',[])
                                         if d.get('required') or d.get('read_evidence_ids')]}))
        model=Mock(complete=complete,usage_purpose='answer');return model,captured

    def test_available_original_survives_removed_preview_and_refusal(self):
        context = {'documents':[{'artifact_id':'other', 'title':'Another Paper', 'aliases':['Another Paper'], 'required':True, 'ref_id':'D2-0'}]}
        def complete(messages, tools):
            return ModelDecision('final', content=json.dumps({'required_document_ids':['other'], 'requirements':[{'id':'R1', 'requirement':'Compare papers',
                'addressed':False, 'abstention_supported':True, 'block_ids':['P1'], 'reason':'No original available'}],
                'blocks':[{'block_id':'P1', 'kind':'uncertainty', 'supported':True, 'evidence_ids':[], 'reason':'Honest refusal','citation_relations':[]}]}))
        model = Mock(complete=complete, usage_purpose='answer')
        checked = check_answer(model, 'Compare papers', 'Cannot verify Another Paper.', self.evidence, self.sources, allow_external=False, paper_context=context)
        self.assertFalse(checked['abstained'])
        self.assertEqual(checked['missing_reads'][0]['ref_id'], 'D2-0')
        self.assertFalse(checked['requirements'][0]['abstention_supported'])
        self.assertIn('D2-0', repair_message(checked))
        inaccessible = check_answer(model, 'Compare papers', 'Cannot verify Another Paper.', self.evidence, self.sources, allow_external=False)
        self.assertTrue(inaccessible['abstained'])

    def test_citation_direction_rejects_reverse_and_mutual_even_if_model_approves(self):
        docs = [{'artifact_id':'paper', 'title':'Alpha Paper', 'aliases':['Alpha', '种子'], 'required':False, 'ref_id':'D1-0'},
                {'artifact_id':'beta', 'title':'Beta Paper', 'aliases':['Beta'], 'required':False, 'ref_id':'D2-0'}]
        context = {'documents':docs, 'citation_edges':[{'source_id':'paper', 'target_id':'beta',
                   'evidence':[{'chunk_id':1, 'ref_id':'D1-0'}]}]}
        for statement, ready in [('引用关系为 Alpha→Beta。 [E1]', True), ('引用关系为 Beta→Alpha。 [E1]', False),
                                 ('已核实种子↔Beta。 [E1]', False), ('尚未确认 Beta→Alpha。 [E1]', True),
                                 ('Beta 与种子是否互引 → 无证据；Alpha 署名已核。 [E1]', True),
                                 ('Beta→Alpha 尚未核实。 [E1]', True)]:
            with self.subTest(statement=statement):
                model,_ = self.verifier([('fact', True, ['E1'], 'approved')])
                checked = check_answer(model, 'Compare citations', statement, self.evidence, self.sources, paper_context=context)
                self.assertEqual(checked['ready'], ready)
        model,_ = self.verifier([('fact', True, ['E1'], 'approved')])
        checked = check_answer(model, 'Compare citations', 'Alpha→Beta。 [E1]', self.evidence, self.sources, paper_context=context)
        context['citation_edges'][0]['evidence'] = [{'chunk_id':3, 'ref_id':'D3-0'}]
        # An old supported paragraph cannot survive changed relation provenance.
        checked = check_answer(model, 'Compare citations', 'Alpha→Beta。 [E1]', self.evidence, self.sources, previous=checked, paper_context=context)
        self.assertFalse(checked['ready'])
        self.assertEqual(checked['missing_reads'][0]['ref_id'], 'D3-0')
        checked = check_answer(model, 'Compare citations', '种子↔Beta。 [E1]', self.evidence, self.sources, paper_context=context)
        self.assertIn('Alpha Paper → Beta Paper', checked['claims'][0]['reason'])
        self.assertIn('Beta Paper → Alpha Paper', checked['claims'][0]['reason'])
        allowed_complete = model.complete
        def scoped_complete(messages, tools):
            data = json.loads(allowed_complete(messages, tools).content)
            data['required_document_ids'] = []
            return ModelDecision('final', content=json.dumps(data))
        model = Mock(complete=scoped_complete, usage_purpose='answer')
        checked = check_answer(model, 'Use only the allowed sources', 'Alpha→Beta。 [E1]', self.evidence, self.sources, paper_context=context)
        self.assertFalse(checked['ready'])
        self.assertEqual(checked['missing_reads'], [])

    def test_missing_relation_field_retries_instead_of_accepting_a_reverse_citation(self):
        context = {'documents':[
            {'artifact_id':'paper','title':'Alpha Paper','required':False,'ref_id':'D1-0'},
            {'artifact_id':'beta','title':'Beta Paper','required':False,'ref_id':'D2-0'}],
            'citation_edges':[{'source_id':'paper','target_id':'beta','evidence':[{'chunk_id':1,'ref_id':'D1-0'}]}]}
        requests = []
        def complete(messages, tools):
            requests.append(messages)
            block = {'block_id':'P1','kind':'fact','supported':True,'evidence_ids':['E1'],'reason':'approved'}
            if len(requests) > 1:
                block['citation_relations'] = [{'source_id':'beta','target_id':'paper'}]
            return ModelDecision('final',content=json.dumps({'required_document_ids':['paper'], 'requirements':[{'id':'R1','requirement':'Citation',
                'addressed':True,'block_ids':['P1'],'reason':'covered'}],'blocks':[block]}))
        result = check_answer(Mock(complete=complete,usage_purpose='answer'), 'Check citation direction',
            'Beta 引用了 Alpha。[E1]', self.evidence, self.sources, paper_context=context)
        self.assertEqual(len(requests),2)
        self.assertIn('citation_relations',requests[1][0]['content'])
        self.assertFalse(result['ready'])

    def test_unindexed_citation_requires_bound_original_quote_and_target_identity(self):
        title = 'Attention Is All You Need'
        quote = 'Vaswani et al. 2017. Attention Is All You Need. NeurIPS.'
        context = {'documents':[
            {'artifact_id':'paper','title':'Alpha Paper','required':False,'ref_id':'D1-0'},
            {'artifact_id':'beta','title':title,'required':False,'ref_id':'D2-0'}], 'citation_edges':[]}
        evidence = [Evidence('E1','S1',quote,'local:D1-0',provenance={'artifact_id':'paper','chunk_id':1}),
                    Evidence('E2','S1',quote,'local:D2-0',provenance={'artifact_id':'paper','chunk_id':2}),
                    Evidence('E3','S1',quote,'local:D3-0',provenance={'artifact_id':'beta','chunk_id':3}),
                    Evidence('E4','S1','Vaswani et al. 2017. Efficient '+title+'. NeurIPS.',
                             'local:D4-0',provenance={'artifact_id':'paper','chunk_id':4})]
        cases = [
            ('E1',quote,True),
            ('E1','Invented quotation. '+title+'.',False),
            ('E2',quote,False),
            ('E3',quote,False),
            ('E4',evidence[3].content,False),
            ('E4','Attention Is All You Need. NeurIPS.',False),
        ]
        for eid, text, ready in cases:
            with self.subTest(eid=eid,text=text):
                def complete(messages, tools):
                    return ModelDecision('final',content=json.dumps({'required_document_ids':['paper'], 'requirements':[{'id':'R1','requirement':'Citation',
                        'addressed':True,'block_ids':['P1'],'reason':'covered'}], 'blocks':[{
                        'block_id':'P1','kind':'fact','supported':True,'evidence_ids':['E1','E3','E4'],'reason':'checked',
                        'citation_relations':[{'source_id':'paper','target_id':'beta','evidence_id':eid,'quote':text}]}]}))
                result = check_answer(Mock(complete=complete,usage_purpose='answer'), 'Check citations',
                    'Alpha cites Attention Is All You Need. [E1][E3][E4]',evidence,self.sources,paper_context=context)
                self.assertEqual(result['ready'],ready)
                self.assertFalse(result['missing_reads'])

    def test_unindexed_citation_quote_can_span_only_adjacent_bound_originals(self):
        context = {'documents':[
            {'artifact_id':'paper','title':'Alpha Paper','required':False,'ref_id':'D1-0'},
            {'artifact_id':'beta','title':'Attention Is All You Need','required':False,'ref_id':'D2-0'}]}
        first = Evidence('E1','S1','Vaswani et al. 2017. Attention Is All','local:D1-0',
                         provenance={'artifact_id':'paper','chunk_id':1,'ordinal':4})
        second = Evidence('E2','S1','You Need. NeurIPS.','local:D2-0',
                          provenance={'artifact_id':'paper','chunk_id':2,'ordinal':5})
        for refs, ordinal, artifact, bound, ready in [
            (['E1','E2'],5,'paper',['E1','E2'],True),
            (['E2','E1'],5,'paper',['E1','E2'],False),
            (['E1','E2'],7,'paper',['E1','E2'],False),
            (['E1','E2'],5,'beta',['E1','E2'],False),
            (['E1','E2'],5,'paper',['E1'],False),
        ]:
            with self.subTest(refs=refs,ordinal=ordinal,artifact=artifact,bound=bound):
                second.provenance.update(ordinal=ordinal,artifact_id=artifact)
                def complete(messages, tools):
                    return ModelDecision('final',content=json.dumps({'required_document_ids':['paper'],
                        'requirements':[{'id':'R1','requirement':'Citation','addressed':True,
                                         'block_ids':['P1'],'reason':'checked'}], 'blocks':[{
                        'block_id':'P1','kind':'fact','supported':True,'evidence_ids':bound,'reason':'checked',
                        'citation_relations':[{'source_id':'paper','target_id':'beta','evidence_ids':refs,
                            'quote':first.content+' '+second.content}]}]}))
                checked = check_answer(Mock(complete=complete,usage_purpose='answer'), 'Check citation',
                    'Alpha cites Attention Is All You Need. [E1][E2]', [first,second],self.sources,paper_context=context)
                self.assertEqual(checked['ready'],ready)

    def test_paper_reading_scope_must_be_explicit_in_verification(self):
        context = {'documents':[{'artifact_id':'paper','title':'Alpha Paper','required':False,'ref_id':'D1-0'},
                                {'artifact_id':'excluded','title':'Beta Paper','required':False,'ref_id':'D2-0'}]}
        base,_ = self.verifier([('fact',True,['E1'],'checked')])
        calls=[]
        def complete(messages, tools):
            calls.append(messages)
            data=json.loads(base.complete(messages,tools).content)
            if len(calls)==1:data.pop('required_document_ids')
            return ModelDecision('final',content=json.dumps(data))
        checked=check_answer(Mock(complete=complete,usage_purpose='answer'), 'Use only Alpha; exclude Beta',
            'RT-DETR. [E1]',self.evidence,self.sources,paper_context=context)
        self.assertEqual(len(calls),2)
        self.assertEqual(checked['required_document_ids'],['paper'])
        self.assertEqual(checked['missing_reads'],[])
        self.assertTrue(checked['ready'])

    def test_stale_verification_discards_queued_reads_before_repair(self):
        from dataclasses import asdict
        from research_agent.verify import BINDING_VERSION
        draft='RT-DETR. [E1]'
        stale={'binding_version':BINDING_VERSION-1, 'blocks':answer_blocks(draft)}
        state={'question':'Use only Alpha; exclude Beta','messages':[],
            'sources':[asdict(s) for s in self.sources], 'evidence':[asdict(e) for e in self.evidence],
            'counters':[1,1,0,0,1], 'cache':[], 'next_iteration':1,
            'pending_decision':None,'revision_pending':True,'draft_answer':draft,'answer_check':stale,
            'queued_tools':[asdict(ModelDecision('tool_call',tool_name='read_evidence',arguments={'ref_id':'D2-0'}))]}
        calls=[];retriever=Mock();events=[]
        class Model:
            name='stale-read-scope-fixture'
            usage_purpose='answer'
            def complete(model,messages,tools):
                if model.usage_purpose=='answer_verification':
                    data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                    complete='runtime' in data['blocks'][0]['text']
                    return ModelDecision('final',content=json.dumps({'requirements':[{'id':'R1',
                        'requirement':'Architecture and runtime','addressed':complete,'abstention_supported':False,
                        'block_ids':['P1'],'reason':'Add runtime from existing original'}], 'blocks':[{
                        'block_id':'P1','kind':'fact','supported':False if not complete else True,
                        'evidence_ids':['E1'],'reason':'Add runtime'}]}))
                calls.append(messages)
                return ModelDecision('final',content=json.dumps({'replace':[{'block_id':answer_blocks(draft)[0]['block_id'],
                    'text':'RT-DETR; runtime is ONNX. [E1]'}]}))
        result=ResearchAgent(None,Model(),self.root/'stale',retrieval=retriever,space_id='s',
            allow_external=False,max_tool_calls=4,max_rounds=6,answer_verification=True,
            resume_state=state,on_event=events.append).run(state['question'])
        self.assertEqual(result.termination,'model_final')
        retriever.read.assert_not_called()
        self.assertEqual(len(calls),1)
        self.assertTrue(any(e['event']=='answer_check_invalidated' for e in events))

    def test_inheritance_requires_a_bound_original_quote_even_if_verifier_approves(self):
        quote = 'Our method extends the alignment objective of Alpha with an anchor graph.'
        originals = [Evidence('E1', 'S1', quote, 'local:D1-0',
                              provenance={'artifact_id':'paper', 'page':2, 'chunk_id':1}),
                     Evidence('E2', 'S1', quote, 'local:D2-0',
                              provenance={'artifact_id':'paper', 'page':3, 'chunk_id':2})]
        context = {'documents':[{'artifact_id':'paper', 'title':'Beta Paper', 'required':False, 'ref_id':'D1-0'}]}
        cases = [
            ('Beta 对 Alpha 存在明确继承。 [E1]', [], False),
            ('这可定为明确引用/问题继承。 [E1]', [], False),
            ('Beta 直接继承 Alpha。 [E1]', [{'evidence_id':'E2', 'quote':quote}], False),
            ('Beta 方法继承 Alpha。 [E1]', [{'evidence_id':'E1', 'quote':'Fabricated quotation about code reuse.'}], False),
            ('Beta 直接继承 Alpha 的对齐目标。 [E1]', [{'evidence_id':'E1', 'quote':quote}], True),
            ('不能仅因引用认定直接继承。 [E1]', [], True),
            ('这是共享研究问题，尚无明确继承证据。 [E1]', [], True),
            ('已读证据只支持共同问题，非明确继承。 [E1]', [], True),
            ('未显示引用种子，故非直接继承。 [E1]', [], True),
            ('直接继承关系尚无证据支持。 [E1]', [], True),
        ]
        for statement, support, ready in cases:
            with self.subTest(statement=statement, support=support):
                def complete(messages, tools):
                    return ModelDecision('final', content=json.dumps({
                        'required_document_ids':['paper'], 'requirements':[{'id':'R1', 'requirement':'relation', 'addressed':True,
                                         'block_ids':['P1'], 'reason':'covered'}],
                        'blocks':[{'block_id':'P1', 'kind':'fact', 'supported':True, 'evidence_ids':['E1'],
                                   'reason':'approved', 'inheritance_evidence':support, 'citation_relations':[]}]}))
                model = Mock(complete=complete, usage_purpose='answer')
                checked = check_answer(model, 'Compare the methods', statement, originals, self.sources, paper_context=context)
                self.assertEqual(checked['ready'], ready)
                if not ready:
                    self.assertIn('不能证明继承', checked['claims'][0]['reason'])

    def test_named_paper_front_matter_remains_visible_when_answer_omits_it(self):
        originals = [*self.evidence, *[Evidence(f'E{i}', 'S1', 'Architecture and runtime '*50, f'local:D{i}-0',
                     provenance={'artifact_id':'paper', 'page':3, 'chunk_id':i}) for i in range(2, 35)],
                     Evidence('E40', 'S2', 'Publication: 2026; Example University.', 'local:D40-0',
                              provenance={'artifact_id':'other', 'page':1, 'chunk_id':40})]
        context = {'documents':[{'artifact_id':'other', 'title':'Another Paper', 'required':True, 'ref_id':'D40-0'}]}
        model, captured = self.verifier([('fact', True, ['E1'], 'supported')])
        check_answer(model, 'Architecture and runtime?', 'RT-DETR. [E1]', originals, self.sources, paper_context=context)
        self.assertIn('E40', [e['evidence_id'] for e in captured[0]['evidence']])

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

    def test_supported_refusal_is_incomplete_and_can_be_replaced_without_unlocking_facts(self):
        model, _ = self.verifier([('fact', True, ['E1'], 'supported'),
                                 ('uncertainty', True, [], 'Need the measured runtime')], covered=False)
        checked = check_answer(model, 'What architecture and measured runtime?',
                               'RT-DETR. [E1]\n\nI cannot determine the runtime.', self.evidence, self.sources)
        self.assertTrue(checked['grounded'])
        self.assertFalse(checked['complete'])
        self.assertFalse(checked['ready'])
        self.assertEqual(checked['state'], 'incomplete')
        fact, refusal = checked['blocks']
        message = repair_message(checked)
        data = json.JSONDecoder().raw_decode(message[message.index('{"requirements"'):])[0]
        self.assertEqual([b['block_id'] for b in data['editable']], [refusal['block_id']])
        self.assertEqual([b['block_id'] for b in data['verified']], [fact['block_id']])
        patch_text = json.dumps({'replace': [{'block_id': refusal['block_id'], 'text': 'Newly read measurement. [E2]'}]})
        revised = apply_answer_patch(patch_text, checked)
        self.assertIn(fact['text'], revised)
        self.assertNotIn('cannot determine', revised)
        with self.assertRaisesRegex(ValueError, 'verified or unknown'):
            apply_answer_patch(patch_text.replace(refusal['block_id'], fact['block_id']), checked)

        justified, _ = self.verifier([('uncertainty', True, [], 'The question asks whether this can be determined')])
        result = check_answer(justified, 'Can the runtime be determined from this excerpt?',
                              'The available evidence is insufficient.', self.evidence, self.sources)
        self.assertTrue(result['ready'])
        self.assertTrue(result['grounded'] and result['complete'])

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

    def test_malformed_verdict_rechecks_unchanged_draft_and_originals(self):
        for failure in ('missing_supported', 'coverage', 'editor', 'invalid_json'):
            with self.subTest(failure=failure):
                base,captured=self.verifier([('fact',True,['E1'],'original supports the claim')])
                prompts=[];deadlines=[];retries=[]
                def complete(messages,tools):
                    prompts.append(messages[0]['content']);deadlines.append(model.request_deadline)
                    decision=base.complete(messages,tools)
                    if len(prompts)>1:return decision
                    data=json.loads(decision.content)
                    if failure=='missing_supported':del data['blocks'][0]['supported']
                    elif failure=='coverage':data['requirements'][0]['coverage']=data['requirements'][0].pop('block_ids')
                    elif failure=='editor':data['blocks'][0]['kind']='editor'
                    else:return ModelDecision('final',content='{"blocks":')
                    return ModelDecision('final',content=json.dumps(data))
                model=Mock(complete=complete,usage_purpose='answer',request_deadline=None,timeout=90)
                result=check_answer(model,'architecture?','RT-DETR. [E1]',self.evidence,self.sources,on_retry=retries.append)
                self.assertTrue(result['ready']);self.assertEqual(result['format_retries'],1)
                self.assertEqual(len(prompts),2);self.assertEqual(captured[0],captured[1])
                self.assertIn(retries[0],prompts[1]);self.assertEqual(deadlines[0],deadlines[1])
                self.assertIsNone(model.request_deadline);self.assertEqual(model.usage_purpose,'answer')

    def test_schema_retry_does_not_infer_support_or_loop_forever(self):
        base,_=self.verifier([('fact',True,['E1'],'explicit prose says the original supports this')])
        def complete(messages,tools):
            data=json.loads(base.complete(messages,tools).content)
            del data['blocks'][0]['supported']
            return ModelDecision('final',content=json.dumps(data))
        model=Mock(usage_purpose='answer',request_deadline=None)
        model.complete.side_effect=complete
        with self.assertRaisesRegex(ValueError,'supported boolean'):
            check_answer(model,'architecture?','RT-DETR. [E1]',self.evidence,self.sources)
        self.assertEqual(model.complete.call_count,2)
        self.assertEqual(model.usage_purpose,'answer');self.assertIsNone(model.request_deadline)

    def test_schema_retry_never_overrides_false_or_unbound_verdict(self):
        for supported,refs in [(False,['E1']),(True,['E999'])]:
            with self.subTest(supported=supported,refs=refs):
                base,_=self.verifier([('fact',supported,refs,'semantic verdict')])
                calls=[]
                def complete(messages,tools):
                    calls.append(messages)
                    data=json.loads(base.complete(messages,tools).content)
                    if len(calls)==1:data['blocks'][0]['kind']='editor'
                    return ModelDecision('final',content=json.dumps(data))
                result=check_answer(Mock(complete=complete,usage_purpose='answer'),
                    'architecture?','RT-DETR. [E1]',self.evidence,self.sources)
                self.assertFalse(result['ready']);self.assertFalse(result['claims'][0]['supported'])
                self.assertEqual(len(calls),2)
        base,captured=self.verifier([('fact',False,['E1'],'wrong architecture')])
        self.assertFalse(check_answer(base,'architecture?','invented. [E1]',self.evidence,self.sources)['ready'])
        self.assertEqual(len(captured),1)

    def test_schema_retry_cannot_extend_expired_task_deadline(self):
        clock=[100.0];model=Mock(usage_purpose='answer',request_deadline=101.0)
        def complete(messages,tools):
            clock[0]=102.0
            return ModelDecision('final',content='{}')
        model.complete.side_effect=complete;retry=Mock()
        with patch('research_agent.verify.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(TimeoutError):
                check_answer(model,'architecture?','RT-DETR. [E1]',self.evidence,self.sources,on_retry=retry)
        self.assertEqual(model.complete.call_count,1);retry.assert_not_called()
        self.assertEqual(model.request_deadline,101.0);self.assertEqual(model.usage_purpose,'answer')

    def test_524_then_malformed_verdict_recovers_without_regenerating_answer(self):
        from dataclasses import asdict
        from io import BytesIO
        from urllib.error import HTTPError
        draft='Architecture: RT-DETR. [E1]'
        state={'question':'architecture?','messages':[], 'sources':[asdict(s) for s in self.sources],
            'evidence':[asdict(e) for e in self.evidence],'counters':[1,1,0,0,1], 'cache':[],
            'next_iteration':1,'pending_decision':asdict(ModelDecision('final',content=draft))}
        verdict={'requirements':[{'id':'R1','requirement':'architecture','addressed':True,'block_ids':['P1'],'reason':'covered'}],
            'blocks':[{'block_id':'P1','kind':'fact','supported':True,'evidence_ids':['E1'],'reason':'supported'}]}
        malformed=json.loads(json.dumps(verdict));del malformed['blocks'][0]['supported']
        def response(value):return BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps(value)}}]}).encode())
        outage=HTTPError('https://example.org',524,'upstream timed out',{},BytesIO(b'upstream timed out'))
        model=OpenAICompatibleModel('https://example.org','test','test');events=[];provider=Mock()
        with patch('research_agent.models.urlopen',side_effect=[outage,response(malformed),response(verdict)]) as network, \
             patch('research_agent.models.time.sleep'):
            result=ResearchAgent(provider,model,self.root/'traces',max_tool_calls=1,resume_state=state,on_event=events.append).run('architecture?')
        self.assertEqual(result.status,'ok');self.assertEqual(result.answer,draft)
        self.assertEqual(network.call_count,3);self.assertFalse(provider.mock_calls)
        self.assertEqual([r['purpose'] for r in model.usage_records],['answer_verification']*3)
        self.assertEqual(sum(e['event']=='answer_check_retry' for e in events),1)
        self.assertFalse(any(e['event']=='answer_repair' for e in events))

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

    def test_slow_verification_completes_once_within_the_existing_allowance(self):
        from io import BytesIO
        for parent_seconds, expected_wait, elapsed in [(None, 90, 60), (70, 70, 60), (240, 240, 120)]:
            with self.subTest(parent_seconds=parent_seconds):
                model=OpenAICompatibleModel('https://example.org','test','test')
                model.usage_purpose='answer_verification'
                model.request_deadline=100+parent_seconds if parent_seconds else None
                clock=[100.0]
                def response(request, timeout):
                    # Slow but valid responses must fit the enclosing task's allowance.
                    self.assertAlmostEqual(timeout,expected_wait)
                    self.assertNotIn('max_tokens',json.loads(request.data))
                    if timeout < elapsed:raise TimeoutError('premature verification cutoff')
                    clock[0]+=elapsed
                    return BytesIO(b'{"choices":[{"message":{"content":"checked"}}],"usage":{"total_tokens":123}}')
                with patch('research_agent.models.time.monotonic',side_effect=lambda:clock[0]), \
                     patch('research_agent.models.urlopen',side_effect=response) as network:
                    self.assertEqual(model.complete([],[]).content,'checked')
                self.assertEqual(network.call_count,1)
                self.assertEqual(len(model.usage_records),1)
                self.assertEqual(model.usage_records[0]['total_tokens'],123)

    def test_grounded_abstention_stops_but_answerable_gap_is_repaired(self):
        from dataclasses import asdict
        draft='The document describes the dataset. [E1]\n\nI cannot determine its size from the available evidence.'
        for answerable in (False, True):
            with self.subTest(answerable=answerable):
                evidence=[Evidence('E1','S1','The dataset contains 100 items.' if answerable else 'Dataset description without a size.', 'local:D1-0')]
                state={'question':'How big is the dataset?','messages':[],
                    'sources':[asdict(s) for s in self.sources],'evidence':[asdict(e) for e in evidence],
                    'counters':[1,1,0,0,1],'cache':[],'next_iteration':1,
                    'pending_decision':asdict(ModelDecision('final',content=draft))}
                calls=[];events=[]
                def complete(messages,tools):
                    calls.append(model.usage_purpose)
                    if model.usage_purpose=='answer_repair':
                        return ModelDecision('final',content=json.dumps({'replace':[
                            {'block_id':answer_blocks(draft)[1]['block_id'],'text':'The dataset contains 100 items. [E1]'}]}))
                    self.assertEqual(model.usage_purpose,'answer_verification')
                    data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                    covered=any('100 items' in b['text'] for b in data['blocks'])
                    return ModelDecision('final',content=json.dumps({
                        'requirements':[{'id':'R1','requirement':'Give the dataset size.','addressed':covered,
                            'abstention_supported':not answerable,'block_ids':['P2'],'reason':'No size evidence' if not answerable else 'E1 gives 100 items'}],
                        'blocks':[{'block_id':b['block_id'],'kind':'uncertainty' if 'cannot determine' in b['text'] else 'fact',
                            'supported':True,'evidence_ids':[] if 'cannot determine' in b['text'] else ['E1'],'reason':'supported'}
                            for b in data['blocks'] if not b['cached']]}))
                model=Mock(complete=complete,usage_purpose='answer',name='fixture',request_deadline=None,supports_answer_verification=True)
                result=ResearchAgent(Mock(),model,self.root/'traces',max_tool_calls=1,resume_state=state,on_event=events.append).run(state['question'])
                self.assertEqual(result.status,'ok')
                self.assertEqual(result.termination,'model_final' if answerable else 'evidence_insufficient')
                self.assertEqual(calls,['answer_verification','answer_repair','answer_verification'] if answerable else ['answer_verification'])
                last=next(e for e in reversed(events) if e['event']=='answer_checked')
                self.assertEqual(last['complete'],answerable)
                self.assertEqual(last['ready'],answerable)
                self.assertEqual(last['abstained'],not answerable)
                self.assertNotIn('尚未完成',result.answer)

    def test_abstention_flag_cannot_approve_unsupported_or_unread_claims(self):
        base,_=self.verifier([('fact',True,['E1'],'asserted support')],covered=False)
        def complete(messages,tools):
            response=base.complete(messages,tools)
            data=json.loads(response.content)
            data['requirements'][0]['abstention_supported']=True
            return ModelDecision('final',content=json.dumps(data))
        model=Mock(complete=complete,usage_purpose='answer')
        for evidence,answer in [([], 'No sources.'),
                               ([Evidence('E1','S1','Preview','local-snippet:D1-0')], 'No size. [E1]'),
                               (self.evidence, 'No size. [E999]')]:
            with self.subTest(answer=answer):
                checked=check_answer(model,'Size?',answer,evidence,self.sources)
                self.assertFalse(checked['abstained'])
                self.assertFalse(checked['ready'])

    def test_preview_relabel_requires_unique_containing_read_original(self):
        preview=Evidence('E2','S1','The layout model','local-snippet:D1-0')
        answer='RT-DETR. [E2] [E999]'
        fixed,changes=original_citations(answer,[*self.evidence,preview])
        self.assertEqual(fixed,'RT-DETR. [E1] [E999]')
        self.assertEqual(changes,{'E2':'E1'})
        for originals in [[],[Evidence('E1','S2',preview.content,'local:D2-0')],
                          [Evidence('E1','S1','Different text','local:D1-0')],
                          [*self.evidence,Evidence('E3','S1',preview.content,'local:D1-1')]]:
            self.assertEqual(original_citations(answer,[preview,*originals]),(answer,{}))

    def test_terminal_verification_and_repair_request_json_at_transport(self):
        from io import BytesIO
        model=OpenAICompatibleModel('https://example.org','test','test')
        for purpose,tools,constrained in [('answer_verification',[],True),('answer_repair',[],True),('answer',[],False),
                                          ('answer_repair',[{'type':'function','function':{'name':'read'}}],False)]:
            model.usage_purpose=purpose
            with patch('research_agent.models.urlopen',return_value=BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')) as network:
                model.complete([{'role':'user','content':'Return JSON'}],tools)
            payload=json.loads(network.call_args.args[0].data)
            self.assertEqual(payload.get('response_format'),{'type':'json_object'} if constrained else None)
            self.assertNotIn('max_tokens',payload)

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
