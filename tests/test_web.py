"""Tests of the web interface and the REST API, through Flask's test client."""

import base64

import pytest
from conftest import wait_for

from probelpangolin_app.access import MAX_FAILURES, Access
from probelpangolin_app.web import API_ENDPOINTS, WebServer, create_app

LOCAL = {"base_url": "http://localhost:8090"}
PASSWORD = "correct horse battery"


def basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def access():
    return Access()


@pytest.fixture
def client(controller, router, access):
    return create_app(controller, router, access).test_client()


@pytest.fixture
def linked(client):
    """The controller of the app connected to the virtual router of the app, through the API."""
    assert client.post("/api/v1/controller/connect", json={}, **LOCAL).get_json()["ok"]
    return client


# -- pages ------------------------------------------------------------------

def test_page_loads(client):
    response = client.get("/", **LOCAL)
    assert response.status_code == 200
    for text in (b"Probel Pangolin", b"Crosspoints", b"Faults on purpose", b"Read captured bytes",
                 b"Crosspoint tally dump request"):
        assert text in response.data
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    for name in ("app.js", "controller.js", "router.js", "analyzer.js", "settings.js", "views.js",
                 "app.css", "logo.png"):
        assert client.get(f"/static/{name}", **LOCAL).status_code == 200


def test_api_index_lists_every_route(client):
    data = client.get("/api/v1/", **LOCAL).get_json()
    assert data["name"] == "Probel Pangolin" and data["endpoints"] == API_ENDPOINTS
    listed = {(item["method"], item["path"]) for item in API_ENDPOINTS}
    served = {(method, rule.rule) for rule in client.application.url_map.iter_rules()
              if rule.rule.startswith("/api/v1/")
              for method in rule.methods - {"HEAD", "OPTIONS"}}
    assert listed == served


# -- controller -------------------------------------------------------------

def test_controller_state_and_polling(client):
    data = client.get("/api/v1/controller", **LOCAL).get_json()
    assert data["status"]["connected"] is False and data["settings"]["port"] > 0
    query = f"?since={data['revision']}&instance={data['instance']}"
    assert client.get("/api/v1/controller" + query, **LOCAL).get_json() == {
        "instance": data["instance"], "revision": data["revision"], "unchanged": True}
    client.patch("/api/v1/controller", json={"confirm": False}, **LOCAL)
    assert "settings" in client.get("/api/v1/controller" + query, **LOCAL).get_json()
    assert "settings" in client.get(f"/api/v1/controller?since={data['revision']}&instance=other",
                                    **LOCAL).get_json()


def test_controller_settings_are_checked(client):
    response = client.patch("/api/v1/controller", json={"port": 70000}, **LOCAL)
    assert response.status_code == 400 and response.get_json()["error"] == "Port must be 1 to 65535"
    assert "settings" in response.get_json()
    assert client.patch("/api/v1/controller", json={"nonsense": 1}, **LOCAL).status_code == 400
    data = client.patch("/api/v1/controller", json={"host": "10.0.0.20", "name_length": 12},
                        **LOCAL).get_json()
    assert data["settings"]["host"] == "10.0.0.20" and data["settings"]["name_length"] == 12


def test_take_interrogate_and_read_again(linked, router):
    answer = linked.post("/api/v1/controller/route", json={"source": "SRC 5", "dest": 3, "levels": "all"},
                         **LOCAL).get_json()
    assert answer["ok"] and answer["result"] == {"source": 5, "dest": 3, "levels": [1, 2], "confirmed": True}
    assert router.settings.routes[0][2] == 5 and router.settings.routes[1][2] == 5
    assert linked.post("/api/v1/controller/interrogate", json={"dest": 3},
                       **LOCAL).get_json()["result"] == {"1": 5, "2": 5}
    assert linked.post("/api/v1/controller/dump", json={"level": 2}, **LOCAL).get_json()["result"] == {"2": 8}
    assert linked.post("/api/v1/controller/names", json={}, **LOCAL).get_json()["result"] == {
        "sources": 8, "destinations": 8}
    assert len(linked.post("/api/v1/controller/commands", json={}, **LOCAL).get_json()["result"]) > 10
    data = linked.get("/api/v1/controller", **LOCAL).get_json()
    assert data["destinations"][2]["sources"] == [5, 5] and data["status"]["connected"]
    assert data["trace"][0]["hex"].startswith("10 02") and data["status"]["stats"]["naks"] == 0


