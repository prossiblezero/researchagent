"""V2 contracts: real PDF parsing, safe persistence, scoped evidence and queued workflows."""
import base64
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from research_agent.contracts import ModelDecision
from research_agent.library import Library, MaterialReader, verified_citations, citation_markdown
from research_agent.materials import atomic_write, fetch_public, identifier, parse_pdf, read_material, read_github, safe_path, split_pages
from research_agent.search import _NetworkResponse
from research_agent.trace import TraceWriter
from research_agent.workbench import Workbench, OfflineRouter
from research_agent.workbench_store import WorkbenchStore, Conflict, NotFound
from server import make_server


def pdf_bytes(blank=False):
    writer=PdfWriter()
    for lines in [('Abstract','We preserve useful experiences across tasks in an external memory.'),
                  ('2 Method','The method retrieves previous task feedback before taking an action.'),
                  ('3 Experiments','We evaluate memory retrieval on twenty held-out tasks.'),
                  ('4 Limitations','Scanned tables and equations require manual verification.')]:
        page=writer.add_blank_page(612,792)
        if not blank:
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
            stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 50 730 Td '+ ' 0 -24 Td '.join('('+line+') Tj' for line in lines)+' ET').encode())
            page[NameObject('/Contents')]=writer._add_object(stream)
    writer.add_metadata({'/Title':'Experience Memory','/Author':'Fixture Researcher'})
    output=io.BytesIO();writer.write(output);return output.getvalue()


