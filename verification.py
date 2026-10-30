"""Repeatable end-to-end acceptance; also runnable inside the frozen executable."""
from datetime import datetime
from pathlib import Path
import json
import sys
from unittest.mock import patch
from models import ROOT, Application, Document, Policy
from demo import load_demo
from agent import run_audit
from extractor import extract_document
from report import export_pdf

# Independently inspected source invoice values. These are assertions, never audit inputs.
EXPECTED = [
    ('25112000000100012345','10000.00',{'date','attachments'}),
    ('25112000000100023456','3390.00',{'date','attachments'}),
    ('25112000000100034567','15000.00',{'required','buyer_name','buyer_tax_id','date','attachments'}),
    ('25112000000100045678','8000.00',{'date','uppercase','attachments'}),
    ('25112000000100056789','6300.00',{'date','tax','attachments'}),
    ('25112000000100067890','12500.00',{'date','total','attachments'}),
    ('24062000000100078901','5000.00',{'date','attachments'}),
    ('25112000000100089012','20000.00',{'required','seller','attachments'}),
    ('251120013579','4000.00',{'invoice_number','date','attachments'}),
    ('25112000000100109876','3180.00',{'quantity','attachments'}),
]

def run_verification(output_dir):
    output=Path(output_dir).resolve();output.mkdir(parents=True,exist_ok=True)
    evidence={'time':datetime.now().isoformat(timespec='seconds'), 'frozen':bool(getattr(sys,'frozen',False)),
              'python':sys.version.split()[0], 'tests':[], 'online_real':'未执行：需要用户本机配置免费 API 密钥',
              'notes':['参考结果单中的90天判定存在矛盾，按真实日期重算。',
                       '原10笔均无报销申请/付款凭证，默认演示制度下如实判缺附件。',
                       '模型工具流程及异常回退使用模拟响应测试，不等同于真实API联通。']}
    def record(name, function):
        try:
            detail=function()
            evidence['tests'].append({'name':name,'passed':True,'detail':detail})
        except Exception as exc:
            evidence['tests'].append({'name':name,'passed':False,'detail':f'{type(exc).__name__}: {exc}'})
    apps=load_demo()
    for app,(number,total,codes) in zip(apps,EXPECTED):
        def check_sample(app=app,number=number,total=total,codes=codes):
            run_audit(app,Policy())
            assert not any(d.error for d in app.documents),[d.error for d in app.documents]
            assert len(app.invoices)==1
            inv=app.invoices[0]
            assert inv.fields.get('invoice_number')==number,inv.fields
            assert inv.fields.get('total')==total,inv.fields
            failures={c.code for c in app.checks if c.status=='不通过'}
            pending=[c.title for c in app.checks if c.status=='待人工复核']
            assert failures==codes,(failures,codes)
            assert not pending,pending
            return {'status':app.status,'failures':sorted(failures),'fields':inv.fields}
        record('原始样例 '+app.id,check_sample)
    # 参考口径复算：只换 max_days 与必需附件，规则代码一行未改，用于证明
    # 与赛题参考结果单的 2 通过 / 8 不通过可以逐笔对上。
    reference_policy=Policy().with_preset('reference')
    def reference_recompute():
        statuses={}
        for source in apps:
            copy=Application.from_dict(source.to_dict())
            run_audit(copy,reference_policy)
            statuses[copy.id]=copy.status
        expected={'BX-2026-0427-001':'预审通过','BX-2026-0427-002':'预审通过'}
        wrong={key:value for key,value in statuses.items() if value!=expected.get(key,'不通过')}
        assert not wrong,wrong
        return {'policy':reference_policy.preset_label(),'max_days':reference_policy.max_days,
                'required_attachments':reference_policy.required_attachments,
                'reference_sheet':'通过 2 笔 / 不通过 8 笔','recomputed':statuses,
                'note':'规则逻辑未改，仅切换口径参数；结论与参考结果单逐笔一致。'}
    record('参考口径复算（对齐参考结果单）',reference_recompute)
    def application_prefill():
        document=extract_document(ROOT/'data'/'supplemental'/'报销申请_含金额.pdf')
        assert document.category=='报销申请',document.category
        fields=document.application_fields
        assert fields.get('requested_amount')=='325.00',fields
        assert fields.get('submitted_on')=='2026-10-15',fields
        assert fields.get('applicant')=='测试用户',fields
        blank=Application('TEST-PREFILL',documents=[document])
        filled=blank.prefill_from_documents()
        assert blank.requested_amount=='325.00' and blank.submitted_on=='2026-10-15',blank.to_dict()
        assert filled and '报销申请' in blank.events[-1],blank.events[-1:]
        typed=Application('TEST-KEEP','手工填写人','手工部门','2026-10-16','999.00',[document])
        typed.prefill_from_documents()
        assert (typed.applicant,typed.requested_amount)==('手工填写人','999.00'),'表单已有值不应被覆盖'
        return {'file':'报销申请_含金额.pdf','extracted':{k:v for k,v in fields.items() if k!='evidence'},
                'filled_into_blank_form':filled,'typed_values_kept':True}
    record('报销申请单申报信息抽取与预填',application_prefill)
    supplemental=[]
    cases=json.loads((ROOT/'data/supplemental/cases.json').read_text(encoding='utf-8'))
    for case in cases:
        app=Application(case['id'],'测试用户','测试部门','2026-10-15',case['amount'],
            [Document(str(ROOT/'data/supplemental'/n)) for n in case['files']])
        supplemental.append(app)
        def check_case(app=app,case=case):
            run_audit(app,Policy())
            assert app.status==case['expected'],[(c.title,c.status,c.actual) for c in app.checks if c.status!='通过']
            return {'status':app.status,'invoices':len(app.invoices),'files':case['files']}
        record('补充场景 '+case['id'],check_case)
    def reliability():
        corrupt=extract_document(ROOT/'data/supplemental/损坏文件.pdf')
        blank=extract_document(ROOT/'data/supplemental/空白图片.png')
        valid=extract_document(ROOT/'data/supplemental/正常普通发票.pdf')
        assert corrupt.error and blank.error and len(valid.invoices)==1
        return '损坏/空白单据记录错误，同批后续有效单据仍能识别；原大图已通过OCR场景。'
    record('坏文件与空白图片隔离',reliability)
    def online_mock():
        app=Application.from_dict(supplemental[0].to_dict())
        messages=[{'role':'assistant','tool_calls':[{'id':str(n),'type':'function',
            'function':{'name':name,'arguments':'{}'}}]}
            for n,name in enumerate(['extract_documents','audit_rules','generate_report'])]
        with patch('agent.call_model',side_effect=messages):
            run_audit(app,Policy(),'MOCK_TEST_SECRET',True)
        assert '在线' in app.mode and app.status=='预审通过'
        assert all(any('智能体调用工具：'+name in e for e in app.events)
                   for name in ['extract_documents','audit_rules','generate_report'])
        assert 'MOCK_TEST_SECRET' not in json.dumps(app.to_dict())
        return '三项工具实际分派成功；仅模拟模型响应，无真实网络请求。'
    record('在线工具流程（模拟）',online_mock)
    def fallback():
        app=Application.from_dict(supplemental[0].to_dict())
        with patch('agent.call_model',side_effect=TimeoutError('secret')):
            run_audit(app,Policy(),'MOCK_TEST_SECRET',True)
        assert app.status=='预审通过' and '降级' in app.mode
        run_audit(app,Policy(),use_ai=True)
        assert app.status=='预审通过' and '未配置' in app.mode
        return '服务超时、无密钥均完成本地审核；未请求付费模型。'
    record('联网异常与无密钥回退',fallback)
    def pdf_exports():
        demo_path=output/'原始10笔审核报告.pdf'
        extra_path=output/'补充场景审核报告.pdf'
        export_pdf(apps,Policy(),str(demo_path))
        export_pdf(supplemental,Policy(),str(extra_path))
        import pypdfium2 as pdf
        result={}
        for path,batch in [(demo_path,apps),(extra_path,supplemental)]:
            with pdf.PdfDocument(path) as document:
                texts=[]
                for page in document:
                    tp=page.get_textpage()
                    try:texts.append(tp.get_text_bounded())
                    finally:tp.close();page.close()
                text=''.join(texts)
                for app in batch:assert app.id in text,(app.id,path)
                for heading in ['基本信息','发票识别','规则校验表','问题清单','结论与处理建议','人工复核']:
                    assert heading in text,heading
                result[path.name]={'pages':len(document),'applications':len(batch),'bytes':path.stat().st_size}
        return result
    record('报告导出与内容完整性',pdf_exports)
    snapshot={'version':1,'policy':Policy().__dict__,'applications':[a.to_dict() for a in apps]}
    (output/'原始10笔审核项目.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    evidence['passed']=sum(t['passed'] for t in evidence['tests'])
    evidence['total']=len(evidence['tests'])
    evidence['all_passed']=evidence['passed']==evidence['total']
    path=output/'验收结果.json';path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    if not evidence['all_passed']:
        raise AssertionError('验收未全部通过，请查看 '+str(path))
    return f"验收通过 {evidence['passed']}/{evidence['total']}；结果：{path}；真实API联通尚待用户密钥验证。"

if __name__=='__main__':
    print(run_verification(sys.argv[1] if len(sys.argv)>1 else ROOT/'output'))
