"""Quiet, native ttk surfaces for the task entry page."""
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageTk

from glacier import BG, BLUE, INK, LINE, MUTED, PAPER


NEW_SURFACE = "#F5F9FF"


def _card(fill, edge):
    scale = 3
    size = (96 * scale, 96 * scale)
    image = Image.new("RGBA", size)
    bounds = (6 * scale, 4 * scale, 90 * scale - 1, 88 * scale - 1)
    shadow = Image.new("L", size)
    ImageDraw.Draw(shadow).rounded_rectangle(
        (bounds[0], bounds[1] + 3 * scale, bounds[2], bounds[3] + 3 * scale),
        radius=19 * scale, fill=19)
    image.paste((45, 59, 83, 255), (0, 0), shadow.filter(ImageFilter.GaussianBlur(3 * scale)))
    ImageDraw.Draw(image).rounded_rectangle(bounds, radius=19 * scale,
                                           fill=fill, outline=edge, width=scale)
    return image.resize((96, 96), Image.Resampling.LANCZOS)


def _pill(height, fill, edge, focused=False):
    scale = 3
    width = height * 2
    image = Image.new("RGBA", (width * scale, height * scale))
    draw = ImageDraw.Draw(image)
    bounds = (3 * scale, 3 * scale, (width - 3) * scale - 1, (height - 3) * scale - 1)
    radius = (height - 6) * scale // 2
    if focused:
        draw.rounded_rectangle((scale, scale, (width - 1) * scale - 1, (height - 1) * scale - 1),
                               radius=radius + 2 * scale, outline="#85B9FF", width=scale)
    draw.rounded_rectangle(bounds, radius=radius, fill=fill, outline=edge, width=scale)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _icon(kind):
    scale = 3
    size = 96 if kind == "document" else 46
    image = Image.new("RGBA", (size * scale, size * scale))
    draw = ImageDraw.Draw(image)
    if kind == "document":
        shadow = Image.new("L", image.size)
        ImageDraw.Draw(shadow).rounded_rectangle((14 * scale, 14 * scale, 82 * scale, 82 * scale),
                                                radius=18 * scale, fill=56)
        image.paste((0, 94, 216, 255), (0, 0), shadow.filter(ImageFilter.GaussianBlur(5 * scale)))
        bounds = (12 * scale, 6 * scale, 84 * scale - 1, 78 * scale - 1)
        mask = Image.new("L", image.size)
        ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=18 * scale, fill=255)
        surface = Image.new("RGBA", image.size)
        gradient = ImageDraw.Draw(surface)
        top, bottom = ImageColor.getrgb("#3983FA"), ImageColor.getrgb("#1670F5")
        for y in range(bounds[1], bounds[3] + 1):
            amount = (y - bounds[1]) / (bounds[3] - bounds[1])
            color = tuple(round(a + (b - a) * amount) for a, b in zip(top, bottom))
            gradient.line((bounds[0], y, bounds[2], y), fill=color)
        image.paste(surface, (0, 0), mask)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((35 * scale, 27 * scale, 60 * scale, 57 * scale),
                               radius=4 * scale, fill="#F7FAFF")
        for y, end in ((34, 49), (40, 54), (46, 54), (52, 47)):
            draw.line((40 * scale, y * scale, end * scale, y * scale),
                      fill="#80B2FF", width=2 * scale)
    else:
        draw.rounded_rectangle((0, 0, size * scale - 1, size * scale - 1),
                               radius=13 * scale, fill="#E7F1FF" if kind == "new" else "#F1F1F4")
        if kind == "new":
            draw.line((23 * scale, 16 * scale, 23 * scale, 30 * scale), fill=BLUE, width=2 * scale)
            draw.line((16 * scale, 23 * scale, 30 * scale, 23 * scale), fill=BLUE, width=2 * scale)
        else:
            draw.ellipse((14 * scale, 14 * scale, 32 * scale, 32 * scale),
                         outline=MUTED, width=2 * scale)
            draw.line((23 * scale, 18 * scale, 23 * scale, 23 * scale, 27 * scale, 26 * scale),
                      fill=MUTED, width=2 * scale, joint="curve")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def install_home_styles(master, style):
    """Return (image references, icons); retain both for the lifetime of the UI."""
    references = []
    for name, fill, edge in (("New", NEW_SURFACE, "#C8DEFF"), ("Continue", PAPER, LINE)):
        photo = ImageTk.PhotoImage(_card(fill, edge), master=master)
        references.append(photo)
        element = "Home.Card." + str(photo)
        style.element_create(element, "image", photo, border=29, padding=0, sticky="nsew")
        style.layout(f"Home.{name}.TFrame", [(element, {"sticky": "nsew"})])
        style.configure(f"Home.{name}.TFrame", background=BG, borderwidth=0)
        style.configure(f"Home.{name}.TLabel", background=fill, foreground=INK)

    height = 56
    for name, colors, foreground, background in (
            ("Primary", (BLUE, "#298EFF", "#0062D5", "#BCD8F7"), "#FFFFFF", NEW_SURFACE),
            ("Secondary", ("#F5F5F7", "#EBEBF0", "#DEDEE5", "#F3F3F5"), INK, PAPER)):
        images = []
        for focused, color in ((False, colors[0]), (False, colors[1]), (False, colors[2]),
                               (False, colors[3]), (True, colors[0]),
                               (True, colors[1]), (True, colors[2])):
            edge = color if name == "Primary" else "#D6D6DD"
            images.append(ImageTk.PhotoImage(_pill(height, color, edge, focused), master=master))
        references.extend(images)
        element = "Home.Pill." + str(images[0])
        style.element_create(element, "image", images[0], ("disabled", images[3]),
                             ("pressed", "focus", images[6]), ("pressed", images[2]),
                             ("active", "focus", images[5]), ("focus", images[4]),
                             ("active", images[1]), border=height // 2, padding=0, sticky="nsew")
        button_style = f"Home.{name}.TButton"
        style.layout(button_style, [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})])
        style.configure(button_style, font=("Microsoft YaHei UI", -20, "bold"),
                        padding=(28, 10), foreground=foreground, background=background,
                        anchor="center", width=0)
        style.map(button_style, foreground=[("disabled", "#96A0AD")],
                  background=[("disabled", background), ("pressed", background), ("active", background)])

    icons = {name: ImageTk.PhotoImage(_icon(name), master=master)
             for name in ("document", "new", "continue")}
    references.extend(icons.values())
    return references, icons
