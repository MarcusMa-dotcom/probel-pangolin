"""Controller engine: connects to a router, switches crosspoints and keeps the trace.

The engine mirrors what the router reports: crosspoints, names and the list
of commands it implements. Everything that crosses the wire, every ACK and
NAK included, goes into the trace, and the engine counts and times it.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from probelpangolin import (AckTimeoutError, AsyncRouter, NakError, NotConnectedError,
                           ResponseTimeoutError, RouteChange, RouteRejectedError, SWP08Error,
                           command_name, swp02)
from probelpangolin.protocol import Names, TraceEvent

from .common import (DEFAULT_PORTS, SWP02, SWP08, LoopThread, check_host, check_protocol, hex_text,
                     load_json, parse_hex, save_json, to_bool, to_int, trace_row)

EXTENDED_CHOICES = {"auto": None, "on": True, "off": False}
NAME_LENGTHS = (4, 8, 12)
SWITCHES = ("query_commands", "fetch_names", "fetch_tally", "wait_ack", "keepalive", "auto_reconnect",
            "confirm")
# Settings that only matter when something is sent. Changing any other one ends an open connection.
LIVE_FIELDS = frozenset({"confirm"})
KEEPALIVE_SECONDS = 30.0
RECONNECT_SECONDS = 3.0
REQUEST_TIMEOUT = 20.0        # longest a single request to the router may take, all parts included
OPEN_TIMEOUT = 60.0           # connecting with reading names and all crosspoints
TICK_SECONDS = 0.25
SAVE_DELAY_SECONDS = 1.0
LOG_SIZE = 300
LOG_SNAPSHOT_ENTRIES = 60
TRACE_SIZE = 5000
TRACE_SNAPSHOT_ENTRIES = 250
SLOW_ACK_MS = 10.0            # the specification asks for the ACK within 10 ms where possible
SALVO_OUTCOMES = {0: "set", 1: "cleared", 2: "empty"}
SNAPSHOT_DESTINATIONS = 1024  # destinations a snapshot lists
SNAPSHOT_SOURCES = 4096       # source names a snapshot lists


@dataclass
class ControllerSettings:
    """What the controller side remembers between starts."""

    protocol: str = SWP08          # sw-p-08 or sw-p-02
    host: str = "127.0.0.1"
    port: int = 8910
    destinations: int = 16         # SW-P-02 has no tally dump: so many destinations are asked one by one
    matrix: int = 1
    levels: int = 1
    extended: str = "auto"         # auto, on, off
    name_length: int = 8           # 4, 8 or 12 characters
    query_commands: bool = True    # ask on connect which commands the router implements
    fetch_names: bool = True       # read source and destination names on connect
    fetch_tally: bool = True       # read all crosspoints on connect
    wait_ack: bool = True          # wait for DLE ACK after every message
    attempts: int = 5              # how often a message is sent until it is acknowledged
    keepalive: bool = True         # interrogate after 30 seconds of silence
    auto_reconnect: bool = True
    confirm: bool = True           # a take waits for Crosspoint Connected


def coerce(name: str, value: Any) -> Any:
    """Validate one controller setting and return it in its stored type.

    Raises:
        ValueError: The value is not valid, or there is no such setting.
    """
    if name == "protocol":
        return check_protocol(value)
    if name == "host":
        return check_host("Host", value)
    if name == "port":
        return to_int("Port", value, 1, 65535)
    if name == "destinations":
        return to_int("Destinations", value, 1, swp02.MAX_DEST)
    if name == "matrix":
        return to_int("Matrix", value, 1, 256)
    if name == "levels":
        return to_int("Levels", value, 1, 256)
    if name == "attempts":
        return to_int("Attempts", value, 1, 10)
    if name == "extended":
        if value not in EXTENDED_CHOICES:
            raise ValueError("Extended commands must be auto, on or off")
        return value
    if name == "name_length":
        number = to_int("Name length", value, 4, 12)
        if number not in NAME_LENGTHS:
            raise ValueError("Name length must be 4, 8 or 12")
        return number
    if name in SWITCHES:
        return to_bool(name, value)
    raise ValueError(f"Unknown setting {name!r}")


def load_settings(path: Path) -> ControllerSettings:
    """Read settings from JSON. Missing or invalid values keep their default."""
    settings = ControllerSettings()
    data = load_json(path)
    if not isinstance(data, dict):
        return settings
    for item in fields(ControllerSettings):
        if item.name in data:
            try:
                setattr(settings, item.name, coerce(item.name, data[item.name]))
            except ValueError:
                pass
    return settings


def reason(exc: BaseException) -> str:
    """An error of the library in words a user can act on."""
    if isinstance(exc, RouteRejectedError):
        return (f"Destination {exc.dest} on level {exc.level} stays on source {exc.actual}: "
                f"the router did not take source {exc.requested}. The destination may be protected")
    if isinstance(exc, AckTimeoutError):
        return f"No ACK: {exc}. The router did not answer on the protocol level"
    if isinstance(exc, NakError):
        return f"NAK: {exc}"
    if isinstance(exc, ResponseTimeoutError):
        return f"The router acknowledged the message but sent no answer: {exc}"
    if isinstance(exc, NotConnectedError):
        return "Not connected" if "not connected" in str(exc) else str(exc).capitalize()
    text = str(exc) or type(exc).__name__
    return text[0].upper() + text[1:]


def _empty_stats() -> dict[str, Any]:
    """Counters of a fresh trace."""
    return {"sent": 0, "received": 0, "acks": 0, "naks": 0, "acks_sent": 0, "naks_sent": 0,
            "errors": 0, "repeats": 0, "ack_slow": 0, "ack_ms_last": None, "ack_ms_max": None,
            "ack_ms_sum": 0.0, "ack_count": 0}


class ControllerEngine:
    """The controller side of the app: one connection to a router, and what it reports."""

    def __init__(self, settings: ControllerSettings | None = None,
                 config_path: Path | str | None = None):
        """Create the engine.

        Args:
            settings: Settings to start with. Without them they are read from
                ``config_path``, or defaults are used.
            config_path: Settings file. Without a path nothing is saved.
        """
        self._lock = threading.RLock()
        self._config_path = Path(config_path) if config_path else None
        if settings is None:
            settings = load_settings(self._config_path) if self._config_path else ControllerSettings()
        self.settings = settings
        self._instance = uuid.uuid4().hex[:8]
        self._revision = 0
        self._loop = LoopThread("pangolin-controller")
        self._router: AsyncRouter | None = None
        self._generation = 0            # rises with every connect, so late callbacks are told apart
        self._connecting = False
        self._wanted = False            # the user asked for a connection and has not ended it
        self._retry_at = 0.0
        self._error = ""
        self._routes: dict[int, dict[int, int]] = {}
        self._changed: dict[int, float] = {}          # destination -> time of its last change
        self._source_names: dict[int, str] = {}
        self._dest_names: dict[int, str] = {}
        self._log: deque[dict] = deque(maxlen=LOG_SIZE)
        self._trace: deque[dict] = deque(maxlen=TRACE_SIZE)
        self._trace_number = 0
        self._stats = _empty_stats()
        self._sent_at: float | None = None
        self._dirty_since = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- state -----------------------------------------------------------

    @property
    def instance(self) -> str:
        """A name that is new at every start, so front ends notice a restart."""
        return self._instance

    @property
    def revision(self) -> int:
        """Rises with every change. Front ends compare it to decide whether to redraw."""
        return self._revision

    @property
    def connected(self) -> bool:
        """True while the connection to the router is open."""
        router = self._router
        return router is not None and router.connected

    @property
    def level_count(self) -> int:
        """Levels in use: the setting for SW-P-08, always one for SW-P-02."""
        return 1 if self.settings.protocol == SWP02 else self.settings.levels

    def _touch(self) -> None:
        """Mark the state as changed."""
        self._revision += 1

    def _record(self, action: str, text: str, problem: bool = False) -> None:
        """Add an entry to the activity log."""
        self._log.append({"time": time.time(), "action": action, "text": text, "problem": problem})
        self._touch()

    def snapshot(self) -> dict:
        """Return settings, status, crosspoints, names, the trace and the log as plain data."""
        with self._lock:
            router = self._router
            settings = self.settings
            levels = self.level_count
            numbers = set(self._dest_names)
            for table in self._routes.values():
                numbers.update(table)
            ordered = sorted(numbers)
            destinations = [{
                "number": number,
                "name": self._dest_names.get(number, ""),
                "sources": [self._routes.get(level, {}).get(number) for level in range(1, levels + 1)],
                "changed": self._changed.get(number),
            } for number in ordered[:SNAPSHOT_DESTINATIONS]]
            highest = max(self._source_names, default=0)
            for table in self._routes.values():
                highest = max(highest, max(table.values(), default=0))
            sources = [{"number": number, "name": name}
                       for number, name in sorted(self._source_names.items())[:SNAPSHOT_SOURCES]]
            commands = router.implemented_commands if router is not None else None
            answer = router.implementation if router is not None else None
            sent_by_router = set(answer.transmitted) if answer is not None else set()
            accepted = set(answer.received) if answer is not None else set()
            stats = dict(self._stats)
            count = stats.pop("ack_count")
            total = stats.pop("ack_ms_sum")
            stats["ack_ms_mean"] = round(total / count, 2) if count else None
            return {
                "instance": self._instance,
                "revision": self._revision,
                "settings": asdict(settings),
                "status": {
                    "connected": self.connected,
                    "connecting": self._connecting,
                    "waiting": self._wanted and not self._connecting and not self.connected
                               and settings.auto_reconnect,
                    "description": f"{settings.host}:{settings.port}",
                    "protocol": settings.protocol,
                    "levels": levels,
                    "error": self._error,
                    "commands": [{"code": code, "name": command_name(code),
                                  "direction": ("from router" if code in sent_by_router
                                                else "to router" if code in accepted else "")}
                                 for code in sorted(commands)] if commands else [],
                    "commands_known": commands is not None,
                    "destination_count": len(ordered),
                    "source_count": highest,
                    "stats": stats,
                    "trace_count": self._trace_number,
                    "log": list(self._log)[-LOG_SNAPSHOT_ENTRIES:],
                },
                "sources": sources,
                "destinations": destinations,
                "trace": list(self._trace)[-TRACE_SNAPSHOT_ENTRIES:],
            }

    def trace(self) -> list[dict]:
        """Everything the trace holds, oldest entry first."""
        with self._lock:
            return list(self._trace)

    # -- settings --------------------------------------------------------

    def update(self, **changes: Any) -> dict:
        """Validate and apply setting changes.

        Changing anything that shapes the connection ends an open connection.

        Raises:
            ValueError: A value is not valid. Nothing is changed in that case.
        """
        clean = {name: coerce(name, value) for name, value in changes.items()}
        before = asdict(self.settings)
        if ("protocol" in clean and clean["protocol"] != before["protocol"] and "port" not in clean
                and before["port"] == DEFAULT_PORTS[before["protocol"]]):
            clean["port"] = DEFAULT_PORTS[clean["protocol"]]   # each protocol has its usual port
        merged = {**before, **clean}
        if (merged["protocol"] == SWP08 and merged["extended"] == "off"
                and (merged["matrix"] > 16 or merged["levels"] > 16)):
            raise ValueError("More than 16 matrices or levels need the extended commands")
        drop = False
        with self._lock:
            changed = {name for name, value in clean.items() if getattr(self.settings, name) != value}
            if changed:
                for name in changed:
                    setattr(self.settings, name, clean[name])
                drop = bool(changed - LIVE_FIELDS) and (self._router is not None or self._wanted)
                self._mark_dirty()
                self._touch()
        if drop:
            self.disconnect()
        return self.snapshot()

    def clear_log(self) -> None:
        """Forget the activity log."""
        with self._lock:
            self._log.clear()
            self._touch()

    def clear_trace(self) -> None:
        """Forget the trace and start counting again."""
        with self._lock:
            self._trace.clear()
            self._trace_number = 0
            self._stats = _empty_stats()
            self._touch()

    # -- connection ------------------------------------------------------

    def connect(self) -> bool:
        """Connect and run the start-up queries. Returns False and sets the error on failure."""
        with self._lock:
            if self._connecting:
                return False
            old = self._router
            self._router = None
            self._connecting = True
            self._wanted = True
            self._error = ""
            self._generation += 1
            generation = self._generation
            self._routes = {}
            self._changed = {}
            self._source_names = {}
            self._dest_names = {}
            settings = ControllerSettings(**asdict(self.settings))
            self._touch()
        self._loop.start()
        if old is not None:
            self._close_router(old)
        router: AsyncRouter | None = None
        error = ""
        try:
            router = self._loop.run(self._open(settings, generation), OPEN_TIMEOUT)
        except SWP08Error as exc:
            error = reason(exc)
        except TimeoutError:
            error = "Connecting took too long"
        except Exception as exc:      # never leave the engine stuck in "connecting"
            error = f"Cannot connect: {exc or type(exc).__name__}"
        with self._lock:
            self._connecting = False
            if generation != self._generation:
                error = error or "Connecting was cancelled"
            if error:
                self._error = error
                self._retry_at = time.monotonic() + RECONNECT_SECONDS
                self._touch()
            else:
                self._router = router
                self._touch()
        if error and router is not None:
            self._close_router(router)
        return not error

    async def _open(self, settings: ControllerSettings, generation: int) -> AsyncRouter:
        """Create the client of the library, hook the engine in and connect. Runs in the loop."""
        if settings.protocol == SWP02:
            router: AsyncRouter = swp02.AsyncRouter(
                settings.host, settings.port, dests=settings.destinations,
                fetch_tally=settings.fetch_tally,
                keepalive=KEEPALIVE_SECONDS if settings.keepalive else None,
                reconnect=settings.auto_reconnect,
            )
        else:
            router = self._swp08_client(settings)
        router.on_trace(lambda event: self._traced(generation, event))
        router.on_route(lambda change: self._routed(generation, change))
        router.on_names(lambda names: self._named(generation, names))
        router.on_connection(lambda up: self._link(generation, up))
        try:
            await router.open()
        except BaseException:
            await router.close()
            raise
        return router

    @staticmethod
    def _swp08_client(settings: ControllerSettings) -> AsyncRouter:
        """The SW-P-08 client of the library for these settings."""
        return AsyncRouter(
            settings.host, settings.port,
            matrix=settings.matrix, levels=settings.levels,
            extended=EXTENDED_CHOICES[settings.extended],
            query_commands=settings.query_commands,
            fetch_names=settings.fetch_names, fetch_tally=settings.fetch_tally,
            name_length=settings.name_length, wait_ack=settings.wait_ack, attempts=settings.attempts,
            keepalive=KEEPALIVE_SECONDS if settings.keepalive else None,
            reconnect=settings.auto_reconnect,
        )

    def _close_router(self, router: AsyncRouter) -> None:
        """Close a client of the library, whatever state it is in."""
        try:
            self._loop.run(router.close(), 10.0)
        except Exception:
            pass

    def disconnect(self) -> None:
        """Close the connection. Crosspoints and names read so far stay visible."""
        with self._lock:
            self._wanted = False
            router = self._router
            self._router = None
            was_connected = router is not None and router.connected
            self._generation += 1       # callbacks of this connection no longer count
            self._error = ""
            self._touch()
        if router is not None:
            self._close_router(router)
        if was_connected:
            with self._lock:
                self._record("disconnected", "")

    # -- what the library reports, called in its loop thread ---------------

    def _link(self, generation: int, up: bool) -> None:
        """The connection came up or went down."""
        with self._lock:
            if generation != self._generation:
                return
            settings = self.settings
            if up:
                self._routes = {}
                self._changed = {}
                self._error = ""
                self._record("connected", f"{settings.host}:{settings.port}")
            else:
                self._error = ("Connection lost. Trying again every few seconds."
                               if settings.auto_reconnect else "Connection lost")
                self._record("disconnected", "the connection was lost", problem=True)

    def _routed(self, generation: int, change: RouteChange) -> None:
        """A crosspoint changed, or was learned for the first time."""
        with self._lock:
            if generation != self._generation:
                return
            self._routes.setdefault(change.level, {})[change.dest] = change.source
            if change.previous is None:
                self._touch()           # first reading, for example from a tally dump
                return
            self._changed[change.dest] = time.time()
            self._record("change", f"level {change.level}: {self._dest_label(change.dest)} <- "
                                   f"{self._source_label(change.source)}, "
                                   f"was {self._source_label(change.previous)}")

    def _named(self, generation: int, names: Names) -> None:
        """A part of a name list arrived."""
        with self._lock:
            if generation != self._generation:
                return
            target = self._source_names if names.kind == "source" else self._dest_names
            target.update(names.as_dict())
            self._touch()

    def _traced(self, generation: int, event: TraceEvent) -> None:
        """Something crossed the wire: count it, time the ACK and keep it in the trace."""
        now = time.time()
        with self._lock:
            if generation != self._generation:
                return
            stats = self._stats
            if event.direction == "tx":
                if event.kind in ("message", "raw"):
                    stats["sent"] += 1
                    if event.note:
                        stats["repeats"] += 1
                    self._sent_at = time.perf_counter() if event.kind == "message" else None
                elif event.kind == "ack":
                    stats["acks_sent"] += 1
                elif event.kind == "nak":
                    stats["naks_sent"] += 1
            elif event.kind in ("ack", "nak"):
                stats["acks" if event.kind == "ack" else "naks"] += 1
                if self._sent_at is not None:
                    took = (time.perf_counter() - self._sent_at) * 1000.0
                    self._sent_at = None
                    stats["ack_ms_last"] = round(took, 2)
                    stats["ack_ms_max"] = round(max(took, stats["ack_ms_max"] or 0.0), 2)
                    stats["ack_ms_sum"] += took
                    stats["ack_count"] += 1
                    if took > SLOW_ACK_MS:
                        stats["ack_slow"] += 1
            elif event.kind == "message":
                stats["received"] += 1
            else:
                stats["errors"] += 1
            self._trace_number += 1
            self._trace.append(trace_row(self._trace_number, now, event))
            self._touch()

    # -- labels ----------------------------------------------------------

    def _source_label(self, number: int) -> str:
        """``source 5 (CAM 5)``."""
        name = self._source_names.get(number)
        return f"source {number} ({name})" if name else f"source {number}"

    def _dest_label(self, number: int) -> str:
        """``destination 3 (MON 3)``."""
        name = self._dest_names.get(number)
        return f"destination {number} ({name})" if name else f"destination {number}"

    def _port(self, value: Any, what: str, names: dict[int, str]) -> int:
        """A source or destination given as number or as name."""
        if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
            wanted = value.strip()
            if not wanted:
                raise ValueError(f"{what} is missing")
            matches = [number for number, name in names.items() if name == wanted]
            if not matches:
                matches = [number for number, name in names.items()
                           if name.strip().casefold() == wanted.casefold()]
            if not matches:
                raise ValueError(f"No {what.lower()} is named {wanted!r}"
                                 + ("" if names else ". No names have been read yet"))
            if len(matches) > 1:
                raise ValueError(f"The name {wanted!r} belongs to several {what.lower()}s: "
                                 f"{', '.join(str(n) for n in sorted(matches))}")
            return matches[0]
        return to_int(what, value, 1, 65536)

    def _levels(self, levels: Any) -> list[int]:
        """Levels given as nothing (level 1), ``"all"``, one number or a list."""
        count = self.level_count
        if count == 1 or levels is None or levels == "":
            return [1]
        if levels == "all":
            return list(range(1, count + 1))
        if isinstance(levels, list):
            if not levels:
                raise ValueError("Levels are missing")
            return [to_int("Level", level, 1, 256) for level in levels]
        return [to_int("Level", levels, 1, 256)]

    # -- talking to the router -------------------------------------------

    def _need(self) -> AsyncRouter:
        """The client, if it is connected. Raises ``ValueError`` otherwise."""
        router = self._router
        if router is None or not router.connected:
            raise ValueError("Not connected")
        return router

    def _ask(self, make: Any, action: str) -> Any:
        """Run a request of the library and turn its errors into ``ValueError`` with plain words."""
        router = self._need()
        try:
            return self._loop.run(make(router), REQUEST_TIMEOUT)
        except SWP08Error as exc:
            text = reason(exc)
        except TimeoutError:
            text = "The router took too long to answer"
        except RuntimeError:
            text = "Not connected"
        with self._lock:
            if "stays on source" in text:
                self._record("refused", text, problem=True)
            else:
                self._record("error", f"{action}: {text[0].lower()}{text[1:]}", problem=True)
        raise ValueError(text)

    def route(self, source: Any, dest: Any, levels: Any = None, confirm: bool | None = None) -> dict:
        """Route a source to a destination.

        Args:
            source: Number of the source, or its name once names are read.
            dest: Number or name of the destination.
            levels: Nothing for level 1, ``"all"``, one level or a list.
            confirm: Wait for the Crosspoint Connected of the router and compare
                it. Defaults to the setting.

        Returns:
            ``source``, ``dest`` and ``levels`` as numbers, and ``confirmed``.

        Raises:
            ValueError: Not connected, a value that does not fit, no answer,
                or the router kept another source.
        """
        with self._lock:
            src = self._port(source, "Source", self._source_names)
            dst = self._port(dest, "Destination", self._dest_names)
            wanted = self._levels(levels)
            confirm = self.settings.confirm if confirm is None else to_bool("confirm", confirm)
            label = f"{self._dest_label(dst)} <- {self._source_label(src)}"
        for level in wanted:
            self._ask(lambda router, level=level: router.route(src, dst, level, confirm=confirm),
                      f"level {level}: {label}")
            with self._lock:
                self._record("take", f"level {level}: {label}"
                                     + (", confirmed by the router" if confirm else ", sent"))
        return {"source": src, "dest": dst, "levels": wanted, "confirmed": confirm}

    def interrogate(self, dest: Any, level: Any = None) -> dict[int, int]:
        """Ask the router which source is on a destination.

        Args:
            dest: Number or name of the destination.
            level: One level, or nothing for all levels.

        Returns:
            Level to source.

        Raises:
            ValueError: Not connected, a value that does not fit, or no answer.
        """
        with self._lock:
            dst = self._port(dest, "Destination", self._dest_names)
            wanted = self._levels("all" if level in (None, "", "all") else level)
        result: dict[int, int] = {}
        for item in wanted:
            source = self._ask(lambda router, item=item: router.get_route(dst, item),
                               f"interrogate {dst} on level {item}")
            result[item] = source
            with self._lock:
                self._record("asked", f"level {item}: {self._dest_label(dst)} has "
                                      f"{self._source_label(source)}")
        return result

    def dump(self, level: Any = None) -> dict[int, int]:
        """Read all crosspoints, of one level or of all.

        Returns:
            Level to the number of destinations the router reported.

        Raises:
            ValueError: Not connected, or no answer.
        """
        wanted = self._levels("all" if level in (None, "", "all") else level)
        result: dict[int, int] = {}
        for item in wanted:
            table = self._ask(lambda router, item=item: router.tally_dump(item),
                              f"tally dump of level {item}")
            result[item] = len(table)
            with self._lock:
                self._record("read", f"level {item}: {len(table)} crosspoints")
        return result

    def read_names(self) -> dict[str, int]:
        """Read all source and destination names.

        Returns:
            The number of ``sources`` and ``destinations`` that have a name now.

        Raises:
            ValueError: Not connected, or no answer.
        """
        self._need()
        with self._lock:
            self._source_names = {}
            self._dest_names = {}
            self._touch()
        sources, dests = self._ask(lambda router: router.read_names(), "names")
        with self._lock:
            self._record("read", f"{len(sources)} source names, {len(dests)} destination names")
        return {"sources": len(sources), "destinations": len(dests)}

    def query_commands(self) -> list[int]:
        """Ask the router which commands it implements.

        Raises:
            ValueError: Not connected, or no answer.
        """
        commands = sorted(self._ask(lambda router: router.query_commands(), "implemented commands"))
        with self._lock:
            self._record("read", f"the router implements {len(commands)} commands")
        return commands

    # -- salvo groups and dual controllers --------------------------------

    def _salvo_number(self, salvo: Any) -> int:
        """A salvo group number. SW-P-02 has one store without a number, which counts as 0."""
        if self.settings.protocol == SWP02:
            return 0
        return to_int("Salvo", salvo, 0, 127)

    def salvo_add(self, source: Any, dest: Any, salvo: Any = 0, levels: Any = None) -> dict:
        """Store a crosspoint in a salvo group. Nothing switches until the group is set.

        Args:
            source: Number or name of the source.
            dest: Number or name of the destination.
            salvo: Number of the salvo group, 0 to 127.
            levels: Nothing for level 1, ``"all"``, one level or a list.

        Raises:
            ValueError: Not connected, a value that does not fit, or no acknowledge.
        """
        with self._lock:
            src = self._port(source, "Source", self._source_names)
            dst = self._port(dest, "Destination", self._dest_names)
            group = self._salvo_number(salvo)
            wanted = self._levels(levels)
            label = f"{self._dest_label(dst)} <- {self._source_label(src)}"
        if src > 1024 or dst > 1024:
            raise ValueError("Salvo groups reach sources and destinations up to 1024")
        self._ask(lambda router: router.salvo_add(src, dst, group, wanted), f"salvo {group}: {label}")
        with self._lock:
            names = ", ".join(str(level) for level in wanted)
            self._record("salvo", f"salvo {group}: stored level {names}: {label}")
        return {"salvo": group, "source": src, "dest": dst, "levels": wanted}

    def salvo_go(self, salvo: Any = 0, clear: bool = False) -> dict:
        """Set all crosspoints stored in a salvo group at once, or clear the group.

        Returns:
            ``salvo`` and ``outcome``: ``set``, ``cleared`` or ``empty``.

        Raises:
            ValueError: Not connected, a number that does not fit, or no answer.
        """
        group = self._salvo_number(salvo)
        clear = to_bool("clear", clear)
        code = self._ask(lambda router: router.salvo_go(group, clear=clear), f"salvo {group}")
        outcome = SALVO_OUTCOMES.get(code, f"code {code}")
        with self._lock:
            text = {"set": "the stored crosspoints were set", "cleared": "the stored crosspoints were cleared",
                    "empty": "the group held nothing"}.get(outcome, outcome)
            self._record("salvo", f"salvo {group}: {text}", problem=outcome == "empty")
        return {"salvo": group, "outcome": outcome}

    def salvo_read(self, salvo: Any = 0) -> list[dict[str, int]]:
        """What a salvo group holds at the router: a list of ``level``, ``dest`` and ``source``.

        Raises:
            ValueError: Not connected, a number that does not fit, or no answer.
        """
        group = self._salvo_number(salvo)
        entries = self._ask(lambda router: router.salvo_read(group), f"salvo {group}")
        with self._lock:
            count = len(entries)
            self._record("read", f"salvo {group} holds {count} crosspoint{'' if count == 1 else 's'}")
        return [{"level": level, "dest": dest, "source": source} for level, dest, source in entries]

    def dual_status(self) -> dict:
        """Ask a router with two controllers for its dual controller status.

        Returns:
            ``status`` as hex and ``bit1``, the bit that tells active from idle.
            The public specification does not say which value means active.

        Raises:
            ValueError: Not connected, or no answer.
        """
        reply = self._ask(lambda router: router.dual_controller_status(), "dual controller status")
        result = {"status": hex_text(reply.status), "bit1": reply.bit1}
        with self._lock:
            self._record("asked", f"dual controller status {result['status'] or 'without bytes'}"
                                  + ("" if reply.bit1 is None
                                     else f", active/idle bit {'set' if reply.bit1 else 'clear'}"))
        return result

    def send_raw(self, data: Any, framed: bool = True) -> dict:
        """Send bytes of your own.

        Args:
            data: Hex text or bytes.
            framed: True sends them as the DATA of a message: DLE STX, byte
                count, checksum and DLE ETX are added and the ACK is awaited.
                False writes them exactly as given, to try broken frames.

        Returns:
            ``sent`` as hex, the bytes that went out.

        Raises:
            ValueError: Not connected, not hex, or the router did not accept it.
        """
        payload = bytes(data) if isinstance(data, (bytes, bytearray)) else parse_hex(data)
        framed = to_bool("framed", framed)
        if framed:
            if len(payload) > 255:
                raise ValueError("A message holds at most 255 bytes")
            self._ask(lambda router: router.send(payload, check=False), f"own message {hex_text(payload)}")
        else:
            self._ask(lambda router: router.write_raw(payload), "own bytes")
        with self._lock:
            self._record("sent", f"own {'message' if framed else 'bytes'}: {hex_text(payload)}")
        return {"sent": hex_text(payload), "framed": framed}

    # -- background timer ------------------------------------------------

    def start_timer(self) -> None:
        """Start the timer thread for connecting again and for saving."""
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="pangolin-controller-engine",
                                            daemon=True)
            self._thread.start()

    def close(self) -> None:
        """Stop the timer, save the settings and close the connection."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            self._save_if_dirty(force=True)
            self._wanted = False
            router = self._router
            self._router = None
            self._generation += 1
        if router is not None:
            self._close_router(router)
        self._loop.stop()

    def _run(self) -> None:
        """Thread: save the settings, and connect again when the first try failed."""
        while not self._stop.wait(TICK_SECONDS):
            now = time.monotonic()
            with self._lock:
                self._save_if_dirty(now=now)
                retry = (self.settings.auto_reconnect and self._wanted and not self._connecting
                         and self._router is None and now >= self._retry_at)
            if retry:
                self.connect()

    def _mark_dirty(self) -> None:
        """Note the time of the first change that is not saved yet."""
        if self._dirty_since == 0.0:
            self._dirty_since = time.monotonic()

    def _save_if_dirty(self, now: float | None = None, force: bool = False) -> None:
        """Save the settings once changes have settled, or at once with ``force``."""
        if self._config_path is None or self._dirty_since == 0.0:
            return
        if not force and (now is None or now - self._dirty_since < SAVE_DELAY_SECONDS):
            return
        self._dirty_since = 0.0
        try:
            save_json(self._config_path, asdict(self.settings))
        except OSError:
            pass


__all__ = ["ControllerEngine", "ControllerSettings"]
