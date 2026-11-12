"""Router tab of the Tkinter window: the virtual router, its crosspoints, names, faults and trace."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import Any

from .gui_colors import error_color
from .gui_common import (TABLE_ROWS, clock, group, make_table, set_columns, show_log, show_trace,
                         sync_table, text_box)
from .router_engine import RouterEngine

ENTRY_FIELDS = ("bind", "port", "sources", "dests", "levels", "matrix")
PROTOCOL_LABELS = {"sw-p-08": "SW-P-08", "sw-p-02": "SW-P-02"}
ALL_LEVELS = "All levels"


class RouterTab(ttk.Frame):
    """The Router tab.

    Every input goes to the router engine, and :meth:`poll` redraws the
    widgets when the engine changed.
    """

    def __init__(self, parent: tk.Widget, engine: RouterEngine):
        """Build the tab.

        Args:
            parent: The notebook the tab belongs to.
            engine: The router engine.
        """
        super().__init__(parent, padding=10)
        self.engine = engine
        self._loading = False
        self._seen_revision = -1
        self._input_error = ""
        self._message = ""
        self._message_failed = False
        self._shown: dict[str, tuple] = {}
        self._levels = 0

        self.vars: dict[str, tk.Variable] = {name: tk.StringVar() for name in ENTRY_FIELDS}
        self.vars["extended"] = tk.BooleanVar()
        self.vars["autostart"] = tk.BooleanVar()
        self.vars["protocol"] = tk.StringVar()
        self.entries: dict[str, ttk.Entry] = {}
        self.source_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        self.level_var = tk.StringVar(value="1")
        self.name_var = tk.StringVar()
        self.announce_var = tk.BooleanVar(value=True)
        self.drop_var = tk.StringVar(value="0")
        self.nak_var = tk.StringVar(value="0")
        self.acks_var = tk.BooleanVar(value=True)
        self.hold_var = tk.BooleanVar(value=False)

        left = ttk.Frame(self)
        right = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="n", padx=(0, 10))
        right.grid(row=0, column=1, sticky="nsew")
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._build_server(left)
        self._build_change(left)
        self._build_faults(left)
        self._build_crosspoints(right)
        self._build_trace(right)
        self._build_log(right)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=3)

    # -- layout ----------------------------------------------------------

    def _entry(self, parent: tk.Widget, name: str, width: int) -> ttk.Entry:
        """A text field of a setting. It is sent when the field is left or Return is pressed."""
        entry = ttk.Entry(parent, textvariable=self.vars[name], width=width)
        entry.bind("<FocusOut>", lambda _event: self._push(name, self.vars[name].get()))
        entry.bind("<Return>", lambda _event: self._push(name, self.vars[name].get()))
        self.entries[name] = entry
        return entry

    def _build_server(self, parent: tk.Widget) -> None:
        """The Virtual router group: address, size, switches, Start button and state."""
        frame = group(parent, "Virtual router")
        ttk.Label(frame, text="Listen address").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Port").grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(frame, text="Protocol").grid(row=0, column=3, sticky="w", padx=(8, 0))
        self._entry(frame, "bind", 18).grid(row=1, column=0, columnspan=2, sticky="w")
        self._entry(frame, "port", 7).grid(row=1, column=2, sticky="w", padx=(8, 0))
        protocol = ttk.Combobox(frame, textvariable=self.vars["protocol"], width=9, state="readonly",
                                values=tuple(PROTOCOL_LABELS.values()))
        protocol.grid(row=1, column=3, sticky="w", padx=(8, 0))
        protocol.bind("<<ComboboxSelected>>", lambda _event: self._push("protocol", next(
            key for key, label in PROTOCOL_LABELS.items() if label == self.vars["protocol"].get())))
        for column, (name, label) in enumerate((("sources", "Sources"), ("dests", "Destinations"),
                                                ("levels", "Levels"), ("matrix", "Matrix"))):
            ttk.Label(frame, text=label).grid(row=2, column=column, sticky="w", pady=(6, 0), padx=(0, 8))
            self._entry(frame, name, 7).grid(row=3, column=column, sticky="w", padx=(0, 8))
        ttk.Checkbutton(frame, text="Extended commands", variable=self.vars["extended"],
                        command=lambda: self._push("extended", bool(self.vars["extended"].get()))
                        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(frame, text="Start with the program", variable=self.vars["autostart"],
                        command=lambda: self._push("autostart", bool(self.vars["autostart"].get()))
                        ).grid(row=4, column=2, columnspan=2, sticky="w", pady=(6, 0))
        self.start_button = ttk.Button(frame, text="Start", command=self._toggle_serving)
        self.start_button.grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.state_label = ttk.Label(frame, text="Router off", wraplength=380, justify="left")
        self.state_label.grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.clients_label = ttk.Label(frame, text="", wraplength=380, justify="left")
        self.clients_label.grid(row=7, column=0, columnspan=4, sticky="w")
        self.error_label = ttk.Label(frame, text="", wraplength=380, justify="left")
        self.error_label.grid(row=8, column=0, columnspan=4, sticky="w")

    def _build_change(self, parent: tk.Widget) -> None:
        """The group for changes made at the router itself: crosspoint, name, protection."""
        frame = group(parent, "Change at the router")
        ttk.Label(frame, text="Source").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Label(frame, text="Destination").grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Label(frame, text="Level").grid(row=0, column=2, sticky="w")
        source = ttk.Entry(frame, textvariable=self.source_var, width=9)
        source.grid(row=1, column=0, sticky="w", padx=(0, 8))
        source.bind("<Return>", lambda _event: self._set())
        self.source_entry = source
        ttk.Entry(frame, textvariable=self.dest_var, width=9).grid(row=1, column=1, sticky="w", padx=(0, 8))
        self.level_box = ttk.Combobox(frame, textvariable=self.level_var, width=9, state="readonly")
        self.level_box.grid(row=1, column=2, sticky="w")
        ttk.Button(frame, text="Set", command=self._set).grid(row=1, column=3, sticky="w", padx=(8, 0))
        ttk.Checkbutton(frame, text="Tell the controllers (Crosspoint Connected)",
                        variable=self.announce_var).grid(row=2, column=0, columnspan=4, sticky="w",
                                                         pady=(6, 0))
        ttk.Label(frame, text="Name").grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.name_var, width=18).grid(row=4, column=0, columnspan=2, sticky="w")
        names = ttk.Frame(frame)
        names.grid(row=4, column=2, columnspan=2, sticky="w")
        ttk.Button(names, text="Name the source", command=lambda: self._rename("source")).pack(side="left")
        ttk.Button(names, text="Name the destination", command=lambda: self._rename("dest")).pack(
            side="left", padx=(6, 0))
        ttk.Button(frame, text="Protect or release the destination", command=self._protect).grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.message_label = ttk.Label(frame, text="", wraplength=380, justify="left")
        self.message_label.grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))

    def _build_faults(self, parent: tk.Widget) -> None:
        """The Faults group: unanswered and refused messages, dropped connections."""
        frame = group(parent, "Faults on purpose")
        ttk.Label(frame, text="Leave unanswered").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Label(frame, text="Refuse with NAK").grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.drop_var, width=9).grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.nak_var, width=9).grid(row=1, column=1, sticky="w", padx=(0, 8))
        self.fault_button = ttk.Button(frame, text="Arm", command=self._arm)
        self.fault_button.grid(row=1, column=2, sticky="w")
        self.kick_button = ttk.Button(frame, text="Drop all controllers", command=self._kick)
        self.kick_button.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.fault_label = ttk.Label(frame, wraplength=380, justify="left",
                                     text="The numbers count the next messages of any controller.")
        self.fault_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.salvo_label = ttk.Label(frame, wraplength=380, justify="left", text="Salvo groups: nothing stored.")
        self.salvo_label.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_crosspoints(self, parent: tk.Widget) -> None:
        """The Crosspoints group: one row per destination."""
        frame = ttk.LabelFrame(parent, text="Crosspoints", padding=8)
        frame.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.count_label = ttk.Label(frame, wraplength=640, justify="left",
                                     text="Select a destination, then set a source, a name or the protection on the left.")
        self.count_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.table = make_table(frame, 9)
        self.table.bind("<<TreeviewSelect>>", lambda _event: self._row_selected())
        self.table.bind("<Double-1>", lambda _event: self.source_entry.focus_set())

    def _build_trace(self, parent: tk.Widget) -> None:
        """The Trace group: counters, switches and every frame."""
        frame = ttk.LabelFrame(parent, text="Trace", padding=8)
        frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        top = ttk.Frame(frame)
        top.grid(row=0, column=0, columnspan=2, sticky="we")
        ttk.Checkbutton(top, text="Show ACK and NAK", variable=self.acks_var, command=self._redraw).pack(
            side="left")
        ttk.Checkbutton(top, text="Hold the display", variable=self.hold_var, command=self._redraw).pack(
            side="left", padx=(10, 0))
        ttk.Button(top, text="Clear and count anew", command=self.engine.clear_trace).pack(side="right")
        self.trace_text = text_box(frame, 10)
        frame.rowconfigure(1, weight=1)
        self.stats_label = ttk.Label(frame, text="", wraplength=640, justify="left")
        self.stats_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _build_log(self, parent: tk.Widget) -> None:
        """The Activity group."""
        frame = ttk.LabelFrame(parent, text="Activity", padding=8)
        frame.grid(row=2, column=0, sticky="we")
        top = ttk.Frame(frame)
        top.grid(row=0, column=0, columnspan=2, sticky="we")
        ttk.Label(top, text="What controllers ask for, and what was changed here.").pack(side="left")
        ttk.Button(top, text="Clear", command=self.engine.clear_log).pack(side="right")
        self.log_text = text_box(frame, 5)

    # -- user input -> engine -------------------------------------------

    def _redraw(self) -> None:
        """Draw again on the next poll."""
        self._seen_revision = -1

    def _push(self, name: str, value: Any) -> None:
        """Send one setting to the engine and remember an error for the display."""
        if self._loading:
            return

        def work() -> None:
            """A change of the size restarts a running router, which takes a moment."""
            try:
                self.engine.update(**{name: value})
                self._input_error = ""
            except ValueError as exc:
                self._input_error = str(exc)
            self._redraw()
        threading.Thread(target=work, daemon=True).start()

    def _toggle_serving(self) -> None:
        """Start or stop the router."""
        work = self.engine.stop_serving if self.engine.running else self.engine.start
        threading.Thread(target=work, daemon=True).start()
        self._input_error = ""
        self._redraw()

    def _try(self, work: Any, done: str) -> None:
        """Run a change and show how it went below the form."""
        try:
            work()
        except ValueError as exc:
            self._message, self._message_failed = str(exc), True
        else:
            self._message, self._message_failed = done, False
        self._redraw()

    def _set(self) -> None:
        """Set the crosspoint of the form."""
        level = "all" if self.level_var.get() == ALL_LEVELS else self.level_var.get()
        source, dest = self.source_var.get(), self.dest_var.get()
        self._try(lambda: self.engine.set_crosspoint(source, dest, level, bool(self.announce_var.get())),
                  f"Source {source} is on destination {dest}.")

    def _rename(self, kind: str) -> None:
        """Give the source or the destination of the form the name of the form."""
        number = self.source_var.get() if kind == "source" else self.dest_var.get()
        self._try(lambda: self.engine.set_name(kind, number, self.name_var.get()),
                  f"{'Source' if kind == 'source' else 'Destination'} {number} is named.")

    def _protect(self) -> None:
        """Protect the destination of the form, or release it if it is protected."""
        dest = self.dest_var.get().strip()
        protected = dest.isdigit() and int(dest) in self.engine.settings.protected
        self._try(lambda: self.engine.protect(dest, not protected),
                  f"Destination {dest} is {'released' if protected else 'protected'}.")

    def _arm(self) -> None:
        """Arm the faults of the form."""
        self._try(lambda: self.engine.inject(self.drop_var.get(), self.nak_var.get()), "Armed.")

    def _kick(self) -> None:
        """Drop every controller."""
        self._try(self.engine.disconnect_clients, "All controllers dropped.")

    def _row_selected(self) -> None:
        """A destination was picked in the table: put it into the form."""
        chosen = self.table.selection()
        if chosen:
            self.dest_var.set(chosen[0])

    # -- engine -> widgets ----------------------------------------------

    def poll(self) -> None:
        """Redraw the tab if the router engine changed since the last look."""
        if self.engine.revision != self._seen_revision:
            self._load(self.engine.snapshot())

    def _load(self, data: dict) -> None:
        """Draw a snapshot of the engine."""
        self._seen_revision = data["revision"]
        settings = data["settings"]
        status = data["status"]
        focused = self.focus_get()

        self._loading = True
        try:
            for name in ENTRY_FIELDS:
                if self.entries[name] is not focused and self.vars[name].get() != str(settings[name]):
                    self.vars[name].set(str(settings[name]))
            for name in ("extended", "autostart"):
                if self.vars[name].get() != settings[name]:
                    self.vars[name].set(settings[name])
            self.vars["protocol"].set(PROTOCOL_LABELS[settings["protocol"]])
        finally:
            self._loading = False

        levels = status["levels"]
        if levels != self._levels:
            self._levels = levels
            choices = [str(level) for level in range(1, levels + 1)]
            self.level_box.configure(values=([ALL_LEVELS] if levels > 1 else []) + choices)
            if self.level_var.get() not in choices + ([ALL_LEVELS] if levels > 1 else []):
                self.level_var.set("1")
            set_columns(self.table, [("dest", "Dest", 50), ("name", "Name", 130), ("lock", "Protected", 70)]
                        + [(f"level{level}", "Source" if levels == 1 else f"Level {level}", 120)
                           for level in range(1, levels + 1)] + [("changed", "Last change", 80)])
            self._shown.clear()

        running = status["running"]
        count = len(status["clients"])
        self.start_button.configure(text="Stop" if running else "Start")
        self.state_label.configure(
            text=f"Listening on {status['description']}, {count} controller{'' if count == 1 else 's'}"
            if running else "Router off")
        self.clients_label.configure(text="\n".join(
            f"{client['address']}  since {clock(client['since'])}, {client['messages']} messages"
            for client in status["clients"][:6]))
        self.error_label.configure(text=self._input_error or status["error"], foreground=error_color(self))
        self.message_label.configure(text=self._message,
                                     foreground=error_color(self) if self._message_failed else "")
        self.fault_button.configure(state="normal" if running else "disabled")
        self.kick_button.configure(state="normal" if running and count else "disabled")
        self.fault_label.configure(
            text=f"Armed: the next {status['drop_pending']} get no answer, the next "
                 f"{status['nak_pending']} a NAK."
            if status["drop_pending"] or status["nak_pending"]
            else "The numbers count the next messages of any controller.")

        self.salvo_label.configure(text="Salvo groups: " + ("; ".join(
            f"{group['number']} holds {len(group['entries'])}" for group in status["salvos"])
            if status["salvos"] else "nothing stored") + ".")

        names = {source["number"]: source["name"] for source in data["sources"]}
        rows = [(str(dest["number"]), (dest["number"], dest["name"], "yes" if dest["protected"] else "",
                                       *(f"{source} {names.get(source, '')}".strip()
                                         for source in dest["sources"]),
                                       clock(dest["changed"])))
                for dest in data["destinations"][:TABLE_ROWS]]
        sync_table(self.table, rows, self._shown)

        if not self.hold_var.get():
            show_trace(self.trace_text, data["trace"], bool(self.acks_var.get()))
        stats = status["stats"]
        self.stats_label.configure(
            text=f"Received {stats['received']} · Sent {stats['sent']} · Takes {stats['takes']} · "
                 f"Refused takes {stats['refused']} · NAK sent {stats['naks_sent']} · "
                 f"NAK received {stats['naks']} · Broken frames {stats['errors']}")
        show_log(self.log_text, status["log"], "Nothing happened yet.")
