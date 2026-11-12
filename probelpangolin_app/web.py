"""Flask web interface and REST API of Probel Pangolin.

Two doors lead to the same controller, the same virtual router and the same
analyzer:

* ``/`` with ``/ui/api/...`` is the web interface. Its optional login uses a
  form and a session cookie.
* ``/api/v1/...`` is the REST API for other programs. Its optional login is
  HTTP Basic.

Each door has its own switch and its own login in the access settings.
"""

from __future__ import annotations

import logging
import math
import secrets
import ssl
import threading
from datetime import timedelta

from flask import Blueprint, Flask, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler, make_server

from . import __version__, about
from .access import MIN_PASSWORD_LENGTH, Access
from .analyzer import BUILD_KINDS, BUILD_KINDS_02, build_message, decode_capture, frame_message
from .common import PROTOCOLS, SWP08, parse_hex
from .controller_engine import ControllerEngine
from .router_engine import MAX_LEVELS, MAX_PORTS, RouterEngine

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SESSION_HOURS = 12
MAX_BODY_BYTES = 1024 * 1024
CHANGING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy":
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}
API_ENDPOINTS = [
    {"method": "GET", "path": "/api/v1/", "does": "This list"},
    {"method": "GET", "path": "/api/v1/controller", "does": "Controller settings, status, crosspoints, names, trace"},
    {"method": "PATCH", "path": "/api/v1/controller", "does": "Change controller settings"},
    {"method": "POST", "path": "/api/v1/controller/connect", "does": "Connect to the router"},
    {"method": "POST", "path": "/api/v1/controller/disconnect", "does": "Close the connection"},
    {"method": "POST", "path": "/api/v1/controller/route", "does": "Route a source to a destination"},
    {"method": "POST", "path": "/api/v1/controller/interrogate", "does": "Ask which source is on a destination"},
    {"method": "POST", "path": "/api/v1/controller/dump", "does": "Read all crosspoints"},
    {"method": "POST", "path": "/api/v1/controller/names", "does": "Read source and destination names"},
    {"method": "POST", "path": "/api/v1/controller/commands", "does": "Ask which commands the router implements"},
    {"method": "POST", "path": "/api/v1/controller/salvo", "does": "Salvo group: store a crosspoint, set, clear or read the group"},
    {"method": "POST", "path": "/api/v1/controller/dual-status", "does": "Ask for the dual controller status"},
    {"method": "POST", "path": "/api/v1/controller/raw", "does": "Send bytes of your own"},
    {"method": "GET", "path": "/api/v1/controller/trace", "does": "The whole trace of the controller"},
    {"method": "POST", "path": "/api/v1/controller/clear-trace", "does": "Forget the trace and its counters"},
    {"method": "POST", "path": "/api/v1/controller/clear-log", "does": "Forget the controller activity log"},
    {"method": "GET", "path": "/api/v1/router", "does": "Virtual router: settings, status, crosspoints, names, trace"},
    {"method": "PATCH", "path": "/api/v1/router", "does": "Change router settings"},
    {"method": "POST", "path": "/api/v1/router/start", "does": "Open the SW-P-08 port"},
    {"method": "POST", "path": "/api/v1/router/stop", "does": "Close the SW-P-08 port"},
    {"method": "POST", "path": "/api/v1/router/crosspoint", "does": "Set a crosspoint at the router"},
    {"method": "POST", "path": "/api/v1/router/name", "does": "Rename a source or destination"},
    {"method": "POST", "path": "/api/v1/router/protect", "does": "Protect or release a destination"},
    {"method": "POST", "path": "/api/v1/router/fault", "does": "Let the next messages go unanswered or be refused"},
    {"method": "POST", "path": "/api/v1/router/disconnect-clients", "does": "Drop all controllers"},
    {"method": "GET", "path": "/api/v1/router/trace", "does": "The whole trace of the router"},
    {"method": "POST", "path": "/api/v1/router/clear-trace", "does": "Forget the trace and its counters"},
    {"method": "POST", "path": "/api/v1/router/clear-log", "does": "Forget the router activity log"},
    {"method": "POST", "path": "/api/v1/analyzer/decode", "does": "Read captured bytes: frames, ACK, NAK, errors"},
    {"method": "POST", "path": "/api/v1/analyzer/frame", "does": "Wrap the data bytes of a message into a frame"},
    {"method": "POST", "path": "/api/v1/analyzer/build", "does": "Build one of the common messages"},
]


