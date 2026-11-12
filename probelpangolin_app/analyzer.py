"""The analyzer: reads captured bytes, frames a message, builds the common messages.

Nothing here touches the network. A capture can come from a serial sniffer,
from Wireshark ("Copy as hex stream") or from the trace of this program.
"""

from __future__ import annotations

from typing import Any

from probelpangolin.protocol import (ACK, DLE, ETX, GUARANTEED_DATA, NAK, STX, Ack, Frame, FrameDecoder,
                                   FrameError, Nak, NameLength, ProtocolError, build_connect,
                                   build_dest_names_request, build_dual_controller_status_request,
                                   build_interrogate, build_protocol_implementation_request,
                                   build_salvo_connect, build_salvo_go, build_salvo_interrogate,
                                   build_source_names_request, build_tally_dump_request, command_name,
                                   encode_frame)

from probelpangolin import swp02

from .common import SWP02, SWP08, check_protocol, hex_text, message_text, to_bool, to_int

MAX_ROWS = 2000   # entries a decoded capture lists

BUILD_KINDS = {
    "connect": "Crosspoint connect",
    "interrogate": "Crosspoint interrogate",
    "tally_dump": "Crosspoint tally dump request",
    "source_names": "All source names request",
    "dest_names": "All destination names request",
    "commands": "Protocol implementation request",
    "salvo_connect": "Salvo: store a crosspoint (connect on go)",
    "salvo_go": "Salvo: set the stored crosspoints (go)",
    "salvo_clear": "Salvo: clear the stored crosspoints",
    "salvo_interrogate": "Salvo: ask for a stored crosspoint",
    "dual_status": "Dual controller status request",
}
SALVO_KINDS = frozenset({"salvo_connect", "salvo_go", "salvo_clear", "salvo_interrogate"})
# What SW-P-02 has of these, under the names of its specification.
BUILD_KINDS_02 = {
    "connect": "Connect",
    "interrogate": "Interrogate",
    "salvo_connect": "Connect on go: store a crosspoint",
    "salvo_go": "Go: set the stored crosspoints",
    "salvo_clear": "Go: clear the stored crosspoints",
}


def frame_message(payload: bytes, protocol: str = SWP08) -> dict[str, Any]:
    """Wrap the DATA bytes of a message into a frame and explain its parts.

    Args:
        payload: The DATA bytes: the command byte and what follows it.
        protocol: ``sw-p-08`` or ``sw-p-02``. An SW-P-02 message has no byte
            count and nothing is doubled: ``byte_count`` is the number of
            bytes after the command, ``stuffed`` is 0.

    Returns:
        ``payload`` and ``frame`` as hex, ``byte_count``, ``checksum``,
        ``stuffed`` (how many bytes were doubled because they equal DLE),
        ``oversize`` (more than the 128 data bytes every system accepts),
        ``name`` of the command, ``text`` in plain words and ``readable``.

    Raises:
        ValueError: The message is empty or longer than 255 bytes.
    """
    if check_protocol(protocol) == SWP02:
        return _frame_message_02(payload)
    try:
        frame = encode_frame(payload)
    except ProtocolError as exc:
        raise ValueError(str(exc).capitalize()) from None
    count = len(payload)
    checksum = (-(sum(payload) + count)) & 0xFF
    body = payload + bytes((count, checksum))
    text, readable = message_text(payload)
    return {
        "protocol": SWP08,
        "payload": hex_text(payload),
        "frame": hex_text(frame),
        "length": len(frame),
        "byte_count": count,
        "checksum": checksum,
        "stuffed": body.count(DLE),
        # 128 data bytes are what every system accepts, up to 255 only custom applications.
        "oversize": count > GUARANTEED_DATA,
        "command": payload[0],
        "name": command_name(payload[0]),
        "text": text,
        "readable": readable,
    }


def _frame_message_02(payload: bytes) -> dict[str, Any]:
    """An SW-P-02 message with start of message and checksum, explained."""
    if not payload:
        raise ValueError("Cannot send an empty message")
    if any(byte & 0x80 for byte in payload):
        raise ValueError("In SW-P-02 the top bit of every byte after the start of message is 0")
    expected = swp02.LENGTHS.get(payload[0])
    if expected is not None and len(payload) != expected + 1:
        raise ValueError(f"{swp02.command_name(payload[0])} has {expected} message "
                         f"byte{'' if expected == 1 else 's'}, here are {len(payload) - 1}")
    frame = swp02.encode_message(payload)
    text, readable = message_text(payload, SWP02)
    return {
        "protocol": SWP02,
        "payload": hex_text(payload),
        "frame": hex_text(frame),
        "length": len(frame),
        "byte_count": len(payload) - 1,
        "checksum": swp02.checksum(payload),
        "stuffed": 0,
        "oversize": False,
        "command": payload[0],
        "name": swp02.command_name(payload[0]),
        "text": text,
        "readable": readable,
    }


