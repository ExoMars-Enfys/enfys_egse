import csv
import sys
import threading
import zipfile
from types import SimpleNamespace

import pytest

from tek_scope_api import ScopeConnectionError, TekScope


class FakeInstrument:
    def __init__(self):
        self.commands = []
        self.queries = []
        self.waveform_unit = "V"
        self.timeout = 10_000
        self.clear_calls = 0

    def write(self, command):
        self.commands.append(command)

    def clear(self):
        self.clear_calls += 1

    def close(self):
        pass

    def query(self, command):
        self.queries.append(command)
        if command == "TRIGger:A:EDGE:SOUrce?":
            return "CH2"
        if command == "*IDN?":
            return "TEKTRONIX,MSO44B,1234,1.0"
        if command in {"SELect:CH1?", "SELect:CH2?"}:
            return "1"
        if command in {"SELect:CH3?", "SELect:CH4?"}:
            return "0"
        if command == "*OPC?":
            return "1"
        if command == "ACQuire:STATE?":
            return "1" if self.commands.count("ACQuire:STATE ON") and not hasattr(self, "stopped") else "0"
        if command == "TRIGger:STATE?":
            return "TRIGGER"
        if command == "WFMOutpre:NR_Pt?":
            return "3"
        if command == "CURVe?":
            return "1,2,3"
        if command == "WFMOutpre:XINcr?":
            return "0.1"
        if command == "WFMOutpre:XZEro?":
            return "-0.1"
        if command == "WFMOutpre:PT_Off?":
            return "0"
        if command == "WFMOutpre:YMUlt?":
            return "2"
        if command == "WFMOutpre:YZEro?":
            return "0.5"
        if command == "WFMOutpre:YOFf?":
            return "1"
        if command == "WFMOutpre:YUNit?":
            return f'"{self.waveform_unit}"'
        raise AssertionError(f"Unexpected scope query: {command}")


def test_trigger_configuration_uses_setup_source_and_enabled_channels():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()

    setup = scope.configure_single_rising_trigger()

    assert setup == {"trigger_source": "CH2", "active_channels": ["CH1", "CH2"]}
    assert scope._trigger_unit == "A"
    assert 'CH2:PROBEFUNC:EXTUNITS "A"' in scope._instrument.commands
    assert "CH2:PROBEFUNC:EXTUNITS:STATE 1" in scope._instrument.commands
    assert "TRIGger:A:EDGE:SLOpe RISe" in scope._instrument.commands
    assert "ACQuire:STOPAfter SEQuence" in scope._instrument.commands


def test_trigger_configuration_rejects_non_channel_trigger_source():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()
    scope._instrument.query = lambda command: "LINE" if command == "TRIGger:A:EDGE:SOUrce?" else "1"

    with pytest.raises(ScopeConnectionError, match="current-probe channel"):
        scope.configure_single_rising_trigger()


def test_arm_single_sets_rising_edge_and_sequence_mode():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()

    scope.arm_single()

    assert scope._instrument.commands == [
        "ACQuire:STATE OFF",
        "ACQuire:STOPAfter SEQuence",
        "TRIGger:A:EDGE:SLOpe RISe",
        "ACQuire:STATE ON",
    ]
    assert "*OPC?" not in scope._instrument.queries


def test_recall_setup_uses_long_completion_timeout_then_restores_default():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    instrument = FakeInstrument()
    observed_timeouts = []
    original_query = instrument.query

    def query(command):
        if command == "*OPC?":
            observed_timeouts.append(instrument.timeout)
        return original_query(command)

    instrument.query = query
    scope._instrument = instrument

    scope.recall_setup("D:\\INRUSH.set")

    assert observed_timeouts == [TekScope.SETUP_RECALL_TIMEOUT_MS]
    assert instrument.timeout == 10_000


def test_recall_setup_replays_commands_from_local_tektronix_archive(tmp_path):
    setup_file = tmp_path / "INRUSH.set"
    with zipfile.ZipFile(setup_file, "w") as archive:
        archive.writestr("INRUSH_lrn.set", "*UNICODELRN;*RST\n:SELECT:CH1 1\n")
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()

    scope.recall_setup(setup_file)

    assert scope._instrument.commands == ["*UNICODELRN;*RST", ":SELECT:CH1 1"]
    assert scope._instrument.queries == ["*OPC?"]
    assert scope._instrument.timeout == 10_000


def test_repository_inrush_setup_is_replayed_as_a_local_tektronix_archive():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()

    scope.recall_setup(
        "C:\\wdir\\ob_egse\\rsrc\\Scope Config\\INRUSH.set"
    )

    assert scope._instrument.commands[0].startswith("*UNICODELRN;*RST;")
    assert scope._instrument.commands[-1] == ":PARAMBATCHING 1;"
    assert "RECall:SETUp" not in scope._instrument.commands
    assert scope._instrument.queries == ["*OPC?"]


