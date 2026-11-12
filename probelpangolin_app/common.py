"""Helpers shared by the controller and the router side of the app.

Validation of settings, the settings file, the background thread that runs
the asyncio code of the library, and the plain-data form of a trace entry
that the window, the web page and the REST API all show.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Coroutine, TypeVar

from probelpangolin import swp02
from probelpangolin.protocol import ProtocolError, TraceEvent, command_name, describe, parse_message

T = TypeVar("T")

# The library reports repeated attempts and lost connections through logging. The app shows
# them in trace and activity log, so they do not need to land on the terminal as well.
logging.getLogger("probelpangolin").addHandler(logging.NullHandler())

MAX_HEX_BYTES = 65536   # longest capture or message a front end may hand in
SWP08 = "sw-p-08"       # General Remote Control Protocol
SWP02 = swp02.PROTOCOL  # General Switcher Communication Protocol, the older one
PROTOCOLS = {SWP08: "SW-P-08", SWP02: "SW-P-02"}
DEFAULT_PORTS = {SWP08: 8910, SWP02: swp02.DEFAULT_PORT}


def check_protocol(value: Any) -> str:
    """Return ``value`` if it names one of the two protocols.

    Raises:
        ValueError: ``value`` is anything else.
    """
    if value not in PROTOCOLS:
        raise ValueError("Protocol must be sw-p-08 or sw-p-02")
    return value


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def to_int(name: str, value: Any, low: int, high: int) -> int:
    """Return ``value`` as a whole number between ``low`` and ``high``.

    Args:
        name: What the number is, for the error message.
        value: A number, or text that holds one.
        low: Smallest allowed value.
        high: Largest allowed value.

    Raises:
        ValueError: ``value`` is not a whole number in that range.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise ValueError(f"{name} must be a whole number") from None
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{name} must be a whole number")
        value = int(value)
    elif not isinstance(value, int):
        raise ValueError(f"{name} must be a number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low} to {high}")
    return value


def to_bool(name: str, value: Any) -> bool:
    """Return ``value`` if it is true or false.

    Raises:
        ValueError: ``value`` is anything else.
    """
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def check_host(label: str, value: Any) -> str:
    """Return a host name or address: not empty, no spaces, no control characters.

    Raises:
        ValueError: ``value`` is not usable as a host.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    value = value.strip()
    if len(value) > 255:
        raise ValueError(f"{label} is too long")
    if any(ch.isspace() or ord(ch) < 33 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{label} must not contain spaces or control characters")
    return value


def default_config_path() -> Path:
    """Where the settings live when no ``--config`` is given."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "probel-pangolin" / "settings.json"


def load_json(path: Path) -> Any:
    """Read a JSON file. Returns ``None`` if it is missing or not valid."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_json(path: Path, data: Any) -> None:
    """Write a JSON file in one step, so a crash never leaves half a file.

    Raises:
        OSError: The file cannot be written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


# ---------------------------------------------------------------------------
# Bytes as text
# ---------------------------------------------------------------------------

def hex_text(data: bytes) -> str:
    """Bytes as upper-case hex pairs with spaces: ``10 02 02 01``."""
    return data.hex(" ").upper()


def parse_hex(text: Any, what: str = "The bytes") -> bytes:
    """Read hex bytes as people write them.

    Accepts ``10 02 0A``, ``10020a``, ``0x10, 0x02`` and line breaks. Single
    digits count as a byte when they stand alone (``2 1 0``).

    Raises:
        ValueError: ``text`` is empty, too long, or holds something that is not hex.
    """
    if not isinstance(text, str):
        raise ValueError(f"{what} must be written as hex, for example 02 01 00 02 04")
    cleaned = text.replace(",", " ").replace(";", " ").replace(":", " ").replace("-", " ")
    out = bytearray()
    for word in cleaned.split():
        if word.lower().startswith("0x"):
            word = word[2:]
        if len(word) == 1:
            word = "0" + word
        if not word or len(word) % 2:
            raise ValueError(f"{what} must be written as hex pairs, {word!r} has an odd number of digits")
        try:
            out += bytes.fromhex(word)
        except ValueError:
            raise ValueError(f"{what} must be written as hex, {word!r} is not") from None
    if not out:
        raise ValueError(f"{what} are missing")
    if len(out) > MAX_HEX_BYTES:
        raise ValueError(f"{what} are too long, at most {MAX_HEX_BYTES} bytes")
    return bytes(out)


