import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from models import Application, Document, Policy, ROOT, FAIL
from extractor import parse_invoice, extract_document
from agent import run_audit

class IntegrationTests(unittest.TestCase):
    def application(self):
        directory=ROOT/'data'/'supplemental'
        return Application('TEST', '测试用户','测试部门','2026-10-15','113.00',
                           [Document(str(directory/n)) for n in ['正常普通发票.pdf','报销申请.pdf','付款凭证.pdf']])

    def test_local_end_to_end(self):
        app=self.application();run_audit(app,Policy())
        self.assertEqual(app.status,'预审通过')
        self.assertTrue(app.ai_summary)
        self.assertEqual(len(app.invoices),1)
        app.invoices[0].edit('total','999')
        run_audit(app,Policy())
        self.assertEqual(app.status,FAIL)
        loaded=Application.from_dict(json.loads(json.dumps(app.to_dict(),ensure_ascii=False)))
        self.assertEqual(loaded.invoices[0].edits,app.invoices[0].edits)

    def test_missing_key_downgrades(self):
        app=self.application()
        with patch('agent.call_model') as call:
            run_audit(app,Policy(),use_ai=True)
            call.assert_not_called()
        self.assertEqual(app.status,'预审通过');self.assertIn('未配置',app.mode)

    def test_timeout_does_not_leak_key(self):
        app=self.application();secret='PRIVATE_TEST_KEY'
        with patch('agent.call_model',side_effect=TimeoutError(secret)):
            run_audit(app,Policy(),secret,True)
        self.assertEqual(app.status,'预审通过');self.assertIn('降级',app.mode)
        self.assertNotIn(secret,json.dumps(app.to_dict()))

    def test_model_tools_really_dispatch(self):
        app=self.application()
        messages=[{'role':'assistant','content':None,'tool_calls':[{'id':str(n),'type':'function',
                    'function':{'name':name,'arguments':'{}'}}]}
                  for n,name in enumerate(['extract_documents','audit_rules','generate_report'])]
        with patch('agent.call_model',side_effect=messages) as call:
            run_audit(app,Policy(),'TEST_KEY',True)
        self.assertEqual(call.call_count,3)
        self.assertEqual(app.status,'预审通过');self.assertIn('在线',app.mode)
        for name in ['extract_documents','audit_rules','generate_report']:
            self.assertTrue(any('智能体调用工具：'+name in e for e in app.events))

    def test_untrusted_tool_and_bad_arguments_fall_back(self):
        for name,args in [('run_shell','{}'),('audit_rules','[]')]:
            app=self.application()
            msg={'role':'assistant','tool_calls':[{'id':'x','function':{'name':name,'arguments':args}}]}
            with patch('agent.call_model',return_value=msg):run_audit(app,Policy(),'key',True)
            self.assertEqual(app.status,'预审通过');self.assertIn('降级',app.mode)

    def test_malformed_identifiers_are_not_truncated(self):
        inv=parse_invoice('电子普通发票 发票代码:123456789012X 发票号码:12345678ABC', 'x.pdf')
        self.assertEqual(inv.fields['invoice_code'],'123456789012X')
        self.assertEqual(inv.fields['invoice_number'],'12345678ABC')

    def test_negative_quantity_and_wrapped_name(self):
        text='*测试服务*软件\n维护 次 -1 单价 100.00 | 金额 100.00 | 税率 13% | 税额 13.00'
        inv=parse_invoice(text,'x.pdf')
        self.assertEqual(inv.items[0]['quantity'],'-1')
        self.assertIn('维护',inv.items[0]['name'])

    def test_standalone_credit_code_labels(self):
        inv=parse_invoice('统一社会信用代码:ABC123 统一社会信用代码:XYZ456', 'x.pdf')
        self.assertEqual(inv.fields['buyer_tax_id'],'ABC123')
        self.assertEqual(inv.fields['seller_tax_id'],'XYZ456')

    def test_qr_is_not_invoice_number(self):
        inv=parse_invoice('发票号码:25112000000100109876\nQR\n开票日期:2026年02月25日','x.png')
        self.assertEqual(inv.fields['invoice_number'],'25112000000100109876')

    def test_corrupt_file_does_not_abort_batch(self):
        bad=extract_document(ROOT/'data'/'supplemental'/'损坏文件.pdf')
        good=extract_document(ROOT/'data'/'supplemental'/'正常普通发票.pdf')
        self.assertTrue(bad.error);self.assertEqual(len(good.invoices),1)

    def test_unsupported_and_missing_file(self):
        self.assertTrue(extract_document('not-a-real-file.txt').error)
        self.assertTrue(extract_document('not-a-real-file.pdf').error)

    def test_blank_ocr_page_is_error_not_header_text(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'blank.png'
            Image.new('RGB',(400,300),'white').save(path)
            with patch('extractor.ocr_image',return_value=('',0)):
                doc=extract_document(path)
        self.assertIn('没有识别到文字',doc.error)

if __name__=='__main__':unittest.main()