LOCAL_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


class _QuietHandler(WSGIRequestHandler):
    """Answers without telling which server and Python version run here."""

    def version_string(self) -> str:
        """The server name sent with every answer."""
        return "Probel-Pangolin"


def is_loopback(host: str) -> bool:
    """True if a web server bound to ``host`` can be reached from this machine only."""
    return host in LOOPBACK_HOSTS


def _request_hostname() -> str:
    """The host name the caller asked for, without port and IPv6 brackets."""
    host = request.host
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.split(":", 1)[0]


def _client() -> str:
    """Network address of the caller, used by the login throttle."""
    return request.remote_addr or "unknown"


def _error(message: str, status: int, **headers: str):
    """A JSON error answer.

    Args:
        message: Text for the ``error`` field.
        status: HTTP status code.
        **headers: Extra headers. Underscores in the names become dashes.
    """
    response = jsonify(error=message)
    response.status_code = status
    for name, value in headers.items():
        response.headers[name.replace("_", "-")] = value
    return response


def _too_many(wait: float):
    """Answer 429 with the waiting time, also as ``Retry-After`` header."""
    seconds = max(1, math.ceil(wait))
    return _error(f"Too many failed logins. Try again in {seconds} seconds.", 429,
                  Retry_After=str(seconds))


def _json_object():
    """The request body as dict, or None if it is not a JSON object."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _state(engine, **extra):
    """The snapshot of an engine as JSON answer.

    A caller that passes ``since`` (a revision) and ``instance`` gets a short
    answer with ``unchanged`` instead of the whole snapshot while nothing has
    changed. That keeps polling a large router cheap.
    """
    since = request.args.get("since")
    if not extra and since is not None and request.args.get("instance") == engine.instance \
            and since == str(engine.revision):
        return jsonify(instance=engine.instance, revision=engine.revision, unchanged=True)
    data = engine.snapshot()
    data.update(extra)
    return jsonify(data)


def _state_error(engine, exc: Exception):
    """Answer 400 with the snapshot and the reason, for a setting that was refused."""
    data = engine.snapshot()
    data["error"] = str(exc)
    return jsonify(data), 400


def _data_blueprint(name: str, controller: ControllerEngine, router: RouterEngine) -> Blueprint:
    """Controller, router and analyzer routes. Mounted once for the web page, once as REST API."""
    bp = Blueprint(name, __name__)

    def body():
        """The JSON object of the request, an empty one if there is no body."""
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else ({} if data is None else None)

    def run(engine, work, status: int = 400):
        """Do something at an engine and answer with its result, or with 400 and the reason."""
        try:
            result = work()
        except ValueError as exc:
            return _error(str(exc), status)
        return jsonify(ok=True, result=result, revision=engine.revision)

    # -- controller: a router somewhere on the network --------------------

    @bp.get("/controller")
    def get_controller():
        """GET /controller: settings, status, crosspoints, names and the latest trace entries."""
        return _state(controller)

    @bp.route("/controller", methods=["PATCH"])
    def patch_controller():
        """PATCH /controller: change controller settings. Answers 400 with ``error`` for bad values."""
        changes = _json_object()
        if changes is None:
            return _error("Body must be a JSON object", 400)
        try:
            controller.update(**changes)
        except ValueError as exc:
            return _state_error(controller, exc)
        return _state(controller)

    @bp.post("/controller/connect")
    def controller_connect():
        """POST /controller/connect: connect and run the start-up queries. ``ok`` tells whether it worked."""
        return _state(controller, ok=controller.connect())

    @bp.post("/controller/disconnect")
    def controller_disconnect():
        """POST /controller/disconnect: close the connection."""
        controller.disconnect()
        return _state(controller, ok=True)

    @bp.post("/controller/clear-log")
    def controller_clear_log():
        """POST /controller/clear-log: forget the activity log."""
        controller.clear_log()
        return _state(controller, ok=True)

    @bp.post("/controller/clear-trace")
    def controller_clear_trace():
        """POST /controller/clear-trace: forget the trace and start counting again."""
        controller.clear_trace()
        return _state(controller, ok=True)

    @bp.get("/controller/trace")
    def controller_trace():
        """GET /controller/trace: everything the trace holds, oldest entry first."""
        return jsonify(trace=controller.trace())

    @bp.post("/controller/route")
    def controller_route():
        """POST /controller/route: route ``source`` to ``dest`` on ``levels``."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        return run(controller, lambda: controller.route(
            data.get("source"), data.get("dest"), data.get("levels", data.get("level")),
            data.get("confirm")))

    @bp.post("/controller/interrogate")
    def controller_interrogate():
        """POST /controller/interrogate: ask which source is on ``dest``, on one level or on all."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        return run(controller, lambda: {str(level): source for level, source in
                                        controller.interrogate(data.get("dest"), data.get("level")).items()})

    @bp.post("/controller/dump")
    def controller_dump():
        """POST /controller/dump: read all crosspoints of ``level``, or of all levels."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        return run(controller, lambda: {str(level): count for level, count in
                                        controller.dump(data.get("level")).items()})

    @bp.post("/controller/names")
    def controller_names():
        """POST /controller/names: read source and destination names."""
        return run(controller, controller.read_names)

    @bp.post("/controller/commands")
    def controller_commands():
        """POST /controller/commands: ask the router which commands it implements."""
        return run(controller, controller.query_commands)

    @bp.post("/controller/salvo")
    def controller_salvo():
        """POST /controller/salvo: ``action`` add, go, clear or read for the group ``salvo``."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        action, salvo = data.get("action"), data.get("salvo")
        if action == "add":
            return run(controller, lambda: controller.salvo_add(
                data.get("source"), data.get("dest"), salvo, data.get("levels", data.get("level"))))
        if action in ("go", "clear"):
            return run(controller, lambda: controller.salvo_go(salvo, clear=action == "clear"))
        if action == "read":
            return run(controller, lambda: controller.salvo_read(salvo))
        return _error("action must be add, go, clear or read", 400)

    @bp.post("/controller/dual-status")
    def controller_dual_status():
        """POST /controller/dual-status: ask a router with two controllers which one this is."""
        return run(controller, controller.dual_status)

    @bp.post("/controller/raw")
    def controller_raw():
        """POST /controller/raw: send ``hex`` as a message, or as it is with ``framed`` false."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        return run(controller, lambda: controller.send_raw(data.get("hex"), data.get("framed", True)))

    # -- router: the virtual router of this program -----------------------

    @bp.get("/router")
    def get_router():
        """GET /router: settings, status, crosspoints, names and the latest trace entries."""
        return _state(router)

    @bp.route("/router", methods=["PATCH"])
    def patch_router():
        """PATCH /router: change router settings. Answers 400 with ``error`` for bad values."""
        changes = _json_object()
        if changes is None:
            return _error("Body must be a JSON object", 400)
        try:
            router.update(**changes)
        except ValueError as exc:
            return _state_error(router, exc)
        return _state(router)

    @bp.post("/router/start")
    def router_start():
        """POST /router/start: open the SW-P-08 port. ``ok`` tells whether it worked."""
        return _state(router, ok=router.start())

    @bp.post("/router/stop")
    def router_stop():
        """POST /router/stop: close the SW-P-08 port."""
        router.stop_serving()
        return _state(router, ok=True)

    @bp.post("/router/clear-log")
    def router_clear_log():
        """POST /router/clear-log: forget the activity log."""
        router.clear_log()
        return _state(router, ok=True)

    @bp.post("/router/clear-trace")
    def router_clear_trace():
        """POST /router/clear-trace: forget the trace and start counting again."""
        router.clear_trace()
        return _state(router, ok=True)

    @bp.get("/router/trace")
    def router_trace():
        """GET /router/trace: everything the trace holds, oldest entry first."""
        return jsonify(trace=router.trace())

    @bp.post("/router/crosspoint")
    def router_crosspoint():
        """POST /router/crosspoint: set ``source`` on ``dest`` at the router itself."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        return run(router, lambda: router.set_crosspoint(
            data.get("source"), data.get("dest"), data.get("level", 1), data.get("announce", True)))

    @bp.post("/router/name")
    def router_name():
        """POST /router/name: rename the source or destination ``number``."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        return run(router, lambda: router.set_name(data.get("kind"), data.get("number"), data.get("name")))

    @bp.post("/router/protect")
    def router_protect():
        """POST /router/protect: protect ``dest``, or release it with ``protected`` false."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        return run(router, lambda: router.protect(data.get("dest"), data.get("protected", True)))

    @bp.post("/router/fault")
    def router_fault():
        """POST /router/fault: the next ``drop`` messages get no answer, the next ``nak`` a NAK."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        return run(router, lambda: router.inject(data.get("drop", 0), data.get("nak", 0)))

    @bp.post("/router/disconnect-clients")
    def router_disconnect_clients():
        """POST /router/disconnect-clients: drop every controller."""
        return run(router, router.disconnect_clients)

    # -- analyzer: no network, only bytes ----------------------------------

    @bp.post("/analyzer/decode")
    def analyzer_decode():
        """POST /analyzer/decode: read the captured bytes in ``hex``."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        try:
            return jsonify(decode_capture(parse_hex(data.get("hex"), "The captured bytes"),
                                          data.get("protocol", SWP08)))
        except ValueError as exc:
            return _error(str(exc), 400)

    @bp.post("/analyzer/frame")
    def analyzer_frame():
        """POST /analyzer/frame: wrap the data bytes in ``hex`` into a frame."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        try:
            return jsonify(frame_message(parse_hex(data.get("hex"), "The data bytes"),
                                         data.get("protocol", SWP08)))
        except ValueError as exc:
            return _error(str(exc), 400)

    @bp.post("/analyzer/build")
    def analyzer_build():
        """POST /analyzer/build: build the message ``kind`` from matrix, level, dest, source."""
        data = body()
        if data is None:
            return _error("Body must be a JSON object", 400)
        options = {key: data[key] for key in ("matrix", "level", "dest", "source", "extended",
                                              "name_length", "salvo", "index") if key in data}
        protocol = data.get("protocol", SWP08)
        try:
            return jsonify(frame_message(build_message(data.get("kind"), protocol, **options), protocol))
        except ValueError as exc:
            return _error(str(exc), 400)

    return bp


