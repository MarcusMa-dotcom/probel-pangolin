"""The analyzer: reading captures, framing messages, building the common ones."""

import pytest

from probelpangolin_app.analyzer import build_message, decode_capture, frame_message
from probelpangolin_app.common import hex_text, parse_hex


def test_parse_hex_reads_what_people_write():
    assert parse_hex("10 02 0a") == b"\x10\x02\x0a"
    assert parse_hex("10020A") == b"\x10\x02\x0a"
    assert parse_hex("0x10, 0x02\n0A") == b"\x10\x02\x0a"
    assert parse_hex("2 1 0") == b"\x02\x01\x00"
    assert parse_hex("10:02-0a") == b"\x10\x02\x0a"
    for bad in ("", "   ", "xyz", "123", None, 5):
        with pytest.raises(ValueError):
            parse_hex(bad)


def test_frame_message_explains_the_frame():
    data = frame_message(bytes.fromhex("02 01 00 02 04"))
    assert data["frame"] == "10 02 02 01 00 02 04 05 F2 10 03"
    assert (data["byte_count"], data["checksum"], data["stuffed"]) == (5, 0xF2, 0)
    assert data["text"] == "Crosspoint connect: matrix 1, level 2, destination 3 <- source 5"
    doubled = frame_message(build_message("connect", source=17, dest=17))
    assert doubled["frame"] == "10 02 02 00 00 10 10 10 10 05 D9 10 03" and doubled["stuffed"] == 2
    with pytest.raises(ValueError, match="too long"):
        frame_message(bytes(256))


def test_build_message_covers_the_common_requests():
    assert hex_text(build_message("interrogate", dest=3)) == "01 00 00 02"
    assert hex_text(build_message("connect", source=5, dest=3, level=2, extended=True)) == "82 00 01 00 02 00 04"
    assert hex_text(build_message("tally_dump", level=2, matrix=2)) == "15 11"
    assert hex_text(build_message("source_names", name_length=12)) == "64 00 02"
    assert hex_text(build_message("dest_names", name_length="4", extended=True)) == "E6 00 00"
    assert hex_text(build_message("commands")) == "61"
    for kind, options, message in (("connect", {"source": 1025}, "Source must be 1 to 1024"),
                                   ("connect", {"level": 17}, "Level must be 1 to 16"),
                                   ("source_names", {"name_length": 5}, "4, 8 or 12"),
                                   ("salvo", {}, "Unknown message")):
        with pytest.raises(ValueError, match=message):
            build_message(kind, **options)


def test_decode_capture_finds_frames_acks_and_errors():
    capture = parse_hex("""
        FF 10 02 02 01 00 02 04 05 F2 10 03   10 06
        10 02 04 01 00 02 04 05 F0 10 03      10 06
        10 02 01 00 00 02 04 99 10 03         10 15
        10 02 02""")
    data = decode_capture(capture)
    kinds = [row["kind"] for row in data["rows"]]
    assert kinds == ["ignored", "message", "ack", "message", "ack", "error", "nak", "error"]
    assert (data["messages"], data["acks"], data["naks"], data["errors"]) == (2, 2, 1, 2)
    rows = data["rows"]
    assert [row["offset"] for row in rows] == [0, 1, 12, 14, 25, 27, 37, 39]
    assert rows[1]["text"].startswith("Crosspoint connect") and "checksum F2 correct" in rows[1]["detail"]
    assert rows[5]["text"] == "Broken frame: checksum mismatch" and "it would be F9" in rows[5]["detail"]
    assert "Incomplete" in rows[7]["text"]
    # Every byte of the capture belongs to exactly one row.
    assert b"".join(parse_hex(row["hex"]) for row in rows) == capture


def test_decode_capture_keeps_doubled_dle_and_restarted_frames_apart():
    doubled = parse_hex("10 02 02 00 00 10 10 10 10 05 D9 10 03")
    row = decode_capture(doubled)["rows"][0]
    assert row["kind"] == "message" and "destination 17 <- source 17" in row["text"]
    assert "2 DLE doubled" in row["detail"]

    restarted = parse_hex("10 02 01 00  10 02 01 00 00 02 04 F9 10 03")
    rows = decode_capture(restarted)["rows"]
    assert [r["kind"] for r in rows] == ["error", "message"]
    assert rows[0]["hex"] == "10 02 01 00" and rows[1]["hex"].startswith("10 02 01 00 00")

    assert [r["kind"] for r in decode_capture(parse_hex("10 02 01 00"))["rows"]] == ["error"]

    assert [r["kind"] for r in decode_capture(parse_hex("01 02 03"))["rows"]] == ["ignored"]


