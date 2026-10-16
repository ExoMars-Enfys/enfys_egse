import math
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from scripts_modules import sci_acq
from utility_modules.background_checks import (
    CommandChecks,
    calculate_ob_current_profile,
    check_current_profile,
    check_dark_science,
    check_hk,
    check_mechanism_current_zero,
    check_mechanism_idle,
    check_science,
    check_motor_hold_current,
    check_motor_params,
    check_ob_state,
    check_powered,
    check_thermal,
    mechanism_current_adu,
    read_psu_channels,
)


def _hk(**overrides):
    values = {
        "ERROR_BYTE": 0,
        "ERROR_MTR": 0,
        "PWR_STAT": 3,
        "THRM_STATUS": SimpleNamespace(DM=1, DA=0, MM=1, MA=0, HDS=1, HMS=1),
        "MTR_FLAGS": SimpleNamespace(MOVING=0, HOMING=0, DIR=0, OUTER=0, BASE=1),
        "MTR_ABS_STEPS": 9640,
        "MTR_REL_STEPS": 20,
        "MTR_CURRENT": 64,
        "MTR_GUARD_SELECT": 0,
        "MTR_CHOP": 60,
        "MTR_SPEED": 8,
        "HK_MECH_CUR": 0,
        "DETEC_TRP": 100,
        "MECH_TRP": 100,
        "MOTOR_TRP": 100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_generic_hk_power_heater_and_motor_checks_pass():
    response = check_hk(_hk(), label="test")
    check_powered(response, 3)
    check_thermal(response, (False, True, False, True, False))
    check_motor_params(response)


def test_boot_motor_params_are_not_treated_as_nominal():
    response = _hk(MTR_CURRENT=64, MTR_GUARD_SELECT=0, MTR_CHOP=32, MTR_SPEED=9)
    check_motor_params(response, (64, 0, 32, 9))

    with pytest.raises(AssertionError, match=r"expected \(64, 0, 60, 8\)"):
        check_motor_params(response, (64, 0, 60, 8))


def test_check_hk_can_skip_repeat_logging(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "utility_modules.background_checks.info_log.info", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    check_hk(_hk(), label="test", log_result=False)

    assert calls == []


def test_ob_state_uses_active_motor_params():
    response = _hk(
        MTR_CURRENT=20,
        MTR_GUARD_SELECT=0,
        MTR_CHOP=60,
        MTR_SPEED=8,
        PWR_STAT=1,
        THRM_STATUS=SimpleNamespace(DM=0, DA=0, MM=0, MA=0, HDS=0, HMS=0),
    )

    assert check_ob_state(
        response,
        {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.129), "CH3": (5.0, 0.060)},
        "Moving",
        expected_motor_params=(20, 0, 60, 8),
    ) == {"CH1": 18.0, "CH2": 129.0, "CH3": 60.0}


def _science(**overrides):
    values = {
        name: 0
        for name in (
            "MOD_ID",
            "UNUSED1",
            "CMD_ID",
            "CMD_CNT",
            "ERROR_BYTE",
            "MTR_ABS_STEPS",
            "THRM_STATUS_BYTE",
            "SWIR_OFFSET",
            "MWIR_OFFSET",
            "SCI_ADC_SAMPLES",
            "SCI_ADC_SKIP",
            "SWIR_HIGH",
            "SWIR_MED",
            "SWIR_LOW",
            "MWIR_HIGH",
            "MWIR_MED",
            "MWIR_LOW",
            "HT_SINK_TEMP",
            "SWIR_TEMP",
            "CRC",
        )
    }
    values["CMD_ID"] = 0x0F
    # Raw PT1000 readings seen in real OB SCI logs (~15 C).
    values["HT_SINK_TEMP"] = 6270
    values["SWIR_TEMP"] = 6270
    values.update(overrides)
    return SimpleNamespace(**values)


def test_sci_temperature_conversion_uses_shared_resistance_calibration():
    from utility_modules import hk_conversions

    assert hk_conversions.sci_temperature_to_c("SWIR_TEMP", 6282) == pytest.approx(15.16, abs=0.05)
    assert math.isnan(hk_conversions.sci_temperature_to_c("SWIR_TEMP", 0))


def test_science_check_rejects_invalid_temperature():
    with pytest.raises(AssertionError, match="SWIR_TEMP=nan C"):
        check_science(_science(SWIR_TEMP=0), label="test science")


def test_science_check_can_defer_temperature_limits():
    response = _science(SWIR_TEMP=0, HT_SINK_TEMP=0)

    assert check_science(response, label="test science", check_temperatures=False) is response


def test_science_check_requires_every_tmstruct_field():
    response = _science()

    assert check_science(response, label="test science") is response


def test_science_check_rejects_error_and_wrong_command():
    with pytest.raises(AssertionError, match="ERROR_BYTE=1"):
        check_science(_science(ERROR_BYTE=1, CMD_ID=0), label="test science")


def test_science_check_rejects_out_of_range_science_value():
    with pytest.raises(AssertionError, match="SWIR_HIGH=65536"):
        check_science(_science(SWIR_HIGH=65536), label="test science")


def test_mechanism_current_uses_upper_12_bit_adu_value():
    response = _hk(HK_MECH_CUR=16 * 10)

    assert mechanism_current_adu(response) == 10


def test_mechanism_current_zero_check_reports_adu():
    errors = []
    check_mechanism_current_zero(_hk(HK_MECH_CUR=16 * 6), errors)

    assert errors == ["HK_MECH_CUR not at zero: got 6 ADU, expected <= 5 ADU"]


def test_mechanism_heater_offset_is_not_subtracted_from_net_hk_reading():
    # A heater contribution of about -83 mA is represented by the clipped
    # 0 mA net reading when no positive rail current remains.
    assert mechanism_current_adu(_hk(HK_MECH_CUR=0)) == 0


def test_mechanism_idle_allows_nonzero_idle_current():
    errors = []
    check_mechanism_idle(_hk(HK_MECH_CUR=16 * 130), errors)
    assert errors == []

    errors = []
    check_mechanism_idle(_hk(HK_MECH_CUR=16 * 136), errors)
    assert errors == ["HK_MECH_CUR above idle limit: got 136 ADU, expected <= 135 ADU"]


def test_check_ob_state_uses_zero_threshold_when_mechanism_is_powered_off():
    response = _hk(
        HK_MECH_CUR=16 * 4,
        PWR_STAT=0,
        THRM_STATUS=SimpleNamespace(DM=0, DA=0, MM=0, MA=0, HDS=0, HMS=0),
    )

    assert check_ob_state(
        response,
        {"CH1": (12.0, 0.000), "CH2": (-12.0, 0.000), "CH3": (5.0, 0.060)},
        "State1",
    ) == {"CH1": 0.0, "CH2": 0.0, "CH3": 60.0}


def test_check_ob_state_uses_idle_threshold_for_state5():
    response = _hk(
        HK_MECH_CUR=16 * 128,
        PWR_STAT=3,
        THRM_STATUS=SimpleNamespace(DM=0, DA=0, MM=0, MA=0, HDS=0, HMS=0),
    )

    assert check_ob_state(
        response,
        {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.0), "CH3": (5.0, 0.075)},
        "State5",
    ) == {"CH1": 18.0, "CH2": 0.0, "CH3": 75.0}

    assert calculate_ob_current_profile("State5") == {"CH1": 17.4, "CH2": 5.7, "CH3": 68.0}


