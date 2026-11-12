# How Probel Pangolin is built

```
 desktop window        web interface        REST API        your program
   (Tkinter)          (page + /ui/api)     (/api/v1)        (import probelpangolin)
       |                     |                 |                    |
       +----------+----------+-----------------+                    |
                  |                                                 |
        ControllerEngine      RouterEngine      analyzer            |
                  |                 |               |               |
                  +--------+--------+---------------+---------------+
                           |
                 probelpangolin (the library)
            protocol   client   simulator   swp02
                           |
                     TCP to a router, or from controllers
```

## The library does the protocol

[probelpangolin](https://github.com/MarcusMa-dotcom/probel-pangolin-lib) knows
the bytes: frames, messages, a client that controls a router, and a simulated
router. It is written for asyncio and has no dependencies. The program adds
nothing to the protocol. If a message is decoded wrong, the place to look is
the library.

## Two engines hold the state

`ControllerEngine` owns one client of the library and mirrors what the router
reports: crosspoints, names, implemented commands. `RouterEngine` owns one
simulated router and what it should look like: size, names, protection,
crosspoints, faults. Each engine

- checks every setting before it takes it (`update(**changes)` raises
  `ValueError` with a sentence a person can act on),
- keeps a trace of up to 5000 entries and an activity log of 300,
- counts what it sees,
- saves its settings a second after they changed,
- answers `snapshot()` with everything as plain data.

The window, the web interface and the REST API never talk to the network
themselves. They call the engines and draw snapshots. That is why they always
agree.

## Threads

The library runs in asyncio, the rest of the program in ordinary threads.
`LoopThread` in `common.py` is the bridge: each engine has one thread with an
event loop, and every call into the library is handed to it and waited for.
What the library reports comes back as callbacks in that thread. The engine
takes a lock for its state, and it never holds that lock while it waits for
the loop: the two would wait for each other.

A connection is told apart from the one before by a generation number. A
callback that arrives after a disconnect carries an old number and is
dropped.

## Polling instead of pushing

A snapshot carries `instance` (new at every start) and `revision` (one more
with every change). The page asks every 400 milliseconds and sends both back.
While nothing changed the answer is a few bytes. The window compares the
revision ten times a second and redraws only when it moved.

## The web server

Flask, in its own thread. The data routes exist twice, as `/ui/api/...` for
the page and as `/api/v1/...` for other programs, with their own switch and
their own login each. Before any route runs, the server checks the host name
(only local names unless started with `--host`), refuses changing requests
that are not JSON or come from another site, and asks the door for its login.
Answers carry headers that forbid framing and inline scripts.

## Files

| Path | Content |
|---|---|
| `probelpangolin_app/controller_engine.py` | The controller: connection, takes, mirror of crosspoints and names, trace and counters |
| `probelpangolin_app/router_engine.py` | The virtual router: size, names, crosspoints, protection, faults, saving |
| `probelpangolin_app/analyzer.py` | Reading captures, framing and building messages, for both protocols |
| `probelpangolin_app/common.py` | Checking settings, hex text, trace entries, the thread that runs the library |
| `probelpangolin_app/access.py` | Switches, logins, password hashing, login throttle |
| `probelpangolin_app/web.py`, `templates/`, `static/` | Web interface and REST API (Flask) |
| `probelpangolin_app/gui.py`, `gui_router.py`, `gui_analyzer.py`, `gui_settings.py`, `gui_common.py` | Desktop window (Tkinter) |
| `packaging/` | Recipe and start script for the packaged Mac app, and the logo drawing |

## Tests

`tests/test_engines.py` lets the two engines talk over real TCP connections on
this machine, with both protocols. `tests/test_web.py` drives the REST API
through Flask's test client, the guards and logins included.
`tests/test_analyzer.py` reads captures and builds messages.
`tests/test_access.py` covers switches, password hashing and the login
throttle. The library has its own tests, among them the examples of the
specification byte for byte.
