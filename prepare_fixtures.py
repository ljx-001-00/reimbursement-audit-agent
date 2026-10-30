"""Generate conspicuously synthetic invoices and attachments for acceptance tests."""
from pathlib import Path
from decimal import Decimal
import json
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from models import ROOT, Policy
from rules import amount_in_words

def create_fixtures():
    folder=ROOT/'data'/'supplemental'
    folder.mkdir(parents=True,exist_ok=True)
    pdfmetrics.registerFont(TTFont('FixtureChinese',str(ROOT/'assets'/'font.ttf')))
    policy=Policy()
    def invoice_pdf(name, numbers, special=False, multiple=False):
        path=folder/name
        c=canvas.Canvas(str(path),pagesize=(842,595))
        for number in numbers:
            c.setFont('FixtureChinese',20)
            c.drawString(42,553,'测试数据 电子发票（'+('专用' if special else '普通')+'发票）')
            c.setFont('FixtureChinese',13)
            rows=[f'发票号码: {number}', '开票日期: 2026年10月01日',
                '购买方信息 名称: '+policy.company_name,
                '销售方信息 名称: 测试商品服务有限公司',
                '纳税人识别号: '+policy.company_tax_id,
                '纳税人识别号: 91110108TEST000001',
                '项目名称 数量 单价 金额 税率 税额',
                '*测试商品*打印纸 2 单价 50.00 | 金额 100.00 | 税率 13% | 税额 13.00']
            net,tax=Decimal('100'),Decimal('13')
            if multiple:
                rows.append('*测试服务*维护服务 1 单价 200.00 | 金额 200.00 | 税率 6% | 税额 12.00')
                net+=200;tax+=12
            gross=net+tax
            rows.extend([f'合计 ¥{net:.2f} ¥{tax:.2f}',
                f'价税合计(大写) {amount_in_words(gross)} (小写)¥{gross:.2f}',
                '仅供程序测试 不可作为真实票据报销'])
            for n,line in enumerate(rows): c.drawString(42,510-n*29,line)
            c.showPage()
        c.save()
        return path
    invoice_pdf('正常普通发票.pdf',['26100100000000000001'])
    invoice_pdf('专票多明细.pdf',['26100100000000000002'],special=True,multiple=True)
    invoice_pdf('多页两张发票.pdf',['26100100000000000003','26100100000000000004'])
    for name,title in [('报销申请.pdf','报销申请'),('付款凭证.pdf','付款凭证')]:
        c=canvas.Canvas(str(folder/name),pagesize=(595,420))
        c.setFont('FixtureChinese',24);c.drawString(40,360,'测试数据 '+title)
        c.setFont('FixtureChinese',15)
        c.drawString(40,305,'申请人: 测试用户  部门: 测试部门')
        c.drawString(40,260,'申请金额由程序表单填写，以表单本次金额为准。')
        c.drawString(40,215,'仅验证附件类别与完整性，不代表真实付款。')
        c.save()
    # 单独一份带申报金额的申请单：验证"从报销申请单读取申报金额并预填表单"，
    # 不接入 cases.json，避免同一份文件服务多个金额不同的用例。
    c=canvas.Canvas(str(folder/'报销申请_含金额.pdf'),pagesize=(595,420))
    c.setFont('FixtureChinese',24);c.drawString(40,360,'测试数据 报销申请')
    c.setFont('FixtureChinese',15)
    for n,line in enumerate(['申请人: 测试用户    部门: 测试部门',
                             '申请日期: 2026-10-15',
                             '申请金额: ¥325.00',
                             '事由: 软件维护服务费',
                             '仅供程序测试，不代表真实审批。']):
        c.drawString(40,305-n*42,line)
    c.save()
    import pypdfium2 as pdf
    from PIL import Image
    with pdf.PdfDocument(folder/'专票多明细.pdf') as document:
        page=document[0];bitmap=page.render(scale=2.5)
        image=bitmap.to_pil().copy();bitmap.close();page.close()
    image.save(folder/'专票图片.png')
    image.convert('RGB').save(folder/'专票图片.jpg',quality=92)
    image.convert('RGB').save(folder/'扫描专票.pdf','PDF',resolution=180)
    Image.new('RGB',(900,550),'white').save(folder/'空白图片.png')
    (folder/'损坏文件.pdf').write_bytes(b'%PDF-corrupt-test-fixture')
    cases=[
        {'id':'TEST-PASS','amount':'113.00','files':['正常普通发票.pdf','报销申请.pdf','付款凭证.pdf'],'expected':'预审通过'},
        {'id':'TEST-SPECIAL','amount':'325.00','files':['专票多明细.pdf','报销申请.pdf','付款凭证.pdf'],'expected':'预审通过'},
        {'id':'TEST-MULTI','amount':'438.00','files':['正常普通发票.pdf','专票多明细.pdf','报销申请.pdf','付款凭证.pdf'],'expected':'预审通过'},
        {'id':'TEST-PAGES','amount':'226.00','files':['多页两张发票.pdf','报销申请.pdf','付款凭证.pdf'],'expected':'预审通过'},
        {'id':'TEST-DIFFERENCE','amount':'100.00','files':['正常普通发票.pdf','报销申请.pdf','付款凭证.pdf'],'expected':'不通过'},
        {'id':'TEST-MISSING','amount':'113.00','files':['正常普通发票.pdf'],'expected':'不通过'},
        {'id':'TEST-SCAN','amount':'325.00','files':['扫描专票.pdf','报销申请.pdf','付款凭证.pdf'],'expected':'预审通过'},
        {'id':'TEST-JPG','amount':'325.00','files':['专票图片.jpg','报销申请.pdf','付款凭证.pdf'],'expected':'预审通过'},
    ]
    (folder/'cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'说明.txt').write_text('此目录全部为程序生成的测试数据，不是真实票据或真实付款证明。\n新增报销：测试用户/测试部门/2026-10-15，金额按cases.json填写。\n正常普通发票113元；专票多明细325元；多页两票226元；前两票合报438元。\n同时选择报销申请.pdf及付款凭证.pdf才能验证附件齐全。',encoding='utf-8')
    return cases

if __name__=='__main__':
    print('Created',len(create_fixtures()),'acceptance cases')