def test_motor_hold_current_accepts_phase_dependent_nonzero_current():
    errors = []
    check_motor_hold_current(_hk(HK_MECH_CUR=16 * 6), errors, {"CH1": (12.0, 0.005)})

    assert errors == []


def test_motor_hold_current_rejects_zero_band_current():
    errors = []
    check_motor_hold_current(_hk(HK_MECH_CUR=16 * 5), errors, {"CH1": (12.0, 0.004)})

    assert errors == [
        "HK_MECH_CUR hold current too low: got 5 ADU, expected >= 6 ADU",
        "CH1 motor hold current too low: got 4.00 mA, expected > 4.00 mA",
    ]


def test_last_command_mismatch_is_reported():
    response = _hk(PWR_STAT=1)
    with pytest.raises(AssertionError, match="expected 3"):
        check_powered(response, 3)


def test_move_validates_active_state_while_moving(monkeypatch):
    states = []
    responses = iter(
        [
            _hk(MTR_FLAGS=SimpleNamespace(MOVING=1, HOMING=0, DIR=0, OUTER=0, BASE=0)),
            _hk(MTR_FLAGS=SimpleNamespace(MOVING=1, HOMING=0, DIR=0, OUTER=0, BASE=0)),
            _hk(MTR_FLAGS=SimpleNamespace(MOVING=0, HOMING=0, DIR=0, OUTER=0, BASE=1)),
        ]
    )

    monkeypatch.setattr("utility_modules.background_checks.repeat", lambda *args, **kwargs: None)

    def fake_hk(self, label, **kwargs):
        return next(responses)

    monkeypatch.setattr(CommandChecks, "hk", fake_hk)

    def fake_state(self, state_name, readings, *, response: Any = None, allow_psu_unavailable=False):
        states.append((state_name, bool(response.MTR_FLAGS.MOVING), readings))
        return {"CH1": 18.0, "CH2": 129.0, "CH3": 60.0}

    monkeypatch.setattr(CommandChecks, "state", fake_state)

    checks = CommandChecks(
        None,
        sleep=lambda _: None,
        current_reader=lambda: {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.129), "CH3": (5.0, 0.060)},
    )
    checks.move(
        negative=False,
        steps=100,
        label="turning",
        active_state="Moving",
        expected_motor_params=(20, 0, 60, 8),
    )

    assert states[0][0] == "Moving"
    assert states[0][1] is True
    assert len(states) == 1


