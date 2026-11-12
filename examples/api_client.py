"""Control Probel Pangolin from Python through its REST API.

Needs nothing but the standard library. Run it against a started Probel Pangolin:

    python api_client.py status
    python api_client.py router-start
    python api_client.py connect 127.0.0.1 --swp-port 8910 --levels 2
    python api_client.py take 5 3 --levels all
    python api_client.py take "CAM 5" "MON 3"
    python api_client.py ask 3
    python api_client.py crosspoints
    python api_client.py trace
    python api_client.py raw "01 00 00 02"
    python api_client.py router-set 7 4
    python api_client.py decode "10 02 02 01 00 02 04 05 F2 10 03 10 06"
    python api_client.py --base https://pangolin.example:8090 --user robot status

``take``, ``ask``, ``crosspoints``, ``trace`` and ``raw`` use the controller of
Probel Pangolin, which talks to the router it is connected to. ``router-start``
and ``router-set`` use the virtual router. With ``--user`` the password is
asked for, or read from PROBEL_PANGOLIN_API_PASSWORD.

The class ``ProbelPangolinApi`` below is small enough to copy into your own program.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


class ApiError(Exception):
    """The API answered with an error. ``status`` is the HTTP status code."""

    def __init__(self, status: int, message: str):
        """Keep the HTTP status code next to the message."""
        super().__init__(f"{status}: {message}")
        self.status = status


class ProbelPangolinApi:
    """A thin client for the REST API of Probel Pangolin."""

    def __init__(self, base: str = "http://localhost:8090", user: str | None = None,
                 password: str | None = None, timeout: float = 30.0):
        """Remember where the API is and how to log in.

        Args:
            base: Address of the web server, without ``/api/v1``.
            user: User name of the API login, or None if the API needs none.
            password: Password of the API login.
            timeout: Seconds to wait for an answer.
        """
        self.base = base.rstrip("/") + "/api/v1"
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if user is not None:
            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """Send one request and return the JSON answer.

        Raises:
            ApiError: The API answered with an error status.
            OSError: The server cannot be reached.
        """
        data = None if method == "GET" else json.dumps({} if body is None else body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                message = json.load(error).get("error", error.reason)
            except ValueError:
                message = error.reason
            raise ApiError(error.code, str(message)) from None

    # -- controller ------------------------------------------------------

    def controller(self) -> dict:
        """Settings, status, crosspoints, names and the latest trace entries."""
        return self.call("GET", "/controller")

    def connect(self, host: str | None = None, port: int | None = None, levels: int | None = None) -> bool:
        """Set the address if given, then connect. Returns whether that worked."""
        changes = {key: value for key, value in (("host", host), ("port", port), ("levels", levels))
                   if value is not None}
        if changes:
            self.call("PATCH", "/controller", changes)
        return bool(self.call("POST", "/controller/connect")["ok"])

    def disconnect(self) -> None:
        """Close the connection to the router."""
        self.call("POST", "/controller/disconnect")

    def take(self, source: int | str, dest: int | str, levels: Any = None, confirm: bool = True) -> dict:
        """Route a source to a destination. Raises ``ApiError`` if the router does not follow."""
        return self.call("POST", "/controller/route",
                         {"source": source, "dest": dest, "levels": levels, "confirm": confirm})["result"]

    def ask(self, dest: int | str, level: int | None = None) -> dict:
        """Which source is on a destination: level to source."""
        return self.call("POST", "/controller/interrogate", {"dest": dest, "level": level})["result"]

    def trace(self) -> list[dict]:
        """Everything the trace of the controller holds, oldest first."""
        return self.call("GET", "/controller/trace")["trace"]

    def raw(self, hex_bytes: str, framed: bool = True) -> dict:
        """Send own bytes, as a message or exactly as written."""
        return self.call("POST", "/controller/raw", {"hex": hex_bytes, "framed": framed})["result"]

    # -- virtual router --------------------------------------------------

    def router(self) -> dict:
        """Settings, status, crosspoints and names of the virtual router."""
        return self.call("GET", "/router")

    def router_start(self) -> bool:
        """Open the SW-P-08 port of the virtual router. Returns whether that worked."""
        return bool(self.call("POST", "/router/start")["ok"])

    def router_set(self, source: int, dest: int, level: Any = 1, announce: bool = True) -> None:
        """Set a crosspoint at the virtual router."""
        self.call("POST", "/router/crosspoint",
                  {"source": source, "dest": dest, "level": level, "announce": announce})

    # -- analyzer --------------------------------------------------------

    def decode(self, hex_bytes: str) -> dict:
        """Read captured bytes: frames, ACK, NAK and whatever is broken."""
        return self.call("POST", "/analyzer/decode", {"hex": hex_bytes})


def _number_or_name(text: str) -> int | str:
    """A source or destination from the command line."""
    return int(text) if text.isdigit() else text


def main(argv: list[str] | None = None) -> int:
    """Run one command against the API and print the outcome."""
    parser = argparse.ArgumentParser(description="Control Probel Pangolin through its REST API.")
    parser.add_argument("--base", default="http://localhost:8090", help="address of the web server")
    parser.add_argument("--user", help="user name of the API login")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="state of controller and virtual router")
    connect = commands.add_parser("connect", help="connect the controller to a router")
    connect.add_argument("host", nargs="?")
    connect.add_argument("--swp-port", type=int)
    connect.add_argument("--levels", type=int)
    commands.add_parser("disconnect", help="close the connection of the controller")
    take = commands.add_parser("take", help="route a source to a destination")
    take.add_argument("source")
    take.add_argument("dest")
    take.add_argument("--levels", default=None, help='a level or "all" (default: level 1)')
    take.add_argument("--no-confirm", action="store_true", help="do not wait for the router's confirmation")
    ask = commands.add_parser("ask", help="ask the router for the source on a destination")
    ask.add_argument("dest")
    commands.add_parser("crosspoints", help="list the crosspoints the controller knows")
    commands.add_parser("trace", help="print the trace of the controller")
    raw = commands.add_parser("raw", help="send own bytes as a message")
    raw.add_argument("hex")
    raw.add_argument("--unframed", action="store_true", help="write the bytes exactly as given")
    commands.add_parser("router-start", help="start the virtual router")
    router_set = commands.add_parser("router-set", help="set a crosspoint at the virtual router")
    router_set.add_argument("source", type=int)
    router_set.add_argument("dest", type=int)
    router_set.add_argument("--level", default="1")
    decode = commands.add_parser("decode", help="read captured bytes")
    decode.add_argument("hex")
    args = parser.parse_args(argv)

    password = None
    if args.user:
        password = os.environ.get("PROBEL_PANGOLIN_API_PASSWORD") or getpass.getpass("API password: ")
    api = ProbelPangolinApi(args.base, args.user, password)

    try:
        if args.command == "status":
            controller, router = api.controller(), api.router()
            status = controller["status"]
            print(f"controller: {'connected to ' + status['description'] if status['connected'] else 'disconnected'}"
                  + (f" ({status['error']})" if status["error"] else ""))
            stats = status["stats"]
            print(f"  sent {stats['sent']}, received {stats['received']}, NAK {stats['naks']}, "
                  f"repeated {stats['repeats']}, broken frames {stats['errors']}")
            state = router["status"]
            print(f"router: {'listening on ' + state['description'] if state['running'] else 'off'}, "
                  f"{len(state['clients'])} controller(s)")
        elif args.command == "connect":
            ok = api.connect(args.host, args.swp_port, args.levels)
            print("connected" if ok else f"failed: {api.controller()['status']['error']}")
            return 0 if ok else 1
        elif args.command == "disconnect":
            api.disconnect()
        elif args.command == "take":
            levels = args.levels if args.levels in (None, "all") else int(args.levels)
            result = api.take(_number_or_name(args.source), _number_or_name(args.dest), levels,
                              not args.no_confirm)
            print(f"source {result['source']} is on destination {result['dest']}, "
                  f"level {', '.join(str(level) for level in result['levels'])}")
        elif args.command == "ask":
            for level, source in api.ask(_number_or_name(args.dest)).items():
                print(f"level {level}: source {source}")
        elif args.command == "crosspoints":
            data = api.controller()
            names = {source["number"]: source["name"] for source in data["sources"]}
            for dest in data["destinations"]:
                sources = "  ".join(f"{source} {names.get(source, '')}".strip() if source else "-"
                                    for source in dest["sources"])
                print(f"{dest['number']:>5}  {dest['name']:<12}  {sources}")
        elif args.command == "trace":
            for row in api.trace():
                print(f"{row['direction'].upper()}  {row['hex']:<44}  {row['text']}")
        elif args.command == "raw":
            print("sent", api.raw(args.hex, not args.unframed)["sent"])
        elif args.command == "router-start":
            ok = api.router_start()
            print("started" if ok else f"failed: {api.router()['status']['error']}")
            return 0 if ok else 1
        elif args.command == "router-set":
            api.router_set(args.source, args.dest, "all" if args.level == "all" else int(args.level))
        elif args.command == "decode":
            for row in api.decode(args.hex)["rows"]:
                print(f"{row['offset']:>5}  {row['hex']:<44}  {row['text']}")
    except ApiError as error:
        print(f"error {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"cannot reach {args.base}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
