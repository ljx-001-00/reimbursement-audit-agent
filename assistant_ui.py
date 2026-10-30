"""In-window 校验能手 assistant.

Both the round ball and the chat panel are ordinary children of the main window:
nothing is created as a desktop window, so the assistant can never float over
other applications. Every visual is rendered with PIL and shown on a Canvas,
which keeps rounded corners, gradients and text antialiased.

Answer flow, tuned for the impression of speed:

1. retrieval answers in about a millisecond and is painted immediately;
2. only then does a worker thread ask the model for a shorter, warmer wording;
3. when the model comes back the same bubble is rewritten in place.
"""
import json
import os
import re
import time
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

from PIL import Image, ImageDraw, ImageFont, ImageTk

import assistant
from glacier import PAPER
from model_settings import profile_path
from models import ROOT

# ---------------------------------------------------------------- palette ----
VIOLET = "#7C5CFF"
VIOLET_DEEP = "#5B3FE0"
CYAN = "#22D3EE"
SKY = "#4FC3F7"
TEXT = "#241E3A"
SOFT = "#8D85A6"
FAINT = "#AFA7C4"
BODY = "#FBF9FF"
CARD = "#FFFFFF"
CHIP_BG = "#F4F0FF"
CHIP_FG = "#6C5CE7"
CHIP_HOVER = "#E9E1FF"
INPUT_BG = "#F5F2FF"
INPUT_EDGE = "#E6DEFF"
DIVIDER = "#F2EDFF"

FONT = "Microsoft YaHei UI"
SS = 2                      # supersample factor for every PIL render
BALL_SIZE = 58
PANEL_W = 408
PANEL_H = 552
HEADER_H = 84
COMPOSER_H = 130
# Docked clear of the action row below and inside the white card above.
BALL_DX = 46
BALL_DY = 104
# Dragging the ball: at most one geometry update per DRAG_INTERVAL seconds, with a
# trailing update so the ball always lands exactly under the cursor on release.
DRAG_INTERVAL = 0.008
DRAG_TRAILING_MS = 12

_FONTS = {}
_IMAGES = {}


def state_path():
    return profile_path().with_name("assistant.json")


def load_state():
    try:
        payload = json.loads(state_path().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    payload = load_state()
    payload.update({key: value for key, value in changes.items() if value is not None})
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def tidy_cjk(value):
    """Flatten model markdown for a plain-text bubble.

    Tk breaks a line at every space, so "超过 90 天" would split as "超过" /
    "90 天"; markdown list markers would leave a lone "-" on its own line.
    """
    text = str(value).replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]*", "", text)
    text = re.sub(r"(?m)^[ \t]*[-*+][ \t]+", "·", text)
    text = re.sub(r"(?m)^[ \t]*(\d+)[.、)][ \t]+", r"\1.", text)
    text = re.sub(r"(?<=[0-9%\uff09\uff0c])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[0-9])", "", text)


def _remember(key, image):
    if len(_IMAGES) > 260:
        _IMAGES.clear()
    _IMAGES[key] = image
    return image


# ---------------------------------------------------------------- drawing ----
def _rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _font(px, bold=False):
    key = (int(px), bool(bold))
    cached = _FONTS.get(key)
    if cached is not None:
        return cached
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = ([windir / "msyhbd.ttc", windir / "msyh.ttc"] if bold
                  else [windir / "msyh.ttc", windir / "msyhl.ttc"])
    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(str(path), int(px))
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.truetype(str(ROOT / "assets" / "font.ttf"), int(px))
    _FONTS[key] = font
    return font


def _mark():
    cached = _IMAGES.get("mark")
    if cached is None:
        cached = _remember("mark", Image.open(ROOT / "assets" / "assistant_mark.png").convert("RGBA"))
    return cached


def _gradient(width, height, start, end):
    seed = Image.new("RGB", (2, 2))
    a, b = _rgb(start), _rgb(end)
    seed.putpixel((0, 0), a)
    seed.putpixel((1, 0), b)
    seed.putpixel((0, 1), a)
    seed.putpixel((1, 1), b)
    return seed.resize((max(1, width), max(1, height)), Image.BILINEAR)


def _card(width, height, radius, fill=None, gradient=None, outline=None, surface=PAPER):
    """Rounded card composited onto `surface`, so its corners blend invisibly."""
    width, height = max(1, int(width)), max(1, int(height))
    box = (width * SS, height * SS)
    base = Image.new("RGB", box, _rgb(surface))
    mask = Image.new("L", box, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, box[0] - 1, box[1] - 1),
                                           radius=radius * SS, fill=255)
    paint = _gradient(box[0], box[1], *gradient) if gradient else Image.new("RGB", box, _rgb(fill))
    base.paste(paint, (0, 0), mask)
    if outline:
        edge = Image.new("RGB", box, _rgb(surface))
        edge_mask = Image.new("L", box, 0)
        ImageDraw.Draw(edge_mask).rounded_rectangle((0, 0, box[0] - 1, box[1] - 1),
                                                    radius=radius * SS, outline=255, width=SS)
        base.paste(edge, (0, 0), edge_mask)
    return base.resize((width, height), Image.LANCZOS)


