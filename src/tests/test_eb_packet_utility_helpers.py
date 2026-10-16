from __future__ import annotations

import math
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from utility_modules import eb_packet_utility as epu


class _CaptureQueue:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def put(self, item: Any) -> None:
        self.items.append(item)


class _FakeEvent:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.clear_calls = 0
        self.wait_timeouts: list[float] = []
        self.set_calls = 0

    def clear(self) -> None:
        self.clear_calls += 1

    def wait(self, timeout: float) -> bool:
        self.wait_timeouts.append(timeout)
        return self.result

    def set(self) -> None:
        self.set_calls += 1


def _payload(tm_type_id: int, marker: int = 0) -> str:
    raw = bytes([marker & 0xFF, 0, 0, 0, 0, (tm_type_id << 2) & 0xFC])
    return " ".join(f"{value:02X}" for value in raw)


def _patch_packet_decoders(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {
        "hk": [],
        "post": [],
        "dump": [],
        "critical": [],
        "noncritical": [],
        "latest_hk": [],
        "latest_post": [],
    }

    def _decoded(kind: str, packet: bytes) -> SimpleNamespace:
        result = SimpleNamespace(kind=kind, marker=packet[0])
        calls[kind].append(result)
        return result

    monkeypatch.setattr(epu, "parse_eb_hk", lambda packet: _decoded("hk", packet))
    monkeypatch.setattr(epu, "decode_post_hk", lambda packet: _decoded("post", packet))
    monkeypatch.setattr(epu, "decode_dump_data", lambda packet: _decoded("dump", packet))
    monkeypatch.setattr(epu, "decode_cscience_data", lambda packet: _decoded("critical", packet))
    monkeypatch.setattr(epu, "decode_ncscience_data", lambda packet: _decoded("noncritical", packet))
    monkeypatch.setattr(epu, "merge_sci_data_packet", lambda packet: packet)
    monkeypatch.setattr(
        epu,
        "set_latest_hk",
        lambda packet, packet_id=None: calls["latest_hk"].append(packet) or True,
    )
    monkeypatch.setattr(
        epu,
        "set_latest_post",
        lambda packet, packet_id=None: calls["latest_post"].append(packet) or True,
    )
    return calls


def test_latest_hk_and_psu_caches_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    event = _FakeEvent(result=True)
    monkeypatch.setattr(epu, "_hk_event", event)
    hk = SimpleNamespace(value="hk")
    psu = {"value": "psu"}

    epu.set_latest_hk(hk)
    epu.set_latest_psu(psu)

    assert epu.get_latest_hk() is hk
    assert epu.get_latest_psu() == psu
    assert event.set_calls == 1


def test_latest_post_cache_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epu, "_post_lock", threading.Lock())
    monkeypatch.setattr(epu, "_latest_post", None)
    monkeypatch.setattr(epu, "_latest_post_packet_id", None)
    monkeypatch.setattr(epu, "_post_sequence", 0)
    post = SimpleNamespace(value="post")

    epu.set_latest_post(post)

    assert epu.get_latest_post() is post
    assert epu.get_post_sequence() == 1


def test_wait_for_fresh_post_returns_new_cached_packet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epu, "_post_lock", threading.Lock())
    monkeypatch.setattr(epu, "_latest_post", None)
    monkeypatch.setattr(epu, "_latest_post_packet_id", None)
    monkeypatch.setattr(epu, "_post_sequence", 0)
    latest = SimpleNamespace(value="post")

    class _ArrivingEvent(_FakeEvent):
        def wait(self, timeout: float) -> bool:
            super().wait(timeout)
            epu.set_latest_post(latest)
            return True

    monkeypatch.setattr(epu, "_post_event", _ArrivingEvent(result=True))

    assert epu.wait_for_fresh_post(timeout=1.5, after_sequence=0) is latest


def test_wait_for_fresh_hk_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    event = _FakeEvent(result=False)
    monkeypatch.setattr(epu, "_hk_event", event)

    assert epu.wait_for_fresh_hk(timeout=0.25) is None
    assert event.wait_timeouts
    assert all(0 < timeout <= 0.25 for timeout in event.wait_timeouts)


def test_wait_for_fresh_hk_returns_latest_value(monkeypatch: pytest.MonkeyPatch) -> None:
    latest = SimpleNamespace(sequence=4)

    class _ArrivingEvent(_FakeEvent):
        def wait(self, timeout: float) -> bool:
            super().wait(timeout)
            epu.set_latest_hk(latest)
            return True

    monkeypatch.setattr(epu, "_hk_event", _ArrivingEvent(result=True))

    assert epu.wait_for_fresh_hk(timeout=1.5) is latest