def message_text(payload: bytes, protocol: str = SWP08) -> tuple[str, bool]:
    """Plain text for the bytes of a message, and whether it could be read."""
    try:
        if protocol == SWP02:
            message = swp02.parse_message(payload)
            return swp02.describe(message), not isinstance(message, swp02.Unknown)
        return describe(parse_message(payload)), True
    except ProtocolError as exc:
        return f"Cannot be decoded: {exc}", False


def message_name(command: int, protocol: str = SWP08) -> str:
    """Name of a command byte in the given protocol."""
    return swp02.command_name(command) if protocol == SWP02 else command_name(command)


def trace_row(number: int, when: float, event: TraceEvent, client: str = "") -> dict[str, Any]:
    """One trace entry as plain data.

    Args:
        number: Running number of the entry, unique within one trace.
        when: Seconds since 1970.
        event: What crossed the wire.
        client: The controller it came from or went to, on the router side.

    Returns:
        A dictionary with ``id``, ``time``, ``direction`` (``tx`` or ``rx``),
        ``kind``, ``hex`` (the bytes on the wire), ``command`` (the command
        byte of a message, else None), ``name``, ``text``, ``note``,
        ``problem`` and ``client``.
    """
    command = None
    name = ""
    problem = False
    if event.kind == "message":
        command = event.payload[0]
        name = message_name(command, event.protocol)
        text, readable = message_text(event.payload, event.protocol)
        problem = not readable
    elif event.kind == "ack":
        text = "ACK: the message was accepted"
    elif event.kind == "nak":
        text = "NAK: the message was refused"
        problem = True
    elif event.kind == "error":
        text = f"Broken frame: {event.note}"
        problem = True
    else:
        text = "Bytes written as they are, without framing"
    if event.kind == "message" and event.note:
        problem = problem or "attempt" in event.note or "purpose" in event.note
    return {
        "id": number,
        "time": when,
        "direction": event.direction,
        "kind": event.kind,
        "hex": hex_text(event.wire),
        "command": command,
        "name": name,
        "text": text,
        "note": event.note if event.kind != "error" else "",
        "problem": problem,
        "client": client,
    }


# ---------------------------------------------------------------------------
# The thread that runs the asyncio code of the library
# ---------------------------------------------------------------------------

class LoopThread:
    """An asyncio event loop in a background thread.

    The library is written for asyncio, the engines are called from the
    window, the web server and timers. This class is the bridge: ``run``
    waits for a coroutine, ``call`` for a plain function, both executed in
    the loop thread. Never call them from that thread itself.
    """

    def __init__(self, name: str):
        """Prepare the thread. It starts with :meth:`start`.

        Args:
            name: Name of the thread, for debugging.
        """
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        """True while the loop runs."""
        return self._loop is not None

    def start(self) -> None:
        """Start the loop if it does not run yet."""
        with self._lock:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            started = threading.Event()

            def work() -> None:
                """Run the loop until it is stopped, then end what is left."""
                asyncio.set_event_loop(loop)
                loop.call_soon(started.set)
                try:
                    loop.run_forever()
                finally:
                    try:
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    finally:
                        loop.close()

            thread = threading.Thread(target=work, name=self._name, daemon=True)
            thread.start()
            started.wait()
            self._loop, self._thread = loop, thread

    def stop(self) -> None:
        """Stop the loop and wait for the thread to end."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is not None and thread is not None:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)

    def run(self, coroutine: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run a coroutine in the loop and return its result.

        Raises:
            RuntimeError: The loop does not run.
            TimeoutError: The coroutine took longer than ``timeout`` seconds.
        """
        loop = self._loop
        if loop is None:
            coroutine.close()
            raise RuntimeError("the loop is not running")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def call(self, function: Callable[[], T]) -> T:
        """Run a plain function in the loop thread and return its result.

        Raises:
            RuntimeError: The loop does not run.
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError("the loop is not running")
        future: Future[T] = Future()

        def work() -> None:
            """Call the function and hand over what it returns or raises."""
            try:
                future.set_result(function())
            except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
                future.set_exception(exc)

        loop.call_soon_threadsafe(work)
        return future.result(10.0)


__all__ = ["DEFAULT_PORTS", "PROTOCOLS", "SWP02", "SWP08", "LoopThread", "check_host", "check_protocol",
           "message_name", "default_config_path", "hex_text", "load_json", "message_text",
           "parse_hex", "save_json", "to_bool", "to_int", "trace_row"]