def test_decode_capture_allows_an_ack_in_the_middle_of_a_frame():
    # The specification permits it. The frame goes on after the ACK.
    capture = parse_hex("10 02 04 11 00  10 06  02 00 05 E4 10 03")
    data = decode_capture(capture)
    rows = data["rows"]
    assert [(row["kind"], row["offset"]) for row in rows] == [("ack", 5), ("message", 0)]
    assert rows[1]["hex"] == "10 02 04 11 00 02 00 05 E4 10 03" and not rows[1]["problem"]
    assert "middle of the frame" in rows[0]["text"]
    assert (data["messages"], data["acks"], data["errors"]) == (1, 1, 0)
    # An ACK in a frame that never ends: the ACK counts, the rest is incomplete.
    rows = decode_capture(parse_hex("10 02 01 00 10 06"))["rows"]
    assert [row["kind"] for row in rows] == ["ack", "error"] and rows[1]["hex"] == "10 02 01 00"


def test_examples_of_the_specification_read_as_it_describes_them():
    data = decode_capture(parse_hex("10 02 02 11 00 02 00 05 E6 10 03 10 06 10 02 04 11 00 02 00 05 E4 10 03 10 06"))
    texts = [row["text"] for row in data["rows"]]
    assert texts[0] == "Crosspoint connect: matrix 2, level 2, destination 3 <- source 1"
    assert texts[2] == "Crosspoint connected: matrix 2, level 2, destination 3 <- source 1"
    answer = decode_capture(parse_hex(
        "10 02 62 08 09 03 04 16 20 22 53 62 63 01 02 15 1F 20 21 43 4A 61 14 9C 10 03"))["rows"][0]
    assert answer["text"] == "Protocol implementation response: the router sends 8 commands and accepts 9"


def test_salvo_and_dual_controller_messages_and_the_guaranteed_length():
    assert hex_text(build_message("salvo_connect", source=5, dest=3, level=2, salvo=7)) == "78 01 00 02 04 07"
    assert hex_text(build_message("salvo_go", salvo=7)) == "79 00 07"
    assert hex_text(build_message("salvo_clear", salvo=7)) == "79 01 07"
    assert hex_text(build_message("salvo_interrogate", salvo=7, index=2)) == "7C 07 02"
    assert hex_text(build_message("dual_status")) == "08"
    assert frame_message(build_message("salvo_go", salvo=7))["text"] == (
        "Crosspoint go group salvo: salvo 7, set the stored crosspoints")
    for kind, options, message in (("salvo_go", {"salvo": 128}, "Salvo must be 0 to 127"),
                                   ("salvo_connect", {"extended": True}, "standard commands only")):
        with pytest.raises(ValueError, match=message):
            build_message(kind, **options)
    assert frame_message(bytes(128))["oversize"] is False
    assert frame_message(bytes(129))["oversize"] is True


def test_swp02_capture_frame_and_build():
    capture = parse_hex("11 FF 02 00 02 04 78 FF 04 00 02 04 76 FF 01 00 FF 07 79 FF 03 00 02 04 70 FF 06")
    data = decode_capture(capture, "sw-p-02")
    rows = data["rows"]
    assert [row["kind"] for row in rows] == ["ignored", "message", "message", "error", "error", "error", "error"]
    assert [row["offset"] for row in rows] == [0, 1, 7, 13, 16, 19, 25]
    assert rows[1]["text"] == "Connect: destination 3 <- source 5" and "checksum 78 correct" in rows[1]["detail"]
    assert "cut short" in rows[3]["text"] and "STATUS_REQUEST" in rows[4]["text"]
    assert "it would be 77" in rows[5]["detail"]
    assert (data["protocol"], data["messages"], data["errors"]) == ("sw-p-02", 2, 4)
    assert b"".join(parse_hex(row["hex"]) for row in rows) == capture        # every byte in one row

    frame = frame_message(build_message("connect", "sw-p-02", source=5, dest=3), "sw-p-02")
    assert frame["frame"] == "FF 02 00 02 04 78" and frame["checksum"] == 0x78 and frame["protocol"] == "sw-p-02"
    assert hex_text(build_message("interrogate", "sw-p-02", dest=3)) == "01 00 02"
    assert hex_text(build_message("salvo_connect", "sw-p-02", source=7, dest=2)) == "05 00 01 06"
    assert hex_text(build_message("salvo_go", "sw-p-02")) == "06 00"
    assert hex_text(build_message("salvo_clear", "sw-p-02")) == "06 01"
    for call, message in ((lambda: build_message("tally_dump", "sw-p-02"), "SW-P-02 has no such message"),
                          (lambda: build_message("connect", "sw-p-02", source=1024), "Source must be 1 to 1023"),
                          (lambda: frame_message(bytes((2, 0, 1)), "sw-p-02"), "CONNECT has 3 message bytes"),
                          (lambda: frame_message(bytes((2, 0x80, 1, 1)), "sw-p-02"), "top bit"),
                          (lambda: decode_capture(b"x", "sw-p-99"), "sw-p-08 or sw-p-02")):
        with pytest.raises(ValueError, match=message):
            call()
