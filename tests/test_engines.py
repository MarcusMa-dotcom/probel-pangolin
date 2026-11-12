"""The controller engine and the router engine, talking to each other over a real socket."""

import pytest
from conftest import free_port, wait_for

from probelpangolin_app.controller_engine import ControllerEngine, ControllerSettings, load_settings
from probelpangolin_app.router_engine import RouterEngine, RouterSettings
from probelpangolin_app.router_engine import load_settings as load_router_settings


def destination(snapshot: dict, number: int) -> dict:
    return next(dest for dest in snapshot["destinations"] if dest["number"] == number)


def test_connect_reads_commands_names_and_crosspoints(router, controller):
    router.set_name("source", 5, "CAM 5")
    router.set_crosspoint(5, 3, 2)
    assert controller.connect()
    data = controller.snapshot()
    status = data["status"]
    assert status["connected"] and not status["error"]
    assert status["commands_known"] and len(status["commands"]) > 10
    assert status["destination_count"] == 8 and status["source_count"] == 8
    assert {"number": 5, "name": "CAM 5"} in data["sources"]
    assert destination(data, 3)["sources"] == [1, 5]
    assert destination(data, 3)["changed"] is None          # read, not changed
    assert [client["address"].startswith("127.0.0.1:") for client in router.snapshot()["status"]["clients"]] == [True]


def test_take_by_number_and_by_name_on_all_levels(router, controller):
    controller.connect()
    result = controller.route("SRC 6", "dst 2", "all")
    assert result == {"source": 6, "dest": 2, "levels": [1, 2], "confirmed": True}
    assert destination(controller.snapshot(), 2)["sources"] == [6, 6]
    assert destination(router.snapshot(), 2)["sources"] == [6, 6]
    assert controller.interrogate(2) == {1: 6, 2: 6}
    assert controller.interrogate(2, 2) == {2: 6}
    actions = [entry["action"] for entry in controller.snapshot()["status"]["log"]]
    assert actions.count("take") == 2 and "asked" in actions
    assert router.snapshot()["status"]["stats"]["takes"] == 2


def test_take_is_refused_by_a_protected_destination(router, controller):
    controller.connect()
    router.protect(4)
    with pytest.raises(ValueError, match="stays on source 1"):
        controller.route(7, 4)
    assert destination(router.snapshot(), 4) | {"changed": None} == {
        "number": 4, "name": "DST 4", "protected": True, "sources": [1, 1], "changed": None}
    assert controller.snapshot()["status"]["log"][-1]["action"] == "refused"
    assert router.snapshot()["status"]["stats"]["refused"] == 1
    router.protect(4, False)
    controller.route(7, 4)
    assert destination(router.snapshot(), 4)["sources"][0] == 7


def test_bad_takes_are_refused_before_anything_is_sent(router, controller):
    with pytest.raises(ValueError, match="Not connected"):
        controller.route(1, 1)
    controller.connect()
    sent = controller.snapshot()["status"]["stats"]["sent"]
    for source, dest, levels, message in ((0, 1, None, "Source must be 1 to 65536"),
                                          ("NOPE", 1, None, "No source is named 'NOPE'"),
                                          (1, "", None, "Destination is missing"),
                                          (1, 1, [], "Levels are missing")):
        with pytest.raises(ValueError, match=message):
            controller.route(source, dest, levels)
    assert controller.snapshot()["status"]["stats"]["sent"] == sent


def test_change_at_the_router_reaches_the_controller(router, controller):
    controller.connect()
    router.set_crosspoint(8, 6, "all")
    wait_for(lambda: destination(controller.snapshot(), 6)["sources"] == [8, 8])
    data = controller.snapshot()
    assert destination(data, 6)["changed"] is not None
    assert data["status"]["log"][-1]["action"] == "change"
    # Without telling the controllers they find out only when they ask.
    router.set_crosspoint(2, 6, 1, announce=False)
    assert destination(controller.snapshot(), 6)["sources"] == [8, 8]
    assert controller.interrogate(6, 1) == {1: 2}
    assert destination(controller.snapshot(), 6)["sources"] == [2, 8]