def create_app(
    controller: ControllerEngine,
    router: RouterEngine | None = None,
    access: Access | None = None,
    local_only: bool = True,
    tls: bool = False,
) -> Flask:
    """Create the Flask app.

    With ``local_only`` the app answers only requests addressed to this
    machine by a loopback name, which blocks DNS rebinding from web pages.
    """
    app = Flask(__name__)
    app.json.sort_keys = False
    # New key at every start: sessions end when the program ends.
    app.secret_key = secrets.token_bytes(32)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_BODY_BYTES,
        SESSION_COOKIE_NAME="probel_pangolin_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=tls,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    )
    if router is None:
        router = RouterEngine()
    if access is None:
        access = Access()

    def logged_in() -> bool:
        """True if this browser holds a session for the current web login."""
        return session.get("g") == access.generation("web") and session.get("in") is True

    def start_session() -> None:
        """Begin a fresh session for the current web login."""
        session.clear()
        session.permanent = True
        session["in"] = True
        session["g"] = access.generation("web")

    # -- guards ----------------------------------------------------------

    def guard_api():
        """Gate of the REST API: first the switch, then HTTP Basic with the throttle."""
        if not access.enabled("api"):
            return _error("The REST API is switched off", 403)
        if not access.login_required("api"):
            return None
        challenge = {"WWW_Authenticate": 'Basic realm="Probel Pangolin API", charset="UTF-8"'}
        auth = request.authorization
        if auth is None or auth.type != "basic" or auth.username is None:
            return _error("Login required", 401, **challenge)
        ok, wait = access.attempt("api", _client(), auth.username, auth.password or "")
        if ok:
            return None
        if wait > 0:
            return _too_many(wait)
        return _error("User name or password is wrong", 401, **challenge)

    def guard_web():
        """Gate of the web interface: first the switch, then the session.

        The login page and the static files stay reachable without a session.
        """
        wants_json = request.path.startswith("/ui/api/")
        if not access.enabled("web"):
            if wants_json:
                return _error("The web interface is switched off", 403)
            return "The web interface is switched off.\n", 403, {"Content-Type": "text/plain"}
        if not access.login_required("web") or logged_in():
            return None
        if request.path in ("/login", "/ui/api/login") or request.path.startswith("/static/"):
            return None
        if wants_json:
            return _error("Login required", 401)
        return redirect("/login")

    @app.before_request
    def guard():
        """Runs before every request.

        It checks the host name, turns away requests that other web pages could
        trigger, and then asks the gate of the door the request goes to.
        """
        if local_only and _request_hostname() not in LOOPBACK_HOSTS:
            return _error("This interface only answers on localhost", 403)
        if request.method in CHANGING_METHODS:
            # A JSON content type cannot be sent by a plain cross-site form,
            # and browsers name the page a request comes from.
            if not request.is_json:
                return _error("Requests that change something must use Content-Type application/json", 415)
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return _error("Cross-site request refused", 403)
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return _error("Request from another origin refused", 403)
        if request.path.startswith("/api/"):
            return guard_api()
        return guard_web()

    @app.after_request
    def harden(response):
        """Add the security headers and forbid caching of everything but static files."""
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if not request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        """HTTP errors as JSON on API paths, as the usual page elsewhere."""
        if request.path.startswith(("/api/", "/ui/api/")):
            return _error(exc.description or exc.name, exc.code or 500)
        return exc

    @app.errorhandler(Exception)
    def unexpected(exc: Exception):
        """Any other error: log it and answer 500 without details."""
        logging.getLogger("probelpangolin_app.web").exception("Request failed")
        return _error("Internal error", 500)

    # -- pages -----------------------------------------------------------

    @app.get("/")
    def index():
        """GET /: the page with all views."""
        return render_template(
            "index.html",
            version=__version__,
            build_kinds=BUILD_KINDS,
            build_kinds_02=BUILD_KINDS_02,
            protocols=PROTOCOLS,
            max_ports=MAX_PORTS,
            max_levels=MAX_LEVELS,
            min_password=MIN_PASSWORD_LENGTH,
            about=about,
        )

    @app.get("/login")
    def login_page():
        """GET /login: the login form, or a redirect to the page if no login is needed."""
        if not access.login_required("web") or logged_in():
            return redirect("/")
        return render_template("login.html", version=__version__)

    # -- web interface only: login and access settings -------------------

    def access_state() -> dict:
        """The access settings for the Settings view, plus what the page needs for its warnings."""
        data = access.public()
        data.update(
            logged_in=access.login_required("web") and logged_in(),
            exposed=not local_only,
            tls=tls,
            min_password=MIN_PASSWORD_LENGTH,
        )
        return data

    @app.post("/ui/api/login")
    def login():
        """POST /ui/api/login: check user and password and start a session."""
        if not access.login_required("web"):
            return jsonify(ok=True)
        data = _json_object() or {}
        user, password = data.get("user"), data.get("password")
        ok, wait = access.attempt(
            "web", _client(),
            user if isinstance(user, str) else "",
            password if isinstance(password, str) else "",
        )
        if not ok:
            if wait > 0:
                return _too_many(wait)
            return _error("User name or password is wrong", 401)
        start_session()
        return jsonify(ok=True)

    @app.post("/ui/api/logout")
    def logout():
        """POST /ui/api/logout: end the session."""
        session.clear()
        return jsonify(ok=True)

    @app.get("/ui/api/access")
    def get_access():
        """GET /ui/api/access: switches and logins, without the hashes."""
        return jsonify(access_state())

    @app.route("/ui/api/access", methods=["PATCH"])
    def patch_access():
        """PATCH /ui/api/access: change switches and logins.

        Without a web login only this machine may do that. With one, a change of
        the web login needs the current password in ``current_password``.
        """
        changes = _json_object()
        if changes is None:
            return _error("Body must be a JSON object", 400)
        if not access.login_required("web") and _client() not in LOCAL_CLIENTS:
            # Without a web login, only this machine may hand out logins and switches.
            # Otherwise anyone on the network could take the program over.
            return _error("Access settings can be changed from another machine "
                          "only when the web interface requires a login", 403)
        current = changes.pop("current_password", None)
        changes_web_login = any(key in changes for key in ("web_login", "web_user", "web_password"))
        if access.login_required("web") and changes_web_login:
            # Changing the web login needs the current password again.
            ok, wait = access.attempt(
                "web", _client(), access.settings.web_user,
                current if isinstance(current, str) else "",
            )
            if not ok:
                if wait > 0:
                    return _too_many(wait)
                return _error("Enter the current web password to change the web login", 403)
        try:
            access.update(**changes)
        except ValueError as exc:
            return _error(str(exc), 400)
        if access.login_required("web"):
            start_session()  # this browser made the change and stays logged in
        return jsonify(access_state())

    # -- REST API only ---------------------------------------------------

    @app.get("/api/v1/")
    def api_index():
        """GET /api/v1/: name, version and the list of endpoints."""
        return jsonify(name="Probel Pangolin", version=__version__, endpoints=API_ENDPOINTS)

    app.register_blueprint(_data_blueprint("ui", controller, router), url_prefix="/ui/api")
    app.register_blueprint(_data_blueprint("v1", controller, router), url_prefix="/api/v1")
    return app