def ball_image(size, surface=PAPER, hover=False):
    key = ("ball", size, surface, hover)
    cached = _IMAGES.get(key)
    if cached is not None:
        return cached
    big = size * 4
    tile = Image.new("RGB", (big, big), _rgb(surface))
    draw = ImageDraw.Draw(tile)
    ring = max(2, big // 26) + (big // 60 if hover else 0)
    draw.ellipse((big * .03, big * .05, big * 1.0, big * 1.02), fill=_rgb("#E4DAFF" if hover else "#E9E1FF"))
    draw.ellipse((big * .05, big * .05, big * .95, big * .95), fill=(255, 255, 255))
    box = (big * .05, big * .05, big * .95, big * .95)
    # A continuous base ring keeps the two gradient arcs from leaving a gap.
    draw.arc(box, 0, 360, fill=_rgb("#6E74FF"), width=ring)
    draw.arc(box, 118, 300, fill=_rgb(VIOLET), width=ring)
    draw.arc(box, 296, 96, fill=_rgb(CYAN), width=ring)
    inner = int(big * .60)
    logo = _mark().copy()
    logo.thumbnail((inner, inner), Image.LANCZOS)
    tile.paste(logo, (int((big - logo.size[0]) / 2), int((big - logo.size[1]) / 2)), logo)
    return _remember(key, tile.resize((size, size), Image.LANCZOS))


def header_image(width, height, mode, surface=PAPER):
    key = ("header", width, height, mode, surface)
    cached = _IMAGES.get(key)
    if cached is not None:
        return cached
    box = (width * SS, height * SS)
    radius = 20 * SS

    base = Image.new("RGB", box, _rgb(surface))
    card = Image.new("L", box, 0)
    ImageDraw.Draw(card).rounded_rectangle((0, 0, box[0] - 1, box[1] - 1), radius=radius, fill=255)
    base.paste(Image.new("RGB", box, _rgb(CARD)), (0, 0), card)

    band = _gradient(box[0], box[1], VIOLET, SKY)
    band_mask = Image.new("L", box, 0)
    ImageDraw.Draw(band_mask).rounded_rectangle((0, 0, box[0] - 1, box[1] + 60 * SS),
                                                radius=radius, fill=255)
    base.paste(band, (0, 0), band_mask)

    overlay = Image.new("RGBA", box, (0, 0, 0, 0))
    brush = ImageDraw.Draw(overlay)
    for cx, cy, dot, alpha in ((width * .84, 6, 58, 24), (width * .97, 62, 42, 18),
                               (width * .68, 76, 24, 14)):
        brush.ellipse(((cx - dot) * SS, (cy - dot) * SS,
                       (cx + dot) * SS, (cy + dot) * SS), fill=(255, 255, 255, alpha))
    for center in (width - 25, width - 55):
        brush.ellipse(((center - 13) * SS, (height / 2 - 13) * SS,
                       (center + 13) * SS, (height / 2 + 13) * SS), fill=(255, 255, 255, 46))
    base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(base)

    cx, cy, dot = 42, height / 2, 21
    draw.ellipse(((cx - dot) * SS, (cy - dot) * SS, (cx + dot) * SS, (cy + dot) * SS),
                 fill=(255, 255, 255))
    logo = _mark().copy()
    inner = int(dot * 1.5 * SS)
    logo.thumbnail((inner, inner), Image.LANCZOS)
    base.paste(logo, (int(cx * SS - logo.size[0] / 2), int(cy * SS - logo.size[1] / 2)), logo)

    draw.text(((cx + dot + 13) * SS, 25 * SS), "校验能手", font=_font(17 * SS, True),
              fill=(255, 255, 255), anchor="la")
    draw.text(((cx + dot + 13) * SS, 49 * SS), mode, font=_font(10 * SS),
              fill=(255, 255, 255), anchor="la")

    # Close / collapse are drawn, not typed: the glyph may be missing from the font.
    pen = max(1, int(1.6 * SS))
    for index, center in enumerate((width - 25, width - 55)):
        mid = height / 2 * SS
        if index == 0:
            pad = 4.5 * SS
            draw.line((center * SS - pad, mid - pad, center * SS + pad, mid + pad),
                      fill=(255, 255, 255), width=pen)
            draw.line((center * SS - pad, mid + pad, center * SS + pad, mid - pad),
                      fill=(255, 255, 255), width=pen)
        else:
            pad = 5 * SS
            draw.line((center * SS - pad, mid, center * SS + pad, mid),
                      fill=(255, 255, 255), width=pen)
    return _remember(key, base.resize((width, height), Image.LANCZOS))


def composer_image(width, height, surface=PAPER):
    key = ("composer", width, height, surface)
    cached = _IMAGES.get(key)
    if cached is not None:
        return cached
    flat = _card(width, height, 20, fill=CARD, surface=surface)
    return _remember(key, flat)


def pill_image(text, active=False, scale=1.0):
    key = ("pill", text, active, round(scale, 2))
    cached = _IMAGES.get(key)
    if cached is not None:
        return cached
    font = _font(round(9.5 * scale * SS))
    pad_x, height = round(12 * scale), round(27 * scale)
    width = round(max(62 * scale, font.getlength(text) / SS + pad_x * 2))
    big = (width * SS, height * SS)
    tile = Image.new("RGB", big, _rgb(CARD))
    mask = Image.new("L", big, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big[0] - 1, big[1] - 1),
                                           radius=height * SS // 2, fill=255)
    tile.paste(Image.new("RGB", big, _rgb(CHIP_HOVER if active else CHIP_BG)), (0, 0), mask)
    ImageDraw.Draw(tile).text((big[0] / 2, big[1] / 2), text, font=font,
                              fill=_rgb(VIOLET_DEEP if active else CHIP_FG), anchor="mm")
    return _remember(key, tile.resize((width, height), Image.LANCZOS))


def send_image(size, ready):
    key = ("send", size, ready)
    cached = _IMAGES.get(key)
    if cached is not None:
        return cached
    big = size * 4
    tile = Image.new("RGB", (big, big), _rgb(CARD))
    draw = ImageDraw.Draw(tile)
    if ready:
        grad = _gradient(big, big, VIOLET, CYAN)
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
        tile.paste(grad, (0, 0), mask)
        draw = ImageDraw.Draw(tile)
    else:
        draw.ellipse((0, 0, big - 1, big - 1), fill=_rgb("#EAE5F8"))
    mid = big / 2
    pen = max(2, big // 14)
    top, bottom = mid - big * .15, mid + big * .13
    draw.line((mid, bottom, mid, top), fill=(255, 255, 255), width=pen)
    draw.line((mid - big * .10, mid - big * .04, mid, top), fill=(255, 255, 255), width=pen)
    draw.line((mid + big * .10, mid - big * .04, mid, top), fill=(255, 255, 255), width=pen)
    return _remember(key, tile.resize((size, size), Image.LANCZOS))


TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,%/:_-]*|\s+|.")
NO_LINE_START = set("。，、；：？！）》」』%‰)]】")


def wrap_text(text, font, max_px):
    """Greedy wrap that keeps latin words whole and never starts a line with 」,。"""
    lines = []
    for raw in text.split("\n"):
        if not raw.strip():
            lines.append("")
            continue
        line = ""
        for token in TOKEN.findall(raw):
            trial = line + token
            if line and font.getlength(trial) > max_px:
                if token and token[0] in NO_LINE_START:
                    lines.append(trial.rstrip())
                    line = ""
                    continue
                lines.append(line.rstrip())
                line = "" if token.isspace() else token
            else:
                line = trial
        lines.append(line.rstrip())
    return lines


def _line_height(font):
    ascent, descent = font.getmetrics()
    return (ascent + descent) / SS * 1.30


def bubble_image(text, max_width, role="ai", scale=1.0, typing=0):
    """Rounded bubble drawn with PIL: gradient for the user, white for the AI.

    A line wrapped in ** is rendered as the clause heading in accent colour.
    """
    key = ("bubble", text, max_width, role, round(scale, 2), typing)
    cached = _IMAGES.get(key)
    if cached is not None:
        return cached

    body_px = 12.5 * scale
    pad_x, pad_y, radius = 15 * scale, 12 * scale, 17 * scale
    inner = max(60 * scale, max_width - pad_x * 2)
    body_font = _font(round(body_px * SS))
    head_font = _font(round(body_px * SS), True)
    text_color = (255, 255, 255) if role == "user" else _rgb(TEXT)
    head_color = (255, 255, 255) if role == "user" else _rgb(VIOLET_DEEP)

    visual = []        # (text, font, colour, gap_before)
    for raw in str(text).split("\n"):
        stripped = raw.strip()
        if not stripped:
            visual.append(("", body_font, text_color, 7 * scale))
            continue
        heading = re.match(r"^\*\*(.+?)\*\*$", stripped)
        font, colour = (head_font, head_color) if heading else (body_font, text_color)
        chunk = heading.group(1) if heading else tidy_cjk(stripped)
        gap = 9 * scale if heading and visual else 0
        for line in wrap_text(chunk, font, inner * SS):
            visual.append((line, font, colour, gap))
            gap = 0

    if typing:
        visual = [("", body_font, text_color, 0)]

    heights = [_line_height(font) for _t, font, _c, _g in visual]
    total_h = pad_y * 2 + sum(heights) + sum(row[3] for row in visual)
    measured = max((font.getlength(line) / SS for line, font, _c, _g in visual), default=0)
    if typing:
        width = 66 * scale
    else:
        width = max(96 * scale, min(max_width, measured + pad_x * 2 + 2))
    width, height = int(width), max(1, int(round(total_h)))

    box = (width * SS, height * SS)
    base = Image.new("RGB", box, _rgb(BODY))
    mask = Image.new("L", box, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, box[0] - 1, box[1] - 1),
                                           radius=int(radius * SS), fill=255)
    if role == "user":
        base.paste(_gradient(box[0], box[1], VIOLET, SKY), (0, 0), mask)
    else:
        base.paste(Image.new("RGB", box, _rgb(CARD)), (0, 0), mask)
        edge_mask = Image.new("L", box, 0)
        ImageDraw.Draw(edge_mask).rounded_rectangle((0, 0, box[0] - 1, box[1] - 1),
                                                    radius=int(radius * SS), outline=255, width=SS)
        base.paste(Image.new("RGB", box, _rgb("#EFE8FF")), (0, 0), edge_mask)

    draw = ImageDraw.Draw(base)
    cursor = pad_y * SS
    if typing:
        for index in range(3):
            phase = (typing + index) % 4
            lift = (0, -2.2, 0, 1.2)[phase] * scale * SS
            dot = 4.0 * scale * SS
            cx = (pad_x + 8 * scale + index * 11 * scale) * SS
            cy = cursor + heights[0] * SS * 0.5 + lift
            shade = (150, 175, 205, 240)[phase]
            draw.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=(shade, shade, shade + 8))
    else:
        for line, font, colour, gap in visual:
            cursor += gap * SS
            if line:
                draw.text((pad_x * SS, cursor), line, font=font, fill=colour, anchor="la")
            cursor += _line_height(font) * SS

    return _remember(key, base.resize((width, height), Image.LANCZOS))

