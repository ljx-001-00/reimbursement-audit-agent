"""Small, cancellable native-widget translations; no business callbacks."""
import ctypes
import os
from time import perf_counter
import tkinter as tk


def _ease_out(progress):
    # cubic-bezier(0.23, 1, 0.32, 1), shared by entry and feedback.
    lo, hi = 0.0, 1.0
    for _ in range(14):
        t = (lo + hi) / 2
        x = 3 * (1-t)**2 * t * .23 + 3 * (1-t) * t*t * .32 + t**3
        if x < progress:
            lo = t
        else:
            hi = t
    return 1 - (1 - (lo + hi) / 2)**3


class OffsetMotion:
    def __init__(self, master):
        self.master = master
        self.jobs = {}
        self.pointer = True
        enabled = ctypes.c_int(1)
        if os.name == "nt":
            ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
        self.enabled = bool(enabled.value)

    def stop(self, widget=None, finish=None):
        for item in list(self.jobs) if widget is None else [widget]:
            job = self.jobs.pop(item, None)
            if job:
                self.master.after_cancel(job)
        if widget is not None and finish is not None and widget.winfo_exists():
            widget.place_configure(y=finish)

    def move(self, widget, target=0, duration=180, start=None):
        continuing = widget in self.jobs
        self.stop(widget)
        if not self.enabled or not self.pointer:
            widget.place_configure(y=target)
            return
        origin = float(widget.place_info().get("y", target)) if continuing or start is None else start
        widget.place_configure(y=round(origin))
        if origin == target:
            return
        began = perf_counter()

        def frame():
            self.jobs.pop(widget, None)
            if not widget.winfo_exists():
                return
            progress = min(1.0, (perf_counter() - began) * 1000 / duration)
            value = target if progress == 1 else origin + (target - origin) * _ease_out(progress)
            widget.place_configure(y=round(value))
            if progress < 1:
                self.jobs[widget] = self.master.after(16, frame)

        frame()