def test_trace_counts_repeats_naks_and_times_the_ack(router, controller):
    controller.connect()
    controller.clear_trace()
    router.inject(drop=1)
    controller.route(2, 1)                       # the first attempt gets no answer
    stats = controller.snapshot()["status"]["stats"]
    assert stats["repeats"] == 1 and stats["sent"] == 2 and stats["acks"] == 1
    assert stats["ack_ms_last"] is not None and stats["ack_ms_mean"] is not None
    router.inject(nak=2)
    controller.route(3, 1)                       # refused twice, the third attempt gets through
    assert controller.snapshot()["status"]["stats"]["naks"] == 2
    controller.update(attempts=2)                # ends the connection
    controller.connect()
    router.inject(nak=2)
    with pytest.raises(ValueError, match="NAK"):
        controller.route(4, 1)
    stats = controller.snapshot()["status"]["stats"]
    assert stats["naks"] == 4 and stats["ack_slow"] >= 0
    trace = controller.trace()
    assert [row["id"] for row in trace] == list(range(1, len(trace) + 1))
    assert trace[0]["direction"] == "tx" and trace[0]["hex"].startswith("10 02")
    assert any(row["note"] == "attempt 2 of 5" for row in trace)
    assert any(row["kind"] == "nak" and row["problem"] for row in trace)
    router_trace = router.trace()
    assert any("no answer" in row["note"] for row in router_trace)
    assert all(row["client"] for row in router_trace)


def test_own_messages_framed_and_as_written(router, controller):
    controller.connect()
    controller.clear_trace()
    assert controller.send_raw("01 00 00 02") == {"sent": "01 00 00 02", "framed": True}
    wait_for(lambda: any(row["name"] == "CROSSPOINT_TALLY" for row in controller.trace()))
    controller.send_raw("10 02 01 00 00 02 04 99 10 03", framed=False)   # wrong checksum
    wait_for(lambda: controller.snapshot()["status"]["stats"]["naks"] == 1)
    assert any(row["kind"] == "raw" for row in controller.trace())
    assert router.snapshot()["status"]["stats"]["errors"] == 1
    with pytest.raises(ValueError, match="hex"):
        controller.send_raw("zz")


def test_read_again_names_dump_and_commands(router, controller):
    controller.update(fetch_names=False, fetch_tally=False, query_commands=False)
    controller.connect()
    data = controller.snapshot()
    assert not data["destinations"] and not data["sources"] and not data["status"]["commands_known"]
    router.set_name("dest", 2, "MON 2")
    assert controller.read_names() == {"sources": 8, "destinations": 8}
    assert controller.dump() == {1: 8, 2: 8}
    assert len(controller.query_commands()) > 10
    assert destination(controller.snapshot(), 2)["name"] == "MON 2"


def test_settings_are_checked_and_a_change_ends_the_connection(router, controller):
    controller.connect()
    with pytest.raises(ValueError, match="Port must be 1 to 65535"):
        controller.update(port=0)
    with pytest.raises(ValueError, match="auto, on or off"):
        controller.update(extended="maybe")
    with pytest.raises(ValueError, match="Attempts must be 1 to 10"):
        controller.update(attempts=0)
    with pytest.raises(ValueError, match="need the extended commands"):
        controller.update(extended="off", levels=17)
    controller.update(confirm=False)
    assert controller.connected                    # only matters when something is sent
    controller.update(name_length=12)
    assert not controller.connected


def test_extended_only_and_standard_only(router, controller):
    controller.update(extended="off")
    controller.connect()
    controller.route(3, 3)
    assert controller.trace()[-4]["name"] == "CROSSPOINT_CONNECT"
    controller.update(extended="on")
    controller.connect()
    controller.route(4, 3)
    assert controller.trace()[-4]["name"] == "EXT_CROSSPOINT_CONNECT"
    assert destination(router.snapshot(), 3)["sources"][0] == 4