def _decode_capture_02(data: bytes) -> dict[str, Any]:
    """Read captured SW-P-02 bytes: messages, and whatever cannot be read."""
    decoder = swp02.MessageDecoder()
    rows: list[dict[str, Any]] = []
    counts = {"messages": 0, "acks": 0, "naks": 0, "errors": 0}
    pending = bytearray()
    start = 0
    ignored = 0
    truncated = False

    def add(row: dict[str, Any]) -> None:
        """Keep a row, up to the limit."""
        nonlocal truncated
        if len(rows) < MAX_ROWS:
            rows.append(row)
        else:
            truncated = True

    def emit(chunk: bytes, event: Any) -> None:
        """List what came before a message and was skipped, then the message."""
        nonlocal start, ignored
        junk = decoder.discarded - ignored
        ignored = decoder.discarded
        if 0 < junk < len(chunk):
            add(_ignored_row(start, chunk[:junk]))
            start += junk
            chunk = chunk[junk:]
        add(_row_02(start, chunk, event, counts))

    for offset, byte in enumerate(data):
        if not pending:
            start = offset
        pending.append(byte)
        for event in decoder.feed(bytes((byte,))):
            chunk = bytes(pending)
            pending.clear()
            if isinstance(event, FrameError) and byte == swp02.SOM:
                chunk = chunk[:-1]                    # this start of message opens the next one
                pending.append(byte)
            emit(chunk, event)
            if pending:
                start = offset
    for event in decoder.flush():
        emit(bytes(pending), event)
        pending.clear()
    if pending:
        chunk = bytes(pending)
        if decoder.discarded - ignored >= len(chunk):
            add(_ignored_row(start, chunk))
        else:
            counts["errors"] += 1
            add({"offset": start, "hex": hex_text(chunk), "kind": "error", "name": "",
                 "text": "Incomplete: the capture ends in the middle of a message", "detail": "",
                 "problem": True})
    return {"protocol": SWP02, "rows": rows, "bytes": len(data), "truncated": truncated, **counts}


def _row_02(offset: int, chunk: bytes, event: Any, counts: dict[str, int]) -> dict[str, Any]:
    """One event of the SW-P-02 decoder as a row."""
    row: dict[str, Any] = {"offset": offset, "hex": hex_text(chunk), "name": "", "detail": "",
                           "problem": False}
    if isinstance(event, FrameError):
        counts["errors"] += 1
        known = event.raw[0] in swp02.LENGTHS if event.raw else False
        detail = ""
        if known and len(event.raw) == swp02.LENGTHS[event.raw[0]] + 2:
            detail = (f"checksum is {event.raw[-1]:02X}, for these bytes it would be "
                      f"{swp02.checksum(event.raw[:-1]):02X}")
        row.update(kind="error", text=f"Not readable: {event.reason}", detail=detail, problem=True,
                   name=swp02.command_name(event.raw[0]) if event.raw else "")
        return row
    counts["messages"] += 1
    payload = event.payload
    text, readable = message_text(payload, SWP02)
    count = len(payload) - 1
    row.update(kind="message", name=swp02.command_name(payload[0]), text=text, problem=not readable,
               payload=hex_text(payload),
               detail=(f"FF, command {payload[0]}, {count} message byte{'' if count == 1 else 's'}, "
                       f"checksum {swp02.checksum(payload):02X} correct"))
    return row