def test_refused_requests_answer_400_with_the_reason(linked, router):
    router.protect(2)
    response = linked.post("/api/v1/controller/route", json={"source": 4, "dest": 2}, **LOCAL)
    assert response.status_code == 400 and "stays on source 1" in response.get_json()["error"]
    assert linked.post("/api/v1/controller/route", json={"source": 4}, **LOCAL).status_code == 400
    assert linked.post("/api/v1/controller/route", json=[4, 2], **LOCAL).status_code == 400
    assert linked.post("/api/v1/controller/raw", json={"hex": "zz"}, **LOCAL).status_code == 400
    linked.post("/api/v1/controller/disconnect", json={}, **LOCAL)
    response = linked.post("/api/v1/controller/route", json={"source": 4, "dest": 3}, **LOCAL)
    assert response.status_code == 400 and response.get_json()["error"] == "Not connected"


def test_own_message_trace_and_clearing(linked):
    answer = linked.post("/api/v1/controller/raw", json={"hex": "01 00 00 02"}, **LOCAL).get_json()
    assert answer["result"] == {"sent": "01 00 00 02", "framed": True}
    trace = linked.get("/api/v1/controller/trace", **LOCAL).get_json()["trace"]
    assert any(row["name"] == "CROSSPOINT_INTERROGATE" and row["direction"] == "tx" for row in trace)
    assert linked.post("/api/v1/controller/clear-trace", json={}, **LOCAL).get_json()["trace"] == []
    assert linked.post("/api/v1/controller/clear-log", json={}, **LOCAL).get_json()["status"]["log"] == []


# -- router -----------------------------------------------------------------

def test_router_start_stop_and_settings(client, router):
    data = client.get("/api/v1/router", **LOCAL).get_json()
    assert data["status"]["running"] and len(data["destinations"]) == 8
    assert "routes" not in data["settings"] and "source_names" not in data["settings"]
    assert client.post("/api/v1/router/stop", json={}, **LOCAL).get_json()["status"]["running"] is False
    data = client.patch("/api/v1/router", json={"dests": 4, "autostart": True}, **LOCAL).get_json()
    assert len(data["destinations"]) == 4 and data["settings"]["autostart"] is True
    response = client.patch("/api/v1/router", json={"levels": 99}, **LOCAL)
    assert response.status_code == 400 and response.get_json()["error"] == "Levels must be 1 to 32"
    assert client.post("/api/v1/router/start", json={}, **LOCAL).get_json()["ok"]


def test_router_crosspoint_name_protect_and_faults(linked, router, controller):
    assert linked.post("/api/v1/router/crosspoint", json={"source": 7, "dest": 5, "level": "all"},
                       **LOCAL).get_json()["ok"]
    wait_for(lambda: controller.snapshot()["destinations"][4]["sources"] == [7, 7])
    linked.post("/api/v1/router/name", json={"kind": "dest", "number": 5, "name": "PGM"}, **LOCAL)
    linked.post("/api/v1/router/protect", json={"dest": 5}, **LOCAL)
    dest = linked.get("/api/v1/router", **LOCAL).get_json()["destinations"][4]
    assert (dest["name"], dest["protected"], dest["sources"]) == ("PGM", True, [7, 7])
    assert linked.post("/api/v1/router/crosspoint", json={"source": 99, "dest": 5}, **LOCAL).status_code == 400
    assert linked.post("/api/v1/router/name", json={"kind": "x", "number": 1, "name": "a"},
                       **LOCAL).status_code == 400
    assert linked.post("/api/v1/router/fault", json={"drop": 2, "nak": 1}, **LOCAL).get_json()["ok"]
    status = linked.get("/api/v1/router", **LOCAL).get_json()["status"]
    assert (status["drop_pending"], status["nak_pending"]) == (2, 1)
    assert linked.post("/api/v1/router/fault", json={"drop": -1}, **LOCAL).status_code == 400
    assert linked.post("/api/v1/router/disconnect-clients", json={}, **LOCAL).get_json()["result"] == 1
    assert any(row["client"] for row in linked.get("/api/v1/router/trace", **LOCAL).get_json()["trace"])
    assert linked.post("/api/v1/router/clear-trace", json={}, **LOCAL).get_json()["trace"] == []
    assert linked.post("/api/v1/router/clear-log", json={}, **LOCAL).get_json()["status"]["log"] == []


# -- analyzer ---------------------------------------------------------------

