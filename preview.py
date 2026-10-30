"""Read-only original-document viewer using the application's existing dependencies."""
from math import sqrt
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageOps, ImageTk
from glacier import BG


class DocumentPreview(tk.Toplevel):
    def __init__(self, master, path, initial_page=1):
        self.path = Path(path).resolve()
        self.pdf = None
        self.original = None
        self.photo = None
        self.page_number = 0
        self.zoom = 1.0
        self.image_size = (0, 0)
        self.fitted = True
        self.resize_job = None
        if not self.path.is_file():
            raise FileNotFoundError("原文件已移动或删除，请重新选择上传。")
        if self.path.stat().st_size > 60 * 1024 * 1024:
            raise ValueError("文件超过 60 MB，请拆分或缩小后查看。")
        suffix = self.path.suffix.lower()
        if suffix == ".pdf":
            import pypdfium2
            self.pdf = pypdfium2.PdfDocument(self.path)
            self.page_count = len(self.pdf)
            if not 1 <= self.page_count <= 50:
                self.pdf.close()
                raise ValueError("仅支持 1 至 50 页的 PDF，请拆分后查看。")
        elif suffix in (".png", ".jpg", ".jpeg"):
            with Image.open(self.path) as source:
                if source.width * source.height > 85_000_000:
                    raise ValueError("图片超过 8500 万像素，请缩小后查看。")
                ImageOps.exif_transpose(source, in_place=True)
                self.original = source.copy()
            self.page_count = 1
        else:
            raise ValueError("仅支持预览 PDF、PNG 或 JPG 单据。")

        try:
            super().__init__(master, background=BG)
            self.title("原图预览 · " + self.path.name)
            width, height = min(1100, self.winfo_screenwidth()-100), min(850, self.winfo_screenheight()-140)
            self.geometry(f"{width}x{height}")
            self.initial_viewport = (width-44, height-140)
            self.minsize(620, 480)
            self.transient(master)
            self.protocol("WM_DELETE_WINDOW", self.close)
            self.bind("<Destroy>", self._destroyed, add="+")
            self.bind("<Escape>", lambda _event: self.close())
            toolbar = ttk.Frame(self, padding=12)
            toolbar.pack(fill="x")
            self.previous_button = ttk.Button(toolbar, text="上一页", style="Compact.TButton", width=0,
                                              command=lambda: self._navigate(-1))
            self.previous_button.pack(side="left")
            self.page_label = ttk.Label(toolbar, style="Muted.TLabel", width=11, anchor="center")
            self.page_label.pack(side="left", padx=6)
            self.next_button = ttk.Button(toolbar, text="下一页", style="Compact.TButton", width=0,
                                          command=lambda: self._navigate(1))
            self.next_button.pack(side="left", padx=(0, 28))
            ttk.Button(toolbar, text="缩小 −", style="Compact.TButton", width=0,
                       command=lambda: self._safe_zoom(.8)).pack(side="left")
            ttk.Button(toolbar, text="放大 ＋", style="Compact.TButton", width=0,
                       command=lambda: self._safe_zoom(1.25)).pack(side="left", padx=6)
            ttk.Button(toolbar, text="适应窗口", style="Compact.TButton", width=0,
                       command=self.fit_page).pack(side="left")
            self.zoom_label = ttk.Label(toolbar, style="Muted.TLabel", width=5, anchor="e")
            self.zoom_label.pack(side="right")
            frame = ttk.Frame(self)
            frame.pack(fill="both", expand=True, padx=12)
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            self.canvas = tk.Canvas(frame, background="#DEE9F3", highlightthickness=0)
            self.canvas.grid(row=0, column=0, sticky="nsew")
            vertical = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
            vertical.grid(row=0, column=1, sticky="ns")
            horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
            horizontal.grid(row=1, column=0, sticky="ew")
            self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            self.canvas.bind("<Configure>", self._resized)
            self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta/120), "units"))
            self.canvas.bind("<Control-MouseWheel>", lambda event: self._safe_zoom(1.25 if event.delta > 0 else .8))
            self.canvas.bind("<ButtonPress-1>", lambda event: self.canvas.scan_mark(event.x, event.y))
            self.canvas.bind("<B1-Motion>", lambda event: self.canvas.scan_dragto(event.x, event.y, gain=1))
            ttk.Label(self, text=str(self.path), style="Shell.TLabel", wraplength=width-32,
                      padding=(12, 10)).pack(fill="x")
            self.update_idletasks()
            self.minsize(max(620, toolbar.winfo_reqwidth()), 480)
            self.show_page(initial_page)
        except Exception:
            self._release()
            if hasattr(self, "_w"):
                self.destroy()
            raise

    def _size(self, number):
        if self.pdf is not None:
            page = self.pdf[number-1]
            try:
                return page.get_size()
            finally:
                page.close()
        return self.original.size

    def _render(self, number, requested_zoom):
        width, height = self._size(number)
        # Bound raster memory before rendering a PDF or allocating a large zoomed image.
        zoom = max(.001, min(requested_zoom, 4.0, 6000/max(width, height), sqrt(24_000_000/(width*height))))
        if self.pdf is not None:
            page = self.pdf[number-1]
            try:
                bitmap = page.render(scale=zoom)
                try:
                    image = bitmap.to_pil().copy()
                finally:
                    bitmap.close()
            finally:
                page.close()
        else:
            image = self.original.resize((max(1, round(width*zoom)), max(1, round(height*zoom))), Image.Resampling.LANCZOS)
        try:
            photo = ImageTk.PhotoImage(image, master=self.canvas)
        finally:
            image.close()
        self.canvas.delete("all")
        self.photo = photo
        self.canvas.create_image(0, 0, image=photo, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, photo.width(), photo.height()))
        self.page_number, self.zoom, self.image_size = number, zoom, (width, height)
        self.page_label.configure(text=f"第 {number} / {self.page_count} 页")
        self.zoom_label.configure(text=f"{zoom:.0%}")
        self.previous_button.configure(state="normal" if number > 1 else "disabled")
        self.next_button.configure(state="normal" if number < self.page_count else "disabled")

    def show_page(self, page_number):
        if not isinstance(page_number, int) or not 1 <= page_number <= self.page_count:
            raise ValueError("页码超出文件范围。")
        width, height = self._size(page_number)
        viewport_width, viewport_height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if viewport_width <= 1 or viewport_height <= 1:
            viewport_width, viewport_height = self.initial_viewport
        zoom = min(max(100, viewport_width-20)/width,
                   max(100, viewport_height-20)/height)
        self._render(page_number, zoom)
        self.fitted = True
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def fit_page(self):
        if self.page_number:
            try:
                self.show_page(self.page_number)
            except Exception as exc:
                messagebox.showerror("无法显示原图", str(exc), parent=self)

    def change_zoom(self, multiplier):
        self._render(self.page_number, self.zoom*multiplier)
        self.fitted = False

    def _safe_zoom(self, multiplier):
        try:
            self.change_zoom(multiplier)
        except Exception as exc:
            messagebox.showerror("无法缩放", str(exc), parent=self)

    def _navigate(self, delta):
        try:
            self.show_page(self.page_number+delta)
        except Exception as exc:
            messagebox.showerror("无法显示该页", str(exc), parent=self)

    def _resized(self, _event):
        if self.fitted:
            if self.resize_job is not None:
                self.after_cancel(self.resize_job)
            self.resize_job = self.after(150, self._fit_after_resize)

    def _fit_after_resize(self):
        self.resize_job = None
        if self.fitted:
            self.fit_page()

    def _release(self):
        if self.resize_job is not None:
            self.after_cancel(self.resize_job)
            self.resize_job = None
        self.photo = None
        if self.pdf is not None:
            self.pdf.close()
            self.pdf = None
        if self.original is not None:
            self.original.close()
            self.original = None

    def _destroyed(self, event):
        if event.widget is self:
            self._release()

    def close(self):
        self._release()
        self.destroy()
