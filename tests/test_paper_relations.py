"""Source-scoped relationships must not turn homonyms or mentions into verified edges."""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from research_agent.contracts import ModelDecision, ReadResponse
from research_agent.library import Library, MaterialReader
from research_agent.loop import ResearchAgent
from research_agent.retrieval import Retriever
from research_agent.workbench_store import WorkbenchStore
from research_agent.paper_relations import authors, PaperRelations


class PaperRelationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WorkbenchStore(self.root / 'state.sqlite')
        self.space = self.store.save_space({'name': 'Relations'})['id']
        self.library = Library(self.store)
        self.retriever = Retriever(self.store, self.library)
        self.retriever._dense_attempted = True

    def paper(self, title, people, body=None, *, space=None, kind='paper'):
        names = '; '.join(p.get('name', '') if isinstance(p, dict) else p for p in people)
        parts = [('正文', title + '\n' + names + '\nSchool of Science, Example University\n{alexlee, samchen}@example.edu')]
        parts += body or [('method', 'The method groups multiple views using a shared representation.')]
        return self.library.save(space or self.space, {
            'kind': kind, 'title': title, 'url': 'https://example.org/' + str(len(self.library.list(space or self.space))),
            'canonical_id': title, 'metadata': {'authors': people}, 'data': None,
            'chunks': [{'text': text, 'page': 1 if i == 0 else i + 1, 'section': section,
                        'line_start': 1, 'line_end': 3} for i, (section, text) in enumerate(parts)],
            'warnings': [], 'boundary': 'fixture original'})[0]['id']

    def lookup(self, mode, **kwargs):
        return self.retriever.lookup_papers(self.space, mode=mode, **kwargs)

    def test_original_bylines_recover_missing_partial_and_producer_metadata(self):
        title = 'Robust Alignment with Multiple Views'
        for metadata in ([], ['Publisher'], ['Alex Lee']):
            for byline in ('Alex Lee a, Sam Chen b, Pat Smith a,∗',
                           'Alex Lee\n , Member , IEEE, Sam Chen, and Pat Smith'):
                with self.subTest(metadata=metadata, byline=byline):
                    chunks = [{'id': 1, 'page': 1, 'section': '正文', 'content': title+'\n'+byline+
                               '\na School of Science, Example University\nAbstract— We compare to Invented Author, Another Person.'},
                              {'id': 2, 'page': 1, 'section': 'introduction',
                               'content': 'Introduction\nE-mail: alexlee@example.edu; patsmith@other.edu'},
                              {'id': 3, 'page': 2, 'section': 'references', 'content': 'Invented Author, Another Person'}]
                    found = authors({'title': title, 'metadata': {'authors': metadata}}, chunks)
                    self.assertEqual([a['name'] for a in found], ['Alex Lee', 'Sam Chen', 'Pat Smith'])
                    self.assertEqual(found[0]['emails'], ['alexlee@example.edu'])
                    self.assertEqual(found[1]['emails'], [])
        chunks = [{'id': 1, 'page': 1, 'section': '正文', 'content': title+'\nA. Lee, Sam Chen\nAbstract\nAlex Lee'}]
        found = authors({'title': title, 'metadata': {'authors': ['Alex Lee']}}, chunks)
        self.assertEqual([a['name'] for a in found], ['A. Lee', 'Sam Chen'])

    def test_verification_inventory_scopes_available_originals_and_directed_edges(self):
        title = 'Robust Alignment with Multiple Views'
        target = self.paper(title, ['Alex Lee'])
        source = self.paper('Graph Recovery for Partially Aligned Data', ['Sam Chen'], [('references', title+'. Proceedings.')])
        question = 'Compare seed paper Graph Recovery for Partially Aligned Data (GRPA) and '+title
        context = self.retriever.verification_context(self.space, question)
        self.assertEqual({d['artifact_id'] for d in context['documents']}, {source, target})
        self.assertTrue(all(d['mentioned'] for d in context['documents']))
        self.assertIn('GRPA', next(d for d in context['documents'] if d['artifact_id'] == source)['aliases'])
        self.assertEqual([(e['source_id'], e['target_id']) for e in context['citation_edges']], [(source, target)])
        with patch.object(self.library, 'authorized', side_effect=lambda p: p['id'] != target):
            limited = PaperRelations(self.store, self.library).verification_context(self.space, question)
        self.assertEqual([d['artifact_id'] for d in limited['documents']], [source])
        self.assertFalse(limited['citation_edges'])

    def test_available_unread_original_is_read_before_repair_instead_of_abandoned(self):
        title = 'Robust Alignment with Multiple Views'
        paper = self.paper(title, ['Alex Lee'])
        calls, events = [], []
        class Model:
            name = 'local-gap-recovery-fixture'
            usage_purpose = 'answer'
            def complete(model, messages, tools):
                if model.usage_purpose == 'answer_verification':
                    data = json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
                    complete = 'Alex Lee' in data['blocks'][0]['text']
                    return ModelDecision('final', content=json.dumps({'required_document_ids':[paper], 'requirements': [{'id':'R1', 'requirement':'Identify author',
                        'addressed':complete, 'abstention_supported':not complete, 'block_ids':['P1'], 'reason':'No original' if not complete else 'Read byline'}],
                        'blocks':[{'block_id':'P1', 'kind':'fact' if complete else 'uncertainty', 'supported':True,
                                   'evidence_ids':['E2'] if complete else [], 'reason':'checked', 'citation_relations':[]}]}))
                calls.append(messages)
                if len(calls) <= 3:
                    return ModelDecision('tool_call', tool_name='retrieve', arguments={'query':title, 'top_k':1})
                if len(calls) == 4:
                    return ModelDecision('final', content='Evidence unavailable.')
                from research_agent.verify import answer_blocks
                return ModelDecision('final', content=json.dumps({'replace':[{'block_id':answer_blocks('Evidence unavailable.')[0]['block_id'],
                            'text':'The byline lists Alex Lee. [E2]'}]}))
        result = ResearchAgent(None, Model(), self.root/'traces', retrieval=self.retriever, space_id=self.space,
            allow_external=False, max_tool_calls=4, max_rounds=8, answer_verification=True, on_event=events.append).run('Who wrote '+title+'?')
        self.assertEqual(result.termination, 'model_final')
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.tool_attempts, 4)
        self.assertIn('Alex Lee', result.answer)
        self.assertTrue(any(e['event'] == 'answer_gap_reads_scheduled' for e in events))
        self.assertTrue(any(e['event'] == 'answer_repair_tools_reopened' for e in events))

    def test_excluded_title_is_never_automatically_read(self):
        allowed_title = 'A Verified Analysis Of Retrieval Systems'
        excluded_title = 'B Excluded Analysis Of Retrieval Systems'
        allowed = self.paper(allowed_title, ['Alex Lee'])
        excluded = self.paper(excluded_title, ['Sam Chen'])
        ref = 'D' + str(self.library.chunks(self.space, allowed)[0]['id']) + '-0'
        questions = [
            f'只使用 {allowed_title} 回答。不得读取或引用 {excluded_title}。',
            f'Use {allowed_title}; do not read or cite {excluded_title}.',
            f'使用 {allowed_title}，{excluded_title} 不要读取或引用。',
            f'仅依据 {allowed_title} 回答，不要参考 {excluded_title}。',
            f'仅根据 {allowed_title} 回答，不读 {excluded_title}。',
        ]
        for question in questions:
            with self.subTest(question=question):
                calls, events = [], []
                class Model:
                    name = 'scope-recovery-fixture'
                    usage_purpose = 'answer'
                    def complete(model, messages, tools):
                        if model.usage_purpose == 'answer_verification':
                            return ModelDecision('final', content=json.dumps({
                                'required_document_ids':[allowed], 'requirements':[{'id':'R1', 'requirement':'Identify the allowed author',
                                    'addressed':True, 'block_ids':['P1'], 'reason':'Read byline'}],
                                'blocks':[{'block_id':'P1', 'kind':'fact', 'supported':True,
                                    'evidence_ids':['E1'], 'reason':'Byline', 'citation_relations':[]}]}))
                        calls.append(messages)
                        if len(calls) == 1:
                            return ModelDecision('tool_call', tool_name='read_evidence', arguments={'ref_id':ref, 'adjacent':0})
                        return ModelDecision('final', content='The author is Alex Lee. [E1]')
                result = ResearchAgent(None, Model(), self.root/'scope-traces', retrieval=self.retriever,
                    space_id=self.space, allow_external=False, max_tool_calls=4, max_rounds=6,
                    answer_verification=True, on_event=events.append).run(question)
                self.assertEqual(result.termination, 'model_final')
                self.assertEqual(result.tool_calls, 1)
                self.assertEqual({e.provenance['artifact_id'] for e in result.evidence}, {allowed})
                self.assertFalse(any(e['event']=='answer_gap_reads_scheduled' for e in events))
        context = self.retriever.verification_context(self.space,
            f'比较 {allowed_title} 和 {excluded_title}。不要联网。')
        self.assertEqual({d['artifact_id'] for d in context['documents'] if d['mentioned']}, {allowed, excluded})
        self.assertTrue(all('required' not in d for d in context['documents']))

    def test_title_suffix_does_not_create_a_citation_edge(self):
        title = 'Attention Based Learning for Incomplete Views'
        target = self.paper(title, ['Alex Lee'])
        for prefix in ('Efficient ', 'Efficient\n'):
            with self.subTest(prefix=prefix):
                citing = self.paper('Source Paper for Testing Citation Boundaries '+str(len(prefix)), ['Sam Chen'],
                    [('references', 'Lee, A. 2025. '+prefix+title+'. Proceedings.')])
                self.assertEqual(self.lookup('references', artifact_ids=[citing])['total'], 0)
        citing = self.paper('Reference With a Valid Title Boundary', ['Sam Chen'],
                            [('references', 'References\n'+title+'. Proceedings.')])
        self.assertEqual(self.lookup('references', artifact_ids=[citing])['results'][0]['target_id'], target)

    def test_full_names_email_support_homonyms_and_initials(self):
        seed = self.paper('Shared Views with a Consistent Representation', ['Alex Lee', 'Sam Chen'])
        other = self.paper('Robust Views with a Consistent Representation', ['Sam Chen, Alex Lee'])
        initial = self.paper('Incomplete Views and Unresolved Initial Names', ['A. Lee', 'Sam Chen'])
        homonym = self.paper('Other Views from a Different Research Institute', ['Alex Lee', 'Pat Smith'])
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE document_chunks SET content=replace(content,'{alexlee, samchen}@example.edu','alex@other.edu') WHERE artifact_id=?", (homonym,))
        hits = {r['artifact_id']: r for r in self.lookup('author_papers', author='Alex Lee', artifact_ids=[seed])['results']}
        self.assertEqual(set(hits), {seed, other, homonym})
        self.assertEqual(hits[other]['identity_match'], 'shared_name_and_email')
        self.assertEqual(hits[homonym]['identity_match'], 'unresolved_same_name')
        self.assertNotIn(initial, hits)
        self.assertEqual({a['name'] for a in hits[other]['authors']}, {'Sam Chen', 'Alex Lee'})
        self.assertEqual([a['name'] for a in hits[other]['authors']], ['Sam Chen', 'Alex Lee'])

    def test_name_prefix_is_not_a_matching_byline(self):
        item = self.paper('Matching Full Names in Paper Metadata', ['Alex Lee'])
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE document_chunks SET content='Matching Full Names in Paper Metadata\nAlex Leeman\nExample University' WHERE artifact_id=? AND ordinal=0", (item,))
        self.assertEqual(self.lookup('author_papers', author='Alex Lee')['total'], 0)

    def test_conflicting_orcid_is_not_overridden_by_a_shared_email(self):
        seed = self.paper('Structured Identities in Multimodal Clustering', [{'name': 'Alex Lee', 'orcid': '0000-0001-2345-6789'}])
        other = self.paper('Disambiguated Identities in Multimodal Clustering', [{'name': 'Alex Lee', 'orcid': '0000-0002-2345-6789'}])
        hits = self.lookup('author_papers', author='Alex Lee', artifact_ids=[seed])['results']
        self.assertEqual(next(h for h in hits if h['artifact_id'] == other)['identity_match'], 'conflicting_orcid')

    def test_shared_citations_cross_chunks_direction_and_source_readback(self):
        title = 'Consistency Aware Padding for Incomplete Multimodal Alignment'
        target = self.paper(title, ['Sam Chen', 'Alex Lee'])
        left = self.paper('Dual Learning for Partially Aligned Multimodal Clustering', ['Alex Lee'], [
            ('references', 'Chen, S.; Lee, A. 2025. Consistency Aware Padding for'),
            ('references', 'Incomplete Multimodal Alignment. In Proceedings, 10-20.')])
        right = self.paper('Local Recovery for Partially Aligned Multimodal Clustering', ['Sam Chen'], [
            ('references', 'Sam Chen and Alex Lee. 2025. ' + title + '. In Proceedings, 10-20.')])
        mention = self.paper('Comparing Existing Approaches to Multimodal Clustering', ['Pat Smith'], [('method', title + '. This is a body mention.')])
        result = self.lookup('shared_references', artifact_ids=[left, right])
        self.assertEqual({(r['source_id'], r['target_id']) for r in result['results']}, {(left, target), (right, target)})
        left_edge = next(e for e in result['results'] if e['source_id'] == left)
        self.assertEqual(len(left_edge['evidence']), 2)
        for evidence in left_edge['evidence']:
            read = self.retriever.read(self.space, evidence['ref_id'], adjacent=0)
            self.assertEqual(read['content'], evidence['quote'])
            self.assertEqual(read['artifact_id'], left)
            self.assertIsInstance(read['ordinal'], int)
        reverse = self.lookup('cited_by', artifact_ids=[target])
        self.assertEqual({e['source_id'] for e in reverse['results']}, {left, right})
        self.assertEqual(self.lookup('references', artifact_ids=[mention])['total'], 0)

    def test_title_prefix_does_not_match_a_different_reference(self):
        target = self.paper('Attention Based Learning for Incomplete Views', ['Alex Lee'])
        citing = self.paper('Another Method for Incomplete Multimodal Views', ['Sam Chen'], [
            ('references', 'Lee, A. 2025. Attention Based Learning for Incomplete Views with Additional Supervision. Proceedings.')])
        self.assertEqual(self.lookup('references', artifact_ids=[citing])['total'], 0)
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE document_chunks SET content=content || ' Lee, A. 2024. Attention Based Learning for Incomplete Views. Proceedings.' WHERE artifact_id=? AND section='references'", (citing,))
        self.assertEqual(self.lookup('references', artifact_ids=[citing])['results'][0]['target_id'], target)

    def test_scope_report_exclusion_authorization_and_stale_edges(self):
        target = self.paper('Consistency Based Learning for Incomplete Multimodal Views', ['Alex Lee'])
        citing = self.paper('Robust Graph Learning for Incomplete Multimodal Views', ['Sam Chen'], [
            ('references', 'Alex Lee. 2024. Consistency Based Learning for Incomplete Multimodal Views. Proceedings.')])
        self.paper('Generated Report from a Prior Agent Conversation', ['Invented Author'], kind='report')
        another = self.store.save_space({'name': 'Other'})['id']
        secret = self.paper('Private Source from a Different Research Space', ['Secret Person'], space=another)
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.lookup('authors', artifact_ids=[secret])
        self.assertEqual(self.lookup('authors', author='Invented Author')['total'], 0)
        self.assertEqual(self.lookup('references', artifact_ids=[citing])['total'], 1)
        with patch.object(self.library, 'authorized', side_effect=lambda p: p['id'] != target):
            self.assertEqual(self.lookup('references', artifact_ids=[citing])['total'], 0)
        self.assertEqual(self.lookup('references', artifact_ids=[citing])['total'], 1)
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE document_chunks SET content='Unrelated revised bibliography.' WHERE artifact_id=? AND section='references'", (citing,))
        self.assertEqual(self.lookup('references', artifact_ids=[citing])['total'], 0)

    def test_metadata_alone_does_not_create_authorship_and_pagination_is_explicit(self):
        for index in range(3):
            self.paper('Independent Research on Multiple Views ' + str(index), ['Alex Lee'])
        empty = self.paper('Metadata Mismatch in a Scientific Document', ['Fake Person'])
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE document_chunks SET content='Different Person, Another University' WHERE artifact_id=? AND ordinal=0", (empty,))
        result = self.lookup('authors', limit=1)
        self.assertEqual(result['total'], 4)
        self.assertEqual(result['next_offset'], 1)
        self.assertEqual(self.lookup('authors', author='Fake Person')['total'], 0)
        fake = self.paper('Graph Learning', ['Graph Learning'])
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE document_chunks SET content='Graph Learning\nActual Person\nGraph Learning University' WHERE artifact_id=? AND ordinal=0", (fake,))
        self.assertEqual(self.lookup('authors', author='Graph Learning')['total'], 0)
        for values in ({'limit': True}, {'offset': -1}, {'artifact_ids': 'bad'}):
            with self.assertRaises(ValueError):
                self.lookup('authors', **values)
        with self.assertRaises(ValueError):
            self.lookup('shared_references', artifact_ids=[empty, empty])

    def test_agent_can_lookup_then_read_original_without_network(self):
        item = self.paper('Evidence Grounded Author Lookup for Research Papers', ['Alex Lee'])
        inputs = []
        class Model:
            name = 'relationship-tool-fixture'
            def complete(model, messages, tools):
                inputs.append((messages, tools))
                if len(inputs) == 1:
                    return ModelDecision('tool_call', tool_name='lookup_papers', arguments={'mode': 'authors', 'artifact_ids': [item]})
                payload = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))['UNTRUSTED_TOOL_DATA']
                if len(inputs) == 2:
                    ref = payload['results'][0]['authors'][0]['evidence'][0]['ref_id']
                    return ModelDecision('tool_call', tool_name='read_evidence', arguments={'ref_id': ref, 'adjacent': 0})
                return ModelDecision('final', content='The paper lists Alex Lee as an author. [' + payload['evidence_id'] + ']')
        search = Mock()
        result = ResearchAgent(search, Model(), self.root / 'traces', retrieval=self.retriever,
            space_id=self.space, allow_external=False, max_tool_calls=3, max_rounds=5).run('Who wrote the paper?')
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.tool_calls, 2)
        search.search.assert_not_called()
        self.assertTrue(any(t['function']['name'] == 'lookup_papers' for t in inputs[0][1]))
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].provenance['artifact_id'], item)


    def test_publisher_pdf_without_extension_uses_existing_parser_and_counts_requests(self):
        url = 'https://example.org/article/view/123/456'
        fallback = Mock()
        fallback.read.return_value = ReadResponse(False, url, media_type='application/pdf', network_requests=2,
            error={'code': 'unsupported_media_type', 'message': 'application/pdf'})
        self.library.fetch = Mock(return_value=(b'%PDF-fixture', 'application/pdf', url, 'utf-8'))
        parsed = {'title': 'A Publisher Paper', 'authors': ['Alex Lee'], 'page_count': 1,
                  'pages': [(1, 'A Publisher Paper\nAlex Lee\nA sufficiently long original method description.')],
                  'warnings': [], 'boundary': 'Text layer only'}
        reader = MaterialReader(fallback, self.library, lambda *args: None)
        with patch('research_agent.materials.parse_pdf', return_value=parsed):
            response = reader.read(url)
        self.assertTrue(response.ok)
        self.assertEqual(response.network_requests, 3)
        self.library.fetch.assert_called_once_with(url, max_bytes=25 * 1024 * 1024)
        self.assertEqual(reader.records[0]['kind'], 'paper')
        fallback.read.return_value = ReadResponse(False, url, media_type='application/pdf', network_requests=1,
            error={'code': 'network_target_denied', 'message': 'Denied'})
        self.assertFalse(reader.read(url).ok)
        self.assertEqual(self.library.fetch.call_count, 1)

    def discovery_fixture(self):
        seed = self.paper('Seed Publication for Grounded Author Discovery', ['Alex Lee'])
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE artifacts SET metadata=? WHERE id=?', (json.dumps({'authors': ['Alex Lee'], 'doi': '10.1234/seed'}), seed))
        person = {'author': {'id': 'https://openalex.org/A123', 'display_name': 'Alex Lee'},
                  'institutions': [{'display_name': 'Example University'}]}
        seed_data = {'doi': 'https://doi.org/10.1234/seed', 'authorships': [person]}
        works = {'meta': {'count': 2}, 'results': [{'id': 'https://openalex.org/W456', 'display_name': 'A Candidate Paper',
            'publication_year': 2025, 'authorships': [person],
            'primary_location': {'landing_page_url': 'https://example.org/candidate'},
            'best_oa_location': {'pdf_url': 'http://127.0.0.1/private'}}]}
        def response(data):
            return (json.dumps(data).encode(), 'application/json', 'https://api.openalex.org/', 'utf-8')
        self.library.fetch = Mock(side_effect=[response(seed_data), response(works)])
        return seed, seed_data, works, response

    def test_author_discovery_binds_doi_id_filter_pagination_and_safe_urls(self):
        seed, _, _, _ = self.discovery_fixture()
        result = self.retriever.discover_author_papers(self.space, seed_artifact_id=seed, author='Alex Lee', query='multi-view', per_page=1)
        self.assertTrue(result['ok'])
        self.assertEqual(result['author_id'], 'A123')
        self.assertEqual(result['network_requests'], 2)
        self.assertEqual(result['next_page'], 2)
        self.assertEqual(result['results'][0]['urls'], ['https://example.org/candidate'])
        params = parse_qs(urlsplit(self.library.fetch.call_args_list[1].args[0]).query)
        self.assertEqual(params['filter'], ['authorships.author.id:A123'])
        self.assertEqual(params['search'], ['multi-view'])

    def test_discovery_rejects_unbound_ids_and_handles_provider_failure(self):
        seed, seed_data, _, response = self.discovery_fixture()
        self.library.fetch.side_effect = [response(seed_data)]
        bad = self.retriever.discover_author_papers(self.space, seed_artifact_id=seed, author='Alex Lee', author_id='A999')
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['network_requests'], 1)
        self.assertEqual(self.library.fetch.call_count, 1)
        self.library.fetch.side_effect = OSError('provider unavailable')
        failed = self.retriever.discover_author_papers(self.space, seed_artifact_id=seed, author='Alex Lee')
        self.assertFalse(failed['ok'])
        self.assertIn('ordinary search/read', failed['guidance'])
        before = self.library.fetch.call_count
        with self.assertRaises(ValueError):
            self.retriever.discover_author_papers(self.space, seed_artifact_id=seed, author='A. Lee')
        self.assertEqual(self.library.fetch.call_count, before)

    def test_discovery_returns_alternative_versions_and_excludes_seed(self):
        seed, seed_data, works, response = self.discovery_fixture()
        candidate = works['results'][0]
        candidate['locations'] = [
            {'pdf_url': 'https://example.org/public.pdf', 'landing_page_url': 'https://example.org/preprint',
             'version': 'submittedVersion', 'is_oa': True},
            {'pdf_url': 'http://127.0.0.1/private'},
            {'pdf_url': 'https://example.org/public.pdf'}]
        works['results'].append({**candidate, 'id': 'seed', 'doi': seed_data['doi']})
        self.library.fetch.side_effect = [response(seed_data), response(works)]
        result = self.retriever.discover_author_papers(self.space, seed_artifact_id=seed, author='Alex Lee')
        self.assertEqual(len(result['results']), 1)
        row = result['results'][0]
        self.assertEqual(row['urls'], ['https://example.org/public.pdf', 'https://example.org/candidate', 'https://example.org/preprint'])
        self.assertTrue(any(loc['version'] == 'submittedVersion' for loc in row['locations']))
        self.assertEqual({s['url'] for s in result['sources']}, set(row['urls']))
        self.assertEqual(row['identity_check'], 'provider_id_match_requires_original')

    def test_local_agent_never_exposes_or_executes_online_author_discovery(self):
        seed, _, _, _ = self.discovery_fixture()
        class Model:
            name = 'local-discovery-denial-fixture'
            def complete(model, messages, tools):
                self.assertNotIn('discover_author_papers', {t['function']['name'] for t in tools})
                return ModelDecision('tool_call', tool_name='discover_author_papers', arguments={'seed_artifact_id': seed, 'author': 'Alex Lee'})
        result = ResearchAgent(None, Model(), self.root / 'traces', retrieval=self.retriever,
            space_id=self.space, allow_external=False).run('Local authors only')
        self.assertEqual(result.termination, 'policy_denied')
        self.library.fetch.assert_not_called()

    def test_discovery_sources_can_be_read_and_network_calls_are_counted(self):
        seed, _, _, _ = self.discovery_fixture()
        calls = []
        class Model:
            name = 'discovery-read-fixture'
            def complete(model, messages, tools):
                calls.append(True)
                if len(calls) == 1:
                    return ModelDecision('tool_call', tool_name='discover_author_papers', arguments={'seed_artifact_id': seed, 'author': 'Alex Lee'})
                payload = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))['UNTRUSTED_TOOL_DATA']
                if len(calls) == 2:
                    return ModelDecision('tool_call', tool_name='read', url=payload['sources'][0]['url'])
                return ModelDecision('final', content='Alex Lee wrote this candidate paper. [E2]')
        reader = Mock()
        reader.read.return_value = ReadResponse(True, 'https://example.org/candidate',
            content='A Candidate Paper. Alex Lee, Example University. The authors describe their clustering method.', network_requests=1)
        result = ResearchAgent(Mock(), Model(), self.root / 'traces', reader=reader, retrieval=self.retriever,
            space_id=self.space, max_tool_calls=3, max_rounds=5).run('Find and read another paper by Alex Lee')
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.network_requests, 3)
        reader.read.assert_called_once_with('https://example.org/candidate')


if __name__ == '__main__':
    unittest.main()
