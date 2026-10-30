"""Text-first PDF extraction, bounded offline OCR and evidence-backed fields."""
import re
import unicodedata
from pathlib import Path
from threading import Lock
from models import ROOT, Document, Invoice

_ocr_engine = None
_ocr_lock = Lock()
NUMBER = r"-?\d[\d,]*(?:\.\d+)?"

def normalize(text):
    return unicodedata.normalize("NFKC", text).replace("⻩", "黄").replace("戶", "户")

def ocr_image(image):
    """Models are bundled. Runtime must never download models behind the user's back."""
    global _ocr_engine
    import numpy as np
    from PIL import ImageOps
    with _ocr_lock:
        if _ocr_engine is None:
            # Load onnxruntime before the OpenCV-based OCR package: OpenCV pulls the
            # system VC runtime early, which can shadow the newer one onnxruntime needs.
            try:
                import onnxruntime  # noqa: F401
            except ImportError:
                pass
            from rapidocr import RapidOCR
            model_dir = ROOT / "assets" / "ocr"
            models = {k: model_dir / v for k, v in {
                "Det.model_path": "det.onnx", "Rec.model_path": "rec.onnx", "Cls.model_path": "cls.onnx"
            }.items()}
            if not all(p.is_file() for p in models.values()):
                raise RuntimeError("OCR 模型不完整，请使用含 assets/ocr 的完整程序包")
            _ocr_engine = RapidOCR(params={**{k: str(v) for k, v in models.items()},
                "Global.text_score": 0.35, "Global.log_level": "error",
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1})
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((3000, 3000))
        output = _ocr_engine(np.asarray(image))
        if not output.txts:
            return "", 0.0
        # Preserve line/column order: same row tokens are joined before parsing a table.
        entries = sorted(zip(output.boxes, output.txts, output.scores), key=lambda x: float(x[0][:, 1].mean()))
        rows = []
        for box, text, score in entries:
            cy = float(box[:, 1].mean())
            height = max(8, float(box[:, 1].max()-box[:, 1].min()))
            if rows and abs(cy - rows[-1][0]) < height * .55:
                rows[-1][1].append((float(box[:, 0].min()), text))
            else:
                rows.append([cy, [(float(box[:, 0].min()), text)]])
        text = "\n".join(" ".join(t for _, t in sorted(row)) for _, row in rows)
        return normalize(text), min(float(s) for s in output.scores)

