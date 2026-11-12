"""Tkinter desktop window: the Controller tab is built here, the other tabs in their modules."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any

from . import __version__
from .access import Access
from .controller_engine import ControllerEngine
from .gui_analyzer import AnalyzerTab
from .gui_colors import error_color
from .gui_common import (TABLE_ROWS, clock, group, in_background, make_table, port_text, set_columns,
                         show_log, show_trace, sync_table, text_box)
from .gui_router import RouterTab
from .gui_settings import AboutTab, SettingsTab
from .router_engine import RouterEngine

POLL_MS = 100
ENTRY_FIELDS = ("host", "port", "matrix", "levels", "attempts", "destinations")
SWITCHES = (("query_commands", "Ask for implemented commands"), ("fetch_names", "Read names"),
            ("fetch_tally", "Read all crosspoints"), ("wait_ack", "Wait for ACK"),
            ("keepalive", "Keep-alive"), ("auto_reconnect", "Reconnect by itself"))
EXTENDED_LABELS = {"auto": "automatic", "on": "always", "off": "never"}
PROTOCOL_LABELS = {"sw-p-08": "SW-P-08", "sw-p-02": "SW-P-02"}
ALL_LEVELS = "All levels"


class ControllerTab(ttk.Frame):
    """The Controller tab: connection, takes, own messages, crosspoints, trace and activity.

    The tab never talks to the network itself. Every input goes to the
    controller engine, requests that wait for the router run in background
    threads, and :meth:`poll` redraws the widgets when the engine changed.
    That keeps window and web interface in step.
    """

    def __init__(self, parent: tk.Widget, engine: ControllerEngine):
        """Build the tab.

        Args:
            parent: The notebook the tab belongs to.
            engine: The controller engine.
        """
        super().__init__(parent, padding=10)
        self.engine = engine
        self._loading = False
        self._seen_revision = -1
        self._input_error = ""
        self._result = ""               # outcome of the last request to the router
        self._result_failed = False
        self._shown: dict[str, tuple] = {}
        self._levels = 0

        self.vars: dict[str, tk.Variable] = {name: tk.StringVar() for name in ENTRY_FIELDS}
        for name, _label in SWITCHES:
            self.vars[name] = tk.BooleanVar()
        self.vars["confirm"] = tk.BooleanVar()
        self.vars["extended"] = tk.StringVar()
        self.vars["name_length"] = tk.StringVar()
        self.vars["protocol"] = tk.StringVar()
        self.salvo_var = tk.StringVar(value="0")
        self.entries: dict[str, ttk.Entry] = {}
        self.source_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        self.level_var = tk.StringVar(value="1")
        self.raw_var = tk.StringVar()
        self.framed_var = tk.BooleanVar(value=True)
        self.acks_var = tk.BooleanVar(value=True)
        self.hold_var = tk.BooleanVar(value=False)

        left = ttk.Frame(self)
        right = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="n", padx=(0, 10))
        right.grid(row=0, column=1, sticky="nsew")
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._build_connection(left)
        self._build_take(left)
        self._build_raw(left)
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

    def _build_connection(self, parent: tk.Widget) -> None:
        """The Connection group: address, protocol options, switches, Connect button and state."""
        frame = group(parent, "Connection")
        for column, (name, label, width) in enumerate((("host", "Host", 16), ("port", "Port", 7),
                                                        ("matrix", "Matrix", 5), ("levels", "Levels", 5),
                                                        ("attempts", "Attempts", 5))):
            ttk.Label(frame, text=label).grid(row=0, column=column, sticky="w", padx=(0, 8))
            self._entry(frame, name, width).grid(row=1, column=column, sticky="w", padx=(0, 8))
        ttk.Label(frame, text="Extended commands").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(frame, text="Name length").grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(frame, text="Protocol").grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(frame, text="Ask dests").grid(row=2, column=4, sticky="w", pady=(6, 0))
        protocol = ttk.Combobox(frame, textvariable=self.vars["protocol"], width=9, state="readonly",
                                values=tuple(PROTOCOL_LABELS.values()))
        protocol.grid(row=3, column=2, columnspan=2, sticky="w")
        protocol.bind("<<ComboboxSelected>>", lambda _event: self._push("protocol", next(
            key for key, label in PROTOCOL_LABELS.items() if label == self.vars["protocol"].get())))
        self._entry(frame, "destinations", 5).grid(row=3, column=4, sticky="w")
        extended = ttk.Combobox(frame, textvariable=self.vars["extended"], width=12, state="readonly",
                                values=tuple(EXTENDED_LABELS.values()))
        extended.grid(row=3, column=0, sticky="w")
        extended.bind("<<ComboboxSelected>>", lambda _event: self._push("extended", next(
            key for key, label in EXTENDED_LABELS.items() if label == self.vars["extended"].get())))
        length = ttk.Combobox(frame, textvariable=self.vars["name_length"], width=5, state="readonly",
                              values=("4", "8", "12"))
        length.grid(row=3, column=1, sticky="w")
        length.bind("<<ComboboxSelected>>",
                    lambda _event: self._push("name_length", self.vars["name_length"].get()))
        for index, (name, label) in enumerate(SWITCHES):
            ttk.Checkbutton(frame, text=label, variable=self.vars[name],
                            command=lambda name=name: self._push(name, bool(self.vars[name].get()))
                            ).grid(row=4 + index // 2, column=(index % 2) * 2, columnspan=2, sticky="w",
                                   pady=(6 if index < 2 else 0, 0))
        self.connect_button = ttk.Button(frame, text="Connect", command=self._toggle_connection)
        self.connect_button.grid(row=8, column=0, sticky="w", pady=(8, 0))
        self.state_label = ttk.Label(frame, text="Disconnected", wraplength=380, justify="left")
        self.state_label.grid(row=9, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.error_label = ttk.Label(frame, text="", wraplength=380, justify="left")
        self.error_label.grid(row=10, column=0, columnspan=5, sticky="w")

    def _build_take(self, parent: tk.Widget) -> None:
        """The Take group: source, destination, level, and the buttons that read again."""
        frame = group(parent, "Take")
        ttk.Label(frame, text="Source, number or name").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Label(frame, text="Destination").grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Label(frame, text="Level").grid(row=0, column=2, sticky="w")
        source = ttk.Entry(frame, textvariable=self.source_var, width=18)
        source.grid(row=1, column=0, sticky="w", padx=(0, 8))
        source.bind("<Return>", lambda _event: self._take())
        self.source_entry = source
        dest = ttk.Entry(frame, textvariable=self.dest_var, width=18)
        dest.grid(row=1, column=1, sticky="w", padx=(0, 8))
        dest.bind("<Return>", lambda _event: self._take())
        self.level_box = ttk.Combobox(frame, textvariable=self.level_var, width=9, state="readonly")
        self.level_box.grid(row=1, column=2, sticky="w")
        ttk.Checkbutton(frame, text="Wait for the confirmation of the router", variable=self.vars["confirm"],
                        command=lambda: self._push("confirm", bool(self.vars["confirm"].get()))
                        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.request_buttons = [
            ttk.Button(buttons, text="Take", command=self._take),
            ttk.Button(buttons, text="Ask the router", command=self._ask),
        ]
        salvo = ttk.Frame(frame)
        salvo.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(salvo, text="Salvo group").pack(side="left", padx=(0, 6))
        ttk.Entry(salvo, textvariable=self.salvo_var, width=5).pack(side="left", padx=(0, 6))
        self.request_buttons += [
            ttk.Button(salvo, text="Store this take", command=self._salvo_add),
            ttk.Button(salvo, text="Set all", command=lambda: self._salvo_go(False)),
            ttk.Button(salvo, text="Read", command=self._salvo_read),
            ttk.Button(salvo, text="Clear", command=lambda: self._salvo_go(True)),
        ]
        again = ttk.Frame(frame)
        again.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(again, text="Read again:").pack(side="left", padx=(0, 6))
        self.request_buttons += [
            ttk.Button(again, text="Crosspoints", command=lambda: self._request(
                self.engine.dump, lambda result: " · ".join(
                    f"level {level}: {count} crosspoints read" for level, count in result.items()))),
            ttk.Button(again, text="Names", command=lambda: self._request(
                self.engine.read_names, lambda result:
                f"{result['sources']} source names and {result['destinations']} destination names read")),
            ttk.Button(again, text="Commands", command=lambda: self._request(
                self.engine.query_commands, lambda result: f"The router implements {len(result)} commands")),
            ttk.Button(again, text="Dual status", command=lambda: self._request(
                self.engine.dual_status, lambda result:
                f"Dual controller status {result['status'] or 'without bytes'}"
                + ("" if result["bit1"] is None
                   else f": the active/idle bit is {'set' if result['bit1'] else 'clear'}"))),
        ]
        for button in self.request_buttons:
            button.pack(side="left", padx=(0, 6))
        self.result_label = ttk.Label(frame, text="", wraplength=380, justify="left")
        self.result_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_raw(self, parent: tk.Widget) -> None:
        """The Own message group: hex bytes to send as a message or exactly as written."""
        frame = group(parent, "Own message")
        ttk.Label(frame, text="Bytes in hex, for example 01 00 00 02").grid(row=0, column=0, columnspan=2,
                                                                            sticky="w")
        entry = ttk.Entry(frame, textvariable=self.raw_var, width=34, font="TkFixedFont")
        entry.grid(row=1, column=0, sticky="w", padx=(0, 8))
        entry.bind("<Return>", lambda _event: self._send_raw())
        self.raw_button = ttk.Button(frame, text="Send", command=self._send_raw)
        self.raw_button.grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(frame, text="As a message: frame it and wait for ACK", variable=self.framed_var
                        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(frame, wraplength=380, justify="left",
                  text="Without the frame the bytes go out exactly as written, to try a router with "
                       "frames that are wrong on purpose.").grid(row=3, column=0, columnspan=2, sticky="w")

    def _build_crosspoints(self, parent: tk.Widget) -> None:
        """The Crosspoints group: one row per destination."""
        frame = ttk.LabelFrame(parent, text="Crosspoints", padding=8)
        frame.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.count_label = ttk.Label(frame, text="Not connected.", wraplength=640, justify="left")
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
        ttk.Label(top, text="Takes, answers and crosspoints that changed at the router.").pack(side="left")
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
            """Changing a setting may end a connection, which waits for the network."""
            try:
                self.engine.update(**{name: value})
                self._input_error = ""
            except ValueError as exc:
                self._input_error = str(exc)
            self._redraw()
        threading.Thread(target=work, daemon=True).start()

    def _toggle_connection(self) -> None:
        """Connect or disconnect, without freezing the window while that takes."""
        status = self.engine.snapshot()["status"]
        if status["connected"] or status["waiting"]:
            threading.Thread(target=self.engine.disconnect, daemon=True).start()
        else:
            threading.Thread(target=self.engine.connect, daemon=True).start()
        self._input_error = ""
        self._redraw()

    def _request(self, work: Any, done: Any) -> None:
        """Run a request to the router in the background and show its outcome below the buttons."""
        self._result = "Waiting for the router..."
        self._result_failed = False
        self._redraw()

        def finished(message: str, result: Any) -> None:
            """Called from the background thread."""
            self._result = message or done(result)
            self._result_failed = bool(message)
            self._redraw()
        in_background(work, finished)

    def _level(self) -> Any:
        """The chosen level as the engine takes it."""
        return "all" if self.level_var.get() == ALL_LEVELS else self.level_var.get()

    def _take(self) -> None:
        """Route the source in the form to the destination in the form."""
        source, dest, level = self.source_var.get(), self.dest_var.get(), self._level()
        self._request(lambda: self.engine.route(source, dest, level), lambda result: (
            f"Source {result['source']} is on destination {result['dest']}, level "
            f"{', '.join(str(item) for item in result['levels'])}"
            + (", confirmed by the router" if result["confirmed"] else ", sent without waiting")))

    def _ask(self) -> None:
        """Ask the router which source is on the destination in the form."""
        dest, level = self.dest_var.get(), self._level()
        self._request(lambda: self.engine.interrogate(dest, None if level == "all" else level),
                      lambda result: " · ".join(f"level {item}: source {source}"
                                                for item, source in result.items()))

    def _salvo_add(self) -> None:
        """Store the take of the form in the salvo group of the form."""
        source, dest, level, salvo = self.source_var.get(), self.dest_var.get(), self._level(), self.salvo_var.get()
        self._request(lambda: self.engine.salvo_add(source, dest, salvo, level), lambda result: (
            f"Stored in salvo {result['salvo']}: source {result['source']} to destination {result['dest']}, "
            f"level {', '.join(str(item) for item in result['levels'])}. Nothing is switched yet"))

    def _salvo_go(self, clear: bool) -> None:
        """Set the stored crosspoints of the salvo group, or clear them."""
        salvo = self.salvo_var.get()
        texts = {"set": "the stored crosspoints were set", "cleared": "the stored crosspoints were cleared",
                 "empty": "the group holds nothing"}
        self._request(lambda: self.engine.salvo_go(salvo, clear),
                      lambda result: f"Salvo {result['salvo']}: {texts.get(result['outcome'], result['outcome'])}")

    def _salvo_read(self) -> None:
        """Ask the router what the salvo group holds."""
        salvo = self.salvo_var.get()
        self._request(lambda: self.engine.salvo_read(salvo), lambda result: (
            f"Salvo {salvo} holds: " + " · ".join(
                f"level {entry['level']}, destination {entry['dest']} from source {entry['source']}"
                for entry in result) if result else f"Salvo {salvo} holds nothing"))

    def _send_raw(self) -> None:
        """Send the bytes of the Own message group."""
        data, framed = self.raw_var.get(), bool(self.framed_var.get())
        self._request(lambda: self.engine.send_raw(data, framed), lambda result: (
            f"Sent {result['sent']}" + (", acknowledged by the router" if result["framed"] else " as written")))

    def _row_selected(self) -> None:
        """A destination was picked in the table: put it into the form."""
        chosen = self.table.selection()
        if chosen:
            self.dest_var.set(chosen[0])

    # -- engine -> widgets ----------------------------------------------

    def poll(self) -> None:
        """Redraw the tab if the controller engine changed since the last look."""
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
            for name in [name for name, _label in SWITCHES] + ["confirm"]:
                if self.vars[name].get() != settings[name]:
                    self.vars[name].set(settings[name])
            self.vars["extended"].set(EXTENDED_LABELS[settings["extended"]])
            self.vars["protocol"].set(PROTOCOL_LABELS[settings["protocol"]])
            self.vars["name_length"].set(str(settings["name_length"]))
        finally:
            self._loading = False

        levels = status["levels"]
        if levels != self._levels:
            self._levels = levels
            choices = [str(level) for level in range(1, levels + 1)]
            self.level_box.configure(values=([ALL_LEVELS] if levels > 1 else []) + choices)
            if self.level_var.get() not in choices + ([ALL_LEVELS] if levels > 1 else []):
                self.level_var.set("1")
            set_columns(self.table, [("dest", "Dest", 50), ("name", "Name", 130)]
                        + [(f"level{level}", "Source" if levels == 1 else f"Level {level}", 120)
                           for level in range(1, levels + 1)] + [("changed", "Last change", 80)])
            self._shown.clear()

        connected = status["connected"]
        if status["connecting"]:
            self.connect_button.configure(text="Connecting...", state="disabled")
        else:
            self.connect_button.configure(
                text="Disconnect" if connected or status["waiting"] else "Connect", state="normal")
        for button in self.request_buttons + [self.raw_button]:
            button.configure(state="normal" if connected else "disabled")
        state = "Waiting for the router" if status["waiting"] else "Disconnected"
        if connected:
            state = (f"Connected: {status['description']}, {status['destination_count']} destinations, "
                     f"{len(data['sources'])} source names")
            if status["commands_known"]:
                state += f", {len(status['commands'])} commands implemented"
        self.state_label.configure(text=state)
        self.error_label.configure(text=self._input_error or status["error"], foreground=error_color(self))
        self.result_label.configure(text=self._result,
                                    foreground=error_color(self) if self._result_failed else "")

        names = {source["number"]: source["name"] for source in data["sources"]}
        rows = [(str(dest["number"]), (dest["number"], dest["name"],
                                       *(port_text(source, names) for source in dest["sources"]),
                                       clock(dest["changed"])))
                for dest in data["destinations"][:TABLE_ROWS]]
        sync_table(self.table, rows, self._shown)
        total = status["destination_count"]
        if not total:
            note = ("Nothing known yet. Read the crosspoints or the names." if connected else "Not connected.")
        else:
            note = (f"Showing {len(rows)} of {total} destinations. " if total > len(rows) else "") + \
                   "Select a destination, type a source on the left and press Return."
        self.count_label.configure(text=note)

        if not self.hold_var.get():
            show_trace(self.trace_text, data["trace"], bool(self.acks_var.get()))
        stats = status["stats"]
        line = (f"Sent {stats['sent']} · Received {stats['received']} · ACK {stats['acks']} · "
                f"NAK {stats['naks']} · Repeated {stats['repeats']} · Broken frames {stats['errors']}")
        if stats["ack_ms_last"] is not None:
            line += (f" · ACK after {stats['ack_ms_last']} ms (mean {stats['ack_ms_mean']}, "
                     f"longest {stats['ack_ms_max']}, {stats['ack_slow']} slower than 10 ms)")
        self.stats_label.configure(text=line)
        show_log(self.log_text, status["log"], "Nothing happened yet.")


class MainWindow:
    """The desktop window with its tabs: Controller, Router, Analyzer, Settings and About.

    Args:
        root: The Tk root window.
        controller: The controller engine.
        router: The router engine.
        access: The access settings. Without them there is no Settings tab.
        server: The web server, for its address in status bar and Settings.
    """

    def __init__(self, root: tk.Tk, controller: ControllerEngine, router: RouterEngine,
                 access: Access | None = None, server=None):
        """Build all tabs and start the poll. See the class description for the arguments."""
        self.root = root
        self.server = server
        root.title(f"Probel Pangolin {__version__}")
        try:
            self.logo = tk.PhotoImage(file=str(Path(__file__).with_name("static") / "logo.png"))
            root.iconphoto(True, self.logo)
        except tk.TclError:
            self.logo = None  # the window works without its picture

        notebook = ttk.Notebook(root)
        notebook.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.controller_tab = ControllerTab(notebook, controller)
        notebook.add(self.controller_tab, text="Controller")
        self.router_tab = RouterTab(notebook, router)
        notebook.add(self.router_tab, text="Router")
        self.analyzer_tab = AnalyzerTab(notebook, controller)
        notebook.add(self.analyzer_tab, text="Analyzer")
        self.settings_tab = None
        if access is not None:
            self.settings_tab = SettingsTab(notebook, access, server)
            notebook.add(self.settings_tab, text="Settings")
        notebook.add(AboutTab(notebook, self.logo), text="About")
        self.web_label = ttk.Label(root, text="", padding=(10, 0, 10, 6))
        self.web_label.grid(row=1, column=0, sticky="e")
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        self._poll()

    def _poll(self) -> None:
        """Let every tab redraw itself if needed, ten times a second."""
        self.controller_tab.poll()
        self.router_tab.poll()
        self.analyzer_tab.poll()
        if self.settings_tab is not None:
            self.settings_tab.poll()
        self.web_label.configure(text=self._web_state())
        self.root.after(POLL_MS, self._poll)

    def _web_state(self) -> str:
        """Where the web interface is, for the line at the bottom."""
        server = self.server
        if server is None:
            return ""
        if server.error:
            return "Web server: error, see Settings"
        if not server.running:
            return "Web interface and REST API: off"
        return f"Web: {server.url}"


def run_gui(controller: ControllerEngine, router: RouterEngine, access: Access | None = None,
            server=None) -> None:
    """Open the window and run until it is closed.

    Args:
        controller: The controller engine.
        router: The router engine.
        access: The access settings, for the Settings tab.
        server: The web server, for its address and state.
    """
    root = tk.Tk()
    MainWindow(root, controller, router, access, server)
    root.mainloop()