def test_analyzer_decode_frame_and_build(client):
    data = client.post("/api/v1/analyzer/decode",
                       json={"hex": "10 02 02 01 00 02 04 05 F2 10 03 10 06"}, **LOCAL).get_json()
    assert [row["kind"] for row in data["rows"]] == ["message", "ack"] and data["messages"] == 1
    assert client.post("/api/v1/analyzer/decode", json={"hex": "nothing"}, **LOCAL).status_code == 400
    frame = client.post("/api/v1/analyzer/frame", json={"hex": "02 01 00 02 04"}, **LOCAL).get_json()
    assert frame["frame"] == "10 02 02 01 00 02 04 05 F2 10 03"
    built = client.post("/api/v1/analyzer/build", json={"kind": "interrogate", "dest": 3}, **LOCAL).get_json()
    assert built["payload"] == "01 00 00 02" and built["text"].startswith("Crosspoint interrogate")
    response = client.post("/api/v1/analyzer/build", json={"kind": "connect", "source": 2000}, **LOCAL)
    assert response.status_code == 400 and "Source must be 1 to 1024" in response.get_json()["error"]


def test_ui_routes_mirror_the_api(client):
    assert client.get("/ui/api/controller", **LOCAL).get_json()["status"]["connected"] is False
    assert client.get("/ui/api/router", **LOCAL).get_json()["status"]["running"] is True
    assert client.post("/ui/api/analyzer/frame", json={"hex": "61"}, **LOCAL).get_json()["frame"] == (
        "10 02 61 01 9E 10 03")


# -- guards -----------------------------------------------------------------

def test_only_localhost_names_are_answered(client):
    assert client.get("/api/v1/router", base_url="http://evil.example:8090").status_code == 403
    assert client.get("/api/v1/router", base_url="http://127.0.0.1:8090").status_code == 200


def test_changes_need_json_and_the_same_origin(client):
    assert client.post("/api/v1/router/stop", data="x", **LOCAL).status_code == 415
    assert client.post("/api/v1/router/stop", json={}, headers={"Sec-Fetch-Site": "cross-site"},
                       **LOCAL).status_code == 403
    assert client.post("/api/v1/router/stop", json={}, headers={"Origin": "http://evil.example"},
                       **LOCAL).status_code == 403
    assert client.patch("/api/v1/router", json=[1], **LOCAL).status_code == 400


def test_api_login(client, access):
    access.update(api_user="robot", api_password=PASSWORD, api_login=True)
    assert client.get("/api/v1/router", **LOCAL).status_code == 401
    assert client.get("/api/v1/router", headers=basic("robot", "wrong"), **LOCAL).status_code == 401
    assert client.get("/api/v1/router", headers=basic("robot", PASSWORD), **LOCAL).status_code == 200
    assert client.get("/ui/api/router", **LOCAL).status_code == 200      # the page has its own door
    for _ in range(MAX_FAILURES):
        client.get("/api/v1/router", headers=basic("robot", "wrong"), **LOCAL)
    locked = client.get("/api/v1/router", headers=basic("robot", PASSWORD), **LOCAL)
    assert locked.status_code == 429 and "Retry-After" in locked.headers


def test_web_login(client, access):
    access.update(web_user="marcus", web_password=PASSWORD, web_login=True)
    assert client.get("/", **LOCAL).status_code == 302
    assert client.get("/ui/api/controller", **LOCAL).status_code == 401
    assert client.get("/login", **LOCAL).status_code == 200
    assert client.post("/ui/api/login", json={"user": "marcus", "password": "wrong"}, **LOCAL).status_code == 401
    assert client.post("/ui/api/login", json={"user": "marcus", "password": PASSWORD}, **LOCAL).status_code == 200
    assert client.get("/", **LOCAL).status_code == 200
    assert client.get("/ui/api/router", **LOCAL).status_code == 200
    client.post("/ui/api/logout", json={}, **LOCAL)
    assert client.get("/ui/api/controller", **LOCAL).status_code == 401


def test_doors_can_be_switched_off(client, access):
    access.update(api_enabled=False)
    assert client.get("/api/v1/router", **LOCAL).status_code == 403
    assert client.get("/ui/api/router", **LOCAL).status_code == 200
    access.update(web_enabled=False)
    assert client.get("/", **LOCAL).status_code == 403


def test_web_server_opens_and_closes_its_port(controller, router):
    import urllib.request
    access = Access()
    server = WebServer(controller, router, access, "127.0.0.1", 0)
    assert server.start() and server.running
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/v1/", timeout=3) as response:
        assert response.status == 200 and response.headers["Server"] == "Probel-Pangolin"
    access.update(web_enabled=False, api_enabled=False)
    wait_for(lambda: not server.running)
    server.stop()


