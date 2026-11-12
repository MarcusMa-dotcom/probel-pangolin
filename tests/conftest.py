"""Fixtures shared by the tests: a free port, a running virtual router, a controller."""

import socket
import time

import pytest

from probelpangolin_app.controller_engine import ControllerEngine, ControllerSettings
from probelpangolin_app.router_engine import RouterEngine, RouterSettings


def free_port() -> int:
    """A TCP port that nothing listens on right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(predicate, timeout: float = 3.0) -> None:
    """Wait until ``predicate()`` is true, or fail."""
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.01)


@pytest.fixture
def router():
    """A virtual router with 8 sources, 8 destinations and 2 levels, listening on a free port."""
    engine = RouterEngine(RouterSettings(port=free_port(), sources=8, dests=8, levels=2))
    assert engine.start()
    yield engine
    engine.close()


@pytest.fixture
def controller(router):
    """A controller set up for the virtual router, not connected yet."""
    engine = ControllerEngine(ControllerSettings(port=router.settings.port, levels=2,
                                                 auto_reconnect=False))
    yield engine
    engine.close()