def parse_invoice(text, source, page=1, method="PDF文字层", confidence=1.0):
    text = normalize(text)
    compact = re.sub(r"\s+", "", text).replace("：", ":")
    invoice = Invoice(str(source), page)
    f = invoice.fields
    def take(key, pattern, content=compact, transform=None):
        match = re.search(pattern, content)
        if match:
            value = match.group(1).strip()
            f[key] = transform(value) if transform else value
            invoice.evidence[key] = f"{method}: {match.group(0)}"
    if "专用发票" in compact:
        invoice.kind = "电子专用发票" if "电子" in compact else "增值税专用发票"
    elif "普通发票" in compact:
        invoice.kind = "电子普通发票" if "电子" in compact else "增值税普通发票"
    # 证据只取票种本身：原来的 `.{0,8}…{0,4}` 会把版式噪声和下一个字段一起卷进来，
    # 报告里显示成“345电子发票(普通发票)开票日”，看起来像识别出错。
    bracketed = re.search(r"(?:增值税)?(?:电子)?发票[（(](?:普通|专用)发票[)）]", compact)
    plain = re.search(r"(?:增值税)?(?:电子)?(?:普通|专用)发票", compact)
    invoice.evidence["kind"] = f"{method}: " + ((bracketed or plain).group(0) if (bracketed or plain) else "类型未识别")
    take("invoice_number", r"发\s*票\s*号\s*码\s*[:：]?\s*([A-Za-z0-9_-]+)", text)
    take("invoice_code", r"发\s*票\s*代\s*码\s*[:：]?\s*([A-Za-z0-9_-]+)", text)
    dm = re.search(r"开票日期[:]?([0-9]{4})[年/.-](\d{1,2})[月/.-](\d{1,2})日?", compact)
    if dm:
        f["invoice_date"] = f"{dm[1]}-{int(dm[2]):02d}-{int(dm[3]):02d}"
        invoice.evidence["invoice_date"] = f"{method}: {dm[0]}"
    names = list(re.finditer(r"名称[:](.*?)(?=销售方信息|统一社会信用代码|纳税人识别号|名称:|项目名称|$)", compact))
    for key, m in zip(["buyer_name", "seller_name"], names[:2]):
        f[key] = m[1].strip()
        invoice.evidence[key] = f"{method}: {m[0]}"
    tax_ids = list(re.finditer(r"(?:统一社会信用代码/纳税人识别号|纳税人识别号|统一社会信用代码:|税号)[:]?([A-Za-z0-9]*)", compact))
    for key, m in zip(["buyer_tax_id", "seller_tax_id"], tax_ids[:2]):
        f[key] = m[1]
        invoice.evidence[key] = f"{method}: {m[0]}"
    # Sample and common invoices expose totals in one row.
    total = re.search(r"(?:^|\n)\s*合\s*计\s*[¥￥]?\s*("+NUMBER+r")\s*[¥￥]\s*("+NUMBER+r")", text)
    if not total:
        total = re.search(r"合计[¥￥]("+NUMBER+r")[¥￥]("+NUMBER+r")", compact)
    if total:
        for key, value in zip(["amount", "tax_amount"], total.groups()):
            f[key] = value.replace(",", "")
            invoice.evidence[key] = f"{method}: {total[0]}"
    take("total", r"小写[)）]?[¥￥:]?("+NUMBER+r")", transform=lambda x:x.replace(",", ""))
    take("uppercase_total", r"大写[)）:]?[^零壹贰叁肆伍陆柒捌玖拾佰仟万亿元圆角分整正]*([零壹贰叁肆伍陆柒捌玖拾佰仟万亿元圆角分整正]+)")
    # Labelled line items (provided fixtures and editable test invoices).
    pat = (r"(?<![\d.\-])(\*.*?)?\s*(-?\d+(?:\.\d+)?)\s*单\s*价\s*("+NUMBER+r")\s*[|｜]?\s*金\s*额\s*("+NUMBER+
           r")\s*[|｜]?\s*税\s*率\s*([\d.]+\s*%|免税|不征税)\s*[|｜]?\s*税\s*额\s*("+NUMBER+r")")
    for n, m in enumerate(re.finditer(pat, text, re.S)):
        label, qty, price, amount, rate, tax = m.groups()
        invoice.items.append(dict(name=re.sub(r"\s+"," ",label).strip() if label else f"明细{n+1}", quantity=qty,
            unit_price=price.replace(",", ""), amount=amount.replace(",", ""), tax_rate=rate.replace(" ", ""),
            tax_amount=tax.replace(",", "")))
    # Real tabular invoices: name, specification/unit, qty, price, amount, rate, tax.
    if not invoice.items:
        for line in text.splitlines():
            row = re.search(r"^(.*?)(-?\d+(?:\.\d+)?)\s+("+NUMBER+r")\s+("+NUMBER+r")\s+([\d.]+%|免税|不征税)\s+("+NUMBER+r")\s*$",line)
            if row and ("*" in row[1] or "商品" in row[1]):
                name,qty,price,amount,rate,tax=row.groups()
                invoice.items.append(dict(name=name.strip(),quantity=qty,unit_price=price,amount=amount,tax_rate=rate,tax_amount=tax))
    if invoice.items:
        rates = sorted(set(i["tax_rate"] for i in invoice.items))
        f["tax_rate"] = rates[0] if len(rates)==1 else "多税率"
        invoice.evidence["tax_rate"] = f"{method}: 明细税率 " + ", ".join(rates)
        # A single explicit line can supply the net/tax total when OCR misses currency glyphs.
        if len(invoice.items)==1:
            for key in ("amount", "tax_amount"):
                if key not in f:
                    f[key]=invoice.items[0][key]
                    invoice.evidence[key]=f"{method}: 唯一明细 {key}={f[key]}"
    if method == "OCR":
        invoice.uncertain = [key for key in ["invoice_number","invoice_date","buyer_name","buyer_tax_id",
            "seller_name","seller_tax_id","amount","tax_amount","total","uppercase_total"] if not f.get(key)]
        if confidence < .8:
            invoice.uncertain.extend(key for key in f if key not in invoice.uncertain)
            invoice.warnings.append("OCR 存在低置信度文字，请对照原票确认字段及明细")
    # Stable text missing a labelled tax ID is a real absence, not a fabricated value.
    for key in ("buyer_tax_id","seller_tax_id"):
        if key in f and not f[key] and method == "PDF文字层":
            invoice.confirmed.append(key)
    if not invoice.items:
        invoice.warnings.append("未自动识别明细，请对照原票补充或确认明细")
    return invoice