def test_connect_clears_stale_replies_and_queries_idn_once(monkeypatch):
    instrument = FakeInstrument()
    events = []
    instrument.clear = lambda: events.append("clear")
    original_query = instrument.query

    def query(command):
        events.append(command)
        return original_query(command)

    instrument.query = query

    class FakeManager:
        def open_resource(self, _resource):
            return instrument

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "pyvisa",
        SimpleNamespace(ResourceManager=lambda _backend: FakeManager()),
    )
    scope = TekScope("TCPIP0::127.0.0.1::INSTR").connect()

    try:
        scope.recall_setup("D:\\INRUSH.set")
    finally:
        scope.close()

    assert events == ["clear", "*IDN?", "*OPC?"]


def test_scope_sessions_are_serialized(monkeypatch):
    class FakeManager:
        def open_resource(self, _resource):
            return FakeInstrument()

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "pyvisa",
        SimpleNamespace(ResourceManager=lambda _backend: FakeManager()),
    )
    first = TekScope("TCPIP0::127.0.0.1::INSTR").connect()
    second_connected = threading.Event()
    second = TekScope("TCPIP0::127.0.0.1::INSTR")
    thread = threading.Thread(target=lambda: (second.connect(), second_connected.set()))

    try:
        thread.start()
        assert not second_connected.wait(0.1)
    finally:
        first.close()
    try:
        assert second_connected.wait(2.0)
    finally:
        second.close()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_trigger_level_converts_ma_to_scope_amperes():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()
    scope._trigger_source = "CH2"
    scope._trigger_unit = "A"

    actual = scope.set_trigger_level_current_ma(90)

    assert actual == pytest.approx(0.09)
    assert "TRIGger:A:LEVel:CH2 0.09" in scope._instrument.commands
    assert "WFMOutpre:YUNit?" not in scope._instrument.queries


def test_configure_inrush_capture_sets_ch1_scale_timebase_and_trigger_without_unit_query():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()

    scope.configure_inrush_capture(0.004)
    actual_trigger = scope.set_trigger_level_current_ma(400)

    assert scope._trigger_source == "CH1"
    assert scope._trigger_unit == "A"
    assert actual_trigger == pytest.approx(0.4)
    assert "CH1:SCAle 2" in scope._instrument.commands
    assert 'CH1:PROBEFUNC:EXTUNITS "A"' in scope._instrument.commands
    assert "CH1:PROBEFUNC:EXTUNITS:STATE 1" in scope._instrument.commands
    assert "HORIZONTAL:SCALE 0.004" in scope._instrument.commands
    assert "TRIGger:A:EDGE:SOUrce CH1" in scope._instrument.commands
    assert "TRIGger:A:LEVel:CH1 0.4" in scope._instrument.commands
    assert "WFMOutpre:YUNit?" not in scope._instrument.queries


def test_configure_inrush_capture_requires_ch1_enabled():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    instrument = FakeInstrument()
    scope._instrument = instrument

    instrument.query = lambda command: (
        "0" if command == "SELect:CH1?" else FakeInstrument.query(instrument, command)
    )
    with pytest.raises(ScopeConnectionError, match="CH1 must be enabled"):
        scope.configure_inrush_capture(0.00002)


def test_mode_transition_axes_set_only_the_specified_current_scale_and_timebase():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()
    scope._instrument.waveform_unit = "A"
    scope._trigger_source = "CH2"
    scope._trigger_unit = "A"

    scope.configure_mode_transition_capture(
        timebase_s=0.001,
        current_scale_ma_per_div=50,
    )

    assert "CH2:SCAle 0.05" in scope._instrument.commands
    assert "HORIZONTAL:SCALE 0.001" in scope._instrument.commands
    assert scope._instrument.commands.count("ACQuire:STATE OFF") == 1
    assert scope._instrument.commands.count("CH1:SCAle 2") == 0


def test_mode_transition_timebase_only_preserves_recalled_vertical_scale():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()

    scope.configure_mode_transition_capture(
        timebase_s=0.0004,
        current_scale_ma_per_div=None,
    )

    assert scope._instrument.commands == [
        "ACQuire:STATE OFF",
        "HORIZONTAL:SCALE 0.0004",
    ]


def test_mode_transition_current_scale_supports_milliamps_units():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()
    scope._trigger_source = "CH2"
    scope._trigger_unit = "MA"

    scope.configure_mode_transition_capture(
        timebase_s=None,
        current_scale_ma_per_div=100,
    )

    assert "CH2:SCAle 100" in scope._instrument.commands
    assert not any(command.startswith("HORIZONTAL:SCALE") for command in scope._instrument.commands)


def test_trigger_level_rejects_unscaled_voltage_channel():
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()
    scope._trigger_source = "CH2"

    with pytest.raises(ScopeConnectionError, match="probe scaling in A or mA"):
        scope.set_trigger_level_current_ma(90)


def test_waveform_export_applies_scope_preamble_scaling(tmp_path):
    scope = TekScope("TCPIP0::127.0.0.1::INSTR")
    scope._instrument = FakeInstrument()

    output = scope.save_waveforms(tmp_path, "Safe to Standby", ["CH1"])

    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["time_s", "CH1_value_V"]
    assert [float(row[0]) for row in rows[1:]] == pytest.approx([-0.1, 0.0, 0.1])
    assert [float(row[1]) for row in rows[1:]] == pytest.approx([0.5, 2.5, 4.5])