def test_read_pkt_processes_all_packet_types_and_skips_bad_rows(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_packet_decoders(monkeypatch)
    hk_queue = _CaptureQueue()
    post_queue = _CaptureQueue()
    sci_queue = _CaptureQueue()
    monkeypatch.setattr(epu.const, "hk_queue", hk_queue)
    monkeypatch.setattr(epu.const, "eb_post_queue", post_queue)
    monkeypatch.setattr(epu.const, "sci_queue", sci_queue)

    rows = [
        "Telemetry Data:",
        "Telecommand.",
        "Telemetry Data:",
        "not hexadecimal",
        "Telemetry Data:",
        "00 01",
        "Telemetry Data:",
        _payload(0x3, 3),
        "Telemetry Data:",
        _payload(0x4, 4),
        "Telemetry Data:",
        _payload(0x5, 5),
        "Telemetry Data:",
        _payload(0x6, 6),
        "Telemetry Data:",
        _payload(0x1, 1),
        "Telemetry Data:",
        _payload(0x3F, 99),
        "Telemetry Data:",
    ]
    path = tmp_path / "rs422.log"
    path.write_text("\n".join(rows), encoding="utf-8")

    hk, post, dump, critical, noncritical, last_index = epu.read_pkt(path)

    assert hk.kind == "hk" and hk.marker == 1
    assert post.kind == "post" and post.marker == 3
    assert dump.kind == "dump" and dump.marker == 4
    assert critical.kind == "critical" and critical.marker == 5
    assert noncritical.kind == "noncritical" and noncritical.marker == 6
    assert critical.TM_TYPE_ID == 0x5
    assert critical.SCI_PACKET_CRITICALITY == "Critical"
    assert noncritical.TM_TYPE_ID == 0x6
    assert noncritical.SCI_PACKET_CRITICALITY == "Non-Critical"
    assert isinstance(last_index, int)
    assert hk_queue.items == [hk]
    assert post_queue.items == [post]
    assert sci_queue.items == [critical, noncritical]
    assert calls["latest_hk"] == [hk]
    assert calls["latest_post"] == [post]


def test_read_pkt_latest_only_prefers_latest_packet_of_each_type(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_packet_decoders(monkeypatch)
    monkeypatch.setattr(epu.const, "hk_queue", _CaptureQueue())
    monkeypatch.setattr(epu.const, "eb_post_queue", _CaptureQueue())
    monkeypatch.setattr(epu.const, "sci_queue", _CaptureQueue())

    entries = [
        (0x1, 1),
        (0x3, 3),
        (0x4, 4),
        (0x5, 5),
        (0x6, 6),
        (0x1, 9),
        (0x3, 13),
        (0x4, 14),
        (0x5, 15),
        (0x6, 16),
    ]
    rows: list[str] = []
    for tm_type, marker in entries:
        rows.extend(["Telemetry Data:", _payload(tm_type, marker)])
    path = tmp_path / "latest.log"
    path.write_text("\n".join(rows), encoding="utf-8")

    hk, post, dump, critical, noncritical, _ = epu.read_pkt(path, latest_only=True)

    assert hk.marker == 9
    assert post.marker == 13
    assert dump.marker == 14
    assert critical.marker == 15
    assert noncritical.marker == 16


def test_rereading_latest_hk_does_not_publish_duplicate_or_advance_sequence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epu, "parse_eb_hk", lambda _packet: SimpleNamespace())
    hk_queue = _CaptureQueue()
    monkeypatch.setattr(epu.const, "hk_queue", hk_queue)
    monkeypatch.setattr(epu.const, "eb_post_queue", _CaptureQueue())
    monkeypatch.setattr(epu.const, "sci_queue", _CaptureQueue())
    monkeypatch.setattr(epu, "_latest_hk_packet_id", None)
    monkeypatch.setattr(epu, "_hk_sequence", 0)

    path = tmp_path / "repeated-hk.log"
    path.write_text("\n".join(["Telemetry Data:", _payload(0x1, 9)]), encoding="utf-8")

    epu.read_pkt(path, latest_only=True)
    first_sequence = epu.get_hk_sequence()
    epu.read_pkt(path, latest_only=True)

    assert first_sequence == 1
    assert epu.get_hk_sequence() == first_sequence
    assert len(hk_queue.items) == 1


def test_post_packet_identity_distinguishes_new_identical_packets_from_rereads(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_packet_decoders(monkeypatch)
    post_queue = _CaptureQueue()
    monkeypatch.setattr(epu.const, "hk_queue", _CaptureQueue())
    monkeypatch.setattr(epu.const, "eb_post_queue", post_queue)
    monkeypatch.setattr(epu.const, "sci_queue", _CaptureQueue())

    payload = _payload(0x3, 7)
    path = tmp_path / "repeated-post.log"
    path.write_text("\n".join(["Telemetry Data:", payload, "Telemetry Data:", payload]), encoding="utf-8")

    epu.read_pkt(path)
    first_post, second_post = post_queue.items
    epu.read_pkt(path, latest_only=True)
    reread_post = post_queue.items[2]

    assert first_post.RS422_PACKET_ID != second_post.RS422_PACKET_ID
    assert reread_post.RS422_PACKET_ID == second_post.RS422_PACKET_ID


def test_simple_packet_decoders_wrap_unpacked_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epu.bitstruct, "unpack_dict", lambda *_args, **_kwargs: {"FIELD": 7})

    assert epu.decode_post_hk(b"packet", struct=[("FIELD", "u8")]).FIELD == 7
    assert epu.decode_dump_data(b"packet", struct=[("FIELD", "u8")]).FIELD == 7


@pytest.mark.parametrize("decoder_name", ["decode_cscience_data", "decode_ncscience_data"])
def test_science_header_decoders_preserve_payload(
    decoder_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epu, "trim_sci_packet_by_block_length", lambda packet: packet)
    monkeypatch.setattr(epu.bitstruct, "unpack_dict", lambda *_args, **_kwargs: {"FIELD": 3})
    monkeypatch.setattr(epu.bitstruct, "calcsize", lambda _fmt: 16)
    decoder = getattr(epu, decoder_name)

    result = decoder(b"\x00\x00PAYLOAD", struct=[("FIELD", "u16")])

    assert result.FIELD == 3
    assert result.SCI_DATA == b"PAYLOAD"


def test_decode_sci_data_points_handles_missing_negative_and_short_payloads(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(epu.bitstruct, "calcsize", lambda _fmt: 16)
    monkeypatch.setattr(epu, "_sci_data_short_warning_shown", False)
    caplog.set_level("WARNING", logger="info_log")

    assert epu.decode_sci_data_points(SimpleNamespace()) == []
    assert epu.decode_sci_data_points(SimpleNamespace(SCI_DATA=-1)) == []
    assert epu.decode_sci_data_points(SimpleNamespace(SCI_DATA=b"\x01")) == []
    assert epu.decode_sci_data_points(SimpleNamespace(SCI_DATA=b"\x02")) == []
    assert sum("shorter than one point" in record.message for record in caplog.records) == 1


def test_decode_sci_data_points_decodes_points_and_ignores_remainder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epu.bitstruct, "calcsize", lambda _fmt: 16)
    monkeypatch.setattr(
        epu.bitstruct,
        "unpack_dict",
        lambda _fmt, _names, raw: {"VALUE": int.from_bytes(raw, "big")},
    )

    points = epu.decode_sci_data_points(SimpleNamespace(SCI_DATA=b"\x00\x01\x00\x02\xff"))

    assert [point.VALUE for point in points] == [1, 2]
    assert [point.POINT_INDEX for point in points] == [0, 1]


def test_decode_sci_data_points_accepts_integer_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epu.bitstruct, "calcsize", lambda _fmt: 16)
    monkeypatch.setattr(
        epu.bitstruct,
        "unpack_dict",
        lambda _fmt, _names, raw: {"VALUE": int.from_bytes(raw, "big")},
    )

    points = epu.decode_sci_data_points(SimpleNamespace(SCI_DATA=0x1234))

    assert len(points) == 1
    assert points[0].VALUE == 0x1234