def parse_application(text):
    """Read the claimed amount and header fields out of a 报销申请 attachment.

    The form stays the source of truth: this only fills empty inputs, so that the
    "申报金额与发票合计一致" check can start from the uploaded application instead of
    asking the user to retype a number that is already printed on the document.
    Anything that cannot be read with confidence is left out rather than guessed.
    """
    try:
        raw = normalize(text)
        compact = re.sub(r"\s+", "", raw).replace("：", ":")
        fields, evidence = {}, {}

        amount = re.search(r"(?:申请金额|报销金额|申请报销金额|金额合计|合计金额|报销总额|申请总额)"
                           r"[:]?[¥￥]?(" + NUMBER + r")", compact)
        value = _amount(amount[1]) if amount else None
        if value is not None:
            fields["requested_amount"] = value
            evidence["requested_amount"] = f"申请单文字层: {amount[0]}"

        day = re.search(r"(?:申请日期|提交日期|报销日期|填报日期|填表日期)"
                        r"[:]?(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})", compact)
        if day:
            year, month, date_part = int(day[1]), int(day[2]), int(day[3])
            if 1 <= month <= 12 and 1 <= date_part <= 31:
                fields["submitted_on"] = f"{year:04d}-{month:02d}-{date_part:02d}"
                evidence["submitted_on"] = f"申请单文字层: {day[0]}"

        for key, pattern in (("applicant", r"申请人[:]?\s*([^\s，,、;；:：]{1,20})"),
                             ("department", r"(?:所属部门|部门)[:]?\s*([^\s，,、;；:：]{1,20})")):
            match = re.search(pattern, raw.replace("：", ":"))
            if match and not match[1].isdigit():
                fields[key] = match[1]
                evidence[key] = f"申请单文字层: {match[0]}"

        purpose = re.search(r"(?m)^\s*(?:事由|用途|报销事由|费用说明)[:]?\s*(.{1,60})$", raw)
        if purpose:
            fields["purpose"] = purpose[1].strip()
            evidence["purpose"] = f"申请单文字层: {purpose[0].strip()}"
        if not fields:
            return {}
        fields["evidence"] = evidence
        return fields
    except Exception:
        # A malformed application attachment must never block the invoice pipeline.
        return {}


def _amount(text):
    """Return a normalised non-negative amount string, or None when implausible."""
    from decimal import Decimal, InvalidOperation
    candidate = str(text).replace(",", "").strip()
    try:
        number = Decimal(candidate)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0 or number.adjusted() > 12:
        return None
    return str(number.quantize(Decimal("0.01")))


def extract_document(path):
    path = Path(path).resolve()
    document = Document(str(path))
    try:
        if path.suffix.lower() not in {".pdf",".png",".jpg",".jpeg"}:
            raise ValueError("只支持 PDF、PNG、JPG 文件")
        if path.stat().st_size > 60*1024*1024:
            raise ValueError("单个文件不能超过 60 MB，请拆分后导入")
        from PIL import Image
        pages = []
        if path.suffix.lower() == ".pdf":
            import pypdfium2 as pdf
            with pdf.PdfDocument(path) as doc:
                if len(doc)>50:
                    raise ValueError("单个 PDF 最多 50 页，请拆分后导入")
                document.pages = len(doc)
                for n in range(len(doc)):
                    page = doc[n]
                    tp=page.get_textpage()
                    try:
                        text = normalize(tp.get_text_bounded())
                    finally:
                        tp.close()
                    method, confidence = "PDF文字层", 1.0
                    if len(re.sub(r"\s", "", text)) < 50:
                        bitmap=page.render(scale=min(3.0,3000/max(page.get_size())))
                        image=bitmap.to_pil().copy()
                        bitmap.close()
                        text, confidence=ocr_image(image)
                        method="OCR"
                    pages.append((text, method, confidence))
                    page.close()
        else:
            with Image.open(path) as image:
                if image.width*image.height > 85_000_000:
                    raise ValueError("图片超过 8500 万像素，请缩小后导入")
                text, confidence=ocr_image(image)
                pages=[(text,"OCR",confidence)]
                document.pages=1
        document.text="\n\n".join(f"--- 第{n+1}页 {method} ---\n{text}" for n,(text,method,_) in enumerate(pages))
        for n,(text,method,confidence) in enumerate(pages):
            compact=re.sub(r"\s+", "", text)
            if "发票号码" in compact or "普通发票" in compact or "专用发票" in compact:
                invoice=parse_invoice(text,str(path),n+1,method,confidence)
                if len(re.findall(r"发票号码", compact)) > 1:
                    invoice.warnings.append("同一页出现多个发票号码，请先拆分为一页一票再审核")
                document.invoices.append(invoice)
        if document.invoices:
            document.category="发票"
            if len(document.invoices) != len(pages):
                document.invoices[0].warnings.append("文件有未识别为发票的页面，请拆分附件或确认全部页内容")
        elif any(s in document.text for s in ["付款凭证","银行回单","支付成功","转账凭证"]):
            document.category="付款凭证"
        elif "报销申请" in document.text:
            document.category="报销申请"
            document.application_fields=parse_application(document.text)
        if not any(text.strip() for text, _, _ in pages):
            raise ValueError("没有识别到文字，请提供清晰单据")
    except Exception as exc:
        document.error=f"{type(exc).__name__}: {exc}"
    return document
