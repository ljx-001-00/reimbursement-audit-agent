"""Shared, serializable data for extraction, audit, UI and reports."""
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
import sys

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PASS, FAIL, REVIEW, NA = "通过", "不通过", "待人工复核", "不适用"
FIELD_LABELS = {
    "kind": "发票类型", "invoice_code": "发票代码", "invoice_number": "发票号码",
    "invoice_date": "开票日期", "buyer_name": "购买方名称", "buyer_tax_id": "购买方税号",
    "seller_name": "销售方名称", "seller_tax_id": "销售方税号", "amount": "不含税金额",
    "tax_rate": "税率", "tax_amount": "税额", "total": "价税合计", "uppercase_total": "大写金额",
}
APPLICATION_LABELS = {"applicant": "申请人", "department": "部门",
                      "submitted_on": "提交日期", "requested_amount": "申请金额"}
APPLICATION_KEYS = ("applicant", "department", "submitted_on", "requested_amount")

# Two ready-made policy settings, so a reviewer can switch the audit baseline in one click.
# They only pre-fill max_days / required_attachments: rules.py never reads this table, so no
# check logic changes when the preset changes.
PRESETS = {
    "strict": {
        "label": "严格口径（公司制度）",
        "short": "严格口径",
        "max_days": 90,
        "required_attachments": ["发票", "报销申请", "付款凭证"],
        "note": "按赛题 2.1.1 三项校验的要求：发票、报销申请、付款凭证齐全，开票日起 90 天内提交。",
    },
    "reference": {
        "label": "演示口径（对齐参考结果单）",
        "short": "演示口径",
        "max_days": 180,
        "required_attachments": ["发票"],
        "note": "为与赛题《标准化报销审核结果单》逐笔比对而复算：只要求发票、期限放宽至 180 天。"
                "不改变任何校验逻辑，参考结果单把 156 天也标为通过，故按此口径才能对齐其结论。",
    },
}
CUSTOM_PRESET = "custom"


@dataclass
class Policy:
    company_name: str = "北京蓝海智能科技有限公司"
    company_tax_id: str = "91110108MA01XYZ7K9"
    max_days: int = 90
    required_attachments: list[str] = field(default_factory=lambda: ["发票", "报销申请", "付款凭证"])

    def preset_name(self):
        """Which preset this policy currently equals; custom when hand-edited."""
        for name, preset in PRESETS.items():
            if self.max_days == preset["max_days"] and list(self.required_attachments) == preset["required_attachments"]:
                return name
        return CUSTOM_PRESET

    def preset_label(self):
        name = self.preset_name()
        return PRESETS[name]["short"] if name in PRESETS else "自定义口径"

    def with_preset(self, name):
        """Return a copy of this policy with one preset's parameters applied."""
        if name not in PRESETS:
            raise ValueError(f"未知的审核口径：{name}")
        preset = PRESETS[name]
        return Policy(self.company_name, self.company_tax_id, preset["max_days"],
                      list(preset["required_attachments"]))

@dataclass
class Invoice:
    source: str
    page: int = 1
    kind: str = "未知"
    fields: dict[str, str] = field(default_factory=dict)
    items: list[dict[str, str]] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    uncertain: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)

    def edit(self, key, value):
        before = self.kind if key == "kind" else self.fields.get(key, "")
        if before != value:
            self.edits.append({"time": datetime.now().isoformat(timespec="seconds"),
                               "field": key, "before": before, "after": value})
        if key == "kind":
            self.kind = value
        else:
            self.fields[key] = value
        if key not in self.confirmed:
            self.confirmed.append(key)
        if key in self.uncertain:
            self.uncertain.remove(key)

@dataclass
class Document:
    path: str
    category: str = "待确认"
    invoices: list[Invoice] = field(default_factory=list)
    text: str = ""
    error: str = ""
    pages: int = 0
    ai_import: dict = field(default_factory=dict)
    # Fields parsed out of a 报销申请 attachment: amount / submitted_on / applicant / department.
    application_fields: dict = field(default_factory=dict)

@dataclass
class Check:
    code: str
    title: str
    status: str
    actual: str = ""
    expected: str = ""
    reason: str = ""
    source: str = "申请汇总"
    field: str = ""

@dataclass
class Application:
    id: str
    applicant: str = ""
    department: str = ""
    submitted_on: str = ""
    requested_amount: str = ""
    documents: list[Document] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    ai_summary: str = ""
    model_note: str = ""
    mode: str = "本地审核"

    @property
    def invoices(self):
        return [i for d in self.documents if d.category == "发票" for i in d.invoices]

    @property
    def status(self):
        states = [c.status for c in self.checks]
        return FAIL if FAIL in states else REVIEW if REVIEW in states or not states else "预审通过"

    def prefill_from_documents(self):
        """Fill blank application inputs from uploaded 报销申请 attachments.

        Only blanks are written, so anything the user typed wins. Every fill is appended to
        events, which keeps "where did this amount come from" answerable in the report.
        """
        filled = []
        for document in self.documents:
            if document.category != "报销申请":
                continue
            fields = document.application_fields or {}
            taken = []
            for key in APPLICATION_KEYS:
                value = str(fields.get(key, "") or "").strip()
                if not value:
                    continue
                current = str(getattr(self, key, "") or "").strip()
                # 新建报销把提交日期预填为今天，那只是占位，允许被申请单覆盖。
                placeholder = key == "submitted_on" and current == date.today().isoformat()
                if current and not placeholder:
                    continue
                setattr(self, key, value)
                taken.append(f"{APPLICATION_LABELS[key]}={value}")
            if taken:
                filled.extend(taken)
                self.events.append(
                    f"从报销申请单读取申报信息：{Path(document.path).name} · " + "、".join(taken))
        return filled

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data["documents"] = [Document(**{**d, "invoices": [Invoice(**i) for i in d.get("invoices", [])]})
                             for d in data.get("documents", [])]
        data["checks"] = [Check(**c) for c in data.get("checks", [])]
        return cls(**data)