def decode_capture(data: bytes, protocol: str = SWP08) -> dict[str, Any]:
    """Read a stream of captured bytes: frames, ACKs, NAKs and whatever is broken.

    Args:
        data: The bytes in the order they crossed the wire, one direction or
            both mixed.
        protocol: ``sw-p-08`` or ``sw-p-02``.

    Returns:
        ``rows``, one per thing found, each with ``offset`` (where it starts),
        ``hex``, ``kind`` (``message``, ``ack``, ``nak``, ``error`` or
        ``ignored``), ``name``, ``text``, ``detail`` and ``problem``. Plus the
        counts ``messages``, ``acks``, ``naks``, ``errors``, the number of
        ``bytes`` and ``truncated`` if the capture held more rows than listed.
    """
    if check_protocol(protocol) == SWP02:
        return _decode_capture_02(data)
    decoder = FrameDecoder()
    rows: list[dict[str, Any]] = []
    counts = {"messages": 0, "acks": 0, "naks": 0, "errors": 0}
    start = 0          # offset where the bytes of the next row begin
    pending = bytearray()
    ignored = 0
    truncated = False

    def add(row: dict[str, Any]) -> None:
        """Keep a row, up to the limit."""
        nonlocal truncated
        if len(rows) < MAX_ROWS:
            rows.append(row)
        else:
            truncated = True

    for offset, byte in enumerate(data):
        if not pending:
            start = offset
        pending.append(byte)
        for event in decoder.feed(bytes((byte,))):
            junk = decoder.discarded - ignored   # bytes skipped before this thing began
            ignored = decoder.discarded
            if isinstance(event, (Ack, Nak)) and decoder.in_frame:
                # Permitted by the specification: an ACK or NAK in the middle of a frame.
                del pending[-2:]
                row = _event_row(offset - 1, bytes((DLE, byte)), event, counts)
                row["text"] += ". It arrived in the middle of the frame listed after it"
                add(row)
                continue
            chunk = bytes(pending)
            pending.clear()
            if isinstance(event, FrameError) and event.reason.startswith("frame restarted"):
                chunk, again = chunk[:-2], chunk[-2:]     # the DLE STX belongs to the next frame
                pending.extend(again)
            elif isinstance(event, (Ack, Nak)):
                junk = len(chunk) - 2
            if 0 < junk < len(chunk):
                add(_ignored_row(start, chunk[:junk]))
                start += junk
                chunk = chunk[junk:]
            add(_event_row(start, chunk, event, counts))
            if pending:
                start = offset - 1
    if pending:
        chunk = bytes(pending)
        if decoder.discarded - ignored >= len(chunk):
            add(_ignored_row(start, chunk))
        else:
            counts["errors"] += 1
            add({"offset": start, "hex": hex_text(chunk), "kind": "error", "name": "",
                 "text": "Incomplete: the capture ends in the middle of a frame", "detail": "",
                 "problem": True})
    return {"protocol": SWP08, "rows": rows, "bytes": len(data), "truncated": truncated, **counts}


def _ignored_row(offset: int, chunk: bytes) -> dict[str, Any]:
    """Bytes that belong to no frame."""
    return {"offset": offset, "hex": hex_text(chunk), "kind": "ignored", "name": "",
            "text": f"{len(chunk)} byte{'' if len(chunk) == 1 else 's'} outside a frame, ignored",
            "detail": "", "problem": True}


def _event_row(offset: int, chunk: bytes, event: Any, counts: dict[str, int]) -> dict[str, Any]:
    """One decoder event as a row."""
    row: dict[str, Any] = {"offset": offset, "hex": hex_text(chunk), "name": "", "detail": "",
                           "problem": False}
    if isinstance(event, Ack):
        counts["acks"] += 1
        row.update(kind="ack", text="ACK: the message before was accepted",
                   detail=f"DLE {ACK:02X}")
    elif isinstance(event, Nak):
        counts["naks"] += 1
        row.update(kind="nak", text="NAK: the message before was refused", problem=True,
                   detail=f"DLE {NAK:02X}")
    elif isinstance(event, FrameError):
        counts["errors"] += 1
        row.update(kind="error", text=f"Broken frame: {event.reason}", problem=True,
                   detail=_body_detail(event.raw))
    elif isinstance(event, Frame):
        counts["messages"] += 1
        payload = event.payload
        text, readable = message_text(payload)
        stuffed = (payload + bytes((len(payload), (-(sum(payload) + len(payload))) & 0xFF))).count(DLE)
        detail = (f"DLE {STX:02X}, {len(payload)} data byte{'' if len(payload) == 1 else 's'}, byte count {len(payload):02X}, "
                  f"checksum {(-(sum(payload) + len(payload))) & 0xFF:02X} correct, DLE {ETX:02X}")
        if stuffed:
            detail += f", {stuffed} DLE doubled"
        row.update(kind="message", name=command_name(payload[0]), text=text, problem=not readable,
                   detail=detail, payload=hex_text(payload))
    return row


