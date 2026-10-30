"""Load application metadata only; no expected results are consumed at runtime."""
import json
from pathlib import Path
from models import ROOT, Application, Document

def load_demo():
    data=json.loads((ROOT/'data'/'demo.json').read_text(encoding='utf-8'))
    apps=[]
    for row in data:
        filename=row.pop('file')
        app=Application(**row, documents=[Document(str(ROOT/'data'/'invoices'/filename))])
        app.events.append('演示申请信息来自参考结果单基本信息；发票字段与结论均重新识别计算。原始附件未提供，按缺失处理。')
        apps.append(app)
    return apps
