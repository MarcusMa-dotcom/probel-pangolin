"""What the tabs of the window share: the crosspoint table, the trace, the log, background work."""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from .gui_colors import error_color, muted_color

TRACE_LINES = 150     # trace entries the window shows
TABLE_ROWS = 512      # destinations the window lists


def clock(stamp: float | None) -> str:
    """Seconds since 1970 as local time of day, empty for nothing."""
    return time.strftime("%H:%M:%S", time.localtime(stamp)) if stamp else ""


def clock_ms(stamp: float) -> str:
    """Local time of day with milliseconds, for the trace."""
    return f"{time.strftime('%H:%M:%S', time.localtime(stamp))}.{int(stamp * 1000) % 1000:03d}"


def port_text(number: int | None, names: dict[int, str]) -> str:
    """``5 CAM 5`` for a number with a name, ``5`` without, a dash for nothing."""
    if number is None:
        return "–"
    name = names.get(number)
    return f"{number} {name}" if name else str(number)


def group(parent: tk.Widget, title: str) -> ttk.LabelFrame:
    """A titled frame, stacked below the previous one."""
    frame = ttk.LabelFrame(parent, text=title, padding=8)
    frame.pack(fill="x", pady=(0, 8))
    return frame


def text_box(parent: tk.Widget, height: int, row: int = 1) -> tk.Text:
    """A read-only text area with a scroll bar, placed in ``parent`` with grid."""
    widget = tk.Text(parent, width=60, height=height, wrap="word", font="TkFixedFont",
                     state="disabled", relief="solid", borderwidth=1)
    scroll = ttk.Scrollbar(parent, orient="vertical", command=widget.yview)
    widget.configure(yscrollcommand=scroll.set)
    widget.grid(row=row, column=0, sticky="nsew", pady=(6, 0))
    scroll.grid(row=row, column=1, sticky="ns", pady=(6, 0))
    parent.columnconfigure(0, weight=1)
    return widget


def make_table(parent: tk.Widget, height: int) -> ttk.Treeview:
    """An empty crosspoint table with a scroll bar. :func:`set_columns` gives it its columns."""
    table = ttk.Treeview(parent, show="headings", height=height, selectmode="browse")
    scroll = ttk.Scrollbar(parent, orient="vertical", command=table.yview)
    table.configure(yscrollcommand=scroll.set)
    table.grid(row=1, column=0, sticky="nsew")
    scroll.grid(row=1, column=1, sticky="ns")
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(1, weight=1)
    return table


def set_columns(table: ttk.Treeview, columns: list[tuple[str, str, int]]) -> None:
    """Give a table its columns, unless it already has exactly these.

    Args:
        table: The table.
        columns: Key, title and width of every column.
    """
    keys = tuple(key for key, _title, _width in columns)
    if tuple(table["columns"]) == keys:
        return
    table.delete(*table.get_children())
    table.configure(columns=keys)
    for key, title, width in columns:
        table.heading(key, text=title, anchor="w")
        table.column(key, width=width, anchor="w", stretch=key == "name")


def sync_table(table: ttk.Treeview, rows: list[tuple[str, tuple]], shown: dict[str, tuple]) -> None:
    """Bring a table in line with the rows of a snapshot.

    Rows are added, changed and removed in place, so the selection stays.

    Args:
        table: The table.
        rows: Name and cell values of every row, in order.
        shown: What the table shows now, kept between calls.
    """
    wanted = {name for name, _values in rows}
    for name in [name for name in shown if name not in wanted]:
        if table.exists(name):
            table.delete(name)
        del shown[name]
    for name, values in rows:
        if not table.exists(name):
            table.insert("", "end", iid=name, values=values)
        elif shown.get(name) != values:
            table.item(name, values=values)
        shown[name] = values


def show_log(widget: tk.Text, entries: list[dict[str, Any]], empty: str) -> None:
    """Write an activity log into a text area, newest entry first."""
    widget.tag_configure("problem", foreground=error_color(widget))
    widget.tag_configure("other", foreground=muted_color(widget))
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    for entry in reversed(entries):
        head = "  ".join(part for part in (clock(entry["time"]), entry.get("client", ""), entry["action"])
                         if part)
        line = f"{head}  {entry['text']}\n" if entry["text"] else f"{head}\n"
        widget.insert("end", line, ("problem",) if entry.get("problem") else ())
    if not entries:
        widget.insert("end", empty, ("other",))
    widget.configure(state="disabled")


def show_trace(widget: tk.Text, rows: list[dict[str, Any]], show_acks: bool) -> None:
    """Write a trace into a text area, newest entry first: time, direction, bytes, meaning."""
    widget.tag_configure("problem", foreground=error_color(widget))
    widget.tag_configure("other", foreground=muted_color(widget))
    if not show_acks:
        rows = [row for row in rows if row["kind"] not in ("ack", "nak")]
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    for row in reversed(rows[-TRACE_LINES:]):
        tags: tuple = ("problem",) if row["problem"] else (("other",) if row["kind"] == "ack" else ())
        client = f"  {row['client']}" if row["client"] else ""
        note = f" ({row['note']})" if row["note"] else ""
        widget.insert("end", f"{clock_ms(row['time'])}  {row['direction'].upper()}{client}  {row['hex']}\n", tags)
        widget.insert("end", f"              {row['text']}{note}\n", tags)
    if not rows:
        widget.insert("end", "Nothing crossed the wire yet.", ("other",))
    widget.configure(state="disabled")


def in_background(work: Callable[[], Any], done: Callable[[str, Any], None]) -> None:
    """Run something that may wait for the network without freezing the window.

    Args:
        work: What to do.
        done: Called with an empty message and the result, or with the message
            of the ``ValueError`` that ``work`` raised and None.
    """
    def run() -> None:
        """Do the work and report how it went."""
        try:
            result = work()
        except ValueError as exc:
            done(str(exc), None)
        else:
            done("", result)
    threading.Thread(target=run, daemon=True).start()
