# REST API of Probel Pangolin

Everything the window and the web interface do goes through the same two
engines, and the REST API reaches all of it.

- [Basics](#basics)
- [Controller](#controller)
- [Router](#router)
- [Analyzer](#analyzer)
- [What the answers hold](#what-the-answers-hold)
- [Polling without load](#polling-without-load)
- [Answers](#answers)
- [Login](#login)
- [A session from start to end](#a-session-from-start-to-end)

## Basics

Base address: `http://localhost:8090/api/v1/`. All answers are JSON.
Requests that change something must send `Content-Type: application/json`,
with `{}` as body if there is nothing to send.

Numbers of matrix, level, source and destination count from 1, as on a panel.
A source or destination may also be given by its name once names are read.
Salvo groups are 0 to 127.

## Controller

| Method | Path | Does |
|---|---|---|
| GET | `/api/v1/` | List of endpoints |
| GET | `/api/v1/controller` | Settings, status with counters and activity, `sources`, `destinations` and the latest 250 trace entries |
| PATCH | `/api/v1/controller` | Change settings: `protocol` (`sw-p-08`, `sw-p-02`), `destinations`, `host`, `port`, `matrix`, `levels`, `extended` (`auto`, `on`, `off`), `name_length`, `attempts`, `query_commands`, `fetch_names`, `fetch_tally`, `wait_ack`, `keepalive`, `auto_reconnect`, `confirm` |
| POST | `/api/v1/controller/connect` | Connect and read what the settings ask for. `ok` tells whether it worked |
| POST | `/api/v1/controller/disconnect` | Close the connection |
| POST | `/api/v1/controller/route` | Take: `source`, `dest` (number or name), `levels` (a number, a list or `"all"`, level 1 if left out), `confirm` |
| POST | `/api/v1/controller/interrogate` | Ask for the source on `dest`, on `level` or on all levels |
| POST | `/api/v1/controller/dump` | Read all crosspoints of `level`, or of all levels |
| POST | `/api/v1/controller/names` | Read source and destination names |
| POST | `/api/v1/controller/commands` | Ask which commands the router implements |
| POST | `/api/v1/controller/salvo` | Salvo group `salvo` (0 to 127): `action` `add` with `source`, `dest`, `levels`; `go`; `clear`; `read` |
| POST | `/api/v1/controller/dual-status` | Ask for the dual controller status: `status` as hex and `bit1`, the bit that tells active from idle |
| POST | `/api/v1/controller/raw` | Send `hex` as a message, or exactly as written with `framed` false |
| GET | `/api/v1/controller/trace` | The whole trace, up to 5000 entries |
| POST | `/api/v1/controller/clear-trace` | Forget the trace and its counters |
| POST | `/api/v1/controller/clear-log` | Forget the activity list |

Connect, route source 5 to destination 3 on all levels, and look at the trace:

```bash
curl -X POST http://localhost:8090/api/v1/controller/connect -H 'Content-Type: application/json' -d '{}'
```

```bash
curl -X POST http://localhost:8090/api/v1/controller/route -H 'Content-Type: application/json' -d '{"source": 5, "dest": 3, "levels": "all"}'
```

```bash
curl http://localhost:8090/api/v1/controller/trace
```

A request to the router that fails answers 400 with the reason in `error`, for
example `Destination 3 on level 1 stays on source 1: the router did not take
source 5. The destination may be protected`.

## Router

| Method | Path | Does |
|---|---|---|
| GET | `/api/v1/router` | Settings, status with controllers, counters and activity, `sources`, `destinations` and the latest 250 trace entries |
| PATCH | `/api/v1/router` | Change settings: `protocol`, `bind`, `port`, `sources`, `dests`, `levels`, `matrix`, `extended`, `autostart`, and as whole lists `source_names`, `dest_names`, `protected`, `routes` |
| POST | `/api/v1/router/start` | Open the SW-P-08 port. `ok` tells whether it worked |
| POST | `/api/v1/router/stop` | Close the SW-P-08 port |
| POST | `/api/v1/router/crosspoint` | Set `source` on `dest` at `level` (a number or `"all"`). `announce` false keeps it from the controllers |
| POST | `/api/v1/router/name` | Rename: `kind` (`source` or `dest`), `number`, `name` |
| POST | `/api/v1/router/protect` | Protect `dest`, or release it with `protected` false |
| POST | `/api/v1/router/fault` | The next `drop` messages get no answer, the next `nak` a NAK |
| POST | `/api/v1/router/disconnect-clients` | Drop all controllers |
| GET | `/api/v1/router/trace` | The whole trace, up to 5000 entries |
| POST | `/api/v1/router/clear-trace` | Forget the trace and its counters |
| POST | `/api/v1/router/clear-log` | Forget the activity list |

## Analyzer

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/analyzer/decode` | Read the captured bytes in `hex`: `rows` with offset, bytes, kind, meaning and frame check, and the counts |
| POST | `/api/v1/analyzer/frame` | Wrap the data bytes in `hex` into a frame: `frame`, `byte_count`, `checksum`, `stuffed`, `text` |
| POST | `/api/v1/analyzer/build` | Build a message: `kind` (`connect`, `interrogate`, `tally_dump`, `source_names`, `dest_names`, `commands`, `salvo_connect`, `salvo_go`, `salvo_clear`, `salvo_interrogate`, `dual_status`), `matrix`, `level`, `dest`, `source`, `name_length`, `extended`, `salvo`, `index` |

All three take `protocol` (`sw-p-08` unless given, or `sw-p-02`). SW-P-02 has
the messages `connect`, `interrogate`, `salvo_connect` (connect on go),
`salvo_go` and `salvo_clear`.

```bash
curl -X POST http://localhost:8090/api/v1/analyzer/decode -H 'Content-Type: application/json' -d '{"hex": "10 02 02 01 00 02 04 05 F2 10 03 10 06"}'
```

```bash
curl -X POST http://localhost:8090/api/v1/analyzer/decode -H 'Content-Type: application/json' -d '{"protocol": "sw-p-02", "hex": "FF 02 00 02 04 78 FF 04 00 02 04 76"}'
```

## What the answers hold

`GET /api/v1/controller` answers with:

| Field | Content |
|---|---|
| `instance`, `revision` | For polling, see below |
| `settings` | Every setting of the controller, as `PATCH` takes them |
| `status.connected`, `connecting`, `waiting` | State of the connection. `waiting` means: the router is not there, trying again |
| `status.description`, `protocol`, `levels` | Address, protocol in use, and the number of levels in use (always 1 with SW-P-02) |
| `status.error` | Why the last connect failed, or that the connection was lost |
| `status.commands`, `commands_known` | Implemented commands as `code`, `name` and `direction` (`to router`, `from router`) |
| `status.destination_count`, `source_count` | How many destinations are known, and the highest source seen |
| `status.stats` | `sent`, `received`, `acks`, `naks`, `acks_sent`, `naks_sent`, `repeats`, `errors`, `ack_ms_last`, `ack_ms_mean`, `ack_ms_max`, `ack_slow` |
| `status.log` | The latest 60 activity entries: `time`, `action`, `text`, `problem` |
| `status.trace_count` | How many trace entries there were since the last clearing |
| `sources` | `number` and `name` of every named source |
| `destinations` | Up to 1024 rows: `number`, `name`, `sources` (one per level, `null` if unknown), `changed` |
| `trace` | The latest 250 trace entries |

`GET /api/v1/router` has the same shape. Its `status` holds `running`,
`clients` (`address`, `since`, `messages`), `salvos` (`number` and
`entries` with `level`, `dest`, `source`), `drop_pending`, `nak_pending` and
the counters `received`, `sent`, `takes`, `refused`, `acks`, `naks`,
`acks_sent`, `naks_sent`, `errors`. Its `destinations` carry `protected`.

A trace entry:

| Field | Content |
|---|---|
| `id` | Running number within the trace |
| `time` | Seconds since 1970, with fractions |
| `direction` | `tx` sent by this side, `rx` received |
| `kind` | `message`, `ack`, `nak`, `error` (a broken frame) or `raw` (own bytes as written) |
| `hex` | The bytes on the wire |
| `command`, `name` | Command byte and its name, for a message |
| `text` | The meaning in words |
| `note` | For example `attempt 2 of 5`, or why the virtual router did not answer |
| `problem` | True for what went wrong |
| `client` | On the router side: the controller it belongs to |

Times are seconds since 1970 in UTC.

## Polling without load

`GET /api/v1/controller` and `GET /api/v1/router` carry `instance` and
`revision`. Send them back as `?since=REVISION&instance=INSTANCE` and the
answer is a short `{"unchanged": true}` while nothing has changed.

## Answers

| Status | Meaning |
|---|---|
| 200 | Done. Requests to the router answer `{"ok": true, "result": ...}` |
| 400 | A value was refused, or the router did not do what was asked. `error` says why |
| 401, 429 | Login needed or wrong, or too many failed logins |
| 403 | The door is switched off, or the request came from another site |
| 415 | A changing request without `Content-Type: application/json` |

`examples/api_client.py` drives all of this from Python with nothing but the
standard library.

## Login

Without a login the API needs no header. With a login set in Settings it
takes HTTP Basic:

```bash
curl -u robot http://localhost:8090/api/v1/controller
```

After five failed logins from one address the answer is 429 with a
`Retry-After` header. Use HTTPS (`--tls-cert`, `--tls-key`) when the API is
reachable from the network: without it the password crosses in the clear.

## A session from start to end

Start the virtual router, connect the controller to it, switch, look at the
trace, and put a fault in:

```bash
curl -X POST http://localhost:8090/api/v1/router/start -H 'Content-Type: application/json' -d '{}'
```

```bash
curl -X POST http://localhost:8090/api/v1/controller/connect -H 'Content-Type: application/json' -d '{}'
```

```bash
curl -X POST http://localhost:8090/api/v1/controller/route -H 'Content-Type: application/json' -d '{"source": 5, "dest": 3, "levels": "all"}'
```

```json
{"ok": true, "result": {"source": 5, "dest": 3, "levels": [1, 2], "confirmed": true}, "revision": 70}
```

```bash
curl -X POST http://localhost:8090/api/v1/router/protect -H 'Content-Type: application/json' -d '{"dest": 3}'
```

```bash
curl -X POST http://localhost:8090/api/v1/controller/route -H 'Content-Type: application/json' -d '{"source": 7, "dest": 3}'
```

```json
{"error": "Destination 3 on level 1 stays on source 5: the router did not take source 7. The destination may be protected"}
```

```bash
curl -X POST http://localhost:8090/api/v1/router/fault -H 'Content-Type: application/json' -d '{"drop": 1}'
```

```bash
curl -X POST http://localhost:8090/api/v1/controller/interrogate -H 'Content-Type: application/json' -d '{"dest": 3}'
```

```bash
curl http://localhost:8090/api/v1/controller/trace
```

The trace now holds the interrogate twice, the second time with the note
`attempt 2 of 5`.

`examples/api_client.py` does the same from Python with nothing but the
standard library, and `examples/set_crosspoint.sh` is the shortest possible
take from a shell.
