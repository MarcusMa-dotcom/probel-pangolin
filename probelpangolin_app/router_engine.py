"""Router engine: a virtual router that any SW-P-08 controller can connect to.

It answers crosspoint connect and interrogate, tally dumps, name requests and
the question for its implemented commands, in the standard and the extended
command set. Crosspoints can be set at the router itself, destinations can be
protected, and faults can be put in on purpose: messages that get no answer,
messages that are refused with NAK.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from probelpangolin import swp02
from probelpangolin.protocol import TraceEvent
from probelpangolin.simulator import SimulatedRouter

from .common import (DEFAULT_PORTS, SWP02, SWP08, LoopThread, check_host, check_protocol, load_json,
                     save_json, to_bool, to_int, trace_row)

MAX_PORTS = 4096              # sources and destinations of the virtual router
MAX_LEVELS = 32
MAX_NAME_LENGTH = 32
MAX_FAULTS = 1000
# Changing one of these while the router runs restarts it.
SERVER_FIELDS = frozenset({"protocol", "bind", "port", "sources", "dests", "levels", "matrix", "extended"})
TICK_SECONDS = 0.25
SAVE_DELAY_SECONDS = 1.0
LOG_SIZE = 300
LOG_SNAPSHOT_ENTRIES = 60
TRACE_SIZE = 5000
TRACE_SNAPSHOT_ENTRIES = 250
SNAPSHOT_ROWS = 1024          # sources and destinations a snapshot lists


@dataclass
class RouterSettings:
    """What the router side remembers between starts."""

    protocol: str = SWP08          # sw-p-08 or sw-p-02
    bind: str = "127.0.0.1"
    port: int = 8910
    sources: int = 16
    dests: int = 16
    levels: int = 2
    matrix: int = 1
    extended: bool = True          # also answer the extended commands
    autostart: bool = False
    source_names: list[str] = field(default_factory=list)
    dest_names: list[str] = field(default_factory=list)
    protected: list[int] = field(default_factory=list)
    routes: list[list[int]] = field(default_factory=list)   # per level: the source of every destination


def _names(label: str, value: Any) -> list[str]:
    """A list of names as stored: text, trimmed, not longer than a label can be."""
    if not isinstance(value, list) or len(value) > MAX_PORTS:
        raise ValueError(f"{label} must be a list of at most {MAX_PORTS} names")
    names = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{label} must be text")
        names.append(_name(item))
    return names


def _name(value: Any) -> str:
    """One name: text without control characters, at most 32 characters."""
    if not isinstance(value, str):
        raise ValueError("A name must be text")
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    if len(text) > MAX_NAME_LENGTH:
        raise ValueError(f"A name has at most {MAX_NAME_LENGTH} characters")
    return text


def coerce(name: str, value: Any) -> Any:
    """Validate one router setting and return it in its stored type.

    Raises:
        ValueError: The value is not valid, or there is no such setting.
    """
    if name == "protocol":
        return check_protocol(value)
    if name == "bind":
        return check_host("Listen address", value)
    if name == "port":
        return to_int("Port", value, 1, 65535)
    if name == "sources":
        return to_int("Sources", value, 1, MAX_PORTS)
    if name == "dests":
        return to_int("Destinations", value, 1, MAX_PORTS)
    if name == "levels":
        return to_int("Levels", value, 1, MAX_LEVELS)
    if name == "matrix":
        return to_int("Matrix", value, 1, 256)
    if name in ("extended", "autostart"):
        return to_bool(name, value)
    if name == "source_names":
        return _names("Source names", value)
    if name == "dest_names":
        return _names("Destination names", value)
    if name == "protected":
        if not isinstance(value, list) or len(value) > MAX_PORTS:
            raise ValueError("Protected destinations must be a list of numbers")
        return sorted({to_int("Protected destination", item, 1, MAX_PORTS) for item in value})
    if name == "routes":
        if not isinstance(value, list) or len(value) > MAX_LEVELS:
            raise ValueError("Crosspoints must be a list with one list of sources per level")
        tables = []
        for table in value:
            if not isinstance(table, list) or len(table) > MAX_PORTS:
                raise ValueError("Crosspoints must be a list with one list of sources per level")
            tables.append([to_int("Source", item, 1, MAX_PORTS) for item in table])
        return tables
    raise ValueError(f"Unknown setting {name!r}")


def load_settings(path: Path) -> RouterSettings:
    """Read settings from JSON. Missing or invalid values keep their default."""
    settings = RouterSettings()
    data = load_json(path)
    if isinstance(data, dict):
        for item in fields(RouterSettings):
            if item.name in data:
                try:
                    setattr(settings, item.name, coerce(item.name, data[item.name]))
                except ValueError:
                    pass
    return settings


def _empty_stats() -> dict[str, int]:
    """Counters of a fresh trace."""
    return {"received": 0, "sent": 0, "acks": 0, "naks": 0, "acks_sent": 0, "naks_sent": 0,
            "errors": 0, "takes": 0, "refused": 0}


class RouterEngine:
    """The router side of the app: the virtual router, its crosspoints, names and faults."""

    def __init__(self, settings: RouterSettings | None = None,
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
            settings = load_settings(self._config_path) if self._config_path else RouterSettings()
        self.settings = settings
        self._fit()
        self._instance = uuid.uuid4().hex[:8]
        self._revision = 0
        self._loop = LoopThread("pangolin-router")
        self._sim: SimulatedRouter | None = None
        self._generation = 0
        self._error = ""
        self._clients: dict[str, dict[str, Any]] = {}
        self._salvos: dict[int, list[tuple[int, int, int]]] = {}
        self._changed: dict[int, float] = {}
        self._log: deque[dict] = deque(maxlen=LOG_SIZE)
        self._trace: deque[dict] = deque(maxlen=TRACE_SIZE)
        self._trace_number = 0
        self._stats = _empty_stats()
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
    def running(self) -> bool:
        """True while the SW-P-08 port is open."""
        return self._sim is not None

    @property
    def level_count(self) -> int:
        """Levels in use: the setting for SW-P-08, always one for SW-P-02."""
        return 1 if self.settings.protocol == SWP02 else self.settings.levels

    def _touch(self) -> None:
        """Mark the state as changed."""
        self._revision += 1

    def _record(self, action: str, text: str, client: str = "", problem: bool = False) -> None:
        """Add an entry to the activity log."""
        self._log.append({"time": time.time(), "action": action, "text": text, "client": client,
                          "problem": problem})
        self._touch()

    def _fit(self) -> None:
        """Bring names, protected destinations and crosspoints in line with the size of the router."""
        s = self.settings
        s.source_names = [(s.source_names[i] if i < len(s.source_names) and s.source_names[i]
                           else f"SRC {i + 1}") for i in range(s.sources)]
        s.dest_names = [(s.dest_names[i] if i < len(s.dest_names) and s.dest_names[i]
                         else f"DST {i + 1}") for i in range(s.dests)]
        s.protected = [number for number in s.protected if number <= s.dests]
        tables = []
        for level in range(s.levels):
            old = s.routes[level] if level < len(s.routes) else []
            tables.append([old[d] if d < len(old) and old[d] <= s.sources else 1 for d in range(s.dests)])
        s.routes = tables

    def snapshot(self) -> dict:
        """Return settings, status, sources, destinations, the trace and the log as plain data."""
        with self._lock:
            s = self.settings
            sim = self._sim
            settings = {item.name: getattr(s, item.name) for item in fields(RouterSettings)
                        if item.name not in ("source_names", "dest_names", "protected", "routes")}
            protected = set(s.protected)
            destinations = [{
                "number": d + 1,
                "name": s.dest_names[d],
                "protected": d + 1 in protected,
                "sources": [s.routes[level][d] for level in range(self.level_count)],
                "changed": self._changed.get(d + 1),
            } for d in range(min(s.dests, SNAPSHOT_ROWS))]
            sources = [{"number": i + 1, "name": s.source_names[i]}
                       for i in range(min(s.sources, SNAPSHOT_ROWS))]
            return {
                "instance": self._instance,
                "revision": self._revision,
                "settings": settings,
                "status": {
                    "running": sim is not None,
                    "protocol": s.protocol,
                    "levels": self.level_count,
                    "description": f"{s.bind}:{sim.port if sim is not None else s.port}",
                    "error": self._error,
                    "clients": sorted(self._clients.values(), key=lambda client: client["since"]),
                    "salvos": [{"number": number,
                                "entries": [{"level": level, "dest": dest, "source": source}
                                            for level, dest, source in entries]}
                               for number, entries in sorted(self._salvos.items())],
                    "drop_pending": sim.drop_frames if sim is not None else 0,
                    "nak_pending": sim.nak_frames if sim is not None else 0,
                    "stats": dict(self._stats),
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

        A change of address, port, size, matrix or command set restarts a
        running router, which drops the controllers.

        Raises:
            ValueError: A value is not valid. Nothing is changed in that case.
        """
        clean = {name: coerce(name, value) for name, value in changes.items()}
        before = self.settings
        if ("protocol" in clean and clean["protocol"] != before.protocol and "port" not in clean
                and before.port == DEFAULT_PORTS[before.protocol]):
            clean["port"] = DEFAULT_PORTS[clean["protocol"]]   # each protocol has its usual port
        merged = {name: clean.get(name, getattr(before, name)) for name in ("protocol", "sources", "dests")}
        if merged["protocol"] == SWP02 and (merged["sources"] > swp02.MAX_SOURCE
                                            or merged["dests"] > swp02.MAX_DEST):
            raise ValueError(f"SW-P-02 reaches {swp02.MAX_SOURCE} sources and {swp02.MAX_DEST} destinations")
        restart = False
        with self._lock:
            changed = {name for name, value in clean.items() if getattr(self.settings, name) != value}
            if not changed:
                return self.snapshot()
            for name in changed:
                setattr(self.settings, name, clean[name])
            self._fit()
            restart = bool(changed & SERVER_FIELDS) and self._sim is not None
            self._mark_dirty()
            self._touch()
        if restart:
            self.stop_serving()
            self.start()
        else:
            self._push_names()
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

    # -- serving ---------------------------------------------------------

    def start(self) -> bool:
        """Open the SW-P-08 port. Returns False and sets the error if that fails."""
        with self._lock:
            if self._sim is not None:
                return True
            s = self.settings
            self._generation += 1
            generation = self._generation
            if s.protocol == SWP02:
                sim: SimulatedRouter = swp02.SimulatedRouter(s.sources, s.dests, host=s.bind, port=s.port)
            else:
                sim = SimulatedRouter(s.sources, s.dests, s.levels, matrix=s.matrix, extended=s.extended,
                                      host=s.bind, port=s.port)
            sim.source_names = list(s.source_names)
            sim.dest_names = list(s.dest_names)
            sim.protected = set(s.protected)
            for level, table in enumerate(s.routes[:self.level_count]):
                for dest, source in enumerate(table):
                    if source != 1:
                        sim.set_crosspoint(source, dest + 1, level + 1, announce=False)
            sim.on_trace = lambda client, event: self._traced(generation, client, event)
            sim.on_crosspoint = lambda *args: self._taken(generation, *args)
            sim.on_client = lambda client, connected: self._client(generation, client, connected)
            sim.on_salvo = lambda *args: self._salvo(generation, sim, *args)
            description = f"{s.bind}:{s.port}"
        self._loop.start()
        try:
            self._loop.run(sim.start(), 10.0)
        except Exception as exc:
            with self._lock:
                detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
                self._error = f"Cannot listen on {description}: {detail or type(exc).__name__}"
                self._touch()
            return False
        with self._lock:
            self._sim = sim
            self._error = ""
            self._clients = {}
            self._salvos = {}
            self._record("started", f"listening on {description}")
        return True

    def stop_serving(self) -> None:
        """Close the SW-P-08 port and drop the controllers. The crosspoints stay as they are."""
        with self._lock:
            sim = self._sim
            self._sim = None
            self._generation += 1
            self._clients = {}
            self._salvos = {}
            self._error = ""
            if sim is not None:
                self._record("stopped", "")
        if sim is not None:
            try:
                self._loop.run(sim.stop(), 10.0)
            except Exception:
                pass

    def disconnect_clients(self) -> int:
        """Drop every controller, to see how it comes back. Returns how many there were."""
        with self._lock:
            sim = self._sim
            count = len(self._clients)
        if sim is None:
            raise ValueError("The router is not running")
        self._loop.call(sim.disconnect_clients)
        with self._lock:
            self._record("dropped", f"{count} controller{'' if count == 1 else 's'} disconnected on purpose")
        return count

    # -- what the virtual router reports, called in its loop thread ---------

    def _client(self, generation: int, client: str, connected: bool) -> None:
        """A controller connected or went away."""
        with self._lock:
            if generation != self._generation:
                return
            if connected:
                self._clients[client] = {"address": client, "since": time.time(), "messages": 0}
                self._record("connected", "", client)
            else:
                self._clients.pop(client, None)
                self._record("disconnected", "", client)

    def _taken(self, generation: int, level: int, dest: int, source: int, client: str,
               accepted: bool) -> None:
        """A controller asked for a crosspoint."""
        with self._lock:
            if generation != self._generation:
                return
            s = self.settings
            label = f"level {level}: {self._dest_label(dest)} <- {self._source_label(source)}"
            if not accepted:
                self._stats["refused"] += 1
                self._record("refused", f"{label}: the destination is protected and stays on "
                                        f"{self._source_label(s.routes[level - 1][dest - 1])}",
                             client, problem=True)
                return
            self._stats["takes"] += 1
            previous = s.routes[level - 1][dest - 1]
            s.routes[level - 1][dest - 1] = source
            self._changed[dest] = time.time()
            self._mark_dirty()
            self._record("take", label + ("" if previous == source else f", was {self._source_label(previous)}"),
                         client)

    def _salvo(self, generation: int, sim: SimulatedRouter, action: str, salvo: int, client: str,
               count: int) -> None:
        """A controller stored a crosspoint in a salvo group, set a group or cleared one."""
        with self._lock:
            if generation != self._generation:
                return
            self._salvos = sim.salvos       # this runs in the loop thread, where the groups live
            held = len(self._salvos.get(salvo, ()))
            text = {
                "stored": f"a crosspoint stored, the group now holds {held}",
                "set": f"{count} stored crosspoint{'' if count == 1 else 's'} set at once",
                "cleared": f"{count} stored crosspoint{'' if count == 1 else 's'} cleared",
                "empty": "asked to set or clear, but the group holds nothing",
            }.get(action, action)
            self._record("salvo", f"salvo {salvo}: {text}", client, problem=action == "empty")

    def _traced(self, generation: int, client: str, event: TraceEvent) -> None:
        """Something crossed the wire: count it and keep it in the trace."""
        now = time.time()
        with self._lock:
            if generation != self._generation:
                return
            stats = self._stats
            if event.direction == "tx":
                key = {"message": "sent", "ack": "acks_sent", "nak": "naks_sent"}.get(event.kind, "sent")
            else:
                key = {"message": "received", "ack": "acks", "nak": "naks"}.get(event.kind, "errors")
            stats[key] += 1
            if event.direction == "rx" and event.kind == "message" and client in self._clients:
                self._clients[client]["messages"] += 1
            self._trace_number += 1
            self._trace.append(trace_row(self._trace_number, now, event, client))
            self._touch()

    # -- labels ----------------------------------------------------------

    def _source_label(self, number: int) -> str:
        """``source 5 (CAM 5)``."""
        names = self.settings.source_names
        name = names[number - 1] if 0 < number <= len(names) else ""
        return f"source {number} ({name})" if name else f"source {number}"

    def _dest_label(self, number: int) -> str:
        """``destination 3 (MON 3)``."""
        names = self.settings.dest_names
        name = names[number - 1] if 0 < number <= len(names) else ""
        return f"destination {number} ({name})" if name else f"destination {number}"

    # -- changes made at the router itself --------------------------------

    def set_crosspoint(self, source: Any, dest: Any, level: Any = 1, announce: bool = True) -> None:
        """Set a crosspoint at the router, as if someone had pressed a button on its panel.

        Args:
            source: Number of the source.
            dest: Number of the destination.
            level: The level, or ``"all"``.
            announce: Tell the controllers with Crosspoint Connected. Without it
                they keep showing the old source until they ask.

        Raises:
            ValueError: A number outside the size of the router.
        """
        announce = to_bool("announce", announce)
        with self._lock:
            s = self.settings
            src = to_int("Source", source, 1, s.sources)
            dst = to_int("Destination", dest, 1, s.dests)
            count = self.level_count
            levels = (list(range(1, count + 1)) if level == "all" or count == 1
                      else [to_int("Level", level, 1, count)])
            sim = self._sim
            for item in levels:
                s.routes[item - 1][dst - 1] = src
            self._changed[dst] = time.time()
            self._mark_dirty()
            names = ", ".join(str(item) for item in levels)
            self._record("set", f"level {names}: {self._dest_label(dst)} <- {self._source_label(src)}"
                                + ("" if announce or sim is None else ", controllers not told"))
        if sim is not None:
            for item in levels:
                self._loop.call(lambda item=item: sim.set_crosspoint(src, dst, item, announce=announce))

    def set_name(self, kind: Any, number: Any, name: Any) -> None:
        """Rename a source or a destination. An empty name brings back the default.

        Raises:
            ValueError: Unknown kind, a number outside the router, or a bad name.
        """
        if kind not in ("source", "dest"):
            raise ValueError('kind must be "source" or "dest"')
        text = _name(name)
        with self._lock:
            s = self.settings
            names = s.source_names if kind == "source" else s.dest_names
            index = to_int("Number", number, 1, len(names)) - 1
            names[index] = text or (f"SRC {index + 1}" if kind == "source" else f"DST {index + 1}")
            self._mark_dirty()
            self._touch()
        self._push_names()

    def protect(self, dest: Any, protected: Any = True) -> None:
        """Protect a destination, or release it. A protected destination keeps its source.

        Raises:
            ValueError: A number outside the router.
        """
        protected = to_bool("protected", protected)
        with self._lock:
            s = self.settings
            number = to_int("Destination", dest, 1, s.dests)
            current = set(s.protected)
            if protected:
                current.add(number)
            else:
                current.discard(number)
            s.protected = sorted(current)
            self._mark_dirty()
            self._record("protected" if protected else "released", self._dest_label(number))
        self._push_names()

    def _push_names(self) -> None:
        """Hand names and protected destinations to a running router."""
        with self._lock:
            sim = self._sim
            s = self.settings
            sources, dests, protected = list(s.source_names), list(s.dest_names), set(s.protected)
        if sim is None:
            return

        def work() -> None:
            """Runs in the loop thread, where the router reads these."""
            sim.source_names = sources
            sim.dest_names = dests
            sim.protected = protected

        self._loop.call(work)

    def inject(self, drop: Any = 0, nak: Any = 0) -> None:
        """Put faults in: the next messages get no answer, or are refused.

        Args:
            drop: How many of the next messages get no ACK and no answer.
            nak: How many of the next messages are answered with NAK.

        Raises:
            ValueError: The router is not running, or a number that does not fit.
        """
        drop = to_int("Messages to drop", drop, 0, MAX_FAULTS)
        nak = to_int("Messages to refuse", nak, 0, MAX_FAULTS)
        if nak and self.settings.protocol == SWP02:
            raise ValueError("SW-P-02 has no NAK. Leave messages unanswered instead")
        with self._lock:
            sim = self._sim
        if sim is None:
            raise ValueError("The router is not running")

        def work() -> None:
            """Runs in the loop thread, where the router counts these down."""
            sim.drop_frames = drop
            sim.nak_frames = nak

        self._loop.call(work)
        with self._lock:
            self._record("fault", f"the next {drop} messages get no answer, the next {nak} get a NAK")

    # -- background timer ------------------------------------------------

    def start_timer(self) -> None:
        """Start the timer thread for saving, and the router if it starts with the program."""
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="pangolin-router-engine", daemon=True)
            self._thread.start()
        if self.settings.autostart:
            self.start()

    def close(self) -> None:
        """Stop the timer, save the settings and close the port."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.stop_serving()
        with self._lock:
            self._save_if_dirty(force=True)
        self._loop.stop()

    def _run(self) -> None:
        """Thread: save the settings once changes have settled."""
        while not self._stop.wait(TICK_SECONDS):
            with self._lock:
                self._save_if_dirty(now=time.monotonic())

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


__all__ = ["RouterEngine", "RouterSettings"]