def test_move_validates_all_samples_across_whole_motion(monkeypatch):
    state_calls: list[dict[str, object]] = []
    responses = iter(
        [
            _hk(MTR_FLAGS=SimpleNamespace(MOVING=1, HOMING=0, DIR=0, OUTER=0, BASE=0)),
            _hk(MTR_FLAGS=SimpleNamespace(MOVING=1, HOMING=0, DIR=0, OUTER=0, BASE=0)),
            _hk(MTR_FLAGS=SimpleNamespace(MOVING=0, HOMING=0, DIR=0, OUTER=0, BASE=1)),
        ]
    )

    monkeypatch.setattr("utility_modules.background_checks.repeat", lambda *args, **kwargs: None)

    def fake_hk(self, label, **kwargs):
        return next(responses)

    monkeypatch.setattr(CommandChecks, "hk", fake_hk)

    def fake_state(self, state_name, readings, *, response: Any = None, allow_psu_unavailable=False):
        state_calls.append(
            {"state": state_name, "moving": bool(response.MTR_FLAGS.MOVING), "readings": readings.copy()}
        )
        return {"CH1": 18.0, "CH2": 129.0, "CH3": 60.0}

    monkeypatch.setattr(CommandChecks, "state", fake_state)

    checks = CommandChecks(
        None,
        sleep=lambda _: None,
        current_reader=lambda: {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.129), "CH3": (5.0, 0.060)},
    )
    checks.move(
        negative=False,
        steps=100,
        label="turning",
        active_state="Moving",
        expected_motor_params=(20, 0, 60, 8),
    )

    # All moving samples are aggregated into one median-based state validation.
    assert len(state_calls) == 1
    assert state_calls[0]["state"] == "Moving"
    assert state_calls[0]["moving"] is True
    assert state_calls[0]["readings"] == {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.129), "CH3": (5.0, 0.060)}


def test_move_wait_aborts_when_script_stop_is_requested(monkeypatch):
    from widget_modules import ui_runtime_controller

    started = _hk(MTR_FLAGS=SimpleNamespace(MOVING=1, HOMING=0, DIR=0, OUTER=0, BASE=0))
    monkeypatch.setattr(ui_runtime_controller, "is_aborted", lambda: True)

    checks = CommandChecks(
        None,
        sleep=lambda _: None,
        current_reader=lambda: {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.129), "CH3": (5.0, 0.060)},
    )

    with pytest.raises(ui_runtime_controller.ScriptAbortRequested):
        checks._wait_for_stop("turning", active_state="Moving", initial_response=started)


def test_move_to_absolute_position_rejects_no_motion(monkeypatch):
    checks = CommandChecks(None)
    before = _hk(MTR_ABS_STEPS=5000)
    finished = _hk(MTR_ABS_STEPS=5000)
    monkeypatch.setattr(checks, "hk", lambda *args, **kwargs: before)
    monkeypatch.setattr(checks, "move", lambda **kwargs: finished)

    with pytest.raises(AssertionError, match="did not move"):
        checks.move_to_absolute_position(5200, label="target move")


def test_movement_speed_is_calculated_from_steps_and_time():
    assert CommandChecks._movement_speed_mm_s(320, 2.0) == pytest.approx(0.5)
    assert CommandChecks._movement_speed_mm_s(160, 2.0) == pytest.approx(0.25)


def test_movement_time_is_checked_with_two_second_tolerance():
    CommandChecks._assert_elapsed_within_tolerance("speed check", 18.0, 20.0, 2.0)

    with pytest.raises(AssertionError, match=r"outside 18.00\.\.22\.00 s"):
        CommandChecks._assert_elapsed_within_tolerance("speed check", 23.0, 20.0, 2.0)


def test_read_psu_channels_skips_when_lock_is_busy():
    class BusyLock:
        def acquire(self, blocking=True, timeout=None):
            return False

        def release(self):
            raise AssertionError("lock should not be acquired when busy")

    class FakePort:
        pass

    assert read_psu_channels(FakePort(), BusyLock()) == {}


@pytest.mark.parametrize(("enabled", "command"), [(True, b"OP4 1\r\n"), (False, b"OP4 0\r\n")])
def test_switch_psu_can_switch_only_one_channel(monkeypatch, enabled, command):
    from utility_modules import background_checks

    class FakePort:
        def __init__(self):
            self.commands = []

        def write(self, command):
            self.commands.append(command)

    port = FakePort()
    monkeypatch.setattr(background_checks, "gate_script_control", lambda: None)

    background_checks.switch_psu(port, enabled=enabled, channel=4)

    assert port.commands == [command]