def test_connect_fails_cleanly_when_nothing_listens():
    engine = ControllerEngine(ControllerSettings(port=free_port(), auto_reconnect=False))
    try:
        assert not engine.connect()
        status = engine.snapshot()["status"]
        assert not status["connected"] and not status["connecting"] and status["error"]
    finally:
        engine.close()


def test_controller_notices_a_router_that_goes_away_and_comes_back(router):
    engine = ControllerEngine(ControllerSettings(port=router.settings.port, levels=2))
    try:
        assert engine.connect()
        router.disconnect_clients()
        wait_for(lambda: any(entry["action"] == "disconnected" for entry in engine.snapshot()["status"]["log"]))
        wait_for(lambda: engine.connected, timeout=8.0)       # the library connects again by itself
        assert engine.route(5, 5)["confirmed"]
    finally:
        engine.close()


def test_router_resize_keeps_what_still_fits(router):
    router.set_crosspoint(8, 8, 2)
    router.set_name("dest", 8, "LAST")
    router.protect(8)
    router.update(dests=4, sources=4, levels=1)
    data = router.snapshot()
    assert data["status"]["running"]                     # restarted with the new size
    assert len(data["destinations"]) == 4 and len(data["sources"]) == 4
    assert all(dest["sources"] == [1] for dest in data["destinations"])
    assert router.settings.protected == []
    with pytest.raises(ValueError, match="Source must be 1 to 4"):
        router.set_crosspoint(5, 1)
    with pytest.raises(ValueError, match="Sources must be 1 to 4096"):
        router.update(sources=0)


def test_router_port_in_use_is_reported(router):
    other = RouterEngine(RouterSettings(port=router.settings.port))
    try:
        assert not other.start()
        assert "Cannot listen" in other.snapshot()["status"]["error"]
        with pytest.raises(ValueError, match="not running"):
            other.inject(drop=1)
    finally:
        other.close()


def test_settings_survive_a_restart(tmp_path):
    path = tmp_path / "settings.json"
    engine = ControllerEngine(config_path=path)
    engine.update(host="10.0.0.20", levels=4, extended="on")
    engine.close()
    assert load_settings(path) == ControllerSettings(host="10.0.0.20", levels=4, extended="on")

    router_path = tmp_path / "settings.router.json"
    router = RouterEngine(config_path=router_path)
    router.update(sources=4, dests=4, levels=1)
    router.set_crosspoint(3, 2)
    router.set_name("source", 3, "VTR")
    router.protect(1)
    router.close()
    loaded = load_router_settings(router_path)
    assert loaded.routes == [[1, 3, 1, 1]] and loaded.source_names[2] == "VTR" and loaded.protected == [1]
    path.write_text('{"port": "abc", "levels": 3}', encoding="utf-8")
    assert load_settings(path) == ControllerSettings(levels=3)       # a bad value keeps its default