class _Row:
    """One message: a bubble image plus an optional meta line under it."""

    def __init__(self, panel, role, text, meta="", typing=0):
        self.panel = panel
        self.role = role
        self.text = text
        self.meta = meta
        self.typing = typing
        self.image_id = None
        self.meta_id = None
        self.photo = None
        self.y = 0
        self.image = None
        self._render()

    def _render(self):
        panel = self.panel
        self.image = bubble_image(self.text, panel.bubble_width, self.role,
                                  panel.scale, self.typing)
        self.photo = ImageTk.PhotoImage(self.image, master=panel.top)
        if self.image_id is None:
            self.image_id = panel.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        else:
            panel.canvas.itemconfigure(self.image_id, image=self.photo)

    def set_text(self, text, meta=None):
        self.text = text
        self.typing = 0
        if meta is not None:
            self.meta = meta
        self._render()
        self.panel.relayout()

    def set_meta(self, meta):
        self.meta = meta
        self.panel.relayout()


class ChatPanel:
    """The chat card, docked inside the main window above the ball."""

    def __init__(self, ball):
        self.ball = ball
        self.app = ball.app
        self.scale = ball.scale
        self.surface = ball.surface
        self.width = int(PANEL_W * ball.scale)
        self.height = int(PANEL_H * ball.scale)
        self.header_h = int(HEADER_H * ball.scale)
        self.composer_h = int(COMPOSER_H * ball.scale)
        self.body_h = self.height - self.header_h - self.composer_h
        self.bubble_width = int(self.width * 0.78)
        self.queue = Queue()
        self.history = []
        self.rows = []
        self.sending = False
        self.visible = False
        self.last_answer = ""
        self.mode_text = ""
        self._thinking = False
        self._typing_job = None
        self._typing_row = None
        self._pending_row = None
        self._placeholder_on = False
        self._chip_photos = {}

        self.top = tk.Frame(self.app, bg=self.surface)
        self.chrome = tk.Canvas(self.top, width=self.width, height=self.height, bg=self.surface,
                                highlightthickness=0, bd=0)
        self.chrome.pack()

        self.mode_text = self._mode_line()
        self._header_photo = ImageTk.PhotoImage(
            header_image(self.width, self.header_h, self.mode_text, self.surface), master=self.top)
        self.header_item = self.chrome.create_image(0, 0, anchor="nw", image=self._header_photo)
        self._composer_photo = ImageTk.PhotoImage(
            composer_image(self.width, self.composer_h, self.surface), master=self.top)
        self.composer_item = self.chrome.create_image(
            0, self.height - self.composer_h, anchor="nw", image=self._composer_photo)

        self.canvas = tk.Canvas(self.top, width=self.width - 2, height=self.body_h, bg=BODY,
                                highlightthickness=0, bd=0, yscrollincrement=22)
        self.canvas.place(x=1, y=self.header_h)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

        self._build_composer()
        self.chrome.bind("<Button-1>", self._on_header_click)
        self.top.bind("<Escape>", lambda _event: self.hide())
        self._greet()

    # ---- chrome ----------------------------------------------------------
    def _mode_line(self):
        try:
            state = self.app._model_state()
        except Exception:
            state = {"title": "纯规则引擎"}
        return "财税助手 · " + state["title"]

    def refresh_mode(self):
        line = self._mode_line()
        if self._thinking:
            line = "财税助手 · 思考中…"
        if line == self.mode_text:
            return
        self.mode_text = line
        self._show_header(line)

    def set_surface(self, colour):
        if colour == self.surface:
            return
        self.surface = colour
        self.top.configure(bg=colour)
        self.chrome.configure(bg=colour)
        self._header_photo = ImageTk.PhotoImage(
            header_image(self.width, self.header_h, self.mode_text, colour), master=self.top)
        self.chrome.itemconfigure(self.header_item, image=self._header_photo)
        self._composer_photo = ImageTk.PhotoImage(
            composer_image(self.width, self.composer_h, colour), master=self.top)
        self.chrome.itemconfigure(self.composer_item, image=self._composer_photo)

    def _build_composer(self):
        s = self.scale
        top = self.height - self.composer_h
        x = int(15 * s)
        for text in assistant.CHIPS:
            photo = ImageTk.PhotoImage(pill_image(text, False, s), master=self.top)
            item = self.chrome.create_image(x, top + int(13 * s), anchor="nw", image=photo)
            self.chrome.tag_bind(item, "<Button-1>", lambda _event, t=text: self.send(t))
            self.chrome.tag_bind(item, "<Enter>",
                                 lambda _event, i=item, t=text: self._hover_chip(i, t, True))
            self.chrome.tag_bind(item, "<Leave>",
                                 lambda _event, i=item, t=text: self._hover_chip(i, t, False))
            self._chip_photos[item] = photo
            x += photo.width() + int(7 * s)

        input_top = top + int(49 * s)
        input_h = int(44 * s)
        send_size = int(36 * s)
        input_w = self.width - int(15 * s) * 2 - send_size - int(14 * s)
        self._input_photo = ImageTk.PhotoImage(
            _card(input_w, input_h, int(14 * s), fill=INPUT_BG, outline=INPUT_EDGE, surface=CARD),
            master=self.top)
        self.chrome.create_image(int(15 * s), input_top, anchor="nw", image=self._input_photo)

        self.entry = tk.Text(self.top, height=2, width=1, bg=INPUT_BG, fg=TEXT, bd=0,
                             highlightthickness=0, font=(FONT, int(round(10.5 * s))), wrap="word",
                             padx=9, pady=7, insertbackground=VIOLET, selectbackground="#DCD2FF")
        self.chrome.create_window(int(15 * s) + int(8 * s), input_top + int(4 * s),
                                  anchor="nw", window=self.entry,
                                  width=input_w - int(16 * s), height=input_h - int(8 * s))
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Escape>", lambda _event: self.hide())
        self.entry.bind("<KeyPress>", self._on_press)
        self.entry.bind("<KeyRelease>", self._on_release)
        self.placeholder = "问报销、发票、财税问题…"

        self.send_photo = ImageTk.PhotoImage(send_image(send_size, False), master=self.top)
        self.send_button = tk.Canvas(self.top, width=send_size, height=send_size, bg=CARD,
                                     highlightthickness=0, bd=0, cursor="hand2")
        self.send_item = self.send_button.create_image(0, 0, anchor="nw", image=self.send_photo)
        self.send_button.bind("<Button-1>", lambda _event: self.submit())
        self.chrome.create_window(int(15 * s) + input_w + int(14 * s),
                                  input_top + (input_h - send_size) // 2,
                                  anchor="nw", window=self.send_button)

        self.chrome.create_text(int(16 * s), self.height - int(21 * s), anchor="w",
                                text="回答依据内置制度与财税法规，仅供参考；以财务复核为准。",
                                fill=FAINT, font=(FONT, int(round(7.5 * s))))
        self._show_placeholder()

    def _hover_chip(self, item, text, active):
        photo = ImageTk.PhotoImage(pill_image(text, active, self.scale), master=self.top)
        self._chip_photos[item] = photo
        self.chrome.itemconfigure(item, image=photo)

    def _on_header_click(self, event):
        if event.y > self.header_h or event.x < self.width - 72:
            return
        self.hide()

    # ---- message area ----------------------------------------------------
    def _greet(self):
        self._add_row("ai", assistant.greeting(self.app.policy), meta="制度摘要 · 本地生成")

    def _add_row(self, role, text, meta="", typing=0):
        row = _Row(self, role, text, meta, typing)
        self.rows.append(row)
        self.relayout()
        self._scroll_end()
        return row

    def relayout(self):
        pad = int(14 * self.scale)
        gap = int(10 * self.scale)
        width = self.width - 2
        y = int(12 * self.scale)
        for row in self.rows:
            height = row.image.size[1]
            x = width - pad - row.image.size[0] if row.role == "user" else pad
            self.canvas.coords(row.image_id, x, y)
            row.y = y
            y += height
            if row.meta:
                if row.meta_id is None:
                    row.meta_id = self.canvas.create_text(
                        0, 0, text=row.meta, fill=FAINT,
                        font=(FONT, int(round(8 * self.scale))),
                        anchor="ne" if row.role == "user" else "nw")
                else:
                    self.canvas.itemconfigure(row.meta_id, text=row.meta)
                anchor_x = width - pad if row.role == "user" else x + int(4 * self.scale)
                self.canvas.coords(row.meta_id, anchor_x, y + int(3 * self.scale))
                y += int(16 * self.scale)
            y += gap
        self.canvas.configure(scrollregion=(0, 0, width, max(y, self.body_h)))

    def _scroll_end(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120) * 2, "units")

    # ---- waiting status ---------------------------------------------------
    def _start_typing(self):
        # No three-dot bubble: report the wait in the panel header instead.
        self._thinking = True
        self.mode_text = "财税助手 · 思考中…"
        self._show_header(self.mode_text)

    def _show_header(self, line):
        self._header_photo = ImageTk.PhotoImage(
            header_image(self.width, self.header_h, line, self.surface), master=self.top)
        self.chrome.itemconfigure(self.header_item, image=self._header_photo)

    def _stop_typing(self):
        if self._typing_job is not None:
            try:
                self.top.after_cancel(self._typing_job)
            except tk.TclError:
                pass
            self._typing_job = None
        self._typing_row = None
        if self._thinking:
            self._thinking = False
            self.mode_text = ""  # force refresh_mode() to repaint the header
            self.refresh_mode()

    # ---- composer --------------------------------------------------------
    def _show_placeholder(self):
        if not self.entry.get("1.0", "end").strip():
            self.entry.insert("1.0", self.placeholder)
            self.entry.configure(fg=FAINT)
            self._placeholder_on = True
        self._refresh_send(False)

    def _clear_input(self):
        self.entry.delete("1.0", "end")
        self.entry.configure(fg=TEXT)
        self._placeholder_on = False

    def _on_press(self, _event=None):
        if self._placeholder_on:
            self._clear_input()
        return None

    def _on_release(self, _event=None):
        text = "" if self._placeholder_on else self.entry.get("1.0", "end").strip()
        self._refresh_send(bool(text))

    def _refresh_send(self, ready):
        if not hasattr(self, "send_button"):
            return
        self.send_photo = ImageTk.PhotoImage(send_image(int(36 * self.scale), ready), master=self.top)
        self.send_button.itemconfigure(self.send_item, image=self.send_photo)

    def _on_return(self, event):
        if event.state & 0x0001:  # Shift+Enter keeps the newline
            return None
        self.submit()
        return "break"

    def submit(self):
        text = self.entry.get("1.0", "end").strip()
        if self._placeholder_on or not text:
            return
        self.send(text)

    # ---- send ------------------------------------------------------------
    def send(self, text):
        if self.sending:
            return
        text = str(text).strip()
        if not text:
            return
        self._clear_input()
        self._show_placeholder()
        self._add_row("user", text)
        self.history.append({"role": "user", "content": text})
        self.refresh_mode()

        config = self.ball.chat_config()
        quick = assistant.instant_answer(text, self.app.policy)
        self._pending_row = None

        if not config["enabled"]:
            self._add_row("ai", quick.text, meta=self._meta(quick, "制度条文"))
            self.last_answer = quick.text
            self.history.append({"role": "assistant", "content": quick.text})
            return

        if quick.sources:
            # Painted in about a millisecond; the model only rewrites it later.
            self._pending_row = self._add_row("ai", quick.text, meta=self._meta(quick, "AI 解读中…"))
        else:
            self._start_typing()
        self.sending = True
        payload = {"question": text, "history": list(self.history[:-1]), "policy": self.app.policy,
                   "base_url": config["base_url"], "model": config["model"],
                   "api_key": config["api_key"], "enabled": True}

        def worker():
            try:
                self.queue.put(("reply", assistant.ask(**payload)))
            except Exception as exc:  # pragma: no cover - defensive
                self.queue.put(("error", exc))

        Thread(target=worker, daemon=True).start()
        self._drain()

    def _meta(self, reply, suffix=""):
        parts = []
        if reply.sources:
            parts.append("依据 " + "、".join(reply.sources[:2]))
        elif reply.level == "none":
            parts.append("通用口径")
        if suffix:
            parts.append(suffix)
        return " · ".join(parts)

    def _drain(self, after=120):
        try:
            kind, payload = self.queue.get_nowait()
        except Empty:
            self.top.after(after, self._drain)
            return
        self.sending = False
        self._stop_typing()
        if kind == "reply":
            self._apply(payload)
        else:
            self.last_answer = "刚才没答上来（" + type(payload).__name__ + "），可以再问一次。"
            self._add_row("ai", self.last_answer)
        self.refresh_mode()

    def _apply(self, reply):
        text = reply.text
        if reply.notice:
            text = text + "\n\n※ " + reply.notice
        suffix = ""
        if reply.mode in ("local", "cloud"):
            suffix = "AI 解读 · 缓存" if reply.cached else "AI 解读"
        elif reply.degraded:
            suffix = reply.degraded.split("·")[0].strip()
        meta = self._meta(reply, suffix)

        typing_row, self._typing_row = self._typing_row, None
        pending, self._pending_row = self._pending_row, None
        if typing_row is not None:
            typing_row.set_text(text, meta=meta)
        elif pending is not None:
            if reply.mode == "off":
                # The clause answer already on screen is the answer: keep it.
                pending.set_meta(meta)
            else:
                pending.set_text(text, meta=meta)
        else:
            self._add_row("ai", text, meta=meta)
        self.last_answer = text
        self.history.append({"role": "assistant", "content": reply.text})
        self._scroll_end()

    # ---- window ----------------------------------------------------------
    def _place(self):
        # Sit directly above the ball wherever the user parked it, but never
        # let the card run off the top of the window.
        gap = int(12 * self.scale)
        bottom = self.ball.dy + self.ball.size + gap
        bottom = max(gap, min(bottom, self.app.winfo_height() - self.height - gap))
        self.top.place(relx=1.0, rely=1.0, anchor="se", x=-self.ball.dx, y=-bottom)
        self.top.lift()

    def show(self):
        self.refresh_mode()
        self._place()
        self.visible = True
        if self._placeholder_on:
            self.entry.focus_set()
        # A fresh conversation reads from the top; a long one from the newest turn.
        if len(self.rows) <= 1:
            self.canvas.yview_moveto(0.0)
        else:
            self._scroll_end()
        self.ball.warm_model()

    def hide(self):
        self.visible = False
        self._stop_typing()
        self.top.place_forget()
        self.ball.on_panel_hidden()

    def destroy(self):
        self._stop_typing()
        try:
            self.top.destroy()
        except tk.TclError:
            pass