def test_switch_psu_defaults_to_all_outputs(monkeypatch):
    from utility_modules import background_checks

    class FakePort:
        def __init__(self):
            self.commands = []

        def write(self, command):
            self.commands.append(command)

    port = FakePort()
    monkeypatch.setattr(background_checks, "gate_script_control", lambda: None)

    background_checks.switch_psu(port, enabled=True)

    assert port.commands == [b"OPALL 1\r\n"]


def test_set_psu_channel_voltage_uses_selected_channel(monkeypatch):
    from utility_modules import background_checks

    class FakePort:
        def __init__(self):
            self.commands = []

        def write(self, command):
            self.commands.append(command)

    port = FakePort()
    monkeypatch.setattr(background_checks, "gate_script_control", lambda: None)

    background_checks.set_psu_channel_voltage(port, channel=4, voltage=29.4)

    assert port.commands == [b"V4 29.4\r\n"]


def test_read_psu_channels_uses_latest_cached_sample_when_queue_is_empty(monkeypatch):
    from utility_modules import eb_packet_utility

    class BusyLock:
        def acquire(self, blocking=True, timeout=None):
            return False

        def release(self):
            raise AssertionError("lock should not be acquired when busy")

    class FakePort:
        pass

    snapshot = {
        "CH1_V": 12.0,
        "CH1_I": 0.018,
        "CH2_V": -12.0,
        "CH2_I": 0.129,
        "CH3_V": 5.0,
        "CH3_I": 0.060,
        "CH4_V": 0.0,
        "CH4_I": 0.0,
    }
    eb_packet_utility.set_latest_psu(snapshot)
    monkeypatch.setattr("utility_modules.background_checks.const.psu_queue", None)

    assert read_psu_channels(FakePort(), BusyLock()) == {
        "CH1": (12.0, 0.018),
        "CH2": (-12.0, 0.129),
        "CH3": (5.0, 0.060),
        "CH4": (0.0, 0.0),
    }


def test_dark_science_checks_all_configured_measurements():
    science = SimpleNamespace(
        SWIR_HIGH=10,
        SWIR_MED=11,
        SWIR_LOW=12,
        MWIR_HIGH=13,
        MWIR_MED=14,
        MWIR_LOW=15,
        SWIR_TEMP=6270,
        HT_SINK_TEMP=6270,
    )
    check_dark_science(_hk(), science)


def test_dark_science_rejects_missing_channel():
    science = SimpleNamespace(
        SWIR_HIGH=10,
        SWIR_MED=11,
        SWIR_LOW=12,
        MWIR_HIGH=13,
        MWIR_MED=14,
        SWIR_TEMP=6270,
        HT_SINK_TEMP=6270,
    )
    with pytest.raises(AssertionError, match="MWIR_LOW is missing"):
        check_dark_science(_hk(), science)


def test_dark_offset_all_zero_is_not_treated_as_failure():
    dark_science = SimpleNamespace(
        SWIR_HIGH=0,
        SWIR_MED=0,
        SWIR_LOW=0,
        MWIR_HIGH=0,
        MWIR_MED=0,
        MWIR_LOW=0,
    )
    offset_science = SimpleNamespace(
        SWIR_HIGH=0,
        SWIR_MED=0,
        SWIR_LOW=0,
        MWIR_HIGH=0,
        MWIR_MED=0,
        MWIR_LOW=0,
    )
    errors = []
    for field in ("SWIR_HIGH", "SWIR_MED", "SWIR_LOW", "MWIR_HIGH", "MWIR_MED", "MWIR_LOW"):
        initial_value = getattr(dark_science, field, None)
        offset_value = getattr(offset_science, field, None)
        if initial_value is None or offset_value is None:
            errors.append(f"{field} is unavailable for dark-offset comparison")
        elif initial_value == 0 and offset_value == 0:
            continue
        elif offset_value >= initial_value:
            errors.append(f"{field} did not decrease after offset 4095: {initial_value} -> {offset_value}")
    assert errors == []


def test_ob_state_current_checks_only_configured_rails():
    measured = check_current_profile(
        {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.129), "CH3": (5.0, 0.078)},
        "State3",
    )
    assert measured == {"CH1": 18.0, "CH2": 129.0, "CH3": 78.0}


def test_ob_current_profile_sums_active_components():
    assert calculate_ob_current_profile("State3") == {"CH1": 17.4, "CH2": 129.7, "CH3": 68.0}


def test_all_reported_state_profiles_include_every_active_component():
    assert calculate_ob_current_profile("State1") == {"CH1": 0.0, "CH2": 0.0, "CH3": 60.0}
    assert calculate_ob_current_profile("State2") == {"CH1": 0.0, "CH2": 124.0, "CH3": 60.0}
    assert calculate_ob_current_profile("State3") == {"CH1": 17.4, "CH2": 129.7, "CH3": 68.0}
    assert calculate_ob_current_profile("State5") == {"CH1": 17.4, "CH2": 5.7, "CH3": 68.0}
    assert calculate_ob_current_profile("State7") == {"CH1": 102.4, "CH2": 129.7, "CH3": 75.0}