def test_salvo_and_dual_status_through_the_api(linked, router):
    add = {"action": "add", "salvo": 3, "source": 5, "dest": 2, "levels": "all"}
    assert linked.post("/api/v1/controller/salvo", json=add, **LOCAL).get_json()["result"] == {
        "salvo": 3, "source": 5, "dest": 2, "levels": [1, 2]}
    assert linked.post("/api/v1/controller/salvo", json={"action": "read", "salvo": 3},
                       **LOCAL).get_json()["result"] == [{"level": 1, "dest": 2, "source": 5},
                                                         {"level": 2, "dest": 2, "source": 5}]
    assert linked.get("/api/v1/router", **LOCAL).get_json()["status"]["salvos"][0]["number"] == 3
    assert router.settings.routes[0][1] == 1
    assert linked.post("/api/v1/controller/salvo", json={"action": "go", "salvo": 3},
                       **LOCAL).get_json()["result"] == {"salvo": 3, "outcome": "set"}
    assert router.settings.routes[0][1] == 5 and router.settings.routes[1][1] == 5
    assert linked.post("/api/v1/controller/salvo", json={"action": "clear", "salvo": 3},
                       **LOCAL).get_json()["result"]["outcome"] == "empty"
    assert linked.post("/api/v1/controller/salvo", json={"action": "fire", "salvo": 3}, **LOCAL).status_code == 400
    assert linked.post("/api/v1/controller/salvo", json={"action": "go", "salvo": 200}, **LOCAL).status_code == 400
    response = linked.post("/api/v1/controller/dual-status", json={}, **LOCAL)
    assert response.status_code == 400 and "DUAL_CONTROLLER_STATUS_REQUEST" in response.get_json()["error"]
    built = linked.post("/api/v1/analyzer/build", json={"kind": "salvo_interrogate", "salvo": 3, "index": 1},
                        **LOCAL).get_json()
    assert built["payload"] == "7C 03 01" and built["oversize"] is False
    data = linked.patch("/api/v1/controller", json={"attempts": 3}, **LOCAL).get_json()
    assert data["settings"]["attempts"] == 3


def test_swp02_through_the_api(client, router):
    assert client.patch("/api/v1/router", json={"protocol": "sw-p-02"}, **LOCAL).get_json()["status"]["running"]
    data = client.patch("/api/v1/controller", json={"protocol": "sw-p-02", "port": router.settings.port,
                                                    "destinations": 8}, **LOCAL).get_json()
    assert data["settings"]["protocol"] == "sw-p-02" and data["status"]["levels"] == 1
    assert client.post("/api/v1/controller/connect", json={}, **LOCAL).get_json()["ok"]
    assert client.post("/api/v1/controller/route", json={"source": 5, "dest": 3},
                       **LOCAL).get_json()["result"]["confirmed"]
    assert router.settings.routes[0][2] == 5
    trace = client.get("/api/v1/controller/trace", **LOCAL).get_json()["trace"]
    assert trace[-2]["hex"] == "FF 02 00 02 04 78" and trace[-1]["text"] == "Connected: destination 3 <- source 5"
    assert client.post("/api/v1/controller/salvo", json={"action": "add", "source": 7, "dest": 2},
                       **LOCAL).get_json()["result"]["salvo"] == 0
    assert client.post("/api/v1/controller/salvo", json={"action": "go"},
                       **LOCAL).get_json()["result"]["outcome"] == "set"
    response = client.post("/api/v1/controller/names", json={}, **LOCAL)
    assert response.status_code == 400 and "no names" in response.get_json()["error"]
    decoded = client.post("/api/v1/analyzer/decode", json={"hex": "FF 02 00 02 04 78", "protocol": "sw-p-02"},
                          **LOCAL).get_json()
    assert decoded["rows"][0]["text"] == "Connect: destination 3 <- source 5"
    built = client.post("/api/v1/analyzer/build", json={"kind": "interrogate", "dest": 3, "protocol": "sw-p-02"},
                        **LOCAL).get_json()
    assert built["frame"] == "FF 01 00 02 7D"
    assert client.post("/api/v1/analyzer/frame", json={"hex": "06 00", "protocol": "x"}, **LOCAL).status_code == 400
    page = client.get("/", **LOCAL).data
    assert b"SW-P-02" in page and b"Connect on go: store a crosspoint" in page
