"""Silver-blue glass surfaces drawn locally for native ttk buttons."""
from tkinter import font

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageTk


BG = "#F5F5F7"
INK = "#1D1D1F"
MUTED = "#6E6E73"
BLUE = "#007AFF"
PAPER = "#FFFFFF"
FIELD = "#F8F9FB"
LINE = "#E2E3E8"
NAV = "#F4F8FD"


def _glass_image(height, top, bottom, edge, focused=False, pressed=False):
    """A wide middle stretches; the circular ends retain their original size."""
    scale = 3
    width = height * 2
    size = (width * scale, height * scale)
    image = Image.new("RGBA", size)
    inset = 3 * scale
    bounds = (inset, inset, size[0] - inset - 1, size[1] - 6 * scale - 1)
    radius = (bounds[3] - bounds[1]) // 2
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).rounded_rectangle(bounds, radius, fill=255)

    shadow = Image.new("L", size)
    shadow_bounds = (bounds[0], bounds[1] + 2 * scale, bounds[2], bounds[3] + 2 * scale)
    ImageDraw.Draw(shadow).rounded_rectangle(shadow_bounds, radius, fill=32 if pressed else 40)
    shadow = shadow.filter(ImageFilter.GaussianBlur(2 * scale))
    image.paste((53, 83, 110, 255), (0, 0), shadow)

    surface = Image.new("RGBA", size)
    draw = ImageDraw.Draw(surface)
    start, end = ImageColor.getrgb(top), ImageColor.getrgb(bottom)
    for y in range(bounds[1], bounds[3] + 1):
        amount = (y - bounds[1]) / max(1, bounds[3] - bounds[1])
        color = tuple(round(a + (b - a) * amount) for a, b in zip(start, end))
        draw.line((0, y, size[0], y), fill=color)
    image.paste(surface, (0, 0), mask)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(bounds, radius, outline=edge, width=scale)
    inner = (bounds[0] + scale, bounds[1] + scale, bounds[2] - scale, bounds[3] - scale)
    draw.rounded_rectangle(inner, max(0, radius - scale), outline=(255, 255, 255, 215), width=scale)
    # Rounded-rectangle rims keep their middle straight when ttk stretches it.
    refraction = Image.new("RGBA", size)
    rim = ImageDraw.Draw(refraction)
    rim.rounded_rectangle(inner, max(0, radius - scale),
                          outline=(145, 171, 196, 75 if pressed else 42), width=scale)
    rim.rectangle((0, 0, size[0], (inner[1] + inner[3]) // 2), fill=(0, 0, 0, 0))
    image.alpha_composite(refraction)
    draw = ImageDraw.Draw(image)
    if focused:
        focus_bounds = (scale, scale, size[0] - scale - 1, size[1] - 4 * scale - 1)
        draw.rounded_rectangle(focus_bounds, radius + 2 * scale, outline=BLUE, width=scale)
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    cap = height // 2
    middle = image.crop((width // 2, 0, width // 2 + 1, height))
    image.paste(middle.resize((width - cap * 2, height)), (cap, 0))
    return image


def install_glass_buttons(master, style):
    """Register glass button styles and return the image references to retain."""
    references = []
    line_height = font.nametofont("TkDefaultFont", root=master).metrics("linespace")
    normal = ("#FFFFFF", "#E5EEF7", "#C4D4E4")
    selected = ("#FFFFFF", "#DBEAFB", "#A6C3E4")
    variants = [
        ("TButton", normal, INK, (16, 9), False),
        ("Primary.TButton", ("#FFFFFF", "#DCEAF8", "#B4CBE0"), INK, (22, 11), False),
        ("Compact.TButton", normal, INK, (11, 7), False),
        ("Step.TButton", ("#F9FCFF", "#E1EBF5", "#C7D7E6"), MUTED, (12, 9), False),
        ("ActiveStep.TButton", selected, BLUE, (12, 9), True),
        ("Nav.TButton", normal, INK, (15, 11), False),
    ]
    for name, colors, foreground, padding, bold in variants:
        height = line_height + padding[1] * 2 + 6
        if height % 2 == 0:
            height += 1
        states = [
            (*colors, False, False),
            ("#FFFFFF", "#F0F6FC", "#AFC6DE", False, False),
            ("#DDE9F5", "#F5F9FD", "#A9BED3", False, True),
            ("#F2F5F8", "#E7EDF3", "#DBE3EB", False, False),
            (*colors, True, False),
        ]
        images = [ImageTk.PhotoImage(_glass_image(height, *state), master=master) for state in states]
        references.extend(images)
        element = "Glacier." + str(images[0])
        style.element_create(element, "image", images[0], ("disabled", images[3]),
                             ("pressed", images[2]), ("focus", images[4]), ("active", images[1]),
                             border=(height // 2, height // 2, height // 2, height // 2),
                             padding=0, sticky="nsew")
        style.layout(name, [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})])
        background = NAV if name == "Nav.TButton" else PAPER if name in ("TButton", "Compact.TButton") else BG
        style.configure(name, padding=padding, foreground=foreground, background=background,
                        width=0, anchor="center",
                        font=("Microsoft YaHei UI", 11 if name == "Nav.TButton" else 10, "bold" if bold else "normal"))
        style.map(name, foreground=[("disabled", "#8B9DAE")])
        style.map(name, background=[("disabled", background), ("pressed", background),
                                    ("active", background)])

    # The footer sits over the shell; modal save buttons sit over the paper.
    style.configure("Paper.Primary.TButton", background=PAPER)
    style.map("Paper.Primary.TButton", background=[("disabled", PAPER), ("pressed", PAPER), ("active", PAPER)])

    paper = Image.new("RGBA", (288, 288))
    shadow = Image.new("L", paper.size)
    ImageDraw.Draw(shadow).rounded_rectangle((12, 18, 275, 281), radius=72, fill=24)
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    paper.paste((63, 98, 128, 255), (0, 0), shadow)
    ImageDraw.Draw(paper).rounded_rectangle((12, 9, 275, 272), radius=72, fill=PAPER,
                                           outline="white", width=3)
    photo = ImageTk.PhotoImage(paper.resize((96, 96), Image.Resampling.LANCZOS), master=master)
    references.append(photo)
    element = "Glacier.Paper." + str(photo)
    style.element_create(element, "image", photo, border=30, padding=0, sticky="nsew")
    style.layout("Paper.TFrame", [(element, {"sticky": "nsew"})])
    style.configure("Paper.TFrame", background=BG, borderwidth=0)

    nav = Image.new("RGBA", (288, 216))
    shadow = Image.new("L", nav.size)
    ImageDraw.Draw(shadow).rounded_rectangle((12, 16, 275, 206), radius=36, fill=35)
    nav.paste((66, 91, 122, 255), (0, 0), shadow.filter(ImageFilter.GaussianBlur(8)))
    draw = ImageDraw.Draw(nav)
    draw.rounded_rectangle((12, 7, 275, 195), radius=36, fill=NAV, outline="#C7D9EB", width=3)
    draw.rounded_rectangle((15, 10, 272, 192), radius=33, outline="#FFFFFF", width=3)
    draw.line((46, 189, 241, 189), fill="#DFEAF6", width=3)
    photo = ImageTk.PhotoImage(nav.resize((96, 72), Image.Resampling.LANCZOS), master=master)
    references.append(photo)
    element = "Glacier.Nav." + str(photo)
    style.element_create(element, "image", photo, border=(20, 20, 20, 20), padding=0, sticky="nsew")
    style.layout("Glass.Nav.TFrame", [(element, {"sticky": "nsew"})])
    style.configure("Glass.Nav.TFrame", background=BG, borderwidth=0)
    style.configure("Nav.Surface.TFrame", background=NAV)
    style.configure("Nav.Title.TLabel", background=NAV, foreground=INK)
    style.configure("Nav.Muted.TLabel", background=NAV, foreground=MUTED)
    return references