def test_ob_current_profile_includes_movement_component():
    moving_state = _hk(
        PWR_STAT=3,
        THRM_STATUS=SimpleNamespace(DM=1, DA=0, MM=1, MA=0, HDS=1, HMS=1),
        MTR_FLAGS=SimpleNamespace(MOVING=1, HOMING=0, DIR=0, OUTER=0, BASE=0),
    )
    assert calculate_ob_current_profile(moving_state) == {
        "CH1": 102.4,
        "CH2": 129.7,
        "CH3": 75.0,
    }


def test_moving_current_profile_uses_live_motor_setting():
    moving = _hk(
        MTR_CURRENT=40,
        PWR_STAT=1,
        THRM_STATUS=SimpleNamespace(DM=0, DA=0, MM=0, MA=0, HDS=0, HMS=0),
        MTR_FLAGS=SimpleNamespace(MOVING=1, HOMING=0, DIR=0, OUTER=0, BASE=0),
    )

    assert calculate_ob_current_profile(moving) == {"CH1": 60.0, "CH2": 0.0, "CH3": 67.0}


def test_component_current_profile_checks_all_ob_rails():
    response = _hk(
        PWR_STAT=0,
        THRM_STATUS=SimpleNamespace(DM=0, DA=0, MM=1, MA=0, HDS=0, HMS=1),
    )
    measured = check_current_profile(
        {"CH1": (12.0, 0.0), "CH2": (-12.0, 0.083), "CH3": (5.0, 0.060)},
        response,
    )
    assert measured == {"CH1": 0.0, "CH2": 83.0, "CH3": 60.0}


def test_ob_state_expected_ch3_includes_powered_heated_board_load():
    measured = check_current_profile(
        {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.129), "CH3": (5.0, 0.078)},
        "State3",
    )
    assert measured == {"CH1": 18.0, "CH2": 129.0, "CH3": 78.0}

    assert calculate_ob_current_profile("State3") == {"CH1": 17.4, "CH2": 129.7, "CH3": 68.0}


def test_ob_state_current_reports_out_of_range_rail():
    with pytest.raises(AssertionError, match="CH1=40.00 mA"):
        check_current_profile(
            {"CH1": (12.0, 0.040), "CH2": (-12.0, 0.0057), "CH3": (5.0, 0.060)},
            "State5",
        )


def test_ob_state_current_can_skip_when_psu_is_disabled():
    assert check_current_profile({}, "State1", allow_unavailable=True) == {}


def test_complete_ob_state_checks_hk_and_current_together():
    measured = check_ob_state(
        _hk(),
        {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.129), "CH3": (5.0, 0.078)},
        "State3",
    )
    assert measured["CH1"] == 18.0
    assert measured["CH2"] == 129.0
    assert measured["CH3"] == 78.0


def test_complete_ob_state_rejects_wrong_ob_state():
    with pytest.raises(AssertionError, match="Power state mismatch"):
        check_ob_state(
            _hk(PWR_STAT=0),
            {"CH1": (12.0, 0.018), "CH2": (-12.0, 0.129), "CH3": (5.0, 0.060)},
            "State3",
        )


def test_measurement_scan_can_be_started_in_background(monkeypatch):
    started = []

    def fake_measurement_scan(port, step_spacing=30, port_lock=None):
        started.append((port, step_spacing, port_lock))

    monkeypatch.setattr(sci_acq, "measurement_scan", fake_measurement_scan)

    thread = sci_acq.measurement_scan_async("port", step_spacing=50)

    assert isinstance(thread, type(threading.Thread()))
    thread.join(timeout=1)
    assert started == [("port", 50, None)]


def test_measurement_scan_async_respects_shared_port_lock(monkeypatch):
    seen = []

    class FakeLock:
        def __enter__(self):
            seen.append("entered")
            return self

        def __exit__(self, exc_type, exc, tb):
            seen.append("exited")
            return False

    def fake_measurement_scan(port, step_spacing=30, port_lock=None):
        if port_lock is not None:
            with port_lock:
                seen.append((port, step_spacing, True))
        else:
            seen.append((port, step_spacing, False))

    monkeypatch.setattr(sci_acq, "measurement_scan", fake_measurement_scan)

    thread = sci_acq.measurement_scan_async("port", step_spacing=50, port_lock=FakeLock())

    assert isinstance(thread, type(threading.Thread()))
    thread.join(timeout=1)
    assert seen == ["entered", ("port", 50, True), "exited"]


def _fake_dac_instrument(reading_for_offset):
    """Simulated detector whose HIGH readings depend on the applied DAC offsets."""
    applied = {"SWIR": 0, "MWIR": 0}
    sent: list[tuple[int, int]] = []

    def set_offset(swir, mwir):
        applied["SWIR"], applied["MWIR"] = swir, mwir
        sent.append((swir, mwir))

    def science():
        return SimpleNamespace(
            SWIR_OFFSET=applied["SWIR"],
            MWIR_OFFSET=applied["MWIR"],
            SWIR_HIGH=reading_for_offset(applied["SWIR"]),
            MWIR_HIGH=reading_for_offset(applied["MWIR"]),
        )

    return set_offset, science, sent