class GroundedModel:
    name='fixture-grounded-model'
    def complete(self,messages,tools):
        data=json.loads(messages[-1]['content'])['UNTRUSTED_DOCUMENT_DATA']
        if 'materials' in data:
            result={'material_ids':[m['id'] for m in data['materials'][:1]],'terms':['memory','method','feedback']}
        elif 'facts' in messages[0]['content']:
            excerpt=data['excerpts'][0]
            result={'facts':[{'aspect':'研究动机','statement':'作者描述了跨任务的经验保存。','evidence':[{'id':excerpt['id'],'span':1}]}],
                    'inferences':['这一机制值得进一步做消融实验。'],'limitations':['未验证表格和公式。']}
        else:
            excerpt=data['excerpts'][0]
            result={'answer':'这份资料描述了相关机制。['+excerpt['id']+']','citations':[{'id':excerpt['id'],'span':1}],'insufficient':False}
        return ModelDecision('final',content=json.dumps(result,ensure_ascii=False))


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=WorkbenchStore(self.root/'state.db');self.a=self.store.save_space({'name':'Agent'})['id'];self.b=self.store.save_space({'name':'Other'})['id']
        self.ca=self.store.create_conversation(self.a,'Reading')['id'];self.lib=Library(self.store)

    def upload(self):
        item,_=self.lib.import_bytes(self.a,'memory.pdf',pdf_bytes())
        return self.lib.extract_import(self.a,item['id'],lambda *args:None)

    def test_real_pdf_pages_sections_and_scanned_boundary(self):
        result=parse_pdf(pdf_bytes());self.assertEqual(result['page_count'],4)
        chunks=split_pages(result['pages']);self.assertEqual([c['page'] for c in chunks],[1,2,3,4])
        self.assertEqual([c['section'] for c in chunks],['abstract','method','experiment','limitation'])
        self.assertIn('公式',result['boundary'])
        self.assertEqual(len(parse_pdf(pdf_bytes(True))['warnings']),4)
        with self.assertRaisesRegex(ValueError,'不是 PDF'):
            parse_pdf(b'<html>paywall</html>')

    def test_download_metadata_identifier_and_hash_dedup_do_not_overwrite(self):
        data=pdf_bytes()
        page=b'<meta name="citation_title" content="Experience Memory"><meta name="citation_publication_date" content="2024/01/01"><meta name="citation_doi" content="10.1234/memory"><meta name="citation_pdf_url" content="/paper.pdf">'
        def fetch(url,**kwargs):
            return (page,'text/html',url,'utf-8') if url.endswith('/article') else (data,'application/pdf',url,'utf-8')
        self.lib.fetch=fetch
        args=dict(job_id=None,topic='../topic',download=True,checkpoint=lambda *args:None)
        item,duplicate,_=self.lib.ingest(self.a,'https://example.org/article',**args)
        self.assertFalse(duplicate);self.assertEqual(item['canonical_id'],'doi:10.1234/memory');self.assertEqual(item['metadata']['year'],'2024')
        path=self.lib.file(self.a,item['id']);self.assertTrue(path.is_relative_to(Path(self.store.space(self.a)['download_root'])))
        again,duplicate,_=self.lib.ingest(self.a,'https://example.org/mirror.pdf',**args)
        self.assertTrue(duplicate);self.assertEqual(again['id'],item['id']);self.assertEqual(path.read_bytes(),data)
        self.assertEqual(len(list(path.parents[2].rglob('*.pdf'))),1)
        self.assertTrue((path.parent/'metadata.json').exists())
        self.assertEqual(identifier('https://arxiv.org/pdf/2401.12345v2.pdf'),'arxiv:2401.12345')
        with self.assertRaises(FileExistsError):
            atomic_write(path.parent,path,b'new')

    def test_public_network_redirect_type_limit_and_private_targets(self):
        with patch('research_agent.materials.resolve_public_addresses',return_value=[('public',)]) as resolver, patch('research_agent.materials._request_once') as request:
            request.return_value=_NetworkResponse(302,{'location':'http://127.0.0.1/admin'},b'',False)
            with self.assertRaises(ValueError):fetch_public('https://example.org/a')
            self.assertEqual(request.call_count,1)
            for response in [_NetworkResponse(200,{'content-type':'application/pdf'},b'%PDF-fake',True),_NetworkResponse(403,{},b'',False),_NetworkResponse(200,{'content-type':'application/exe'},b'x',False)]:
                request.return_value=response
                with self.assertRaises(ValueError):fetch_public('https://example.org/a')
            before=request.call_count
            for url in ('file:///etc/passwd','http://localhost/a','https://user:pass@example.org/a'):
                with self.assertRaises(ValueError):fetch_public(url)
            self.assertEqual(request.call_count,before)
        with patch('research_agent.materials.resolve_public_addresses',side_effect=ValueError('private DNS')):
            with self.assertRaises(ValueError):fetch_public('https://example.org/a')

    def test_arxiv_html_uses_observed_same_paper_pdf_link(self):
        calls=[]
        def fetch(url,**kwargs):
            calls.append(url)
            if '/html/' in url:
                return b'<a href="/pdf/2303.11366v4">PDF</a>','text/html',url,'utf-8'
            return pdf_bytes(),'application/pdf',url,'utf-8'
        result=read_material('https://arxiv.org/html/2303.11366',fetch)
        self.assertEqual(result['kind'],'paper')
        self.assertEqual(calls[-1],'https://arxiv.org/pdf/2303.11366v4')

    def test_original_paths_scope_and_changed_files_are_not_cited(self):
        item=self.upload()
        with self.assertRaises(NotFound):self.lib.get(self.b,item['id'])
        with self.assertRaises(NotFound):self.lib.chunks(self.b,item['id'])
        outside=self.root/'outside.txt';outside.write_text('secret')
        with self.assertRaises(PermissionError):safe_path(self.root/'authorized',outside)
        with closing(self.store._connect()) as db,db:
            db.execute('UPDATE artifacts SET original_path=? WHERE id=?',(str(outside),item['id']))
        self.assertFalse(self.lib.authorized(self.lib.get(self.a,item['id'])))
        with self.assertRaises(PermissionError):self.lib.file(self.a,item['id'])

    def test_analysis_and_local_qa_have_exact_quotes_and_file_page_links(self):
        item=self.upload();model=GroundedModel()
        item=self.lib.analyze(self.a,item['id'],model,lambda *args:None)
        self.assertEqual(item['analysis_status'],'complete',item['analysis'])
        self.assertIn('Agent 推断',item['analysis']);self.assertIn('第 1 页',item['analysis'])
        job=self.store.enqueue(self.a,self.ca,'本地论文中的 memory method 是什么','local',[],kind='LOCAL_QA')
        with patch.object(self.lib,'fetch',side_effect=AssertionError('local QA cannot use network')):
            answer=self.lib.answer(job,model,lambda *args:None)
        self.assertIn('[L',answer);self.assertIn('local-L',answer);self.assertIn(item['original_path'],answer)
        with closing(self.store._connect()) as db:
            self.assertGreater(db.execute('SELECT count(*) FROM citations WHERE job_id=?',(job['id'],)).fetchone()[0],0)
        chunks=self.lib.chunks(self.a,item['id'])
        for refs in ([{'id':'L999999','quote':'fabricated evidence'}],[{'id':'L'+str(chunks[0]['id']),'quote':'invented quote content'}]):
            with self.assertRaises(ValueError):verified_citations(refs,chunks)

    def test_empty_library_does_not_call_model_and_reports_are_scoped(self):
        job=self.store.enqueue(self.b,self.store.create_conversation(self.b,'Empty')['id'],'本地资料','local',[],kind='LOCAL_QA')
        answer=self.lib.answer(job,Mock(complete=Mock(side_effect=AssertionError('no evidence'))),lambda *args:None)
        self.assertIn('没有可读取',answer)
        item=self.upload();self.assertEqual(self.lib.list(self.b),[])
        self.store.save_space({'download_root':str(self.root/'new-root')},self.a)
        self.assertFalse(self.lib.authorized(item))

    def test_bad_quotes_are_corrected_once_and_persistent_fabrication_fails(self):
        item=self.upload(); events=[]
        grounded=GroundedModel()
        class CorrectingModel:
            def __init__(self, persistent=False):
                self.calls=0; self.persistent=persistent
            def complete(inner,messages,tools):
                inner.calls+=1
                decision=grounded.complete(messages,tools)
                value=json.loads(decision.content)
                if 'facts' in value and (inner.calls==1 or inner.persistent):
                    value['facts'][0]['evidence'][0]={'id':value['facts'][0]['evidence'][0]['id'],'quote':'This is an invented sentence without evidence.'}
                return ModelDecision('final',content=json.dumps(value))
        model=CorrectingModel()
        result=self.lib.analyze(self.a,item['id'],model,lambda *args:events.append(args))
        self.assertEqual(result['analysis_status'],'complete');self.assertEqual(model.calls,2)
        self.assertTrue(any(e[0]=='correcting' for e in events))
        with closing(self.store._connect()) as db,db:
            db.execute("UPDATE artifacts SET analysis_status='pending' WHERE id=?",(item['id'],))
        model=CorrectingModel(True)
        result=self.lib.analyze(self.a,item['id'],model,lambda *args:None)
        self.assertEqual(result['analysis_status'],'failed');self.assertEqual(model.calls,2)
        self.assertNotIn('原文依据与作者表述',result['analysis'])

    def test_verbatim_span_preserves_ordinary_pdf_text(self):
        chunks=[{'id':7,'content':'Two basic requirements to process PDF documents are text and images.'}]
        cited=verified_citations([{'id':'L7','span':1}],chunks)
        self.assertEqual(cited[0][1],chunks[0]['content'])
        for span in (0,999,True,'1'):
            with self.assertRaises(ValueError):verified_citations([{'id':'L7','span':span}],chunks)

    def test_multiple_quotes_from_one_chunk_survive_note_rendering(self):
        item=self.upload();chunk=self.lib.chunks(self.a,item['id'])[0]
        note=citation_markdown([(chunk,'First supported sentence.'),(chunk,'Another supported sentence.'),(chunk,'First supported sentence.')])
        self.assertEqual(note.count('First supported sentence.'),1)
        self.assertIn('Another supported sentence.',note)

    def test_queue_routes_retry_and_cancel_preserve_kind_and_payload(self):
        app=Workbench(self.store,OfflineRouter,Mock(),self.root/'traces',start_worker=False);self.addCleanup(app.close)
        job=app.send(self.a,self.ca,'本地论文的方法是什么')['job']
        self.assertEqual(job['kind'],'LOCAL_QA');app.execute(self.store.claim_next())
        # Local QA now enters retrieval even with no documents: prior-session history may answer.
        # This routing-only model cannot perform retrieval and must not report a successful study.
        self.assertEqual(self.store.job(self.a,job['id'])['status'],'failed')
        task=app.add_material(self.a,{'conversation_id':self.ca,'url':'https://example.org/p.pdf','download':True})
        with self.assertRaises(Conflict):self.store.save_space({'download_root':str(self.root/'elsewhere')},self.a)
        self.store.cancel(self.a,task['id']);retry=app.retry(self.a,task['id'])
        self.assertEqual(retry['kind'],'MATERIAL');self.assertEqual(json.loads(retry['payload'])['url'],'https://example.org/p.pdf')
        self.store.cancel(self.a,retry['id']);self.assertIsNone(self.store.claim_next())

    def test_background_material_task_and_failure_recovery(self):
        app=Workbench(self.store,GroundedModel,Mock(),self.root/'traces',start_worker=False);self.addCleanup(app.close)
        app.library.fetch=lambda url,**kwargs:(pdf_bytes(),'application/pdf',url,'utf-8')
        job=app.add_material(self.a,{'conversation_id':self.ca,'url':'https://example.org/p.pdf','download':True})
        app.execute(self.store.claim_next());done=self.store.job(self.a,job['id'])
        self.assertEqual(done['status'],'completed',done['error']);self.assertIn('原文依据',done['summary'])
        events=[json.loads(line) for line in Path(done['trace_path']).read_text(encoding='utf-8').splitlines()]
        self.assertIn('extracting',[x.get('stage') for x in events])
        app.library.fetch=Mock(side_effect=ValueError('HTTP 403'))
        failed=app.add_material(self.a,{'conversation_id':self.ca,'url':'https://example.org/fail.pdf','download':True})
        app.execute(self.store.claim_next());self.assertEqual(self.store.job(self.a,failed['id'])['status'],'failed')
        self.assertEqual(len(app.library.list(self.a)),1)

    def test_cancel_during_download_stops_before_archive_or_analysis(self):
        app=Workbench(self.store,GroundedModel,Mock(),self.root/'traces',start_worker=False);self.addCleanup(app.close)
        job=app.add_material(self.a,{'conversation_id':self.ca,'url':'https://example.org/cancel.pdf','download':True})
        def fetch(url,**kwargs):
            self.store.cancel(self.a,job['id'])
            return pdf_bytes(),'application/pdf',url,'utf-8'
        app.library.fetch=fetch;app.execute(self.store.claim_next())
        self.assertEqual(self.store.job(self.a,job['id'])['status'],'cancelled')
        self.assertEqual(app.library.list(self.a),[])
        self.assertEqual(list(self.root.rglob('原文.pdf')),[])

    def test_auto_collection_enforces_pdf_and_count_budget_and_records_reason(self):
        self.store.save_space({'auto_download':True,'download_count':1,'download_mb':1},self.a)
        app=Workbench(self.store,GroundedModel,Mock(),self.root/'traces',start_worker=False);self.addCleanup(app.close)
        job=self.store.enqueue(self.a,self.ca,'memory','brief',[]);self.store.claim_next()
        trace_path=self.root/'traces'/'research.jsonl'
        with closing(TraceWriter(trace_path,'research')) as trace:trace.emit('run_finished')
        result=SimpleNamespace(answer='Existing answer',trace_path=str(trace_path),sources=[SimpleNamespace(source_id='S1',title='Memory paper',url='https://example.org/p.pdf',snippet='memory method')])
        class Selector(GroundedModel):
            def complete(self,messages,tools):
                if '"papers"' in messages[0]['content']:
                    return ModelDecision('final',content=json.dumps({'papers':[{'source_id':'S1','category':'representative','reason':'经验复用机制对应研究问题'}]}))
                return super().complete(messages,tools)
        app.library.fetch=lambda url,**kwargs:(pdf_bytes(),'application/pdf',url,'utf-8')
        app._collect_papers(job,result,Selector())
        items=app.library.list(self.a);self.assertEqual(len(items),1);self.assertEqual(items[0]['metadata']['selection_category'],'representative')
        self.assertIn('本轮资料归档',result.answer)
        self.assertGreater(len(trace_path.read_text(encoding='utf-8').splitlines()),1)
        with patch.object(app.library,'fetch',return_value=(b'<html><p>web only</p></html>','text/html','https://example.org/blog','utf-8')):
            with self.assertRaisesRegex(ValueError,'未提供'):
                app.library.ingest(self.a,'https://example.org/blog',job_id=job['id'],topic='test',download=True,checkpoint=lambda *a:None,require_paper=True)
        self.assertEqual(len(app.library.list(self.a)),1)

    def test_analysis_retry_updates_note_without_changing_original(self):
        item=self.upload();original=self.lib.file(self.a,item['id']);content=original.read_bytes()
        broken=Mock(complete=Mock(side_effect=ValueError('provider error')))
        result=self.lib.analyze(self.a,item['id'],broken,lambda *a:None)
        self.assertEqual(result['analysis_status'],'failed');self.assertFalse((original.parent/'报告.md').exists())
        result=self.lib.analyze(self.a,item['id'],GroundedModel(),lambda *a:None)
        self.assertEqual(result['analysis_status'],'complete');self.assertTrue((original.parent/'报告.md').exists())
        self.assertEqual(json.loads((original.parent/'metadata.json').read_text(encoding='utf-8'))['analysis_status'],'complete')
        self.assertEqual(original.read_bytes(),content)

    def test_github_reads_pinned_files_without_executing_or_cloning(self):
        requests=[]
        def fetch(url,**kwargs):
            requests.append(url)
            if '/commits/' in url:data={'sha':'a'*40}
            elif '/git/trees/' in url:data={'tree':[{'type':'blob','path':'README.md','size':100},{'type':'blob','path':'main.py','size':100},{'type':'blob','path':'requirements.txt','size':100}],'truncated':False}
            elif '/contents/' in url:data={'type':'file','encoding':'base64','content':base64.b64encode(b'Read-only fixture; do not execute this file.').decode()}
            else:data={'default_branch':'main','description':'fixture'}
            return json.dumps(data).encode(),'application/json',url,'utf-8'
        result=read_github('https://github.com/owner/repo',fetch)
        self.assertEqual(result['metadata']['commit'],'a'*40);self.assertIsNone(result['data'])
        self.assertIn('requirements.txt',result['metadata']['files_read'])
        contents=[url for url in requests if '/contents/' in url]
        self.assertEqual(len(contents),3);self.assertTrue(all(url.endswith('?ref='+'a'*40) for url in contents))

    def test_additive_migration_and_append_trace(self):
        task=self.store.enqueue(self.a,self.ca,'old','brief',[])
        with closing(self.store._connect()) as db,db:
            db.execute('ALTER TABLE research_jobs DROP COLUMN kind');db.execute('ALTER TABLE research_jobs DROP COLUMN payload')
        migrated=WorkbenchStore(self.store.path);self.assertEqual(migrated.job(self.a,task['id'])['kind'],'RESEARCH')
        self.assertEqual(migrated.space(self.a)['auto_download'],0)
        path=self.root/'events.jsonl'
        with closing(TraceWriter(path,'run')) as trace:trace.emit('research')
        with closing(TraceWriter(path,'run',append=True)) as trace:trace.emit('download')
        self.assertEqual([json.loads(s)['seq'] for s in path.read_text().splitlines()],[1,2])