def _body_detail(body: bytes) -> str:
    """What byte count and checksum of a broken frame should have been."""
    if len(body) < 3:
        return ""
    data = body[:-2]
    expected = (-(sum(data) + len(data))) & 0xFF
    return (f"{len(data)} data byte{'' if len(data) == 1 else 's'}, byte count says {body[-2]:02X}, checksum is {body[-1]:02X}, "
            f"for these data bytes it would be {expected:02X}")


def _build_message_02(kind: Any, options: dict[str, Any]) -> bytes:
    """The SW-P-02 form of a message a controller sends."""
    if kind not in BUILD_KINDS_02:
        raise ValueError(f"SW-P-02 has no such message. It has: {', '.join(BUILD_KINDS_02)}")
    try:
        if kind in ("salvo_go", "salvo_clear"):
            return swp02.build_go(clear=kind == "salvo_clear")
        dest = to_int("Destination", options.get("dest", 1), 1, swp02.MAX_DEST)
        if kind == "interrogate":
            return swp02.build_interrogate(dest)
        source = to_int("Source", options.get("source", 1), 1, swp02.MAX_SOURCE)
        if kind == "connect":
            return swp02.build_connect(source, dest)
        return swp02.build_connect_on_go(source, dest)
    except ProtocolError as exc:
        raise ValueError(str(exc).capitalize()) from None


def build_message(kind: Any, protocol: str = SWP08, **options: Any) -> bytes:
    """Build the DATA bytes of one of the common messages a controller sends.

    Args:
        kind: One of the keys of ``BUILD_KINDS``, for SW-P-02 of ``BUILD_KINDS_02``.
        protocol: ``sw-p-08`` or ``sw-p-02``.
        **options: ``matrix``, ``level``, ``dest``, ``source`` (all counted
            from 1), ``extended``, ``name_length`` (4, 8 or 12), ``salvo`` and
            ``index`` (both 0 to 127), as far as the message needs them.

    Raises:
        ValueError: Unknown kind, or a number that does not fit the message.
    """
    if check_protocol(protocol) == SWP02:
        return _build_message_02(kind, options)
    if kind not in BUILD_KINDS:
        raise ValueError(f"Unknown message {kind!r}. Known: {', '.join(BUILD_KINDS)}")
    extended = to_bool("extended", options.get("extended", False))
    if kind in SALVO_KINDS and extended:
        raise ValueError("Salvo groups exist in the standard commands only")
    limit = 65536 if extended else 1024
    matrix = to_int("Matrix", options.get("matrix", 1), 1, 256 if extended else 16)
    level = to_int("Level", options.get("level", 1), 1, 256 if extended else 16)
    try:
        if kind == "connect":
            return build_connect(to_int("Source", options.get("source", 1), 1, limit),
                                 to_int("Destination", options.get("dest", 1), 1, limit),
                                 level, matrix, extended=extended)
        if kind == "interrogate":
            return build_interrogate(to_int("Destination", options.get("dest", 1), 1, limit),
                                     level, matrix, extended=extended)
        if kind == "tally_dump":
            return build_tally_dump_request(level, matrix, extended=extended)
        if kind in ("source_names", "dest_names"):
            chars = to_int("Name length", options.get("name_length", 8), 4, 12)
            if chars not in (4, 8, 12):
                raise ValueError("Name length must be 4, 8 or 12")
            length = NameLength.from_chars(chars)
            if kind == "source_names":
                return build_source_names_request(matrix, level, length, extended=extended)
            return build_dest_names_request(matrix, length, extended=extended)
        if kind in SALVO_KINDS:
            salvo = to_int("Salvo", options.get("salvo", 0), 0, 127)
            if kind == "salvo_connect":
                return build_salvo_connect(to_int("Source", options.get("source", 1), 1, limit),
                                           to_int("Destination", options.get("dest", 1), 1, limit),
                                           salvo, level, matrix)
            if kind == "salvo_interrogate":
                return build_salvo_interrogate(salvo, to_int("Index", options.get("index", 0), 0, 127))
            return build_salvo_go(salvo, clear=kind == "salvo_clear")
        if kind == "dual_status":
            return build_dual_controller_status_request()
        return build_protocol_implementation_request()
    except ProtocolError as exc:
        raise ValueError(str(exc).capitalize()) from None


__all__ = ["BUILD_KINDS", "BUILD_KINDS_02", "build_message", "decode_capture", "frame_message"]