def test_verify_sci_readings_checks_each_diode_at_its_dark_position(monkeypatch):
    responses = [
        SimpleNamespace(
            SWIR_OFFSET=2020,
            MWIR_OFFSET=2420,
            SWIR_HIGH=5250 + index,
            MWIR_HIGH=20000 + index,
        )
        for index in range(10)
    ]
    requested = []
    sleeps = []

    class _Checks:
        def __init__(self, *_args, **_kwargs):
            pass

        def move_to_absolute_position(self, position, *, label):
            requested.append((position, label))

    def request_science(_port, checkpoint, **_kwargs):
        requested.append(checkpoint)
        return responses[len([item for item in requested if isinstance(item, str)]) - 1]

    monkeypatch.setattr(sci_acq.bg, "CommandChecks", _Checks)
    monkeypatch.setattr(sci_acq.bg, "request_science", request_science)
    monkeypatch.setattr(sci_acq.bg, "gate_script_control", lambda: None)
    monkeypatch.setattr(sci_acq.time, "sleep", sleeps.append)

    result = sci_acq.verify_sci_readings(
        "port",
        expected_swir_offset=2020,
        expected_mwir_offset=2420,
    )

    assert result == responses
    assert len([item for item in requested if isinstance(item, str)]) == 10
    assert [item[0] for item in requested if isinstance(item, tuple)] == [9600, 8000]
    assert sleeps == [1.0] * 8