class V2HTTPTests(unittest.TestCase):
    def test_upload_readers_and_cross_space_origin_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);server=make_server(port=0,db_path=root/'state.db',trace_dir=root/'traces',start_worker=False,model_factory=GroundedModel)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            address=f'http://127.0.0.1:{server.server_port}'
            store=server.app.store;a=store.save_space({'name':'A'})['id'];b=store.save_space({'name':'B'})['id'];c=store.create_conversation(a,'C')['id']
            route='/api/spaces/'+a+'/materials/upload?'+urlencode({'filename':'memory.pdf','conversation_id':c})
            def request(path,data=None,origin=address):
                return urlopen(Request(address+path,data=data,headers={'Origin':origin,'Content-Type':'application/octet-stream'}),timeout=10)
            try:
                with self.assertRaises(HTTPError) as error:request(route,pdf_bytes(),'https://evil.example')
                self.assertEqual(error.exception.code,403)
                with request(route,pdf_bytes()) as response:uploaded=json.load(response)
                server.app.execute(store.claim_next());item_id=uploaded['item']['id']
                prefix=f'/api/spaces/{a}/materials/{item_id}'
                with request(prefix+'/reader') as response:page=response.read().decode()
                self.assertIn('local-L',page);self.assertIn('原文依据',page)
                with request(prefix+'/original') as response:self.assertEqual(response.read(),pdf_bytes())
                with self.assertRaises(HTTPError) as error:request(prefix.replace(a,b)+'/reader')
                self.assertEqual(error.exception.code,404)
                with request(f'/api/spaces/{a}/jobs/'+uploaded['job']['id']+'/report.html') as response:self.assertIn(b'<article>',response.read())
            finally:
                server.shutdown();server.server_close();worker.join(2)


if __name__=='__main__':unittest.main()
