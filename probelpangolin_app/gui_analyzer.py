"""Analyzer tab of the Tkinter window: reads captured bytes and builds messages."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from .analyzer import BUILD_KINDS, BUILD_KINDS_02, build_message, decode_capture, frame_message
from .common import PROTOCOLS, SWP02, SWP08, hex_text, parse_hex
from .controller_engine import ControllerEngine
from .gui_colors import error_color, muted_color
from .gui_common import in_background

EXAMPLE = "\n".join((
    "10 02 02 01 00 02 04 05 F2 10 03",          # connect: level 2, destination 3 <- source 5
    "10 06",                                     # ACK
    "10 02 04 01 00 02 04 05 F0 10 03",          # connected
    "10 06",
    "10 02 02 00 00 10 10 10 10 05 D9 10 03",    # destination 17 <- source 17: 0x10 is doubled
    "10 06",
    "10 02 01 00 00 02 04 99 10 03",             # wrong checksum
    "10 15",                                     # NAK
))
EXAMPLE_02 = "\n".join((
    "FF 02 00 02 04 78",       # connect: destination 3 <- source 5
    "FF 04 00 02 04 76",       # connected
    "FF 01 00 02 7D",          # interrogate destination 3
    "FF 03 00 02 04 77",       # tally
    "FF 06 00 7A",             # go
    "FF 0D 02 71",             # go done: nothing to set
    "FF 03 00 02 04 70",       # wrong checksum
))
NUMBER_FIELDS = (("matrix", "Matrix"), ("level", "Level"), ("dest", "Destination"), ("source", "Source"),
                 ("name_length", "Name length"), ("salvo", "Salvo"), ("index", "Index"))
START_VALUES = {"name_length": "8", "salvo": "0", "index": "0"}


COUNTING = ("Matrix, level, source and destination count from 1, as on a panel. On the wire they count "
            "from 0. Salvo groups and their indexes are 0 to 127 as they are.")


class AnalyzerTab(ttk.Frame):
    """The Analyzer tab. It needs no connection, only the Send button uses the controller."""

    def __init__(self, parent: tk.Widget, controller: ControllerEngine):
        """Build the tab.

        Args:
            parent: The notebook the tab belongs to.
            controller: The controller engine, for sending a built message.
        """
        super().__init__(parent, padding=10)
        self.controller = controller
        self._note = ""
        self._note_failed = False
        self._note_shown: tuple[str, bool] | None = None

        self.kind_var = tk.StringVar(value=BUILD_KINDS["connect"])
        self.protocol_var = tk.StringVar(value=PROTOCOLS[SWP08])
        self.fields: dict[str, tk.StringVar] = {name: tk.StringVar(value=START_VALUES.get(name, "1"))
                                                for name, _label in NUMBER_FIELDS}
        self.extended_var = tk.BooleanVar(value=False)
        self.payload_var = tk.StringVar()

        self._build_decode()
        self._build_builder()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

    # -- layout ----------------------------------------------------------

    def _build_decode(self) -> None:
        """The group that reads captured bytes."""
        frame = ttk.LabelFrame(self, text="Read captured bytes", padding=8)
        frame.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        top = ttk.Frame(frame)
        top.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(top, text="Protocol").pack(side="left", padx=(0, 6))
        chooser = ttk.Combobox(top, textvariable=self.protocol_var, width=9, state="readonly",
                               values=tuple(PROTOCOLS.values()))
        chooser.pack(side="left", padx=(0, 12))
        chooser.bind("<<ComboboxSelected>>", lambda _event: self._protocol_changed())
        ttk.Label(top, text="Bytes in hex, in the order they crossed the wire. Both directions may be mixed."
                  ).pack(side="left")
        self.capture = tk.Text(frame, width=90, height=6, wrap="word", font="TkFixedFont", relief="solid",
                               borderwidth=1)
        self.capture.grid(row=1, column=0, columnspan=2, sticky="we", pady=(6, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        for text, command in (("Read", self._decode), ("Example", self._example), ("Empty", self._empty),
                              ("Take the controller trace", self._take_trace)):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6))
        self.summary = ttk.Label(frame, text="", wraplength=760, justify="left")
        self.summary.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.result = tk.Text(frame, width=90, height=14, wrap="word", font="TkFixedFont", state="disabled",
                              relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.result.yview)
        self.result.configure(yscrollcommand=scroll.set)
        self.result.grid(row=4, column=0, sticky="nsew", pady=(6, 0))
        scroll.grid(row=4, column=1, sticky="ns", pady=(6, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)

    def _build_builder(self) -> None:
        """The group that builds a message and shows its frame."""
        frame = ttk.LabelFrame(self, text="Build a message", padding=8)
        frame.grid(row=1, column=0, sticky="we")
        ttk.Label(frame, text="Message").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.kind_box = ttk.Combobox(frame, textvariable=self.kind_var, width=30, state="readonly",
                                     values=tuple(BUILD_KINDS.values()))
        self.kind_box.grid(row=1, column=0, sticky="w", padx=(0, 8))
        for column, (name, label) in enumerate(NUMBER_FIELDS, start=1):
            ttk.Label(frame, text=label).grid(row=0, column=column, sticky="w", padx=(0, 8))
            ttk.Entry(frame, textvariable=self.fields[name], width=8).grid(row=1, column=column, sticky="w",
                                                                           padx=(0, 8))
        ttk.Checkbutton(frame, text="Extended", variable=self.extended_var).grid(row=1, column=8, sticky="w",
                                                                                 padx=(0, 8))
        ttk.Button(frame, text="Build", command=self._build).grid(row=1, column=9, sticky="w")
        ttk.Label(frame, text="Data bytes of the message, to change by hand").grid(
            row=2, column=0, columnspan=10, sticky="w", pady=(8, 0))
        entry = ttk.Entry(frame, textvariable=self.payload_var, width=60, font="TkFixedFont")
        entry.grid(row=3, column=0, columnspan=6, sticky="w")
        entry.bind("<Return>", lambda _event: self._frame())
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=6, columnspan=4, sticky="w")
        ttk.Button(buttons, text="Frame", command=self._frame).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Send with the controller", command=self._send).pack(side="left")
        self.frame_label = ttk.Label(frame, text="", font="TkFixedFont", wraplength=760, justify="left")
        self.frame_label.grid(row=4, column=0, columnspan=10, sticky="w", pady=(6, 0))
        self.note_label = ttk.Label(frame, wraplength=760, justify="left", text=COUNTING)
        self.note_label.grid(row=5, column=0, columnspan=10, sticky="w", pady=(4, 0))

    # -- protocol --------------------------------------------------------

    def _protocol(self) -> str:
        """The chosen protocol as the analyzer takes it."""
        return SWP02 if self.protocol_var.get() == PROTOCOLS[SWP02] else SWP08

    def _kinds(self) -> dict[str, str]:
        """The messages the chosen protocol has, key to title."""
        return BUILD_KINDS_02 if self._protocol() == SWP02 else BUILD_KINDS

    def _protocol_changed(self) -> None:
        """Offer the messages of the chosen protocol and read the capture again."""
        kinds = self._kinds()
        self.kind_box.configure(values=tuple(kinds.values()))
        self.kind_var.set(kinds["connect"])
        self.payload_var.set("")
        self.frame_label.configure(text="")
        if self.capture.get("1.0", "end").strip():
            self._decode()

    # -- reading ---------------------------------------------------------

    def _decode(self) -> None:
        """Read the bytes of the text area and list what they hold."""
        try:
            data = decode_capture(parse_hex(self.capture.get("1.0", "end"), "The captured bytes"),
                                  self._protocol())
        except ValueError as exc:
            self.summary.configure(text=str(exc), foreground=error_color(self))
            self._show_rows([])
            return
        if data["protocol"] == SWP02:
            text = f"{data['bytes']} bytes · {data['messages']} messages · {data['errors']} not readable"
        else:
            text = (f"{data['bytes']} bytes · {data['messages']} messages · {data['acks']} ACK · "
                    f"{data['naks']} NAK · {data['errors']} broken")
        if data["truncated"]:
            text += f" · only the first {len(data['rows'])} entries are listed"
        self.summary.configure(text=text, foreground="")
        self._show_rows(data["rows"])

    def _show_rows(self, rows: list[dict[str, Any]]) -> None:
        """Write the decoded entries into the result area."""
        widget = self.result
        widget.tag_configure("problem", foreground=error_color(widget))
        widget.tag_configure("other", foreground=muted_color(widget))
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for row in rows:
            tags: tuple = ("problem",) if row["problem"] else ()
            widget.insert("end", f"{row['offset']:>6}  {row['hex']}\n", tags)
            widget.insert("end", f"        {row['text']}\n", tags)
            if row["detail"]:
                widget.insert("end", f"        {row['detail']}\n", ("other",))
        widget.configure(state="disabled")

    def _example(self) -> None:
        """Put an example capture into the text area and read it."""
        self._set_capture(EXAMPLE_02 if self._protocol() == SWP02 else EXAMPLE)
        self._decode()

    def _empty(self) -> None:
        """Empty the text area and the result."""
        self._set_capture("")
        self.summary.configure(text="")
        self._show_rows([])

    def _take_trace(self) -> None:
        """Put the frames of the controller trace into the text area, oldest first, and read them."""
        rows = self.controller.trace()
        protocol = self.controller.settings.protocol
        if protocol != self._protocol():
            self.protocol_var.set(PROTOCOLS[protocol])
            self._protocol_changed()
        # A broken SW-P-02 message is kept in the trace without its start byte: put it back.
        self._set_capture("\n".join(f"FF {row['hex']}" if protocol == SWP02 and row["kind"] == "error"
                                    else row["hex"] for row in rows))
        if rows:
            self._decode()
        else:
            self.summary.configure(text="The trace of the controller is empty.", foreground="")

    def _set_capture(self, text: str) -> None:
        """Replace the content of the text area."""
        self.capture.delete("1.0", "end")
        self.capture.insert("1.0", text)

    # -- building --------------------------------------------------------

    def _build(self) -> None:
        """Build the chosen message and show its frame."""
        kind = next(key for key, label in self._kinds().items() if label == self.kind_var.get())
        options: dict[str, Any] = {name: variable.get() for name, variable in self.fields.items()}
        options["extended"] = bool(self.extended_var.get())
        try:
            payload = build_message(kind, self._protocol(), **options)
        except ValueError as exc:
            self._say(str(exc), True)
            return
        self.payload_var.set(hex_text(payload))
        self._frame()

    def _frame(self) -> None:
        """Show the frame for the data bytes in the field."""
        try:
            data = frame_message(parse_hex(self.payload_var.get(), "The data bytes"), self._protocol())
        except ValueError as exc:
            self.frame_label.configure(text="")
            self._say(str(exc), True)
            return
        if data["protocol"] == SWP02:
            text = f"{data['frame']}\n{data['text']}\nStart of message FF, checksum {data['checksum']:02X}"
        else:
            text = (f"{data['frame']}\n{data['text']}\nByte count {data['byte_count']:02X}, "
                    f"checksum {data['checksum']:02X}")
        if data["stuffed"]:
            text += f", {data['stuffed']} DLE doubled"
        if data["oversize"]:
            text += "\nLonger than the 128 data bytes that every system is guaranteed to accept."
        self.frame_label.configure(text=text)
        self._say(COUNTING, False)

    def _send(self) -> None:
        """Send the data bytes with the controller, in the background."""
        data = self.payload_var.get()
        if self.controller.settings.protocol != self._protocol():
            self._say("The controller is set to the other protocol. Choose the same one in the Controller tab.",
                      True)
            return
        self._say("Waiting for the router...", False)

        def done(message: str, result: Any) -> None:
            """Called from the background thread: the next poll shows the outcome."""
            self._note = message or f"Sent {result['sent']}. The answer is in the trace of the controller."
            self._note_failed = bool(message)
        in_background(lambda: self.controller.send_raw(data, True), done)

    def _say(self, text: str, failed: bool) -> None:
        """Set the line below the frame."""
        self._note, self._note_failed = text, failed

    def poll(self) -> None:
        """Show the latest note. Notes of background work arrive this way."""
        current = (self._note, self._note_failed)
        if self._note and current != self._note_shown:
            self._note_shown = current
            self.note_label.configure(text=self._note,
                                      foreground=error_color(self) if self._note_failed else "")