class WebServer:
    """Runs the Flask app in a background thread, as long as a door is open.

    The server listens only while the web interface or the REST API is
    switched on. Switching both off closes the port.
    """

    def __init__(
        self,
        controller: ControllerEngine,
        router: RouterEngine | None = None,
        access: Access | None = None,
        host: str = "127.0.0.1",
        port: int = 8090,
        tls: tuple[str, str] | None = None,
    ):
        """Prepare the server. The port opens with :meth:`start`.

        Args:
            controller: The controller engine.
            router: The virtual router. A fresh one is made if left out.
            access: The access settings. Defaults to both doors open, no logins.
            host: Address to listen on. Anything but a loopback address makes the
                server reachable from other machines.
            port: Port to listen on, 0 for a free one.
            tls: Certificate file and key file for HTTPS, or None for HTTP.
        """
        self.host = host
        self.port = port
        self.error = ""
        self.access = access if access is not None else Access()
        self._tls = tls
        # The pages poll several times a second: keep the request log quiet.
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        self._app = create_app(controller, router, self.access,
                               local_only=is_loopback(host), tls=tls is not None)
        self._lock = threading.Lock()
        self._server = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self.access.on_change(self._access_changed)

    @property
    def exposed(self) -> bool:
        """True if the server listens beyond this machine."""
        return not is_loopback(self.host)

    @property
    def tls(self) -> bool:
        """True if the server speaks HTTPS."""
        return self._tls is not None

    @property
    def wanted(self) -> bool:
        """True if at least one door is switched on."""
        return self.access.enabled("web") or self.access.enabled("api")

    @property
    def running(self) -> bool:
        """True while the port is open."""
        return self._server is not None

    @property
    def url(self) -> str:
        """Address of the web interface as a person would type it."""
        scheme = "https" if self._tls else "http"
        shown = "localhost" if self.host in ("0.0.0.0", "::") or is_loopback(self.host) else self.host
        return f"{scheme}://{shown}:{self.port}/"

    def start(self) -> bool:
        """Open the port if a door is switched on. Returns False if that failed."""
        self.apply()
        return not self.error

    def apply(self) -> None:
        """Open or close the port to match the access settings."""
        with self._lock:
            if self._closed:
                return
            if self.wanted and self._server is None:
                try:
                    server = make_server(self.host, self.port, self._app, threaded=True,
                                         ssl_context=self._tls, request_handler=_QuietHandler)
                except SystemExit:
                    # Werkzeug ends the program when it cannot bind. Keep running instead.
                    self.error = (f"Cannot serve on {self.host}:{self.port}: "
                                  "the port is in use or not allowed")
                    return
                except (OSError, ssl.SSLError, ValueError) as exc:
                    self.error = f"Cannot serve on {self.host}:{self.port}: {exc}"
                    return
                self.error = ""
                self.port = server.server_port
                self._server = server
                self._thread = threading.Thread(target=server.serve_forever, name="pangolin-web", daemon=True)
                self._thread.start()
            elif not self.wanted and self._server is not None:
                self._shutdown()

    def _shutdown(self) -> None:
        """Close the port. Called with the lock held."""
        server, thread = self._server, self._thread
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)
        # Only now: "running" must stay true until the port is really closed.
        self._server = None
        self._thread = None

    def _access_changed(self) -> None:
        """Listener for access changes: adjust the port a moment later."""
        # A moment later, so the answer to the request that made the change still goes out.
        timer = threading.Timer(0.3, self.apply)
        timer.daemon = True
        timer.start()

    def stop(self) -> None:
        """Close the port for good, at program end."""
        with self._lock:
            self._closed = True
            if self._server is not None:
                self._shutdown()