def test_salvo_group_is_stored_then_set_at_once(router, controller):
    controller.connect()
    router.protect(4)
    assert controller.salvo_add("SRC 5", 1, 7) == {"salvo": 7, "source": 5, "dest": 1, "levels": [1]}
    controller.salvo_add(6, 2, 7, "all")
    controller.salvo_add(3, 4, 7)
    assert controller.salvo_read(7) == [{"level": 1, "dest": 1, "source": 5}, {"level": 1, "dest": 2, "source": 6},
                                        {"level": 2, "dest": 2, "source": 6}, {"level": 1, "dest": 4, "source": 3}]
    assert destination(router.snapshot(), 1)["sources"] == [1, 1]          # stored, not switched
    stored = router.snapshot()["status"]["salvos"]
    assert [group["number"] for group in stored] == [7] and len(stored[0]["entries"]) == 4
    assert controller.salvo_go(7) == {"salvo": 7, "outcome": "set"}
    data = router.snapshot()
    assert destination(data, 1)["sources"] == [5, 1] and destination(data, 2)["sources"] == [6, 6]
    assert destination(data, 4)["sources"] == [1, 1]                       # protected
    assert data["status"]["salvos"] == [] and data["status"]["stats"]["takes"] == 3
    # The router sends no Connected messages for a salvo, the controller follows the acknowledges.
    wait_for(lambda: destination(controller.snapshot(), 2)["sources"] == [6, 6])
    assert destination(controller.snapshot(), 1)["sources"] == [5, 1]
    assert controller.salvo_go(7) == {"salvo": 7, "outcome": "empty"}
    controller.salvo_add(8, 8, 9)
    assert controller.salvo_go(9, clear=True) == {"salvo": 9, "outcome": "cleared"}
    assert destination(router.snapshot(), 8)["sources"] == [1, 1]
    assert [entry["action"] for entry in router.snapshot()["status"]["log"]].count("salvo") == 8
    for call, message in ((lambda: controller.salvo_go(128), "Salvo must be 0 to 127"),
                          (lambda: controller.salvo_add(1, 1, "x"), "Salvo must be a whole number"),
                          (lambda: controller.salvo_add(2000, 1, 1), "up to 1024")):
        with pytest.raises(ValueError, match=message):
            call()


def test_commands_are_listed_with_their_direction(router, controller):
    controller.connect()
    commands = {command["name"]: command["direction"] for command in controller.snapshot()["status"]["commands"]}
    assert commands["CROSSPOINT_CONNECT"] == "to router"
    assert commands["CROSSPOINT_CONNECTED"] == "from router"
    assert commands["CROSSPOINT_GO_GROUP_SALVO"] == "to router"
    assert commands["EXT_CROSSPOINT_TALLY"] == "from router"


def test_dual_controller_status_without_an_answer_is_reported(router, controller):
    controller.connect()
    # The virtual router lists the commands it implements. This one is not among them.
    with pytest.raises(ValueError, match="does not implement DUAL_CONTROLLER_STATUS_REQUEST"):
        controller.dual_status()


# -- SW-P-02, the older General Switcher protocol --------------------------------

@pytest.fixture
def old_router():
    """A virtual router that speaks SW-P-02, with 8 sources and 8 destinations."""
    engine = RouterEngine(RouterSettings(protocol="sw-p-02", port=free_port(), sources=8, dests=8, levels=2))
    assert engine.start()
    yield engine
    engine.close()


@pytest.fixture
def old_controller(old_router):
    """A controller set to SW-P-02 for that router."""
    engine = ControllerEngine(ControllerSettings(protocol="sw-p-02", port=old_router.settings.port, levels=2,
                                                 destinations=8, auto_reconnect=False))
    yield engine
    engine.close()


def test_swp02_connect_asks_the_destinations_one_by_one(old_router, old_controller):
    old_router.set_crosspoint(5, 3)
    assert old_controller.connect()
    data = old_controller.snapshot()
    status = data["status"]
    assert status["connected"] and status["protocol"] == "sw-p-02" and status["levels"] == 1
    assert status["destination_count"] == 8 and not status["commands_known"] and not data["sources"]
    assert destination(data, 3)["sources"] == [5]                 # one level, whatever Levels says
    trace = old_controller.trace()
    assert trace[0]["hex"] == "FF 01 00 00 7F" and trace[0]["name"] == "INTERROGATE"
    assert trace[1]["text"] == "Tally: destination 1 <- source 1"
    assert status["stats"]["sent"] == 8 and status["stats"]["acks"] == 0     # no ACK in this protocol
    router_status = old_router.snapshot()["status"]
    assert router_status["protocol"] == "sw-p-02" and router_status["levels"] == 1
    assert len(old_router.snapshot()["destinations"][0]["sources"]) == 1