ChatWindow = ChatPanel


class AssistantBall:
    """Round button inside the app window; a click opens the chat panel."""

    def __init__(self, app, *, chat_config, surface=PAPER):
        self.app = app
        self.chat_config = chat_config
        self.scale = max(1.0, app.winfo_fpixels("1i") / 96)
        self.size = int(round(BALL_SIZE * self.scale))
        self.surface = surface
        self.window = None
        self.visible = False
        self._warmed = False
        self._restore = True
        self._drag = None
        self._moved = False
        self._dragging = False
        self._bounds = None
        self._target = None
        self._pending = None
        self._last_move = 0.0

        state = load_state()
        self.dx = int(state.get("ball_dx", round(BALL_DX * self.scale)))
        # Clear of the action row below, and fully inside the white card above,
        # so the square corners never straddle two different backgrounds.
        self.dy = int(state.get("ball_dy", round(BALL_DY * self.scale)))

        self.top = tk.Frame(app, bg=surface)
        self.canvas = tk.Canvas(self.top, width=self.size, height=self.size, bg=surface,
                                highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.pack()
        self._ball_images = {}
        self.image_normal = self._ball_image(False)
        self.item = self.canvas.create_image(0, 0, anchor="nw", image=self.image_normal)
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Enter>", lambda _event: self._zest(True))
        self.canvas.bind("<Leave>", lambda _event: self._zest(False))
        self.canvas.bind("<Button-3>", self._context_menu)

        self._place()
        if state.get("visible", True):
            self.show()

    def _ball_image(self, active):
        """Cached PhotoImage per hover state, rebuilt only when the surface changes."""
        key = bool(active)
        image = self._ball_images.get(key)
        if image is None:
            image = ImageTk.PhotoImage(ball_image(self.size, self.surface, key), master=self.app)
            self._ball_images[key] = image
        return image

    def _place(self, lift=True):
        """Move the ball with one place_configure. Lifting on every motion event forced a
        window restack (and a repaint) per event, which is what made dragging stutter."""
        if self.top.winfo_manager():
            self.top.place_configure(x=-self.dx, y=-self.dy)
        else:
            self.top.place(relx=1.0, rely=1.0, anchor="se", x=-self.dx, y=-self.dy)
        if lift:
            self.top.lift()

    def _zest(self, active):
        if self._dragging:
            return
        self.image_normal = self._ball_image(active)
        self.canvas.itemconfigure(self.item, image=self.image_normal)

    def set_surface(self, colour):
        """Match the corners to whatever sits behind the ball inside the app."""
        if colour == self.surface:
            return
        self.surface = colour
        self._ball_images.clear()
        self.top.configure(bg=colour)
        self.canvas.configure(bg=colour)
        self._zest(False)
        if self.window is not None:
            self.window.set_surface(colour)

    def show(self):
        self._place()
        self.visible = True
        save_state(visible=True)

    def reset_position(self):
        """Dock the ball back at its default spot; used by the smoke test."""
        self.dx = int(round(BALL_DX * self.scale))
        self.dy = int(round(BALL_DY * self.scale))
        if self.visible:
            self._place()

    def hide(self):
        if self.window is not None and self.window.visible:
            self.window.hide()
        self.top.place_forget()
        self.visible = False
        save_state(visible=False)

    def set_visible(self, value):
        self.show() if value else self.hide()

    def warm_model(self):
        """Load a local model while the user is still typing."""
        if self._warmed:
            return
        self._warmed = True
        config = self.chat_config()
        if not config["enabled"] or not assistant.provider_is_local(config["base_url"]):
            return
        payload = dict(base_url=config["base_url"], model=config["model"], api_key=config["api_key"])
        Thread(target=lambda: assistant.warm(**payload), daemon=True).start()

    # ---- pointer ---------------------------------------------------------
    def _app_bounds(self):
        return (self.app.winfo_rootx(), self.app.winfo_rooty(),
                self.app.winfo_width(), self.app.winfo_height())

    def _press(self, event):
        self._drag = (event.x_root, event.y_root, self.top.winfo_rootx(), self.top.winfo_rooty())
        self._bounds = self._app_bounds()   # cached for the whole drag: no winfo per motion event
        self._target = None
        self._last_move = 0.0
        self._moved = False
        self._dragging = True

    def _move(self, event):
        if not self._drag:
            return
        start_x, start_y, origin_x, origin_y = self._drag
        delta_x, delta_y = event.x_root - start_x, event.y_root - start_y
        if abs(delta_x) + abs(delta_y) > 4:
            self._moved = True
        self._target = (origin_x + delta_x, origin_y + delta_y)
        if time.monotonic() - self._last_move >= DRAG_INTERVAL:
            self._flush_move()
        elif self._pending is None:
            self._pending = self.top.after(DRAG_TRAILING_MS, self._flush_move)

    def _flush_move(self):
        """Apply the newest pointer position; coalesces bursts of motion events."""
        if self._pending is not None:
            try:
                self.top.after_cancel(self._pending)
            except tk.TclError:
                pass
            self._pending = None
        if self._target is None or not self._drag:
            return
        self._last_move = time.monotonic()
        self._move_to(self._target[0], self._target[1])

    def _move_to(self, root_x, root_y):
        app_x, app_y, width, height = self._bounds or self._app_bounds()
        margin = int(8 * self.scale)
        dx = width - (root_x - app_x) - self.size
        dy = height - (root_y - app_y) - self.size
        self.dx = max(margin, min(dx, width - self.size - margin))
        self.dy = max(margin, min(dy, height - self.size - margin))
        self._place(lift=False)

    def _release(self, _event):
        if self._pending is not None:
            try:
                self.top.after_cancel(self._pending)
            except tk.TclError:
                pass
            self._pending = None
        if self._moved and self._target is not None:
            self._move_to(self._target[0], self._target[1])   # land exactly under the cursor
        self._dragging = False
        if self._moved:
            save_state(ball_dx=self.dx, ball_dy=self.dy)
        else:
            self.open_chat()
        self._drag = None
        self._target = None
        self._bounds = None
        self.top.lift()

    def _context_menu(self, event):
        menu = tk.Menu(self.top, tearoff=0)
        menu.add_command(label="打开校验能手", command=self.open_chat)
        menu.add_separator()
        menu.add_command(label="隐藏悬浮球（可在高级设置恢复）", command=self.hide)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---- chat ------------------------------------------------------------
    def open_chat(self):
        if self.window is None:
            self.window = ChatPanel(self)
        self._restore = self.visible
        self.top.place_forget()
        self.window.show()

    def on_panel_hidden(self):
        if self._restore and self.visible:
            self._place()

    def close_chat(self):
        if self.window is not None:
            self.window.hide()

    def destroy(self):
        if self.window is not None:
            self.window.destroy()
            self.window = None
        try:
            self.top.destroy()
        except tk.TclError:
            pass