def test_merge_sci_data_packet_preserves_empty_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epu, "decode_sci_data_points", lambda _param: [])
    param = SimpleNamespace(SCI_DATA=b"raw", HEADER=2)

    merged = epu.merge_sci_data_packet(param)

    assert merged.HEADER == 2
    assert merged.SCI_DATA == b"raw"
    assert merged.SCI_POINT_COUNT == 0
    assert merged.SCI_POINTS == []


def test_merge_sci_data_packet_exposes_first_point_and_all_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    points = [
        SimpleNamespace(VALUE=10, POINT_INDEX=0),
        SimpleNamespace(VALUE=20, POINT_INDEX=1),
    ]
    monkeypatch.setattr(epu, "decode_sci_data_points", lambda _param: points)
    param = SimpleNamespace(DUMP_DATA=b"dump", HEADER=3)

    merged = epu.merge_sci_data_packet(param)

    assert merged.HEADER == 3
    assert merged.VALUE == 10
    assert merged.POINT_INDEX == 0
    assert merged.SCI_POINT_COUNT == 2
    assert merged.SCI_POINTS == points
    assert merged.SCI_DATA == b"dump"


def test_trim_sci_packet_returns_short_header_unchanged() -> None:
    packet = b"short"

    assert epu.trim_sci_packet_by_block_length(packet) == packet


@pytest.mark.parametrize("adu", [4095, 5000])
def test_ob_thermistor_invalid_top_end_returns_nan(adu: int) -> None:
    assert math.isnan(epu.adu_to_temp(adu))


def test_ob_thermistor_nominal_conversion_is_finite() -> None:
    assert math.isfinite(epu.adu_to_temp(2000))


@pytest.mark.parametrize("adu", [-1, 0, 2**16])
def test_eb_thermistor_invalid_values_return_nan(adu: int) -> None:
    assert math.isnan(epu.decode_eb_trps(adu))


def test_eb_thermistor_nominal_conversion_is_finite() -> None:
    assert math.isfinite(epu.decode_eb_trps(30000))
