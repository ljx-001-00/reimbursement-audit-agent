"""Single-window desktop UI. Tk widgets are accessed only on the main thread."""
import json
import os
import ctypes
import re
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from models import Application, Invoice, Policy, FIELD_LABELS, PASS, FAIL, REVIEW, PRESETS, ROOT
from glacier import BG, INK, MUTED, BLUE, LINE, PAPER, FIELD, NAV, install_glass_buttons
from home_style import install_home_styles
from motion import OffsetMotion
from model_settings import (DEFAULT_BASE_URL, DEFAULT_LOCAL_BASE_URL, LOCAL_SERVICE_HINT,
                            load_profiles, save_profiles, model_endpoint, validate_model,
                            is_local_endpoint, provider_kind, probe_local_service)
from assistant_ui import AssistantBall


CATEGORIES = ("发票", "报销申请", "付款凭证", "其他", "待确认")
ITEM_LABELS = {"name": "项目名称", "quantity": "数量", "unit_price": "单价", "amount": "金额", "tax_rate": "税率", "tax_amount": "税额"}


class ReimbursementApp(tk.Tk):
    def __init__(self):
        if os.name == "nt":
            ctypes.windll.user32.SetProcessDPIAware()
        super().__init__()
        self.title("报销审核")
        try:
            icon = ROOT / "assets" / "app.ico"
            if icon.is_file():
                self.iconbitmap(str(icon))
        except Exception:
            pass
        scale = max(1, self.winfo_fpixels("1i") / 96)
        width = min(int(1150 * scale), self.winfo_screenwidth() - 80)
        height = min(int(800 * scale), self.winfo_screenheight() - 100)
        self.geometry(f"{width}x{height}+30+30")
        self.minsize(min(int(1060 * scale), width), min(int(750 * scale), height))
        self.configure(bg=BG)
        self.apps = []
        self.policy = Policy()
        self.current_id = None
        self._empty_draft = None
        self.project_path = None
        self.busy = False
        self._ai_import_running = False
        self.loading = False
        self.fields_dirty = False
        self.home_visible = True
        self.motion = OffsetMotion(self)
        self._view_key = None
        self.edit_invoice = None
        self.preview_window = None
        self.invoice_choices = []
        self.items_buffer = []
        self.queue = Queue()
        self.controls = []
        self.smoke_failure = None
        self.use_ai = tk.BooleanVar(value=False)
        self._model_menu = None
        self._menu_focus_token = None
        self._chip_hover = False
        self.model_profiles, self.active_model_profile = load_profiles()
        selected_model = next(item for item in self.model_profiles if item["name"] == self.active_model_profile)
        # A profile saved by the user wins; environment variables only seed the very first run.
        fresh_install = not self._profiles_exist()
        env_base = os.environ.get("REIMBURSEMENT_BASE_URL", "").strip()
        base_url = selected_model["base_url"]
        model_name = selected_model["model"]
        if fresh_install and env_base:
            base_url = env_base.rstrip("/")
            model_name = os.environ.get("REIMBURSEMENT_MODEL", selected_model["model"]).strip() or selected_model["model"]
        self.model_base_url = tk.StringVar(value=base_url)
        self.model_name = tk.StringVar(value=model_name)
        legacy_key = os.environ.get("GLM_API_KEY", "") if base_url.rstrip("/") == DEFAULT_BASE_URL else ""
        environment_key = os.environ.get("REIMBURSEMENT_API_KEY", "")
        same_environment_endpoint = env_base.rstrip("/") == base_url.rstrip("/")
        self.api_key = tk.StringVar(value=environment_key if same_environment_endpoint else legacy_key)
        self.home_mode_text = tk.StringVar(value="原票本地识别，修改与审核记录可追溯。")
        self.result_mode_text = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="就绪 · 本地识别与规则审核无需密钥")
        self._style()
        self._build()
        # 校验能手: a ball docked inside this window; it never floats over the desktop.
        self.assistant = AssistantBall(self, chat_config=self._chat_config,
                                       surface=BG if self.home_visible else PAPER)
        for widget, _state in self.controls:
            widget.bind("<KeyPress>", lambda _event: setattr(self.motion, "pointer", False), add="+")
        self.bind_all("<ButtonPress-1>", lambda _event: setattr(self.motion, "pointer", True), add="+")
        self.bind_all("<KeyPress>", lambda _event: setattr(self.motion, "pointer", False), add="+")
        self.bind("<Unmap>", lambda event: self.motion.stop() if event.widget is self else None, add="+")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._poll_job = self.after(100, self._poll)

    @property
    def current(self):
        return next((app for app in self.apps if app.id == self.current_id), None)

    @staticmethod
    def _profiles_exist():
        try:
            import model_settings
            return model_settings.profile_path().exists()
        except OSError:
            return False

    def _style(self):
        from tkinter import font
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            font.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
        self.option_clear()
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10), background=BG, foreground=INK,
                        bordercolor=LINE, lightcolor=PAPER, darkcolor=PAPER, focuscolor=BG)
        style.configure("TFrame", background=PAPER)
        style.configure("Shell.TFrame", background=BG)
        style.configure("TLabel", background=PAPER, foreground=INK)
        style.configure("Muted.TLabel", background=PAPER, foreground=MUTED)
        style.configure("Shell.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 20, "bold"))
        self.button_images = install_glass_buttons(self, style)
        self.home_images, self.home_icons = install_home_styles(self, style)
        style.configure("Home.Header.TFrame", background=PAPER)
        style.configure("Home.Header.TLabel", background=PAPER, foreground=INK)
        # 首页的文字按钮只有两种底色：卡片内是 PAPER、页面底是 BG。底色不匹配时 ttk 会在
        # 卡片外露出一块白底，所以按底色各注册一份，而不是让两处共用一个 style。
        for name, background in (("Link", PAPER), ("Text", BG)):
            style.configure(f"Home.{name}.TButton", background=background, foreground=BLUE,
                            font=("Microsoft YaHei UI", -12), padding=(4, 3), width=0)
            style.layout(f"Home.{name}.TButton", [("Button.padding", {"sticky": "nsew", "children": [
                ("Button.focus", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
            style.map(f"Home.{name}.TButton", foreground=[("disabled", MUTED), ("active", "#0062D5")],
                      background=[("disabled", background), ("pressed", background), ("active", background)])
        style.configure("Accent.Nav.TButton", foreground=BLUE)
        style.configure("Current.Nav.TButton", foreground=BLUE, font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Shell.TButton", background=BG)
        style.map("Shell.TButton", background=[("disabled", BG), ("pressed", BG), ("active", BG)])
        style.configure("TEntry", padding=9, fieldbackground=FIELD, foreground=INK,
                        borderwidth=1, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.map("TEntry", bordercolor=[("focus", BLUE)])
        style.configure("Error.TEntry", fieldbackground="#FFF2F2", foreground="#D70015")
        style.configure("TCombobox", padding=8, fieldbackground=FIELD, background=PAPER, arrowcolor=MUTED,
                        bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], background=[("readonly", FIELD)])
        style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=39, background=PAPER,
                        fieldbackground=PAPER, foreground=INK, borderwidth=0, relief="flat", bordercolor=PAPER)
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.map("Treeview", background=[("selected", "#E7F0FC")], foreground=[("selected", INK)])
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"), background=FIELD,
                        foreground=MUTED, padding=(9, 8), relief="flat", borderwidth=0, bordercolor=FIELD)
        style.layout("Treeview.Heading", [("Treeheading.cell", {"sticky": "nswe"}),
                     ("Treeheading.padding", {"sticky": "nswe", "children": [("Treeheading.text", {"sticky": "we"})]})])
        style.configure("Sidebar.Treeview", background=BG, fieldbackground=BG, rowheight=53, bordercolor=BG)
        style.map("Sidebar.Treeview", background=[("selected", PAPER)], foreground=[("selected", INK)])
        style.configure("Flow.TNotebook", background=PAPER, borderwidth=0, bordercolor=PAPER, tabmargins=0)
        style.layout("Flow.TNotebook.Tab", [])
        style.configure("TCheckbutton", background=PAPER, foreground=INK)
        style.map("TCheckbutton", background=[("active", PAPER)])
        style.configure("TSeparator", background=LINE)
        style.configure("Vertical.TScrollbar", background="#C4D4E3", troughcolor=PAPER, borderwidth=0,
                        arrowcolor=MUTED, bordercolor=PAPER)
        style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#E7ECF3", borderwidth=0,
                        bordercolor=BG, lightcolor=BLUE, darkcolor=BLUE, thickness=6)

    def _button(self, parent, text, command, primary=False, **grid):
        button = ttk.Button(parent, text=text, command=command,
                            style="Primary.TButton" if primary else "TButton")
        button.grid(**grid)
        self.controls.append((button, "normal"))
        return button

    def _entry(self, parent, variable, width=30, **grid):
        entry = ttk.Entry(parent, textvariable=variable, width=width, font=("Microsoft YaHei UI", 10))
        entry.grid(**grid)
        self.controls.append((entry, "normal"))
        return entry

    def _build(self):
        self.top_nav = ttk.Frame(self, style="Shell.TFrame", padding=(24, 16))
        self.top_nav.pack(fill="x", padx=18, pady=(10, 6))
        brand = self.brand = ttk.Frame(self.top_nav, style="Shell.TFrame")
        brand.pack(side="left")
        self.brand_title = ttk.Label(brand, text="报销审核", style="Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        self.brand_title.grid(row=0, column=0, sticky="w")
        self.brand_subtitle = ttk.Label(brand, text="本地工作台", style="Shell.TLabel", font=("Microsoft YaHei UI", 9))
        self.brand_subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.nav_buttons = {}
        for label, command in [
            ("主页", self.show_home),
            ("新建报销", self.new_application), ("打开项目", self.open_project),
            ("保存项目", self.save_project), ("审核规则", self.policy_dialog), ("高级设置", self.advanced_dialog),
        ]:
            button = ttk.Button(self.top_nav, text=label, command=command, style="Nav.TButton")
            self.controls.append((button, "normal"))
            self.nav_buttons[label] = button
        self.nav_buttons["新建报销"].configure(style="Accent.Nav.TButton")
        # Narrow windows fold the secondary entries here instead of pushing the
        # backend chip off the right edge.
        self.nav_collapsed = ("保存项目", "审核规则", "高级设置")
        self.nav_overflow = ttk.Button(self.top_nav, text="更多", command=self._nav_overflow_menu,
                                       style="Nav.TButton")
        self.controls.append((self.nav_overflow, "normal"))
        # Persistent chip: shows which model backend is active and opens the quick switcher.
        # It is packed first so a shrinking window always keeps it visible.
        self.model_chip = tk.Canvas(self.top_nav, height=36, bg=BG, highlightthickness=0,
                                    bd=0, cursor="hand2")
        self.model_chip.bind("<Button-1>", lambda _event: self.model_switch_popup())
        self.model_chip.bind("<Enter>", lambda _event: self._set_chip_hover(True))
        self.model_chip.bind("<Leave>", lambda _event: self._set_chip_hover(False))
        self._nav_active = True
        self._nav_density = "full"
        self._nav_applied = None
        self._layout_nav()
        self.top_nav.bind("<Configure>", self._nav_resized)
        shell = ttk.Frame(self, style="Shell.TFrame", padding=(22, 13, 26, 16))
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)
        sidebar = ttk.Frame(shell, style="Shell.TFrame", padding=(5, 12, 5, 0))
        self.sidebar = sidebar
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 25))
        sidebar.configure(width=210)
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(2, weight=1)
        ttk.Label(sidebar, text="报销任务", style="Shell.TLabel", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.count_label = ttk.Label(sidebar, text="共 0 笔", style="Shell.TLabel", font=("Microsoft YaHei UI", 9))
        self.count_label.grid(row=1, column=0, sticky="w", pady=(0, 10))
        self.app_tree = ttk.Treeview(sidebar, columns=("name", "status"), show="", selectmode="browse", height=12, style="Sidebar.Treeview")
        self.app_tree.column("name", width=110, minwidth=85, stretch=True)
        self.app_tree.column("status", width=80, minwidth=65, stretch=False)
        self.app_tree.grid(row=2, column=0, sticky="nsew")
        self.app_tree.tag_configure("fail", foreground="#BC2635")
        self.app_tree.tag_configure("review", foreground="#9A6700")
        self.app_tree.tag_configure("pass", foreground="#1A6B45")
        self.app_tree.bind("<<TreeviewSelect>>", self._select_app)
        right = ttk.Frame(shell, style="Shell.TFrame")
        self.work_area = right
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)
        self.title_label = ttk.Label(right, text="报销工作台", style="Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        self.title_label.grid(row=0, column=0, sticky="w", pady=(0, 7))
        self.summary_label = ttk.Label(right, text="让每一笔报销，清楚有据。", style="Shell.TLabel")
        self.summary_label.grid(row=1, column=0, sticky="w", pady=(0, 15))
        self.flow_nav = ttk.Frame(right, style="Shell.TFrame", padding=(0, 4))
        self.flow_nav.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.step_buttons = []
        for index, label in enumerate(("①  导入材料", "②  确认信息", "③  查看审核", "④  导出报告")):
            self.flow_nav.columnconfigure(index, weight=1, uniform="steps")
            button = ttk.Button(self.flow_nav, text=label, style="Step.TButton", command=lambda i=index: self.show_step(i))
            button.grid(row=0, column=index, padx=(0 if index == 0 else 5, 0 if index == 3 else 5), sticky="ew")
            self.step_buttons.append(button)
            self.controls.append((button, "normal"))
        self.workspace_card = ttk.Frame(right, style="Paper.TFrame", padding=25)
        self.workspace_card.grid(row=3, column=0, sticky="nsew")
        self.notebook = ttk.Notebook(self.workspace_card, style="Flow.TNotebook")
        self.notebook.place(x=0, y=0, relwidth=1, relheight=1)
        self.info_tab = ttk.Frame(self.notebook, padding=5)
        self.docs_tab = ttk.Frame(self.notebook, padding=5)
        self.results_tab = ttk.Frame(self.notebook, padding=5)
        self.log_tab = ttk.Frame(self.notebook, padding=5)
        for tab, label in [(self.info_tab, "导入材料"), (self.docs_tab, "确认信息"),
                           (self.results_tab, "查看审核"), (self.log_tab, "导出报告")]:
            self.notebook.add(tab, text=label)
        self._build_info()
        self._build_documents()
        self._build_results()
        self._build_logs()
        self.flow_actions = ttk.Frame(right, style="Shell.TFrame", padding=(0, 15, 0, 0))
        self.flow_actions.grid(row=4, column=0, sticky="ew")
        self.flow_actions.columnconfigure(1, weight=1)
        self.back_button = self._button(self.flow_actions, "上一步", self.previous_step, row=0, column=0)
        self.back_button.configure(style="Shell.TButton")
        self.flow_primary = self._button(self.flow_actions, "下一步：确认信息", self.next_step, primary=True, row=0, column=2, sticky="e")
        self._add_primary_arrow()
        self._build_home(shell)
        footer = self.footer = ttk.Frame(self, style="Shell.TFrame", padding=(22, 0, 22, 13))
        footer.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=110)
        self.status_dot = ttk.Label(footer, text="●", foreground="#30C759", style="Home.Header.TLabel", font=("Microsoft YaHei UI", -10))
        self.status_label = ttk.Label(footer, textvariable=self.status_text, style="Shell.TLabel", font=("Microsoft YaHei UI", 9))
        self.status_dot.pack(side="left", padx=(0, 7))
        self.status_label.pack(side="left")
        shell.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _event: self._update_flow())
        self._update_flow()

    def _build_home(self, shell):
        from tkinter import font
        self.welcome = ttk.Frame(shell, style="Shell.TFrame")
        self.welcome.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.home_content = ttk.Frame(self.welcome, style="Shell.TFrame")
        self.home_content.place(relx=.5, rely=.5, anchor="center", width=620)
        self.home_content.columnconfigure(0, weight=1)
        self.home_fonts = {name: font.Font(self, family="Microsoft YaHei UI", size=-size, weight=weight)
                           for name, size, weight in (("heading", 32, "bold"), ("subtitle", 16, "normal"),
                               ("card", 18, "bold"), ("detail", 13, "normal"), ("note", 13, "normal"))}
        ttk.Label(self.home_content, image=self.home_icons["document"], style="Shell.TLabel").grid(row=0, column=0, pady=(0, 8))
        self.home_heading = ttk.Label(self.home_content, text="开始一项报销审核", style="Title.TLabel",
                                      font=self.home_fonts["heading"], anchor="center")
        self.home_heading.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(self.home_content, text="新建审核任务，或继续处理已有报销。", style="Shell.TLabel",
                  font=self.home_fonts["subtitle"], anchor="center").grid(row=2, column=0, sticky="ew", pady=(0, 18))
        self.home_cards = ttk.Frame(self.home_content, style="Shell.TFrame", height=264)
        self.home_cards.grid(row=3, column=0, sticky="ew")
        self.home_new_card = ttk.Frame(self.home_cards, style="Home.New.TFrame")
        self.home_continue_card = ttk.Frame(self.home_cards, style="Home.Continue.TFrame")
        self.home_new_card.place(relx=0, y=0, relwidth=.5, width=-8, relheight=1)
        self.home_continue_card.place(relx=.5, x=8, y=0, relwidth=.5, width=-8, relheight=1)
        for card, kind, title, style in (
                (self.home_new_card, "new", "新建报销", "Home.New.TLabel"),
                (self.home_continue_card, "continue", "继续处理报销", "Home.Continue.TLabel")):
            ttk.Label(card, image=self.home_icons[kind], style=style).place(relx=.5, rely=.21, anchor="center")
            ttk.Label(card, text=title, style=style, font=self.home_fonts["card"]).place(relx=.5, rely=.41, anchor="center")
        self.home_new_detail = ttk.Label(self.home_new_card, text="填写申请信息，添加发票与附件\n（PDF / PNG / JPG）。",
            style="Home.New.TLabel", foreground=MUTED, font=self.home_fonts["detail"], justify="center", anchor="center")
        self.home_new_detail.place(relx=.5, rely=.55, anchor="center")
        self.home_continue_detail = ttk.Label(self.home_continue_card, text="打开已有项目，继续识别、修改与审核。",
            style="Home.Continue.TLabel", foreground=MUTED, font=self.home_fonts["detail"], justify="center", anchor="center")
        self.home_continue_detail.place(relx=.5, rely=.55, anchor="center")
        self.home_new_button = ttk.Button(self.home_new_card, text="开始", command=self.new_application, style="Home.Primary.TButton")
        self.home_new_button.place(relx=.5, rely=.75, anchor="center")
        self.home_continue_button = ttk.Button(self.home_continue_card, text="打开", command=self.continue_home, style="Home.Secondary.TButton")
        self.home_continue_button.place(relx=.5, rely=.75, anchor="center")
        self.home_open_button = ttk.Button(self.home_continue_card, text="打开其他项目", command=self.open_project, style="Home.Link.TButton")
        for button in (self.home_new_button, self.home_continue_button, self.home_open_button):
            self.controls.append((button, "normal"))
        # 10 笔样例就是本题的验收场景，但入口原本只藏在"申请信息"页的按钮行里——评委双击
        # exe 后首屏看不到，等于把最能说明问题的那条路径藏起来。首屏给一条文字入口。
        self.home_demo_button = ttk.Button(self.home_content, text="没有现成材料？载入 10 笔演示样例（含赛题参考结果）",
                                           command=self.load_demo_from_home, style="Home.Text.TButton")
        self.home_demo_button.grid(row=4, column=0, sticky="ew", pady=(16, 0))
        self.controls.append((self.home_demo_button, "normal"))
        self.home_mode_label = ttk.Label(self.home_content, textvariable=self.home_mode_text, style="Shell.TLabel",
                  font=self.home_fonts["note"], anchor="center", cursor="hand2")
        self.home_mode_label.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.home_mode_label.bind("<Button-1>", lambda _event: self.model_switch_popup())
        self.home_mode_label.bind("<Enter>", lambda _event: self.home_mode_label.configure(foreground=BLUE))
        self.home_mode_label.bind("<Leave>", lambda _event: self.home_mode_label.configure(foreground=MUTED))
        self.welcome.bind("<Configure>", self._layout_home)
        self.welcome.bind("<Map>", lambda _event: self.motion.move(self.home_content, start=8))
        self.welcome.bind("<Unmap>", lambda _event: self.motion.stop(self.home_content, finish=0))
        for card in (self.home_new_card, self.home_continue_card):
            for widget in (card, *card.winfo_children()):
                widget.bind("<Enter>", lambda event, panel=card: self._hover_home_card(panel, event), add="+")
                widget.bind("<Leave>", lambda event, panel=card: self._hover_home_card(panel, event), add="+")

    def _layout_home(self, event):
        factor = min(1.4, max(.9, event.height / 700))
        width = min(max(620, round(620 * factor)), max(1, event.width - 64))
        self.home_content.place_configure(width=width)
        self.home_cards.configure(height=max(260, round(264 * factor)))
        for name, size in (("heading", 32), ("subtitle", 16), ("card", 18), ("detail", 13), ("note", 13)):
            self.home_fonts[name].configure(size=-max(size, round(size * factor)))
        for label in (self.home_new_detail, self.home_continue_detail):
            label.configure(wraplength=max(160, width // 2 - 44))

    def _hover_home_card(self, card, event):
        if self.busy or event.state & 0x100 or not self.home_visible:
            return
        x, y = self.winfo_pointerxy()
        inside = (card.winfo_rootx() <= x < card.winfo_rootx() + card.winfo_width() and
                  card.winfo_rooty() <= y < card.winfo_rooty() + card.winfo_height())
        self.motion.move(card, -2 if inside else 0, duration=120)

    def _add_primary_arrow(self):
        from PIL import Image, ImageDraw, ImageTk
        self.arrow_images = []
        for color in (INK, "#9DAFBE"):
            image = Image.new("RGBA", (156, 102))
            draw = ImageDraw.Draw(image)
            draw.ellipse((54, 1, 154, 101), fill=color)
            draw.line((95, 32, 115, 51, 95, 70), fill="white", width=6, joint="curve")
            self.arrow_images.append(ImageTk.PhotoImage(image.resize((52, 34), Image.Resampling.LANCZOS), master=self))
        self.flow_primary.configure(image=(self.arrow_images[0], "disabled", self.arrow_images[1]), compound="right")

    @staticmethod
    def _scroll_panel(parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        canvas = tk.Canvas(parent, background=PAPER, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scroll.set)
        content = ttk.Frame(canvas)
        content.columnconfigure(0, weight=1)
        window = canvas.create_window(0, 0, window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        def scroll_wheel(event):
            if event.delta:
                canvas.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
            return "break"
        def bind_wheel(widget):
            if widget.winfo_class() in ("Treeview", "Text", "TCombobox"):
                return
            widget.bind("<MouseWheel>", scroll_wheel)
            for child in widget.winfo_children():
                bind_wheel(child)
        canvas.bind("<MouseWheel>", scroll_wheel)
        content.bind("<Map>", lambda _event: bind_wheel(content))
        return canvas, content

    def _build_info(self):
        self.info_canvas, content = self._scroll_panel(self.info_tab)
        ttk.Label(content, text="申请信息", font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 17))
        form = ttk.Frame(content)
        form.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)
        self.info_vars, self.info_entries = {}, {}
        for index, (key, label) in enumerate([("applicant", "申请人"), ("department", "所属部门"),
                                             ("submitted_on", "提交日期"), ("requested_amount", "申请金额（元）")]):
            row, col = (index // 2) * 2, index % 2
            ttk.Label(form, text=label, style="Muted.TLabel").grid(row=row, column=col, sticky="w", pady=(4, 7), padx=(0, 20))
            variable = tk.StringVar()
            self.info_vars[key] = variable
            self.info_entries[key] = self._entry(form, variable, 28, row=row+1, column=col, sticky="ew", padx=(0, 25), pady=(0, 16))
            variable.trace_add("write", lambda *_args, field=key: self._info_changed(field))
        ttk.Label(content, text="日期示例 2026-10-15 · 金额以元填写 · 新建时提交日期取今天，补录历史票据请改成实际提交日期",
                  style="Muted.TLabel", font=("Microsoft YaHei UI", 9)).grid(row=2, column=0, sticky="w", pady=(0, 23))
        ttk.Separator(content).grid(row=3, column=0, sticky="ew", pady=(0, 20))
        material_title = ttk.Frame(content)
        material_title.grid(row=4, column=0, sticky="ew", pady=(0, 13))
        material_title.columnconfigure(0, weight=1)
        ttk.Label(material_title, text="报销材料", font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, sticky="w")
        self.ai_import_button = self._button(material_title, "AI 一键导入", lambda: self.import_documents(use_ai=True), primary=True, row=0, column=1, padx=(0, 8))
        self._button(material_title, "普通导入", self.import_documents, row=0, column=2, padx=(0, 8))
        self._button(material_title, "载入演示", self.load_demo, row=0, column=3)
        self.doc_tree = ttk.Treeview(content, columns=("file", "category", "pages", "result"), show="headings", height=4, selectmode="browse")
        for key, label, width in [("file", "文件", 230), ("category", "类别", 110), ("pages", "页数", 65), ("result", "识别情况", 240)]:
            self.doc_tree.heading(key, text=label)
            self.doc_tree.column(key, width=width, minwidth=50, stretch=key in ("file", "result"))
        self.doc_tree.grid(row=5, column=0, sticky="ew")
        self.doc_tree.bind("<<TreeviewSelect>>", self._select_document)
        self.doc_tree.bind("<Double-1>", lambda _event: self.preview_original())
        self.doc_tree.tag_configure("fail", foreground="#D70015")
        actions = ttk.Frame(content)
        actions.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        self._button(actions, "预览原图", self.preview_original, row=0, column=0, padx=(0, 8))
        self._button(actions, "外部打开", self.open_original, row=0, column=1, padx=(0, 8))
        self._button(actions, "移除", self.remove_document, row=0, column=2, padx=(0, 16))
        ttk.Label(actions, text="材料类别", style="Muted.TLabel").grid(row=0, column=3, padx=(0, 8))
        self.category_var = tk.StringVar(value="待确认")
        self.category_combo = ttk.Combobox(actions, textvariable=self.category_var, values=CATEGORIES, width=9, state="readonly", font=("Microsoft YaHei UI", 10))
        self.category_combo.grid(row=0, column=4)
        self.category_combo.bind("<<ComboboxSelected>>", self.change_category)
        self.controls.append((self.category_combo, "readonly"))
        ttk.Label(content, text="支持 PDF、PNG、JPG，可同时添加发票、报销申请和付款凭证。", style="Muted.TLabel", font=("Microsoft YaHei UI", 9), wraplength=760).grid(row=7, column=0, sticky="w", pady=(17, 0))
        self.ai_import_panel = ttk.LabelFrame(content, text="AI 导入工作台", padding=16)
        self.ai_import_panel.grid(row=8, column=0, sticky="ew", pady=(20, 0))
        self.ai_import_panel.columnconfigure(0, weight=1)
        header = ttk.Frame(self.ai_import_panel)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.ai_import_state = tk.StringVar(value="等待导入 · 普通导入不会调用大模型")
        self.ai_import_status_label = ttk.Label(header, textvariable=self.ai_import_state, foreground=BLUE, wraplength=470)
        self.ai_import_status_label.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.ai_existing_button = self._button(header, "AI 整理已有材料", self.organize_existing_documents, row=0, column=1)
        ttk.Label(self.ai_import_panel, text="读取文字 → 大模型整理 → 核对原文依据 → 人工确认\n所选材料的识别文字将发送至高级设置中的模型；原票和已确认字段保留。", style="Muted.TLabel", wraplength=730, justify="left").grid(row=1, column=0, sticky="w", pady=(10, 12))
        ai_fields_frame = ttk.Frame(self.ai_import_panel)
        ai_fields_frame.grid(row=2, column=0, sticky="ew")
        ai_fields_frame.columnconfigure(0, weight=1)
        self.ai_import_tree = ttk.Treeview(ai_fields_frame, columns=("field", "original", "suggested", "action"), show="headings", height=4, selectmode="browse")
        for key, label, width in (("field", "字段", 135), ("original", "本地识别值", 170), ("suggested", "AI 建议值", 170), ("action", "处理状态", 175)):
            self.ai_import_tree.heading(key, text=label)
            self.ai_import_tree.column(key, width=width, minwidth=85, stretch=True)
        self.ai_import_tree.grid(row=0, column=0, sticky="ew")
        ai_fields_scroll = ttk.Scrollbar(ai_fields_frame, orient="vertical", command=self.ai_import_tree.yview)
        ai_fields_scroll.grid(row=0, column=1, sticky="ns")
        self.ai_import_tree.configure(yscrollcommand=ai_fields_scroll.set)
        self.ai_import_tree.tag_configure("review", foreground="#9A6700")
        self.ai_import_tree.bind("<<TreeviewSelect>>", self._show_ai_import_detail)
        self.ai_import_detail = tk.Text(self.ai_import_panel, height=4, wrap="word", bg=FIELD, fg=INK, font=("Microsoft YaHei UI", 10), relief="flat", padx=10, pady=8, state="disabled")
        self.ai_import_detail.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(self.ai_import_panel, text="实际执行记录", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 5))
        self.ai_import_log = tk.Text(self.ai_import_panel, height=4, wrap="word", bg=FIELD, fg=MUTED, font=("Microsoft YaHei UI", 9), relief="flat", padx=10, pady=8, state="disabled")
        self.ai_import_log.grid(row=5, column=0, sticky="ew")
        self._ai_import_rows = []
        self.policy_summary = ttk.Label(content, text="", style="Muted.TLabel")
        self._refresh_policy_summary()

    def _build_documents(self):
        self.document_canvas, content = self._scroll_panel(self.docs_tab)
        ttk.Label(content, text="确认识别信息", font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(content, text="对照原件核对字段。修改会保留记录，并要求重新审核。", style="Muted.TLabel", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, sticky="w", pady=(0, 13))
        selector = ttk.Frame(content)
        selector.grid(row=2, column=0, sticky="ew", pady=(0, 15))
        selector.columnconfigure(0, weight=1)
        self.invoice_combo = ttk.Combobox(selector, state="readonly", font=("Microsoft YaHei UI", 10))
        self.invoice_combo.grid(row=0, column=0, sticky="ew")
        self.invoice_combo.bind("<<ComboboxSelected>>", self._choose_invoice)
        self.controls.append((self.invoice_combo, "readonly"))
        self._button(selector, "预览原图", lambda: self.preview_original(from_invoice=True), row=0, column=1, padx=(8, 0))
        self._button(selector, "查看原文与依据", self.show_evidence, row=0, column=2, padx=(4, 0))
        fields = ttk.Frame(content)
        fields.grid(row=3, column=0, sticky="ew")
        fields.columnconfigure(1, weight=1)
        fields.columnconfigure(3, weight=1)
        self.field_vars, self.field_entries = {}, {}
        for index, (key, label) in enumerate(FIELD_LABELS.items()):
            row, col = index // 2, (index % 2) * 2
            ttk.Label(fields, text=label, style="Muted.TLabel").grid(row=row, column=col, sticky="w", padx=(0 if col == 0 else 22, 10), pady=4)
            variable = tk.StringVar()
            entry = tk.Entry(fields, textvariable=variable, width=23, relief="flat", borderwidth=0,
                             font=("Microsoft YaHei UI", 10), highlightthickness=1, highlightbackground=LINE, highlightcolor=BLUE,
                             bg=FIELD, fg=INK, insertbackground=INK)
            entry.grid(row=row, column=col+1, sticky="ew", pady=4, ipady=6)
            self.field_vars[key], self.field_entries[key] = variable, entry
            self.controls.append((entry, "normal"))
            variable.trace_add("write", self._field_changed)
        item_actions = ttk.Frame(content)
        item_actions.grid(row=4, column=0, sticky="ew", pady=(19, 9))
        ttk.Label(item_actions, text="项目明细", font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, padx=(0, 15))
        self._button(item_actions, "添加", lambda: self.item_dialog(new=True), row=0, column=1, padx=(0, 5))
        self._button(item_actions, "编辑", self.item_dialog, row=0, column=2, padx=(0, 5))
        self._button(item_actions, "删除", self.remove_item, row=0, column=3)
        self.items_tree = ttk.Treeview(content, columns=tuple(ITEM_LABELS), show="headings", height=2, selectmode="browse")
        for key, label in ITEM_LABELS.items():
            self.items_tree.heading(key, text=label)
            self.items_tree.column(key, width=220 if key == "name" else 90, minwidth=55, stretch=True)
        self.items_tree.grid(row=5, column=0, sticky="ew")
        self.items_tree.bind("<Double-1>", lambda _event: self.item_dialog())
        self.warning_label = ttk.Label(content, text="", foreground="#9A6700", wraplength=1100, justify="left", font=("Microsoft YaHei UI", 9))
        self.warning_label.grid(row=6, column=0, sticky="w", pady=(11, 4))
        self.confirm_pages = tk.BooleanVar(value=False)
        page_check = ttk.Checkbutton(content, text="我已对照原票，核对本票全部页面与明细", variable=self.confirm_pages)
        page_check.grid(row=7, column=0, sticky="w", pady=(3, 5))
        self.controls.append((page_check, "normal"))
        confirm = ttk.Frame(content)
        confirm.grid(row=8, column=0, sticky="ew", pady=(9, 0))
        self._button(confirm, "保存字段与核对记录", self.save_invoice_fields, row=0, column=0)
        self.field_hint = ttk.Label(confirm, text="", style="Muted.TLabel", font=("Microsoft YaHei UI", 9))
        self.field_hint.grid(row=0, column=1, sticky="w", padx=(15, 0))

    def _build_results(self):
        self.result_canvas, content = self._scroll_panel(self.results_tab)
        self.result_heading = ttk.Label(content, text="尚未审核", font=("Microsoft YaHei UI", 17, "bold"))
        self.result_heading.grid(row=0, column=0, sticky="w", pady=(0, 7))
        self.result_summary = ttk.Label(content, text="审核后在这里查看需要处理的问题。", style="Muted.TLabel", wraplength=760, justify="left")
        self.result_summary.grid(row=1, column=0, sticky="w", pady=(0, 8))
        self.result_mode_label = ttk.Label(content, textvariable=self.result_mode_text, style="Muted.TLabel",
                                           font=("Microsoft YaHei UI", 9), cursor="hand2")
        self.result_mode_label.grid(row=2, column=0, sticky="w", pady=(0, 14))
        self.result_mode_label.bind("<Button-1>", lambda _event: self.model_switch_popup())
        filter_bar = ttk.Frame(content)
        filter_bar.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.show_all_checks = tk.BooleanVar(value=False)
        toggle = ttk.Checkbutton(filter_bar, text="显示全部校验项", variable=self.show_all_checks, command=self._refresh_results)
        toggle.grid(row=0, column=0, sticky="w")
        self.controls.append((toggle, "normal"))
        self._button(filter_bar, "重新审核", self.audit_current, row=0, column=1, padx=(15, 6))
        self._button(filter_bar, "审核全部任务", self.audit_all, row=0, column=2)
        frame = ttk.Frame(content)
        frame.grid(row=4, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.result_tree = ttk.Treeview(frame, columns=("status", "title", "actual", "expected"), show="headings", height=9)
        for key, label, width in [("status", "结果", 86), ("title", "校验项", 184), ("actual", "实际值", 245), ("expected", "期望值", 245)]:
            self.result_tree.heading(key, text=label)
            self.result_tree.column(key, width=width, minwidth=65, stretch=key in ("actual", "expected"))
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.result_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.result_tree.configure(yscrollcommand=scrollbar.set)
        self.result_tree.tag_configure(FAIL, foreground="#D70015")
        self.result_tree.tag_configure(REVIEW, foreground="#9A6700")
        self.result_tree.tag_configure(PASS, foreground=INK)
        self.result_tree.bind("<<TreeviewSelect>>", self._show_check_detail)
        ttk.Label(content, text="选择一项查看原因与原始依据。", style="Muted.TLabel", font=("Microsoft YaHei UI", 9)).grid(row=5, column=0, sticky="w", pady=(17, 8))
        self.check_detail = tk.Text(content, height=6, wrap="word", bg=FIELD, fg=INK,
                                    font=("Microsoft YaHei UI", 10), relief="flat", padx=13, pady=11, state="disabled")
        self.check_detail.grid(row=6, column=0, sticky="ew")

    def _build_logs(self):
        self.export_canvas, content = self._scroll_panel(self.log_tab)
        ttk.Label(content, text="报告已准备好", font=("Microsoft YaHei UI", 17, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.export_summary = ttk.Label(content, text="完成审核后，可以导出清楚有据的 PDF 报告。", style="Muted.TLabel", wraplength=760, justify="left")
        self.export_summary.grid(row=1, column=0, sticky="w", pady=(0, 25))
        ttk.Separator(content).grid(row=2, column=0, sticky="ew", pady=(0, 20))
        ttk.Label(content, text="报告包含", font=("Microsoft YaHei UI", 11, "bold")).grid(row=3, column=0, sticky="w", pady=(0, 12))
        ttk.Label(content, text="申请信息与材料清单\n识别字段及修改记录\n逐项校验、问题原因与处理建议\n人工复核与签字栏", style="Muted.TLabel", justify="left").grid(row=4, column=0, sticky="w", pady=(0, 20))
        self._button(content, "合并导出全部任务", self.export_all, row=5, column=0, sticky="w", pady=(0, 22))
        self.logs_visible = False
        self.log_toggle = self._button(content, "查看执行记录 ＋", self.toggle_logs, row=6, column=0, sticky="w")
        self.log_text = tk.Text(content, height=9, wrap="word", bg=FIELD, fg=MUTED,
                                font=("Microsoft YaHei UI", 9), relief="flat", padx=13, pady=11, state="disabled")
        self.log_text.grid(row=7, column=0, sticky="nsew", pady=(10, 0))
        self.log_text.grid_remove()

    def toggle_logs(self):
        self.logs_visible = not self.logs_visible
        if self.logs_visible:
            self.log_text.grid()
            self.log_toggle.configure(text="收起执行记录 −")
        else:
            self.log_text.grid_remove()
            self.log_toggle.configure(text="查看执行记录 ＋")

    def advanced_dialog(self):
        if self.busy:
            return
        popup = tk.Toplevel(self)
        popup.title("高级设置")
        popup.transient(self)
        popup.grab_set()
        outer = ttk.Frame(popup, padding=22)
        outer.pack(fill="both", expand=True)
        _canvas, frame = self._scroll_panel(outer)
        frame.columnconfigure(1, weight=1)
        enabled = tk.BooleanVar(popup, value=self.use_ai.get())
        profile = tk.StringVar(popup, value=self.active_model_profile)
        name = tk.StringVar(popup, value=self.active_model_profile or "自定义模型")
        base = tk.StringVar(popup, value=self.model_base_url.get())
        model = tk.StringVar(popup, value=self.model_name.get())
        key = tk.StringVar(popup, value=self.api_key.get())
        note = tk.StringVar(popup, value="只保存配置名称、地址和模型；密钥只在本次运行使用。")
        ttk.Label(frame, text="模型设置", font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))
        ttk.Checkbutton(frame, text="启用模型辅助审核", variable=enabled).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Label(frame, text="已保存配置").grid(row=2, column=0, sticky="w", padx=(0, 16))
        choices = ttk.Combobox(frame, textvariable=profile, values=[item["name"] for item in self.model_profiles], state="readonly", width=34)
        choices.grid(row=2, column=1, sticky="ew", pady=6)

        def select_profile(_event=None):
            if self.busy:
                return
            item = next((item for item in self.model_profiles if item["name"] == profile.get()), None)
            if item:
                name.set(item["name"])
                base.set(item["base_url"])
                model.set(item["model"])
                key.set("")
                note.set("已切换配置，请填写该服务对应的密钥；本机无认证服务可留空。")

        def new_profile():
            if self.busy:
                return
            profile.set("")
            name.set("自定义模型")
            base.set("")
            model.set("")
            key.set("")
            note.set("填写新配置后点击保存配置；接口地址与模型名称可自由填写。")

        choices.bind("<<ComboboxSelected>>", select_profile)
        add = ttk.Button(frame, text="新增", command=new_profile, style="Compact.TButton")
        add.grid(row=2, column=2, padx=(10, 0))
        for row, label, variable in ((3, "配置名称", name), (4, "接口地址", base),
                                    (5, "模型名称", model), (6, "API 密钥", key)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16))
            ttk.Entry(frame, textvariable=variable, show="●" if row == 6 else "", width=43).grid(row=row, column=1, columnspan=2, sticky="ew", pady=6)
        local_box = ttk.LabelFrame(frame, text="本机模型（Ollama）", padding=12)
        local_box.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(14, 2))
        local_box.columnconfigure(0, weight=1)
        local_status = tk.StringVar(popup, value=LOCAL_SERVICE_HINT)
        ttk.Label(local_box, textvariable=local_status, style="Muted.TLabel",
                  wraplength=620, justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        local_choice = tk.StringVar(popup, value="")
        local_combo = ttk.Combobox(local_box, textvariable=local_choice, state="readonly", width=34)
        local_combo.grid(row=1, column=0, sticky="ew", pady=4)
        found = {"models": []}

        def detect_local():
            if self.busy:
                return
            probe = base.get().strip()
            if provider_kind(probe) != "local":
                probe = DEFAULT_LOCAL_BASE_URL
            models, message = probe_local_service(probe)
            found["models"] = models
            local_combo.configure(values=[f"{item['name']} · {item['parameters']} · {item['size']}".strip(" ·")
                                          for item in models])
            if models:
                local_combo.current(0)
                local_status.set(message + "。选择后点“使用此模型”，地址与模型名会填入上方；点“完成”生效。")
            else:
                local_choice.set("")
                local_status.set(message)

        def use_local():
            if self.busy:
                return
            index = local_combo.current()
            if index < 0 or index >= len(found["models"]):
                messagebox.showinfo("先检测本机模型", "点击“检测本机模型”，再选择要使用的模型。", parent=popup)
                return
            item = found["models"][index]
            url = base.get().strip() if provider_kind(base.get()) == "local" else DEFAULT_LOCAL_BASE_URL
            name.set(item["name"])
            base.set(url)
            model.set(item["name"])
            key.set("")
            note.set(f"已填入本机模型 {item['name']}；点击“测试连接”验证，或点“完成”直接使用。")

        detect_button = ttk.Button(local_box, text="检测本机模型", command=detect_local, style="Compact.TButton")
        detect_button.grid(row=1, column=1, padx=(8, 0))
        use_button = ttk.Button(local_box, text="使用此模型", command=use_local, style="Compact.TButton")
        use_button.grid(row=2, column=1, padx=(8, 0), pady=(2, 0))
        floating = ttk.LabelFrame(frame, text="校验能手（窗口内悬浮助手）", padding=12)
        floating.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(12, 2))
        floating.columnconfigure(0, weight=1)
        ttk.Label(floating, text="窗口右下角常驻一个圆形悬浮球，点击展开对话面板；面板只在主窗口内显示，不会浮到桌面上。"
                                "启用上面的模型时由模型改写条文，未启用时直接引用制度条文，约 1 毫秒给出答案。",
                  style="Muted.TLabel", wraplength=620, justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ball_visible = tk.BooleanVar(popup, value=bool(getattr(getattr(self, "assistant", None), "visible", True)))
        ball_check = ttk.Checkbutton(floating, text="显示悬浮球（悬浮球上右键也可隐藏）", variable=ball_visible)
        ball_check.grid(row=1, column=0, sticky="w")

        def toggle_ball():
            target = getattr(self, "assistant", None)
            if target is not None:
                target.set_visible(bool(ball_visible.get()))

        ball_check.configure(command=toggle_ball)
        ttk.Button(floating, text="打开对话", style="Compact.TButton",
                   command=lambda: getattr(self, "assistant", None) and self.assistant.open_chat()
                   ).grid(row=1, column=1, padx=(8, 0))

        ttk.Label(frame, text="支持 OpenAI 兼容的 Chat Completions 接口和工具调用。\n接口地址可填 Base URL 或完整 /chat/completions 地址。\n远程服务请用 HTTPS，不要把密钥写在地址中。\n本机 Ollama 示例：http://localhost:11434/v1；模型名填写实际已安装名称。\n启用后，必要识别文本会发送至该服务；费用以你选择的平台为准。", style="Muted.TLabel", justify="left", wraplength=650).grid(row=9, column=0, columnspan=3, sticky="w", pady=(12, 8))
        ttk.Label(frame, textvariable=note, style="Muted.TLabel", wraplength=650).grid(row=10, column=0, columnspan=3, sticky="w", pady=(0, 12))
        actions = ttk.Frame(frame)
        actions.grid(row=11, column=0, columnspan=3, sticky="ew")
        actions.columnconfigure(2, weight=1)

        def values():
            model_endpoint(base.get())
            return {"name": name.get().strip(), "base_url": base.get().strip().rstrip("/"), "model": validate_model(model.get())}

        def apply():
            if self.busy:
                return
            try:
                item = values()
            except ValueError as exc:
                messagebox.showerror("模型配置未完成", str(exc), parent=popup)
                return
            self.model_base_url.set(item["base_url"])
            self.model_name.set(item["model"])
            self.api_key.set(key.get().strip())
            self.use_ai.set(enabled.get())
            self.active_model_profile = item["name"]
            self._paint_model_chip()
            popup.destroy()

        def save():
            if self.busy:
                return
            try:
                item = values()
                profiles = [item if old["name"] == item["name"] else old for old in self.model_profiles]
                if not any(old["name"] == item["name"] for old in profiles):
                    profiles.append(item)
                save_profiles(profiles, item["name"])
            except (OSError, ValueError) as exc:
                messagebox.showerror("配置未保存", str(exc), parent=popup)
                return
            self.model_profiles = profiles
            choices.configure(values=[item["name"] for item in profiles])
            profile.set(item["name"])
            note.set("配置已保存到本机用户目录，不含密钥；点击完成后使用。")

        def test():
            if self.busy:
                return
            try:
                item = values()
            except ValueError as exc:
                messagebox.showerror("模型配置未完成", str(exc), parent=popup)
                return
            secret = key.get().strip()
            for button in (add, save_button, test_button, done_button):
                button.configure(state="disabled")
            note.set("正在测试连接，不发送任何单据……")
            def worker(_log):
                from agent import call_model
                try:
                    call_model([{"role": "user", "content": "请只回复：连接成功。"}], secret,
                               base_url=item["base_url"], model=item["model"])
                    return "已收到模型响应。实际审核仍需模型支持工具调用。"
                except Exception as exc:
                    return "连接测试未完成（" + type(exc).__name__ + "），请检查地址、模型、密钥与网络。"
            def done(result):
                self.status_text.set(result)
                if popup.winfo_exists():
                    note.set(result)
                    for button in (add, save_button, test_button, done_button):
                        button.configure(state="normal")
            self._start_job("测试模型连接", worker, done)

        save_button = ttk.Button(actions, text="保存配置", command=save)
        save_button.grid(row=0, column=0, padx=(0, 10))
        test_button = ttk.Button(actions, text="测试连接", command=test)
        test_button.grid(row=0, column=1)
        done_button = ttk.Button(actions, text="完成", command=apply, style="Paper.Primary.TButton")
        done_button.grid(row=0, column=3)
        popup.update_idletasks()
        width = min(frame.winfo_reqwidth() + 70, self.winfo_screenwidth() - 80)
        height = min(frame.winfo_reqheight() + 60, self.winfo_screenheight() - 100)
        popup.geometry(f"{width}x{height}")
        popup.minsize(min(620, width), min(480, height))

    def show_home(self):
        if self.busy or not self._flush_edits():
            return
        self.home_visible = True
        self._update_flow()

    def continue_home(self):
        if self.busy or not self._flush_edits():
            return
        self.resume_current() if self.current else self.open_project()

    def resume_current(self):
        if self.current:
            self.show_step(self.notebook.index(self.notebook.select()))

    def show_step(self, index):
        if self.busy or not self.current or not self._flush_edits():
            return
        self.home_visible = False
        self.notebook.select(index)
        self._update_flow()

    def previous_step(self):
        self.show_step(max(0, self.notebook.index(self.notebook.select()) - 1))

    def next_step(self):
        if self.busy or not self.current or not self._flush_edits():
            return
        index = self.notebook.index(self.notebook.select())
        if index == 0:
            self.show_step(1)
        elif index == 1:
            self.audit_current()
        elif index == 2:
            self.show_step(3)
        else:
            self.export_current()

    def _layout_nav(self, force=False):
        """Re-pack the top bar: chip pinned right, secondary entries folded when narrow."""
        density = getattr(self, "_nav_density", "full")
        active = getattr(self, "_nav_active", True)
        key = (density, active)
        if key == getattr(self, "_nav_applied", None) and not force:
            return
        self._nav_applied = key
        for widget in (*self.nav_buttons.values(), self.nav_overflow, self.model_chip):
            widget.pack_forget()
        if active:
            self.model_chip.pack(side="right", padx=(14, 0))
        else:
            self.model_chip.pack(side="right", padx=(0, 2))
            return
        if density == "compact":
            self.nav_overflow.pack(side="right", padx=(8, 0))
        visible = [label for label in self.nav_buttons
                   if density == "full" or label not in self.nav_collapsed]
        for label in reversed(visible):
            self.nav_buttons[label].pack(side="right", padx=(18 if label == "审核规则" else 8, 0))

    def _nav_resized(self, event):
        """Pick the densest layout that still fits the chip plus the entries."""
        if not hasattr(self, "nav_overflow"):
            return
        # Ignore the pre-map configure pass (1 px wide, e.g. a withdrawn window).
        if event.width <= 1 or not self.top_nav.winfo_ismapped():
            return
        chrome = self.brand.winfo_reqwidth() + self.model_chip.winfo_reqwidth() + 72
        tap = lambda label: self.nav_buttons[label].winfo_reqwidth() + (18 if label == "审核规则" else 8)
        full = chrome + sum(tap(label) for label in self.nav_buttons)
        self._nav_density = "full" if event.width >= full else "compact"
        self._layout_nav()

    def _nav_overflow_menu(self):
        menu = tk.Menu(self, tearoff=0)
        for label in self.nav_collapsed:
            menu.add_command(label=label, command=self.nav_buttons[label].invoke)
        try:
            menu.tk_popup(self.nav_overflow.winfo_rootx(),
                          self.nav_overflow.winfo_rooty() + self.nav_overflow.winfo_height() + 2)
        finally:
            menu.grab_release()

    def _update_flow(self):
        if not hasattr(self, "welcome"):
            return
        if not self.apps:
            self.home_visible = True
        active = bool(self.apps) and not self.home_visible
        view_key = (active, self.notebook.select() if active else None)
        changed = view_key != self._view_key
        self._view_key = view_key
        helper = getattr(self, "assistant", None)
        if helper is not None:
            # The ball and panel corners blend into whatever sits behind them.
            helper.set_surface(PAPER if active else BG)
        self._nav_active = active
        self._layout_nav()
        self.brand_subtitle.grid() if active else self.brand_subtitle.grid_remove()
        self.top_nav.configure(style="Glass.Nav.TFrame", padding=(24, 18) if active else (24, 16))
        self.brand.configure(style="Nav.Surface.TFrame")
        self.brand_title.configure(style="Nav.Title.TLabel",
            font=("Microsoft YaHei UI", 16, "bold") if active else ("Microsoft YaHei UI", -20, "bold"))
        self.brand_subtitle.configure(style="Nav.Muted.TLabel")
        self.footer.configure(style="Shell.TFrame" if active else "Home.Header.TFrame",
                              padding=(22, 0, 22, 13) if active else (16, 10))
        self.status_label.configure(style="Shell.TLabel" if active else "Home.Header.TLabel",
            font=("Microsoft YaHei UI", 9) if active else ("Microsoft YaHei UI", -12), foreground=MUTED)
        self.status_dot.pack_forget() if active else self.status_dot.pack(side="left", before=self.status_label, padx=(0, 7))
        for widget in (self.sidebar, self.work_area):
            widget.grid() if active else widget.grid_remove()
        self.app_tree.grid() if active else self.app_tree.grid_remove()
        for widget in (self.flow_nav, self.workspace_card, self.flow_actions):
            widget.grid() if active else widget.grid_remove()
        self.welcome.grid_remove() if active else self.welcome.grid()
        self.home_continue_button.configure(text="继续" if self.current else "打开")
        self.home_continue_detail.configure(text=("继续上次的识别、修改与审核。"
            if self.current else "打开已有项目，继续识别、修改与审核。"))
        if self.current:
            self.home_open_button.place(relx=.5, rely=.92, anchor="center")
        else:
            self.home_open_button.place_forget()
        self.nav_buttons["主页"].configure(style="Nav.TButton" if active else "Current.Nav.TButton")
        if not active:
            self.motion.stop(self.notebook, finish=0)
            self.title_label.configure(text="报销工作台")
            self._paint_model_chip()
            return
        for widget in (self.home_content, self.home_new_card, self.home_continue_card):
            self.motion.stop(widget, finish=0)
        if changed:
            self.motion.move(self.notebook, 0, start=6, duration=160)
        index = self.notebook.index(self.notebook.select())
        self.title_label.configure(text=("导入报销材料", "核对识别信息", "查看审核结果", "导出审核报告")[index])
        for i, button in enumerate(self.step_buttons):
            button.configure(style="ActiveStep.TButton" if i == index else "Step.TButton")
        self.flow_primary.configure(text=("下一步：确认信息", "开始审核", "查看报告", "导出 PDF")[index])
        self.back_button.grid_remove() if index == 0 else self.back_button.grid()
        self._paint_model_chip()

    # ---- model backend indicator and quick switcher -------------------------
    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, radius, **options):
        points = [x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
                  x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
                  x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1]
        return canvas.create_polygon(points, smooth=True, **options)

    def _set_chip_hover(self, value):
        if self._chip_hover != value:
            self._chip_hover = value
            self._paint_model_chip()

    def _model_state(self):
        """Single source of truth for the 'which backend am I using' label."""
        if not self.use_ai.get():
            return {"kind": "off", "title": "纯规则引擎", "short": "规则引擎",
                    "color": "#9DAFBE", "detail": "只在本机做识别与规则校验，不发送任何文字"}
        base = self.model_base_url.get().strip()
        model = self.model_name.get().strip() or "未选择模型"
        if provider_kind(base) == "local":
            return {"kind": "local", "title": f"本地模型 · {model}", "short": "本地",
                    "color": "#1F9D55", "detail": "调用本机 Ollama，单据文字不出本机"}
        return {"kind": "cloud", "title": f"在线模型 · {model}", "short": "在线",
                "color": BLUE, "detail": "单据识别文字将发送至你配置的云端服务"}

    def _paint_model_chip(self):
        canvas = getattr(self, "model_chip", None)
        if canvas is None or not canvas.winfo_exists():
            return
        from tkinter import font as tk_font
        state = self._model_state()
        background = NAV if (hasattr(self, "welcome") and not self.home_visible) else BG
        canvas.configure(bg=background)
        canvas.delete("all")
        font = tk_font.Font(family="Microsoft YaHei UI", size=10)
        width = font.measure(state["title"]) + 66
        height = 34
        canvas.configure(width=width, height=height)
        self._round_rect(canvas, 1, 1, width - 1, height - 1, height // 2,
                         fill="#FFFFFF", outline=BLUE if self._chip_hover else LINE, width=1)
        canvas.create_oval(14, height // 2 - 4, 22, height // 2 + 4, fill=state["color"], outline="")
        canvas.create_text(30, height // 2, text=state["title"], anchor="w", fill=INK, font=font)
        canvas.create_text(width - 15, height // 2, text="▼", anchor="e", fill=MUTED,
                           font=("Microsoft YaHei UI", 7))
        self._refresh_mode_hint()

    def _chat_config(self):
        return {"base_url": self.model_base_url.get().strip() or DEFAULT_BASE_URL,
                "model": self.model_name.get().strip() or "未选择模型",
                "api_key": self.api_key.get().strip(),
                "enabled": bool(self.use_ai.get())}

    def _refresh_mode_hint(self):
        state = self._model_state()
        if hasattr(self, "result_mode_text"):
            self.result_mode_text.set("审核方式：" + ("规则引擎（未调用模型）" if state["kind"] == "off"
                                                    else state["title"]))
        if hasattr(self, "home_mode_text"):
            self.home_mode_text.set("当前接入：" + state["title"] + "　·　点击此处切换")

    def _remember_profile(self, item):
        clean = {"name": item["name"], "base_url": item["base_url"].rstrip("/"), "model": item["model"]}
        profiles = [profile for profile in self.model_profiles if profile["name"] != clean["name"]]
        profiles.append(clean)
        try:
            save_profiles(profiles, clean["name"])
        except (OSError, ValueError):
            return
        self.model_profiles = profiles
        self.active_model_profile = clean["name"]

    def _apply_model_choice(self, *, profile=None, local_model=None, disable=False):
        """Switch backend immediately; nothing is sent anywhere by this action."""
        previous = self.model_base_url.get().strip().rstrip("/")
        if disable:
            self.use_ai.set(False)
            self.status_text.set("已切换为纯规则引擎：不调用任何模型")
        elif local_model:
            self.use_ai.set(True)
            self.model_base_url.set(local_model["base_url"])
            self.model_name.set(local_model["name"])
            self.api_key.set("")
            self.status_text.set(f"已接入本机模型 {local_model['name']} · 文字只在本机流转")
            self._remember_profile({"name": f"本机 {local_model['name']}",
                                    "base_url": local_model["base_url"], "model": local_model["name"]})
        elif profile:
            self.use_ai.set(True)
            self.model_base_url.set(profile["base_url"])
            self.model_name.set(profile["model"])
            same_endpoint = previous == profile["base_url"].rstrip("/")
            key = self.api_key.get().strip() if same_endpoint else ""
            env_base = os.environ.get("REIMBURSEMENT_BASE_URL", "").strip().rstrip("/")
            if not key and env_base == profile["base_url"].rstrip("/"):
                key = os.environ.get("REIMBURSEMENT_API_KEY", "").strip()
            self.api_key.set(key)
            self.active_model_profile = profile["name"]
            self.status_text.set(f"已切换模型配置：{profile['name']} · {profile['model']}"
                                 + ("" if key or provider_kind(profile["base_url"]) == "local"
                                    else "（尚未填写密钥，可在高级设置中补充）"))
            try:
                save_profiles(self.model_profiles, profile["name"])
            except (OSError, ValueError):
                pass
        self._paint_model_chip()

    def _menu_row(self, parent, title, subtitle, active, command, dot):
        row = tk.Frame(parent, bg=PAPER, cursor="hand2")
        row.pack(fill="x", padx=6, pady=1)
        marker = tk.Label(row, text="●", bg=PAPER, fg=dot, font=("Microsoft YaHei UI", 8))
        marker.pack(side="left", padx=(6, 6))
        text = tk.Frame(row, bg=PAPER)
        text.pack(side="left", fill="x", expand=True, pady=5)
        name = tk.Label(text, text=title, bg=PAPER, fg=INK, anchor="w", font=("Microsoft YaHei UI", 10))
        name.pack(fill="x")
        note = tk.Label(text, text=subtitle, bg=PAPER, fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 8))
        note.pack(fill="x")
        badge = tk.Label(row, text="当前" if active else "", bg=PAPER, fg=BLUE,
                         font=("Microsoft YaHei UI", 8, "bold"))
        badge.pack(side="right", padx=(4, 8))

        def paint(color):
            for widget in (row, marker, text, name, note, badge):
                widget.configure(bg=color)

        def enter(_event=None):
            paint("#F2F7FF")

        def leave(_event=None):
            paint(PAPER)

        def click(_event=None):
            self._close_model_menu()
            command()

        for widget in (row, marker, text, name, note, badge):
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)
            widget.bind("<Button-1>", click)
        return row

    def model_switch_popup(self):
        if self.busy:
            return
        if self._model_menu is not None and self._model_menu.winfo_exists():
            self._close_model_menu()
            return
        menu = tk.Toplevel(self)
        menu.overrideredirect(True)
        menu.configure(bg=LINE)
        self._model_menu = menu
        body = tk.Frame(menu, bg=PAPER)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(body, bg=PAPER)
        header.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(header, text="模型接入方式", bg=PAPER, fg=INK,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        tk.Label(header, text="点击即切换", bg=PAPER, fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="right")
        tk.Label(body, text="切换只影响之后的新审核与 AI 导入，已完成的结论保留记录。", bg=PAPER,
                 fg=MUTED, font=("Microsoft YaHei UI", 8), anchor="w",
                 wraplength=300, justify="left").pack(fill="x", padx=14, pady=(0, 6))

        local_base = (self.model_base_url.get() if provider_kind(self.model_base_url.get()) == "local"
                      else DEFAULT_LOCAL_BASE_URL)
        models, message = probe_local_service(local_base)
        current = self._model_state()
        tk.Label(body, text="本机模型（Ollama）", bg=PAPER, fg=MUTED, anchor="w",
                 font=("Microsoft YaHei UI", 8, "bold")).pack(fill="x", padx=16, pady=(4, 2))
        if models:
            for item in models:
                detail = " · ".join(part for part in (f"Ollama", item["parameters"], item["size"]) if part)
                self._menu_row(body, item["name"], detail,
                               current["kind"] == "local" and self.model_name.get() == item["name"],
                               lambda item=item: self._apply_model_choice(local_model=item), "#1F9D55")
        else:
            tk.Label(body, text=message, bg=PAPER, fg=MUTED, anchor="w",
                     font=("Microsoft YaHei UI", 8), wraplength=300, justify="left").pack(fill="x", padx=20, pady=(0, 4))

        saved_local = [profile for profile in self.model_profiles if provider_kind(profile["base_url"]) == "local"
                       and profile["model"] not in {item["name"] for item in models}]
        for profile in saved_local:
            self._menu_row(body, profile["model"], f"已保存的本地配置 · {profile['base_url']}",
                           current["kind"] == "local" and self.model_name.get() == profile["model"],
                           lambda profile=profile: self._apply_model_choice(profile=profile), "#1F9D55")

        tk.Frame(body, bg=LINE, height=1).pack(fill="x", padx=12, pady=6)
        tk.Label(body, text="在线服务", bg=PAPER, fg=MUTED, anchor="w",
                 font=("Microsoft YaHei UI", 8, "bold")).pack(fill="x", padx=16, pady=(0, 2))
        cloud = [profile for profile in self.model_profiles if provider_kind(profile["base_url"]) == "cloud"]
        if cloud:
            for profile in cloud:
                host = profile["base_url"].split("//")[-1].split("/")[0]
                self._menu_row(body, profile["name"], f"{profile['model']} · {host}",
                               current["kind"] == "cloud" and self.profile_matches(profile),
                               lambda profile=profile: self._apply_model_choice(profile=profile), BLUE)
        else:
            tk.Label(body, text="还没有在线服务配置，可在高级设置中添加。", bg=PAPER, fg=MUTED, anchor="w",
                     font=("Microsoft YaHei UI", 8)).pack(fill="x", padx=20, pady=(0, 4))

        tk.Frame(body, bg=LINE, height=1).pack(fill="x", padx=12, pady=6)
        self._menu_row(body, "不使用模型", "纯规则引擎，结论仍完整可信", current["kind"] == "off",
                       lambda: self._apply_model_choice(disable=True), "#9DAFBE")

        footer = tk.Frame(body, bg=FIELD)
        footer.pack(fill="x", pady=(6, 0))
        manage = tk.Label(footer, text="管理模型配置…", bg=FIELD, fg=BLUE, cursor="hand2",
                          font=("Microsoft YaHei UI", 9), anchor="w")
        manage.pack(fill="x", padx=16, pady=8)
        manage.bind("<Enter>", lambda _event: manage.configure(fg="#0062D5"))
        manage.bind("<Leave>", lambda _event: manage.configure(fg=BLUE))
        manage.bind("<Button-1>", lambda _event: (self._close_model_menu(), self.advanced_dialog()))

        menu.update_idletasks()
        width = max(322, body.winfo_reqwidth())
        height = body.winfo_reqheight() + 2
        x = self.model_chip.winfo_rootx() + self.model_chip.winfo_width() - width
        y = self.model_chip.winfo_rooty() + self.model_chip.winfo_height() + 3
        x = max(12, min(x, self.winfo_screenwidth() - width - 12))
        if y + height > self.winfo_screenheight() - 40:
            y = self.model_chip.winfo_rooty() - height - 8
        menu.geometry(f"{width}x{height}+{x}+{y}")
        menu.bind("<Escape>", lambda _event: self._close_model_menu())
        menu.focus_force()
        # Grace period: the pointer may still be travelling from the chip to the panel.
        menu.after(450, lambda: self._watch_menu_pointer(menu))

    def _watch_menu_pointer(self, menu):
        """Close when the pointer leaves both the menu and the chip; no focus juggling."""
        try:
            if self._model_menu is not menu or not menu.winfo_exists():
                return
            x, y = self.winfo_pointerxy()
            margin = 10
            inside = (menu.winfo_rootx() - margin <= x <= menu.winfo_rootx() + menu.winfo_width() + margin and
                      menu.winfo_rooty() - margin <= y <= menu.winfo_rooty() + menu.winfo_height() + margin)
            chip = self.model_chip
            on_chip = (chip.winfo_rootx() - margin <= x <= chip.winfo_rootx() + chip.winfo_width() + margin and
                       chip.winfo_rooty() - margin <= y <= chip.winfo_rooty() + chip.winfo_height() + margin)
            if not inside and not on_chip:
                self._close_model_menu()
                return
            menu.after(400, lambda: self._watch_menu_pointer(menu))
        except tk.TclError:
            self._model_menu = None

    def profile_matches(self, profile):
        return (self.model_base_url.get().strip().rstrip("/") == profile["base_url"].rstrip("/")
                and self.model_name.get().strip() == profile["model"])

    def _close_model_menu(self):
        menu = getattr(self, "_model_menu", None)
        self._model_menu = None
        if menu is not None:
            try:
                if menu.winfo_exists():
                    menu.destroy()
            except tk.TclError:
                pass

    @staticmethod
    def _put_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _refresh_policy_summary(self):
        p = self.policy
        self.policy_summary.configure(text=f"当前审核制度（演示）· {p.preset_label()}\n公司：{p.company_name}\n税号：{p.company_tax_id}    报销期限：{p.max_days} 天\n必需附件：{'、'.join(p.required_attachments) or '无'}")

    def _refresh_list(self):
        selected = self.current_id
        self.app_tree.delete(*self.app_tree.get_children())
        for app in self.apps:
            status = app.status if app.checks else "待审核"
            tag = "fail" if status == FAIL else "review" if status == REVIEW else "pass" if app.checks else ""
            mark = "● " if app.checks else "○ "
            self.app_tree.insert("", "end", iid=app.id, values=(app.applicant or app.id, mark + status), tags=(tag,))
        if selected and self.app_tree.exists(selected):
            self.app_tree.selection_set(selected)
        self.count_label.configure(text=f"共 {len(self.apps)} 笔")

    def _refresh_current(self):
        app = self.current
        self.loading = True
        for key, variable in self.info_vars.items():
            variable.set(getattr(app, key) if app else "")
        self.loading = False
        self.summary_label.configure(text="让每一笔报销，清楚有据。" + (f"    当前：{app.applicant or '未填写申请人'}" if app else ""))
        self.doc_tree.delete(*self.doc_tree.get_children())
        self.invoice_choices = []
        if app:
            for index, doc in enumerate(app.documents):
                result = f"识别失败：{doc.error}" if doc.error else f"{len(doc.invoices)} 张发票" if doc.invoices else "可查看原文并确认类别"
                self.doc_tree.insert("", "end", iid=str(index), values=(Path(doc.path).name, doc.category, doc.pages, result), tags=("fail",) if doc.error else ())
                self.invoice_choices.extend((doc, inv) for inv in doc.invoices)
        self.invoice_combo.configure(values=[f"{Path(doc.path).name} · 第 {inv.page} 页 · {inv.fields.get('invoice_number', '号码待确认')}" for doc, inv in self.invoice_choices])
        if self.invoice_choices:
            self.invoice_combo.current(0)
            self._display_invoice(self.invoice_choices[0][1])
        else:
            self.invoice_combo.set("")
            self._display_invoice(None)
        if app and app.documents:
            self.doc_tree.selection_set("0")
            self.category_var.set(app.documents[0].category)
        self._refresh_results()
        self._refresh_logs()
        self._refresh_ai_import()
        if app:
            self.export_summary.configure(text=f"{app.applicant or '未填写申请人'}  ·  {app.id}\n{len(app.documents)} 份材料  ·  {app.status if app.checks else '尚未审核'}\n报告按当前已保存字段与审核规则生成，原始依据和修改记录均可追溯。")
        self._update_flow()

    def _refresh_results(self):
        app = self.current
        self.result_tree.delete(*self.result_tree.get_children())
        checks = app.checks if app else []
        for key, entry in self.info_entries.items():
            entry.configure(style="Error.TEntry" if any(c.status == FAIL and c.field == key for c in checks) else "TEntry")
        self._paint_fields()
        if not app or not app.checks:
            self.result_heading.configure(text="等待审核", foreground=INK)
            self.result_summary.configure(text="确认信息后，点击“开始审核”查看结果。")
            self._put_text(self.check_detail, "")
            return
        failed = sum(c.status == FAIL for c in app.checks)
        pending = sum(c.status == REVIEW for c in app.checks)
        self.result_heading.configure(text="有问题需要处理" if failed else "有信息需要确认" if pending else "预审已通过", foreground="#D70015" if failed else INK)
        self.result_summary.configure(text=f"{self.policy.preset_label()} · {failed} 项不通过 · {pending} 项待复核 · 共检查 {len(app.checks)} 项。" + (" 当前显示全部校验项。" if self.show_all_checks.get() else " 优先显示需要处理的问题。"))
        shown = 0
        for index, check in enumerate(app.checks):
            if not self.show_all_checks.get() and check.status not in (FAIL, REVIEW):
                continue
            self.result_tree.insert("", "end", iid=str(index), values=(check.status, check.title, check.actual, check.expected), tags=(check.status,))
            shown += 1
        self.result_tree.configure(height=max(1, min(shown, 10)))
        self._put_text(self.check_detail, app.ai_summary or "请选择校验项查看详细依据。")

    def _refresh_logs(self):
        app = self.current
        text = "\n".join(app.events) if app else "请先新建或载入一笔报销。"
        if app:
            edits = [(inv, edit) for doc in app.documents for inv in doc.invoices for edit in inv.edits]
            if edits:
                text += "\n\n—— 字段修订记录 ——\n" + "\n".join(self._describe_edit(inv, edit) for inv, edit in edits)
        self._put_text(self.log_text, text)
        self.log_text.see("end")

    def _refresh_ai_import(self):
        self.ai_import_tree.delete(*self.ai_import_tree.get_children())
        self._ai_import_rows = []
        doc = self._selected_document()
        result = doc.ai_import if doc else {}
        if not result:
            self.ai_import_status_label.configure(foreground=MUTED)
            self.ai_import_state.set("本地识别结果 · 尚未使用 AI 整理" if doc else "等待导入 · 普通导入不会调用大模型")
            self._put_text(self.ai_import_detail, "点击“AI 一键导入”选择新材料，或点击“AI 整理已有材料”。\n模型建议会逐项列出原值、建议值和原文依据，仍需人工核对。")
        else:
            self.ai_import_status_label.configure(foreground=BLUE if result.get("status") == "AI 已完成" else "#D70015")
            self.ai_import_state.set(f"{result.get('status', 'AI 未完成')} · {result.get('model', '')} · {Path(doc.path).name}")
            self._ai_import_rows = result.get("fields", [])
            for index, item in enumerate(self._ai_import_rows):
                action = item.get("action", "")
                self.ai_import_tree.insert("", "end", iid=str(index), values=(f"第{item.get('page', 1)}页 · {item.get('label', item.get('field', ''))}", item.get("original", ""), item.get("suggested", ""), action), tags=("review",) if any(word in action for word in ("复核", "确认", "拒绝")) else ())
            detail = result.get("summary", "")
            if result.get("reason"):
                detail += "\n未完成原因：" + result["reason"]
            self._put_text(self.ai_import_detail, detail + "\n选择上方字段查看原文依据；请到“确认信息”核对和保存。")
        events = [line for line in self.current.events if "AI 导入" in line] if self.current else []
        self._put_text(self.ai_import_log, "\n".join(events[-60:]) or "尚未发起模型请求。这里显示实际阶段记录，不展示模型内部推理。")
        self.ai_import_log.see("end")

    def _show_ai_import_detail(self, _event=None):
        selected = self.ai_import_tree.selection()
        if not selected:
            return
        item = self._ai_import_rows[int(selected[0])]
        doc = self._selected_document()
        self._put_text(self.ai_import_detail, f"{Path(doc.path).name if doc else ''} · 第{item.get('page', 1)}页\n{item.get('label', item.get('field', ''))}：{item.get('original', '')} → {item.get('suggested', '')}\n原文依据：{item.get('evidence', '未提供')}\n处理状态：{item.get('action', '')}")

    def _ai_import_configuration(self):
        key, base, model = self.api_key.get().strip(), self.model_base_url.get().strip(), self.model_name.get().strip()
        try:
            endpoint = model_endpoint(base)
            validate_model(model)
            if not key and not is_local_endpoint(endpoint):
                raise ValueError("请先填写 API 密钥；本机无认证模型服务可留空。")
            if key and endpoint.startswith("http:") and not is_local_endpoint(endpoint):
                raise ValueError("远程模型携带密钥时须使用 HTTPS 地址。")
        except ValueError as exc:
            messagebox.showinfo("先配置 AI 导入", f"{exc}\n在高级设置中填写地址、模型和密钥，再点击“完成”。", parent=self)
            self.advanced_dialog()
            return None
        return key, base, model

    def _begin_ai_import(self, model):
        self._ai_import_running = True
        self.ai_import_status_label.configure(foreground=BLUE)
        self.ai_import_state.set(f"准备材料 · 使用模型 {model}")
        self.ai_import_tree.delete(*self.ai_import_tree.get_children())
        self._ai_import_rows = []
        self._put_text(self.ai_import_detail, "正在处理。完成后将显示模型建议与对应原文；已有字段不会被自动覆盖。")
        self._put_text(self.ai_import_log, "")
        self.update_idletasks()
        total = max(1, self.ai_import_panel.master.winfo_height())
        self.info_canvas.yview_moveto(self.ai_import_panel.winfo_y() / total)

    def _organize_ai_documents(self, app, documents, configuration, log):
        from ai_import import organize_document
        key, base, model = configuration
        for index, doc in enumerate(documents, 1):
            def event(message):
                line = f"{datetime.now():%H:%M:%S} AI 导入 {index}/{len(documents)} · {Path(doc.path).name} · {message}"
                app.events.append(line)
                log(line)
            event("开始整理")
            try:
                organize_document(doc, api_key=key, base_url=base, model=model, log=event)
            except Exception as exc:
                doc.ai_import = {"status": "AI 未完成", "model": model, "summary": "本地识别结果已保留，可人工核对。", "fields": [], "reason": type(exc).__name__}
                event("模型整理未完成（" + type(exc).__name__ + "），保留本地结果")

    def organize_existing_documents(self):
        if self.busy or not self._flush_edits():
            return
        app = self.current
        if not app or not app.documents:
            messagebox.showinfo("先添加材料", "请先导入材料，或直接使用“AI 一键导入”。", parent=self)
            return
        configuration = self._ai_import_configuration()
        if configuration is None:
            return
        self._invalidate(app)
        self._begin_ai_import(configuration[2])
        def worker(log):
            self._organize_ai_documents(app, list(app.documents), configuration, log)
        def done(_result):
            self._ai_import_running = False
            self._refresh_current()
            finished = sum(doc.ai_import.get("status") == "AI 已完成" for doc in app.documents)
            self.status_text.set(f"AI 整理完成 {finished}/{len(app.documents)} 份 · 请查看工作台，未完成材料保留本地结果")
        self._start_job("AI 整理已有材料", worker, done)

    @staticmethod
    def _describe_edit(invoice, edit):
        before, after = edit.get("before", ""), edit.get("after", "")
        label = FIELD_LABELS.get(edit.get("field"), edit.get("field", "字段"))
        if edit.get("field") == "items":
            label = "项目明细"
            def describe(items):
                if not items:
                    return "无明细"
                return "；".join("，".join(f"{label}：{item.get(key, '')}" for key, label in ITEM_LABELS.items()) for item in items)
            before, after = describe(before), describe(after)
        return f"{edit.get('time', '')}  {Path(invoice.source).name} 第{invoice.page}页  {label}：{before} → {after}"

    def _invalidate(self, app):
        if app.checks or app.ai_summary:
            app.events.append(f"{datetime.now():%H:%M:%S} 数据已修改，旧审核结果作废。")
        app.checks.clear()
        app.ai_summary = ""
        app.mode = "等待审核"
        self._refresh_list()
        self._refresh_results()
        if app is self.current:
            self.summary_label.configure(text="让每一笔报销，清楚有据。" + f"    当前：{app.applicant or '未填写申请人'}")

    def _info_changed(self, key):
        if self.loading or self.busy or not self.current:
            return
        setattr(self.current, key, self.info_vars[key].get().strip())
        self._invalidate(self.current)

    def _field_changed(self, *_args):
        if self.loading or self.busy or not self.edit_invoice or not self.current:
            return
        self.fields_dirty = True
        self.field_hint.configure(text="字段已修改 · 保存后重新审核", foreground="#936007")
        self._invalidate(self.current)

    def _refresh_items(self):
        self.items_tree.delete(*self.items_tree.get_children())
        for index, item in enumerate(self.items_buffer):
            self.items_tree.insert("", "end", iid=str(index), values=[item.get(key, "") for key in ITEM_LABELS])

    def item_dialog(self, new=False):
        if self.busy or not self.edit_invoice:
            return
        selected = self.items_tree.selection()
        if not new and not selected:
            messagebox.showinfo("选择明细", "请先选择需要编辑的明细。", parent=self)
            return
        index = None if new else int(selected[0])
        item = {} if new else self.items_buffer[index]
        popup = tk.Toplevel(self)
        popup.title("添加项目明细" if new else "编辑项目明细")
        popup.resizable(False, False)
        popup.transient(self)
        popup.grab_set()
        frame = ttk.Frame(popup, padding=20)
        frame.pack(fill="both", expand=True)
        variables = {}
        for row, (key, label) in enumerate(ITEM_LABELS.items()):
            variables[key] = tk.StringVar(value=item.get(key, ""))
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 20), pady=9)
            ttk.Entry(frame, textvariable=variables[key], width=33).grid(row=row, column=1, sticky="w")
        ttk.Label(frame, text="金额按原票填写，留空将提示人工复核。税率示例：13%", style="Muted.TLabel", wraplength=480).grid(row=6, column=0, columnspan=2, sticky="w", pady=(5, 14))
        def save():
            value = dict(item)
            value.update({key: variable.get().strip() for key, variable in variables.items()})
            if new:
                self.items_buffer.append(value)
            else:
                self.items_buffer[index] = value
            self._refresh_items()
            self._field_changed()
            popup.destroy()
        ttk.Button(frame, text="保存明细", command=save, style="Paper.Primary.TButton").grid(row=7, column=1, sticky="e")
        popup.update_idletasks()
        popup.minsize(popup.winfo_reqwidth(), popup.winfo_reqheight())

    def remove_item(self):
        if self.busy or not self.edit_invoice:
            return
        selected = self.items_tree.selection()
        if selected:
            del self.items_buffer[int(selected[0])]
            self._refresh_items()
            self._field_changed()

    def _display_invoice(self, invoice):
        self.loading = True
        self.edit_invoice = invoice
        for key, variable in self.field_vars.items():
            variable.set((invoice.kind if key == "kind" else invoice.fields.get(key, "")) if invoice else "")
        self._paint_fields()
        self.items_buffer = json.loads(json.dumps(invoice.items)) if invoice else []
        self._refresh_items()
        self.confirm_pages.set(False)
        self.warning_label.configure(text="识别提醒：" + "；".join(invoice.warnings) if invoice and invoice.warnings else "多页文件请逐页核对，确认所需明细均已录入。" if invoice else "")
        self.fields_dirty = False
        self.field_hint.configure(text="黄色：待确认    红色：审核错误", foreground=MUTED)
        self.loading = False

    def _paint_fields(self):
        invoice = self.edit_invoice
        for key, entry in self.field_entries.items():
            failed = invoice and self.current and any(c.status == FAIL and c.field == key and self._check_matches_invoice(c, invoice) for c in self.current.checks)
            uncertain = invoice and key in invoice.uncertain and key not in invoice.confirmed
            entry.configure(bg="#fff0f2" if failed else "#fff8df" if uncertain else FIELD, fg="#bd1c32" if failed else INK)

    @staticmethod
    def _check_matches_invoice(check, invoice):
        if not check.source or check.source == "申请汇总":
            return False
        basename = Path(invoice.source).name
        page = re.search(r"\s+第\s*(\d+)\s*页(?:\s|$)", check.source)
        source = check.source[:page.start()].strip() if page else check.source.strip()
        if Path(source).name != basename:
            return False
        return not page or int(page.group(1)) == invoice.page

    def save_invoice_fields(self, quiet=False):
        if self.busy:
            return False
        inv, app = self.edit_invoice, self.current
        if not inv or not app:
            return True
        items = json.loads(json.dumps(self.items_buffer))
        for key, variable in self.field_vars.items():
            value = variable.get().strip()
            before = inv.kind if key == "kind" else inv.fields.get(key, "")
            if not quiet or value != before:
                inv.edit(key, value)
        if items != inv.items:
            inv.edits.append({"time": datetime.now().isoformat(timespec="seconds"), "field": "items", "before": inv.items, "after": items})
            inv.items = items
        if not quiet and "items" not in inv.confirmed:
            inv.confirmed.append("items")
        if not quiet and self.confirm_pages.get():
            if "pages" not in inv.confirmed:
                inv.confirmed.append("pages")
            app.events.append(f"人工确认本票全部页面及明细：{Path(inv.source).name} 第{inv.page}页；原识别提醒：{'；'.join(inv.warnings) or '无'}")
            inv.warnings.clear()
        self.fields_dirty = False
        self._invalidate(app)
        self._display_invoice(inv)
        self._refresh_logs()
        self.status_text.set("字段及修订记录已保存，请重新审核")
        return True

    def _flush_edits(self):
        return not self.fields_dirty or self.save_invoice_fields(quiet=True)

    def _select_app(self, _event=None):
        if self.busy:
            return
        selected = self.app_tree.selection()
        if not selected or selected[0] == self.current_id:
            return
        if not self._flush_edits():
            self.app_tree.selection_set(self.current_id)
            return
        self.current_id = selected[0]
        self.home_visible = False
        self._refresh_current()

    def _choose_invoice(self, _event=None):
        if self.busy or not self._flush_edits():
            return
        index = self.invoice_combo.current()
        if 0 <= index < len(self.invoice_choices):
            document, invoice = self.invoice_choices[index]
            self._display_invoice(invoice)
            if self.current and document in self.current.documents:
                self.doc_tree.selection_set(str(self.current.documents.index(document)))

    def _select_document(self, _event=None):
        if self.busy or not self.current:
            return
        selected = self.doc_tree.selection()
        if selected:
            self.category_var.set(self.current.documents[int(selected[0])].category)
            self._refresh_ai_import()

    def _selected_document(self):
        selected = self.doc_tree.selection()
        return self.current.documents[int(selected[0])] if self.current and selected else None

    def _show_check_detail(self, _event=None):
        if self.busy or not self.current:
            return
        selected = self.result_tree.selection()
        if selected:
            check = self.current.checks[int(selected[0])]
            self._put_text(self.check_detail, f"{check.title}：{check.status}\n实际值：{check.actual}\n期望值：{check.expected}\n原因：{check.reason}\n关联单据：{check.source}    字段：{FIELD_LABELS.get(check.field, check.field)}")

    def new_application(self):
        if self.busy or not self._flush_edits():
            return
        app = Application(id=f"BX-{datetime.now():%Y%m%d%H%M%S%f}", submitted_on=date.today().isoformat())
        self.apps.append(app)
        self.current_id = app.id
        self._empty_draft = app
        self.home_visible = False
        self._refresh_list()
        self._refresh_current()
        self.notebook.select(self.info_tab)

    def load_demo_from_home(self):
        """首屏一键演示：先建一张空单，载入完成后它会被 10 笔样例替换掉。"""
        if self.busy:
            return
        if self.current is None:
            self.new_application()
        self.load_demo()

    def load_demo(self):
        if self.busy or not self._flush_edits():
            return
        def worker(log):
            from demo import load_demo
            from extractor import extract_document
            log("正在载入 10 笔演示，并独立识别原始发票……")
            apps = load_demo()
            for index, app in enumerate(apps, 1):
                for position, doc in enumerate(app.documents):
                    log(f"识别演示原票 {index}/{len(apps)}：{Path(doc.path).name}")
                    try:
                        app.documents[position] = extract_document(doc.path)
                    except Exception as exc:
                        doc.error = str(exc)
                    app.events.append(f"独立提取演示原票：{Path(doc.path).name}")
            return apps
        def done(apps):
            existing = {app.id for app in self.apps}
            added = [app for app in apps if app.id not in existing]
            draft = self._empty_draft
            if added and draft is not None and self.current is draft and draft == Application(
                    id=draft.id, submitted_on=date.today().isoformat()):
                self.apps.remove(draft)
                self._empty_draft = None
            self.apps.extend(added)
            if added:
                self.current_id = added[0].id
            self.home_visible = False
            self._refresh_list()
            self._refresh_current()
            self.status_text.set(f"演示已载入 · 新增 {len(added)} 笔，原票与申请一一对应")
        self._start_job("载入演示", worker, done)

    def import_documents(self, use_ai=False):
        if self.busy or not self._flush_edits():
            return
        configuration = self._ai_import_configuration() if use_ai else None
        if use_ai and configuration is None:
            return
        if not self.current:
            self.new_application()
        paths = filedialog.askopenfilenames(parent=self, title="选择发票和附件（支持多选）", filetypes=[("支持的单据", "*.pdf *.png *.jpg *.jpeg"), ("PDF", "*.pdf"), ("图片", "*.png *.jpg *.jpeg")])
        if not paths:
            return
        app = self.current
        self._invalidate(app)
        if use_ai:
            self._begin_ai_import(configuration[2])
        def worker(log):
            from extractor import extract_document
            from models import Document
            documents = []
            for index, path in enumerate(paths, 1):
                log(f"提取单据 {index}/{len(paths)}：{Path(path).name}")
                try:
                    doc = extract_document(path)
                except Exception as exc:
                    doc = Document(path=path, error=str(exc))
                app.documents.append(doc)
                documents.append(doc)
                app.events.append(f"提取单据：{Path(path).name} · {doc.error or doc.category}")
            if use_ai:
                self._organize_ai_documents(app, documents, configuration, log)
        def done(_result):
            self._ai_import_running = False
            prefilled = self._prefill_from_application(app)
            self._refresh_current()
            note = f" · 已从报销申请单填入 {len(prefilled)} 项申报信息" if prefilled else ""
            if use_ai:
                selected = str(len(app.documents) - len(paths))
                self.doc_tree.selection_set(selected)
                self._select_document()
                finished = sum(doc.ai_import.get("status") == "AI 已完成" for doc in app.documents[-len(paths):])
                self.status_text.set(f"AI 导入已处理 {len(paths)} 份 · 模型整理完成 {finished} 份{note} · 可继续添加或确认信息")
            else:
                self.status_text.set(f"已处理 {len(paths)} 个文件{note}，可继续添加材料，或点击下一步确认信息")
        self._start_job("AI 一键导入" if use_ai else "识别单据", worker, done)

    def _prefill_from_application(self, app):
        """Fill blank application inputs from an uploaded 报销申请 attachment (see models)."""
        return app.prefill_from_documents() if app is not None else []

    def change_category(self, _event=None):
        if self.busy or not self._flush_edits():
            return
        doc = self._selected_document()
        if doc and doc.category != self.category_var.get():
            before = doc.category
            doc.category = self.category_var.get()
            if doc.category == "发票" and not doc.invoices:
                doc.invoices.append(Invoice(doc.path, warnings=["人工补录：请对照原件核对全部页面、字段与明细后确认"]))
            if doc.category == "报销申请" and not doc.application_fields:
                from extractor import parse_application
                doc.application_fields = parse_application(doc.text)
            self.current.events.append(f"附件类别修改：{Path(doc.path).name}  {before} → {doc.category}")
            if doc.category == "报销申请":
                self._prefill_from_application(self.current)
            self._invalidate(self.current)
            self._refresh_current()

    def remove_document(self):
        if self.busy or not self._flush_edits():
            return
        doc = self._selected_document()
        if doc:
            self.current.documents.remove(doc)
            self.current.events.append(f"移除单据引用：{Path(doc.path).name}（原文件保留）")
            self._invalidate(self.current)
            self._refresh_current()

    def preview_original(self, from_invoice=False):
        if self.busy:
            return
        doc, page = self._selected_document(), 1
        if from_invoice:
            index = self.invoice_combo.current()
            if 0 <= index < len(self.invoice_choices):
                doc, invoice = self.invoice_choices[index]
                page = invoice.page
        if doc is None:
            messagebox.showinfo("选择材料", "请先选择一份已上传的材料。", parent=self)
            return
        try:
            from preview import DocumentPreview
            viewer = DocumentPreview(self, doc.path, initial_page=page)
        except Exception as exc:
            messagebox.showerror("无法预览原图", f"无法读取这份原件，请确认文件仍在原位置且可以正常打开。\n\n{exc}", parent=self)
            return
        if self.preview_window is not None and self.preview_window.winfo_exists():
            self.preview_window.close()
        self.preview_window = viewer

    def open_original(self):
        if self.busy:
            return
        doc = self._selected_document()
        if doc:
            if Path(doc.path).suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg"):
                messagebox.showerror("无法打开单据", "仅支持打开 PDF、PNG 或 JPG 单据。", parent=self)
                return
            try:
                os.startfile(doc.path)
            except OSError as exc:
                messagebox.showerror("无法打开原文件", str(exc), parent=self)

    def show_evidence(self):
        if self.busy:
            return
        doc = self._selected_document()
        if not doc:
            return
        popup = tk.Toplevel(self)
        popup.title(f"原文与字段依据 · {Path(doc.path).name}")
        popup.geometry("860x650")
        text = tk.Text(popup, wrap="word", padx=15, pady=12, font=("Microsoft YaHei UI", 10), bg=PAPER, fg=INK)
        text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(popup, command=text.yview)
        scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        parts = [f"文件：{doc.path}\n类别：{doc.category}\n页数：{doc.pages}\n识别错误：{doc.error or '无'}\n"]
        for invoice in doc.invoices:
            parts.append(f"\n—— 第 {invoice.page} 页字段依据 ——\n" + "\n".join(f"{FIELD_LABELS.get(key, key)}：{value}" for key, value in invoice.evidence.items()))
            if invoice.warnings:
                parts.append("\n识别提醒：" + "；".join(invoice.warnings))
        parts.append("\n—— 单据识别原文 ——\n" + (doc.text or "无可读取文本"))
        self._put_text(text, "\n".join(parts))

    def policy_dialog(self):
        if self.busy or not self._flush_edits():
            return
        popup = tk.Toplevel(self)
        popup.title("审核规则设置（项目演示制度）")
        popup.resizable(False, False)
        popup.transient(self)
        popup.grab_set()
        frame = ttk.Frame(popup, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="审核口径").grid(row=0, column=0, sticky="w", pady=(0, 12), padx=(0, 15))
        preset_bar = ttk.Frame(frame)
        preset_bar.grid(row=0, column=1, sticky="w", pady=(0, 12))
        variables = [tk.StringVar(value=self.policy.company_name), tk.StringVar(value=self.policy.company_tax_id), tk.StringVar(value=str(self.policy.max_days))]
        for row, (label, variable) in enumerate(zip(("公司名称", "公司税号", "报销期限（天）"), variables), start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=9, padx=(0, 15))
            ttk.Entry(frame, textvariable=variable, width=36).grid(row=row, column=1, sticky="w")
        ttk.Label(frame, text="必需附件").grid(row=4, column=0, sticky="w", pady=12)
        checks = ttk.Frame(frame)
        checks.grid(row=4, column=1, sticky="w")
        required = {name: tk.BooleanVar(value=name in self.policy.required_attachments) for name in CATEGORIES[:3]}
        for index, (name, variable) in enumerate(required.items()):
            ttk.Checkbutton(checks, text=name, variable=variable).grid(row=0, column=index, padx=(0, 8))
        hint = ttk.Label(frame, text="保存后，所有已有审核结果作废，需按新规则重新审核。", foreground="#936007", wraplength=475)
        hint.grid(row=5, column=0, columnspan=2, sticky="w", pady=15)

        def apply_preset(name):
            preset = PRESETS[name]
            variables[2].set(str(preset["max_days"]))
            wanted = set(preset["required_attachments"])
            for label, variable in required.items():
                variable.set(label in wanted)
            hint.configure(text=f"{preset['label']}：{preset['note']} 保存后需重新审核。")

        for column, (name, preset) in enumerate(PRESETS.items()):
            ttk.Button(preset_bar, text="套用" + preset["short"],
                       command=lambda preset_name=name: apply_preset(preset_name)).grid(row=0, column=column, padx=(0, 8))

        def save():
            try:
                days = int(variables[2].get())
                if days < 0 or days > 36500:
                    raise ValueError()
                name, tax_id = variables[0].get().strip(), variables[1].get().strip()
                if not name or not tax_id:
                    raise ValueError()
            except ValueError:
                messagebox.showerror("规则不完整", "请填写公司名称、税号，并输入 0～36500 的整数天数。", parent=popup)
                return
            new_policy = Policy(name, tax_id, days, [name for name, variable in required.items() if variable.get()])
            self._apply_policy(new_policy)
            popup.destroy()
        ttk.Button(frame, text="保存规则", command=save, style="Paper.Primary.TButton").grid(row=6, column=1, sticky="e")
        popup.update_idletasks()
        popup.minsize(popup.winfo_reqwidth(), popup.winfo_reqheight())

    def _apply_policy(self, policy):
        if asdict(policy) != asdict(self.policy):
            self.policy = policy
            for app in self.apps:
                self._invalidate(app)
            self._refresh_policy_summary()
            self._refresh_current()

    def save_project(self):
        if self.busy or not self._flush_edits():
            return
        path = filedialog.asksaveasfilename(parent=self, title="保存项目（含修改记录，不含模型密钥）", defaultextension=".json", initialfile=Path(self.project_path).name if self.project_path else "报销审核项目.json", filetypes=[("项目文件", "*.json")])
        if not path:
            return
        try:
            payload = {"version": 1, "policy": asdict(self.policy), "applications": [app.to_dict() for app in self.apps]}
            target = Path(path)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)
            self.project_path = path
            self.status_text.set(f"项目已保存：{Path(path).name} · 原始单据仍使用原路径")
        except (OSError, ValueError) as exc:
            messagebox.showerror("项目保存失败", str(exc), parent=self)

    def open_project(self):
        if self.busy or not self._flush_edits():
            return
        path = filedialog.askopenfilename(parent=self, title="打开报销审核项目", filetypes=[("项目文件", "*.json")])
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("不支持的项目文件版本")
            policy = Policy(**payload["policy"])
            apps = [Application.from_dict(item) for item in payload["applications"]]
            if len({app.id for app in apps}) != len(apps):
                raise ValueError("项目中的报销编号存在重复")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror("项目打开失败", str(exc), parent=self)
            return
        if self.apps and not messagebox.askyesno("切换项目", "打开后将替换当前列表，请确认当前项目已保存。", parent=self):
            return
        self.policy, self.apps, self.project_path = policy, apps, path
        self.current_id = apps[0].id if apps else None
        self.home_visible = not bool(apps)
        self._refresh_policy_summary()
        self._refresh_list()
        self._refresh_current()
        self.status_text.set(f"已打开项目：{Path(path).name}")

    def audit_current(self):
        self._audit([self.current] if self.current else [])

    def audit_all(self):
        self._audit(list(self.apps))

    def _audit(self, applications):
        if self.busy or not self._flush_edits() or not applications:
            return
        key = self.api_key.get().strip()
        online = self.use_ai.get()
        base_url, model = self.model_base_url.get().strip(), self.model_name.get().strip()
        def worker(log):
            from agent import run_audit
            errors = []
            for index, app in enumerate(applications, 1):
                log(f"审核 {index}/{len(applications)}：{app.id}")
                try:
                    run_audit(app, self.policy, api_key=key, use_ai=online, log=log, base_url=base_url, model=model)
                except Exception as exc:
                    app.checks.clear()
                    app.ai_summary = ""
                    app.mode = "审核失败，待重试"
                    app.events.append(f"审核失败：{exc}")
                    errors.append(app.id)
            return errors
        def done(errors):
            self._refresh_list()
            self._refresh_current()
            self.notebook.select(self.results_tab)
            self.status_text.set(f"已完成 {len(applications)} 笔审核" + (f"，{len(errors)} 笔失败，请查看执行记录" if errors else " · 请选择结果查看原因"))
        self._start_job("审核报销", worker, done)

    def export_current(self):
        self._export([self.current] if self.current else [])

    def export_all(self):
        self._export(list(self.apps))

    def _export(self, applications):
        if self.busy or not self._flush_edits() or not applications:
            return
        if any(not app.checks for app in applications):
            messagebox.showinfo("需要先审核", "存在尚未审核或修改后尚未重新审核的报销，请先审核再导出。", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出审核报告", defaultextension=".pdf", initialfile="报销审核汇总报告.pdf" if len(applications) > 1 else f"{applications[0].id}_审核报告.pdf", filetypes=[("PDF 报告", "*.pdf")])
        if not path:
            return
        def worker(log):
            from report import export_pdf
            log(f"生成 {len(applications)} 笔报销的中文审核报告……")
            export_pdf(applications, self.policy, path)
            for app in applications:
                app.events.append(f"已导出报告：{Path(path).name}")
            return path
        def done(result):
            self._refresh_logs()
            self.status_text.set(f"报告已导出：{result}")
            messagebox.showinfo("报告已导出", f"报告保存在：\n{result}", parent=self)
        self._start_job("导出报告", worker, done)

    def _set_busy(self, busy):
        self.busy = busy
        if busy:
            self._close_model_menu()
        for widget, state in self.controls:
            widget.configure(state="disabled" if busy else state)
        if busy:
            self.progress.pack(side="right", pady=7)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _start_job(self, title, worker, done):
        if self.busy:
            return
        self._set_busy(True)
        self.status_text.set(f"{title}中，请稍候……")
        def log(message):
            self.queue.put(("log", str(message)))
        def run():
            try:
                result = worker(log)
                self.queue.put(("done", done, result))
            except Exception as exc:
                self.queue.put(("error", str(exc)))
        Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                event = self.queue.get_nowait()
                if event[0] == "log":
                    self.status_text.set(event[1])
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", "\n" + event[1])
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                    if self._ai_import_running:
                        self.ai_import_state.set(event[1])
                        self.ai_import_log.configure(state="normal")
                        self.ai_import_log.insert("end", event[1] + "\n")
                        self.ai_import_log.see("end")
                        self.ai_import_log.configure(state="disabled")
                elif event[0] == "done":
                    self._set_busy(False)
                    event[1](event[2])
                elif event[0] == "error":
                    self._set_busy(False)
                    self._ai_import_running = False
                    self._refresh_list()
                    self._refresh_current()
                    self.status_text.set("本次操作失败，可修正后重试")
                    if self.smoke_failure:
                        self.smoke_failure(RuntimeError(event[1]))
                        return
                    messagebox.showerror("操作失败", event[1], parent=self)
        except Empty:
            pass
        self._poll_job = self.after(100, self._poll)

    def _close(self):
        self._close_model_menu()
        if self.busy:
            messagebox.showinfo("任务进行中", "请等待当前识别、审核或导出任务完成后关闭。", parent=self)
            return
        if self.apps and not messagebox.askyesno("关闭程序", "请确认项目已保存。现在关闭程序？", parent=self):
            return
        self.destroy()

    def destroy(self):
        self._close_model_menu()
        assistant = getattr(self, "assistant", None)
        if assistant is not None:
            self.assistant = None
            try:
                assistant.destroy()
            except tk.TclError:
                pass
        self.motion.stop()
        if getattr(self, "_poll_job", None):
            self.after_cancel(self._poll_job)
            self._poll_job = None
        super().destroy()

    def _ensure_window_shown(self):
        """Automated launchers can start us hidden or minimized; force a visible window."""
        try:
            self.state("normal")
        except Exception:
            pass
        self.deiconify()
        self.update()
        if os.name == "nt" and not self.winfo_ismapped():
            try:
                ctypes.windll.user32.ShowWindow(int(self.wm_frame(), 16), 9)  # SW_RESTORE
            except Exception:
                pass
            self.update()

    def run_smoke(self, prefix):
        """Real GUI smoke with screenshots; failure exits nonzero for automated checks."""
        prefix = Path(prefix)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        started = datetime.now()
        self._ensure_window_shown()
        self.api_key.set("")
        self.use_ai.set(False)
        checks = {}

        def fail(exc):
            import traceback
            prefix.with_suffix(".error.txt").write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")
            self.destroy()
            raise SystemExit(1)

        self.smoke_failure = fail

        def when_idle(callback):
            if self.busy:
                if (datetime.now() - started).total_seconds() > 240:
                    fail(TimeoutError("GUI 验收超过 240 秒"))
                self.after(300, lambda: when_idle(callback))
                return
            try:
                callback()
            except Exception as exc:
                fail(exc)

        def loaded():
            assert len(self.apps) == 10, f"应有 10 笔演示，实际 {len(self.apps)}"
            assert len(self.notebook.tabs()) == 4
            assert all(app.invoices for app in self.apps), "10 笔原票应全部独立识别"
            assert all(self.field_entries[key].winfo_exists() for key in FIELD_LABELS)
            checks["loaded_10_original_invoices"] = True
            self._ensure_window_shown()
            assert set(self.nav_buttons) == {"主页", "新建报销", "打开项目", "保存项目", "审核规则", "高级设置"}
            # Narrow windows fold secondary entries into 更多; every entry must still be reachable.
            # Layout (who is packed) is checked instead of mapping, so headless or
            # not-yet-shown windows do not produce false negatives.
            stuck = [label for label, button in self.nav_buttons.items()
                     if not button.winfo_manager() and not self.nav_overflow.winfo_manager()]
            assert not stuck, f"顶部导航未布局：{stuck}；布局={self._nav_density}/{self._nav_active}"
            assert self.model_chip.winfo_manager(), "接入方式胶囊未布局"
            assert self.model_chip.winfo_reqwidth() > 100, "接入方式胶囊宽度异常"
            if self.model_chip.winfo_ismapped():
                assert self.model_chip.winfo_width() > 40
                assert (self.model_chip.winfo_rootx() + self.model_chip.winfo_width()
                        <= self.winfo_rootx() + self.winfo_width()), "接入方式胶囊超出窗口右边界"
            checks["top_navigation_visible"] = True
            checks["model_chip_pinned_inside_window"] = True
            current_id = self.current_id
            self.show_step(1)
            selected_tab = self.notebook.select()
            self.nav_buttons["主页"].invoke()
            self.update()
            assert self.home_visible and self.welcome.winfo_viewable() and self.home_continue_button.winfo_viewable(), (
                f"主页未显示：state={self.state()} home={self.home_visible} "
                f"welcome={self.welcome.winfo_viewable()} 继续按钮={self.home_continue_button.winfo_viewable()}")
            assert self.home_continue_button.cget("text") == "继续"
            assert not any(button.winfo_viewable() for button in self.nav_buttons.values())
            assert not self.sidebar.winfo_viewable() and not self.work_area.winfo_viewable()
            screenshot("_主页继续")
            self.home_continue_button.invoke()
            self.update()
            assert not self.home_visible and self.current_id == current_id and self.notebook.select() == selected_tab
            assert self.work_area.winfo_viewable()
            checks["home_roundtrip_preserves_current_task_and_step"] = True
            self.show_step(0)
            self.audit_all()
            self.after(300, lambda: when_idle(audited))

        def audited():
            from decimal import Decimal
            from unittest.mock import patch
            from models import Check, Invoice
            assert all(app.checks for app in self.apps), "全部报销应具有真实审核结果"
            checks["audited_all"] = True
            app = self.current
            original_amount = app.requested_amount
            self.info_vars["requested_amount"].set(str(Decimal(original_amount) + 1))
            assert not app.checks, "修改申请金额必须清空旧审核结果"
            self.info_vars["requested_amount"].set(original_amount)
            checks["application_edits_invalidate_checks"] = True
            inv = self.edit_invoice
            original_value = inv.fields.get("buyer_name", "")
            original_confirmed = list(inv.confirmed)
            self.field_vars["buyer_name"].set(original_value + "（界面验收）")
            assert self.save_invoice_fields(quiet=True)
            assert inv.edits and inv.edits[-1]["after"].endswith("（界面验收）")
            self.field_vars["buyer_name"].set(original_value)
            assert self.save_invoice_fields(quiet=True)
            assert inv.fields["buyer_name"] == original_value
            inv.confirmed = original_confirmed
            checks["field_edits_have_history_and_restore"] = True
            original_items = json.loads(json.dumps(inv.items))
            self.item_dialog(new=True)
            popup = next(child for child in self.winfo_children()
                         if isinstance(child, tk.Toplevel) and not getattr(child, "assistant_window", False))
            frame = popup.winfo_children()[0]
            entries = sorted((child for child in frame.winfo_children() if isinstance(child, ttk.Entry)), key=lambda child: int(child.grid_info()["row"]))
            for entry, value in zip(entries, ("界面验收项目", "2", "5.00", "10.00", "0%", "0.00")):
                entry.delete(0, "end")
                entry.insert(0, value)
            next(child for child in frame.winfo_children() if isinstance(child, ttk.Button)).invoke()
            assert self.items_buffer[-1]["name"] == "界面验收项目" and self.fields_dirty
            assert self.save_invoice_fields(quiet=True)
            assert inv.edits[-1]["field"] == "items"
            self.items_buffer = original_items
            self._field_changed()
            assert self.save_invoice_fields(quiet=True)
            assert inv.items == original_items
            checks["chinese_item_editor_saves_and_restores"] = True
            original_warnings = list(inv.warnings)
            inv.warnings = ["界面验收：核对多页明细"]
            inv.confirmed = [key for key in original_confirmed if key != "pages"]
            self._display_invoice(inv)
            assert self.save_invoice_fields()
            assert inv.warnings and "items" in inv.confirmed and "pages" not in inv.confirmed
            self.confirm_pages.set(True)
            assert self.save_invoice_fields()
            assert not inv.warnings and "pages" in inv.confirmed
            assert any("原识别提醒：界面验收：核对多页明细" in event for event in app.events)
            inv.warnings, inv.confirmed = original_warnings, original_confirmed
            self._display_invoice(inv)
            checks["page_warnings_need_explicit_confirmation_and_audit_event"] = True
            self._set_busy(True)
            assert all(str(widget.cget("state")) == "disabled" for widget, _state in self.controls)
            self.update_idletasks()
            assert self.progress.winfo_viewable()
            self._set_busy(False)
            self.update_idletasks()
            assert not self.progress.winfo_viewable()
            checks["background_work_locks_all_mutations"] = True
            old_policy = self.policy
            self._apply_policy(Policy(old_policy.company_name, old_policy.company_tax_id, old_policy.max_days + 1, list(old_policy.required_attachments)))
            assert all(not app.checks for app in self.apps), "修改规则必须清空全部旧审核结果"
            self._apply_policy(old_policy)
            checks["policy_edits_invalidate_all_checks"] = True
            check = Check("test", "分页匹配", FAIL, source="分页验收.pdf 第2页", field="total")
            assert not self._check_matches_invoice(check, Invoice("分页验收.pdf", page=1))
            assert self._check_matches_invoice(check, Invoice("分页验收.pdf", page=2))
            checks["same_file_pages_highlight_separately"] = True
            project = str(prefix) + "_项目往返.json"
            marker = "GUI_SMOKE_KEY_MUST_NOT_BE_SAVED"
            self.api_key.set(marker)
            with patch("ui.filedialog.asksaveasfilename", return_value=project):
                self.save_project()
            payload = Path(project).read_text(encoding="utf-8")
            assert marker not in payload and "api_key" not in payload
            with patch("ui.filedialog.askopenfilename", return_value=project), patch("ui.messagebox.askyesno", return_value=True):
                self.open_project()
            assert len(self.apps) == 10 and self.current.invoices[0].edits
            self.api_key.set("")
            checks["project_roundtrip_with_edits_without_key"] = True
            self.audit_all()
            self.after(300, lambda: when_idle(start_ai_import))

        def start_ai_import():
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            doc = self.current.documents[0]
            invoice = doc.invoices[0]
            proof = re.search(r"发票号码[:：]\s*\d+", doc.text).group(0)
            requests = []
            answer = {"summary": "已核对原文中的发票号码，保留已有值。", "field_suggestions": [
                {"page": invoice.page, "field": "invoice_number", "value": invoice.fields["invoice_number"], "evidence": proof}]}

            class Handler(BaseHTTPRequestHandler):
                def do_POST(handler):
                    requests.append(json.loads(handler.rfile.read(int(handler.headers["Content-Length"]))))
                    message = {"tool_calls": [{"id": "portable-ai-import", "type": "function", "function": {
                        "name": "organize_document", "arguments": json.dumps(answer, ensure_ascii=False)}}]}
                    body = json.dumps({"choices": [{"message": message}]}).encode("utf-8")
                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/json")
                    handler.send_header("Content-Length", str(len(body)))
                    handler.end_headers()
                    handler.wfile.write(body)

                def log_message(handler, *_args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            Thread(target=lambda: server.serve_forever(poll_interval=.1), daemon=True).start()
            previous = (self.model_base_url.get(), self.model_name.get(), self.api_key.get())
            original_fields = dict(invoice.fields)
            self.model_base_url.set(f"http://127.0.0.1:{server.server_port}/v1")
            self.model_name.set("portable-ai-import-check")
            self.api_key.set("")
            self.show_step(0)
            self.organize_existing_documents()

            def imported():
                server.shutdown()
                server.server_close()
                assert len(requests) == 1 and requests[0]["model"] == "portable-ai-import-check"
                assert doc.ai_import["status"] == "AI 已完成" and doc.ai_import["fields"]
                sent = json.loads(requests[0]["messages"][-1]["content"])
                assert proof in sent["pages"][0]["text"]
                assert invoice.fields == original_fields
                assert self.ai_import_tree.get_children() and self.notebook.select() == str(self.info_tab)
                row = self.ai_import_tree.get_children()[0]
                self.ai_import_tree.selection_set(row)
                self._show_ai_import_detail()
                assert proof in self.ai_import_detail.get("1.0", "end")
                checks["ai_import_real_request_uses_selected_model_and_ocr"] = True
                checks["ai_import_preserves_existing_fields"] = True
                checks["ai_import_workbench_shows_evidence_and_keeps_step"] = True
                screenshot("_AI导入")
                self.model_base_url.set(previous[0])
                self.model_name.set(previous[1])
                self.api_key.set(previous[2])
                self.audit_all()
                self.after(300, lambda: when_idle(prepare_capture))

            self.after(300, lambda: when_idle(imported))

        def prepare_capture():
            assert all(app.checks for app in self.apps)
            self.notebook.select(self.info_tab)
            self.lift()
            self.update_idletasks()
            self.after(500, lambda: when_idle(capture_import))

        capture_notes = []

        def _foreground_title():
            """Who is in front right now - only used to explain a bad capture."""
            try:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                return buf.value
            except Exception:
                return ""

        def _all_black(image):
            try:
                return image.convert("RGB").getextrema() == ((0, 0), (0, 0), (0, 0))
            except Exception:
                return False

        def _grab(rect, suffix, window=None, topmost=False):
            """BitBlt sometimes hands back an all-black frame while the window is
            still compositing or another window briefly covers it. Keep the app on
            top and retry, so evidence screenshots are never blank."""
            import time
            from PIL import ImageGrab
            previous = self.attributes("-topmost") if topmost else None
            image = None
            try:
                for attempt in range(8):
                    if topmost:
                        self.attributes("-topmost", True)
                    self.lift()
                    if window is not None:
                        window.update_idletasks()
                        window.update()
                    else:
                        self.update()
                    image = ImageGrab.grab(bbox=rect)
                    if not _all_black(image):
                        return image
                    time.sleep(0.25)
            finally:
                if previous is not None:
                    self.attributes("-topmost", previous)
            capture_notes.append({"shot": suffix, "foreground": _foreground_title(),
                                  "attempts": 8, "rect": list(rect)})
            return image

        def screenshot(suffix):
            rect = (self.winfo_rootx(), self.winfo_rooty(), self.winfo_rootx() + self.winfo_width(), self.winfo_rooty() + self.winfo_height())
            _grab(rect, suffix, topmost=True).save(str(prefix) + suffix + ".png")
            return list(rect)

        def capture_import():
            screenshot("_导入")
            self.notebook.select(self.docs_tab)
            self.update_idletasks()
            self.after(500, lambda: when_idle(capture_documents))

        def capture_documents():
            screenshot("_识别")
            self.notebook.select(self.results_tab)
            self.update_idletasks()
            self.after(500, lambda: when_idle(capture_results))

        def capture_results():
            screenshot("_审核结果")
            self.notebook.select(self.log_tab)
            self.update_idletasks()
            self.after(500, lambda: when_idle(capture_assistant))

        def screenshot_window(window, suffix):
            window.update_idletasks()
            window.update()
            rect = (window.winfo_rootx(), window.winfo_rooty(),
                    window.winfo_rootx() + window.winfo_width(), window.winfo_rooty() + window.winfo_height())
            _grab(rect, suffix, window, topmost=True).save(str(prefix) + suffix + ".png")
            return rect

        def capture_assistant():
            import time
            self._ensure_window_shown()
            ball = getattr(self, "assistant", None)
            assert ball is not None, "校验能手悬浮球未创建"
            ball.show()
            ball.reset_position()   # assert the shipped default, not a dragged one
            self.update()
            assert ball.top.winfo_ismapped(), "悬浮球未显示"
            size = ball.top.winfo_width()
            assert size >= 40, f"悬浮球尺寸异常：{size}"
            # The whole point: it lives inside this window, never over the desktop.
            left, top = self.winfo_rootx(), self.winfo_rooty()
            right, bottom = left + self.winfo_width(), top + self.winfo_height()
            bx, by = ball.top.winfo_rootx(), ball.top.winfo_rooty()
            assert left <= bx and bx + size <= right, "悬浮球超出主窗口左右边界"
            assert top <= by and by + size <= bottom, "悬浮球超出主窗口上下边界"
            assert not any(getattr(child, "assistant_window", False) for child in self.winfo_children()), (
                "校验能手不应再创建独立顶层窗口")
            # A helper must never sit on top of the primary action.
            primary = self.flow_primary
            qx, qy = primary.winfo_rootx(), primary.winfo_rooty()
            qw, qh = primary.winfo_width(), primary.winfo_height()
            overlap_x = min(bx + size, qx + qw) - max(bx, qx)
            overlap_y = min(by + size, qy + qh) - max(by, qy)
            assert overlap_x <= 0 or overlap_y <= 0, "悬浮球压住了主操作按钮"
            checks["assistant_ball_clears_primary_action"] = True
            screenshot_window(ball.top, "_校验能手悬浮球")
            checks["assistant_ball_inside_window"] = True

            ball.open_chat()
            self.update()
            window = ball.window
            deadline = time.time() + 5
            while time.time() < deadline:
                self.update()
                self.update_idletasks()
                if window is not None and window.top.winfo_ismapped():
                    break
                time.sleep(0.03)
            assert window is not None and window.top.winfo_ismapped(), (
                f"对话面板未显示：exists={window is not None}, "
                f"mapped={window.top.winfo_ismapped() if window is not None else None}, "
                f"place={window.top.place_info() if window is not None else None}, "
                f"app={self.winfo_width()}x{self.winfo_height()}")
            waiting = time.time() + 4
            while not window.send_button.winfo_reqwidth() > 20 and time.time() < waiting:
                self.update()
                time.sleep(0.05)
            assert window.entry.winfo_manager(), "对话面板输入框未布局"
            assert window.send_button.winfo_reqwidth() > 20, "对话面板发送按钮未布局"
            assert window.rows, "对话面板消息区为空"
            assert "财税助手" in window.mode_text, window.mode_text
            px, py = window.top.winfo_rootx(), window.top.winfo_rooty()
            assert left <= px and px + window.width <= right, "对话面板超出主窗口"
            assert top <= py and py + window.height <= bottom, "对话面板超出主窗口"
            checks["assistant_panel_opens_inside_window"] = True

            previous = self.use_ai.get()
            self.use_ai.set(False)  # offline path: quote clauses, no network at all
            started = time.time()
            window.send("报销时限是多久？")
            deadline = time.time() + 20
            while window.sending and time.time() < deadline:
                self.update()
                time.sleep(0.02)
            elapsed = time.time() - started
            assert not window.sending, "校验能手离线回答超时"
            answer = window.last_answer
            assert "90" in answer and "报销时限" in answer, answer[:120]
            assert elapsed < 0.5, f"离线回答未能即时给出：{elapsed:.2f}s"
            self.use_ai.set(previous)
            assert window.history and window.history[-1]["role"] == "assistant"
            screenshot_window(window.top, "_校验能手对话")
            checks["assistant_answers_instantly"] = True

            window.hide()
            self.update()
            assert ball.top.winfo_ismapped(), "关闭对话后悬浮球未恢复"
            checks["assistant_ball_restores"] = True
            self.after(300, lambda: when_idle(capture_export))

        def capture_export():
            rect = screenshot("_导出")
            prefix.with_suffix(".json").write_text(json.dumps({"applications": len(self.apps), "tabs": 4, "invoice_fields": len(self.field_entries), "window": rect,
                "status": "GUI 已启动、10 笔原票已识别审核，修改及保存往返检查通过", "checks": checks,
                "capture_notes": capture_notes}, ensure_ascii=False, indent=2), encoding="utf-8")
            self.destroy()

        def start():
            self._ensure_window_shown()
            screenshot("_欢迎")
            self.update_idletasks()
            # 演示入口曾经只存在于任务里，首屏截图上根本看不到；这条检查防止它再被藏回去。
            assert self.home_demo_button.winfo_viewable(), "首屏缺少演示入口"
            checks["home_demo_entry_visible"] = True
            self.home_new_button.invoke()
            assert self.current and not self.home_visible
            checks["home_start_opens_new_task"] = True
            self.load_demo()
            self.after(500, lambda: when_idle(loaded))

        self.after(500, lambda: when_idle(start))