def test_swp02_take_interrogate_and_change_at_the_router(old_router, old_controller):
    old_controller.connect()
    assert old_controller.route(6, 2, "all") == {"source": 6, "dest": 2, "levels": [1], "confirmed": True}
    assert destination(old_router.snapshot(), 2)["sources"] == [6]
    assert old_controller.interrogate(2) == {1: 6}
    old_router.set_crosspoint(8, 4, "all")
    wait_for(lambda: destination(old_controller.snapshot(), 4)["sources"] == [8])
    old_router.protect(5)
    with pytest.raises(ValueError, match="stays on source 1"):
        old_controller.route(3, 5)
    with pytest.raises(ValueError, match="out of range"):
        old_controller.interrogate(9)
    old_router.inject(drop=1)
    with pytest.raises(ValueError, match="sent no answer"):
        old_controller.route(3, 6)
    assert old_controller.dump() == {1: 8}


def test_swp02_connect_on_go(old_router, old_controller):
    old_controller.connect()
    assert old_controller.salvo_add(5, 1, 99) == {"salvo": 0, "source": 5, "dest": 1, "levels": [1]}
    old_controller.salvo_add(6, 2)
    assert old_router.snapshot()["status"]["salvos"] == [{"number": 0, "entries": [
        {"level": 1, "dest": 1, "source": 5}, {"level": 1, "dest": 2, "source": 6}]}]
    assert destination(old_router.snapshot(), 1)["sources"] == [1]
    assert old_controller.salvo_go() == {"salvo": 0, "outcome": "set"}
    assert destination(old_router.snapshot(), 2)["sources"] == [6]
    # After a GO the router sends no CONNECTED: the controller interrogates.
    wait_for(lambda: destination(old_controller.snapshot(), 2)["sources"] == [6])
    assert old_controller.salvo_go()["outcome"] == "empty"
    old_controller.salvo_add(7, 7)
    assert old_controller.salvo_go(clear=True)["outcome"] == "cleared"


def test_swp02_says_what_it_does_not_have(old_router, old_controller):
    old_controller.connect()
    for call, message in ((old_controller.read_names, "carries no names"),
                          (old_controller.query_commands, "no list of implemented commands"),
                          (old_controller.dual_status, "no dual controller status"),
                          (old_controller.salvo_read, "cannot read back"),
                          (lambda: old_controller.route(1024, 1), "Source 1024 out of range"),
                          (lambda: old_router.inject(nak=1), "SW-P-02 has no NAK")):
        with pytest.raises(ValueError, match=message):
            call()
    assert old_controller.send_raw("01 00 02") == {"sent": "01 00 02", "framed": True}
    wait_for(lambda: old_controller.trace()[-1]["text"] == "Tally: destination 3 <- source 1")
    old_controller.send_raw("FF 03 00 02 04 70", framed=False)            # wrong checksum, no answer
    wait_for(lambda: old_router.snapshot()["status"]["stats"]["errors"] == 1)


def test_protocol_switch_moves_the_usual_port_and_checks_the_size():
    controller = ControllerEngine()
    router = RouterEngine()
    try:
        assert controller.update(protocol="sw-p-02")["settings"]["port"] == 2000
        assert controller.update(protocol="sw-p-08")["settings"]["port"] == 8910
        controller.update(port=9000)
        assert controller.update(protocol="sw-p-02")["settings"]["port"] == 9000       # not the usual one: kept
        assert router.update(protocol="sw-p-02")["settings"]["port"] == 2000
        with pytest.raises(ValueError, match="1023 sources and 1024 destinations"):
            router.update(sources=1024)
        router.update(protocol="sw-p-08")
        router.update(sources=1024)
        with pytest.raises(ValueError, match="1023 sources"):
            router.update(protocol="sw-p-02")
        with pytest.raises(ValueError, match="sw-p-08 or sw-p-02"):
            controller.update(protocol="sw-p-99")
    finally:
        controller.close()
        router.close()