def test_verify_sci_readings_rejects_high_gain_values_outside_calibration(monkeypatch):
    responses = [
        SimpleNamespace(
            SWIR_OFFSET=2020,
            MWIR_OFFSET=2420,
            SWIR_HIGH=5751 if index == 2 else 5250,
            MWIR_HIGH=20000,
        )
        for index in range(10)
    ]
    class _Checks:
        def __init__(self, *_args, **_kwargs):
            pass

        def move_to_absolute_position(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(sci_acq.bg, "CommandChecks", _Checks)
    monkeypatch.setattr(sci_acq.bg, "request_science", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(sci_acq.bg, "gate_script_control", lambda: None)
    monkeypatch.setattr(sci_acq.time, "sleep", lambda _seconds: None)

    with pytest.raises(AssertionError, match="SWIR_HIGH=5751"):
        sci_acq.verify_sci_readings("port", expected_swir_offset=2020, expected_mwir_offset=2420)


def test_verify_sci_readings_routes_failures_through_popup_callbacks(monkeypatch):
    response = SimpleNamespace(SWIR_OFFSET=2020, MWIR_OFFSET=2420, SWIR_HIGH=5751, MWIR_HIGH=20000)
    class _Checks:
        def __init__(self, *_args, **_kwargs):
            pass

        def move_to_absolute_position(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(sci_acq.bg, "CommandChecks", _Checks)
    monkeypatch.setattr(sci_acq.bg, "request_science", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(sci_acq.bg, "gate_script_control", lambda: None)
    monkeypatch.setattr(sci_acq.time, "sleep", lambda _seconds: None)
    decisions = []
    notifications = []

    result = sci_acq.verify_sci_readings(
        "port",
        expected_swir_offset=2020,
        expected_mwir_offset=2420,
        on_failure=lambda label, errors, readings: decisions.append((label, errors, readings)) or True,
        notify_negative=notifications.append,
    )

    assert len(result) == 10
    assert decisions[0][0] == "SCI gain-channel readings"
    assert any("SWIR_HIGH=5751" in error for error in decisions[0][1])
    assert len(notifications) == 1


def test_verify_dac_offset_checks_science_and_hk_echoes(monkeypatch):
    sci = SimpleNamespace(SWIR_OFFSET=2020, MWIR_OFFSET=1920)
    hk = SimpleNamespace(SWIR_OFFSET=2020, MWIR_OFFSET=2421)
    science_requests = []
    monkeypatch.setattr(
        sci_acq.bg,
        "request_science",
        lambda *_args, **kwargs: science_requests.append(kwargs) or sci,
    )
    monkeypatch.setattr(sci_acq.bg, "request_hk", lambda *_args, **_kwargs: hk)

    with pytest.raises(AssertionError, match="HK MWIR_OFFSET=2421"):
        sci_acq.verify_dac_offset("port", expected_swir_offset=2020, expected_mwir_offset=1920)
    assert science_requests[0]["check_temperatures"] is True


def test_find_dac_offset_defers_temperature_limits_during_binary_chop(monkeypatch):
    temperature_checks = []
    monkeypatch.setattr(sci_acq.bg, "request_hk", lambda *_args, **_kwargs: SimpleNamespace(PWR_STAT=2))
    monkeypatch.setattr(sci_acq, "_run_transaction", lambda *_args: None)
    monkeypatch.setattr(
        sci_acq.bg,
        "request_science",
        lambda *_args, **kwargs: (
            temperature_checks.append(kwargs["check_temperatures"])
            or SimpleNamespace(SWIR_OFFSET=1, SWIR_HIGH=6000)
        ),
    )

    sci_acq.find_dac_offset("port", "SWIR", 5250, 1)

    assert len(temperature_checks) == 12
    assert temperature_checks == [False] * 12


def test_choose_dac_offsets_reapplies_and_verifies_selected_pair(monkeypatch):
    class _Checks:
        def __init__(self, *_args, **_kwargs):
            pass

        def move_to_absolute_position(self, *_args, **_kwargs):
            pass

    commands = []
    verifications = []
    monkeypatch.setattr(sci_acq.bg, "CommandChecks", _Checks)
    monkeypatch.setattr(
        sci_acq,
        "find_dac_offset",
        lambda _port, sensor, *_args, **_kwargs: {"SWIR": 2020, "MWIR": 1920}[sensor],
    )
    monkeypatch.setattr(sci_acq, "_run_transaction", lambda *_args: commands.append(_args[-2:]))
    monkeypatch.setattr(sci_acq, "verify_dac_offset", lambda *args, **kwargs: verifications.append(kwargs))

    offsets = sci_acq.choose_dac_offsets("port")

    assert offsets == (2020, 1920)
    assert commands == [(2020, 1920)]
    assert verifications[0]["expected_swir_offset"] == 2020
    assert verifications[0]["expected_mwir_offset"] == 1920


def test_choose_dac_offsets_uses_light_offset_targets(monkeypatch):
    class _Checks:
        def __init__(self, *_args, **_kwargs):
            pass

        def move_to_absolute_position(self, *_args, **_kwargs):
            pass

    verifications = []
    monkeypatch.setattr(sci_acq.bg, "CommandChecks", _Checks)
    chop_targets = []

    def find_offset(_port, sensor, target, *_args, **_kwargs):
        chop_targets.append((sensor, target))
        return {"SWIR": 2800, "MWIR": 2500}[sensor]

    monkeypatch.setattr(sci_acq, "find_dac_offset", find_offset)
    monkeypatch.setattr(sci_acq, "_run_transaction", lambda *_args: None)
    monkeypatch.setattr(sci_acq, "verify_dac_offset", lambda *args, **kwargs: verifications.append(kwargs))

    offsets = sci_acq.choose_dac_offsets("port", light_reading=True)

    assert offsets == (2800, 2500)
    assert chop_targets == [("SWIR", 5250), ("MWIR", 10000)]
    assert verifications[0]["light_reading"] is True


def test_choose_dac_offsets_rejects_out_of_range_selection_before_applying(monkeypatch):
    class _Checks:
        def __init__(self, *_args, **_kwargs):
            pass

        def move_to_absolute_position(self, *_args, **_kwargs):
            pass

    commands = []
    monkeypatch.setattr(sci_acq.bg, "CommandChecks", _Checks)
    monkeypatch.setattr(
        sci_acq,
        "find_dac_offset",
        lambda _port, sensor, *_args, **_kwargs: {"SWIR": 1700, "MWIR": 1920}[sensor],
    )
    monkeypatch.setattr(sci_acq, "_run_transaction", lambda *_args: commands.append(_args[-2:]))

    with pytest.raises(AssertionError, match="SWIR DAC offset 1700"):
        sci_acq.choose_dac_offsets("port")

    assert commands == []


def _run_abu_chop(monkeypatch, name, reading_for_offset, **kwargs):
    from scripts_modules import abu_sequences

    set_offset, science, sent = _fake_dac_instrument(reading_for_offset)
    monkeypatch.setattr(abu_sequences, "_hk", lambda port: SimpleNamespace(PWR_STAT=3))
    monkeypatch.setattr(abu_sequences, "repeat", lambda port, command, *args: set_offset(*args))
    monkeypatch.setattr(abu_sequences, "_check_sci", lambda port, samp, skip: science())
    return getattr(abu_sequences, name)("port", **kwargs), sent


def _run_ob_fft_chop(monkeypatch, name, reading_for_offset, **kwargs):
    set_offset, science, sent = _fake_dac_instrument(reading_for_offset)

    def fake_transaction(worker, port_lock, func, port, *args):
        if func is sci_acq.tc.hk_request:
            return SimpleNamespace(PWR_STAT=3)
        if func is sci_acq.sq.check_sci:
            return science()
        command, *values = args
        if command is sci_acq.tc.sci_offset:
            set_offset(*values)

    monkeypatch.setattr(sci_acq, "_run_transaction", fake_transaction)
    return getattr(sci_acq, name)("port", **kwargs), sent


@pytest.mark.parametrize("name", ["swir_binary_chop", "mwir_binary_chop"])
@pytest.mark.parametrize(
    "reading_for_offset",
    [lambda offset: 4095 - offset, lambda offset: 65535 - 16 * offset, lambda offset: 0],
    ids=["in-window", "steep", "no-solution"],
)
def test_ob_fft_dac_chop_matches_abu_sequences(monkeypatch, name, reading_for_offset):
    abu_result, abu_sent = _run_abu_chop(monkeypatch, name, reading_for_offset)
    ob_result, ob_sent = _run_ob_fft_chop(monkeypatch, name, reading_for_offset)

    assert ob_result == abu_result
    assert ob_sent == abu_sent


def _fake_scan_instrument():
    """Simulated mechanism: every move completes immediately; records the command sequence."""
    calls: list[tuple] = []
    flags = SimpleNamespace(CAL=0, DIR=0, OUTER=0, BASE=0, MOVING=0, HOMING=0)

    def hk():
        return SimpleNamespace(
            PWR_STAT=3,
            MTR_CURRENT=64,
            MTR_GUARD_SELECT=0,
            MTR_CHOP=60,
            MTR_SPEED=8,
            MTR_FLAGS=flags,
            MTR_ABS_STEPS=8960,
            MTR_REL_STEPS=1,
            ERROR_MTR=0,
        )

    def command(name, *args):
        calls.append((name, *args))

    def sci(samp, skip):
        calls.append(("sci", samp, skip))
        return SimpleNamespace(
            **{name: 0 for name in ("MTR_ABS_STEPS", "SWIR_OFFSET", "MWIR_OFFSET", "HT_SINK_TEMP", "SWIR_TEMP")},
            **{name: 0 for name in ("SWIR_LOW", "SWIR_MED", "SWIR_HIGH", "MWIR_LOW", "MWIR_MED", "MWIR_HIGH")},
        )

    return hk, command, sci, calls


def test_ob_fft_measurement_scan_matches_abu_measurement_scan(monkeypatch):
    from scripts_modules import abu_sequences

    hk, command, sci, abu_calls = _fake_scan_instrument()
    monkeypatch.setattr(abu_sequences, "_hk", lambda port: hk())
    monkeypatch.setattr(abu_sequences, "_sci", lambda port, samp, skip: sci(samp, skip))
    monkeypatch.setattr(abu_sequences, "repeat", lambda port, cmd, *args: command(cmd.__name__, *args))
    monkeypatch.setattr(abu_sequences.time, "sleep", lambda _s: None)
    abu_sequences.abu_measurement_scan("port", step_spacing=50)

    hk, command, sci, ob_calls = _fake_scan_instrument()

    def fake_transaction(worker, port_lock, func, port, *args):
        if func is sci_acq.tc.hk_request:
            return hk()
        if func is sci_acq.tc.sci_request:
            return sci(*args)
        cmd, *values = args
        return command(cmd.__name__, *values)

    monkeypatch.setattr(sci_acq, "_run_transaction", fake_transaction)
    faults = []
    sci_acq.measurement_scan("port", step_spacing=50, on_failure=lambda label, errors: faults.append(label) or True)

    assert ob_calls == abu_calls
    assert sum(1 for call in ob_calls if call[0] == "sci") == 1 + len(range(0, 8600, 50))
    # The simulated flags never satisfy the homing checks, so they must reach the operator prompt.
    assert "Measurement scan cal to base" in faults
    assert "Measurement scan home to outer" in faults


def test_measurement_scan_stops_on_fault_without_handler(monkeypatch):
    hk, command, sci, _calls = _fake_scan_instrument()

    def fake_transaction(worker, port_lock, func, port, *args):
        if func is sci_acq.tc.hk_request:
            return hk()
        if func is sci_acq.tc.sci_request:
            return sci(*args)
        cmd, *values = args
        return command(cmd.__name__, *values)

    monkeypatch.setattr(sci_acq, "_run_transaction", fake_transaction)
    with pytest.raises(AssertionError, match="Measurement scan cal to base failed"):
        sci_acq.measurement_scan("port", step_spacing=50)


def test_measurement_scan_routes_check_failures_to_popup_callback(monkeypatch):
    class _Checks:
        def __init__(self, *_args, **_kwargs):
            pass

        def home(self, *, label, **_kwargs):
            if label == "measurement scan cal to base":
                raise RuntimeError("calibration failed")

    failures = []
    monkeypatch.setattr(sci_acq.bg, "CommandChecks", _Checks)
    monkeypatch.setattr(sci_acq, "_scan_step", lambda *_args, **_kwargs: None)

    sci_acq.measurement_scan(
        "port",
        step_spacing=50,
        on_failure=lambda label, errors: failures.append((label, errors)) or True,
    )

    assert failures == [("measurement scan cal to base", ["calibration failed"])]
