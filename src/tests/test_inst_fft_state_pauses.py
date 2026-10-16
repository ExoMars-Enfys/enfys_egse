from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core_modules import config
from scripts_modules import INST_FFT as fft
from widget_modules import ui_runtime_controller as urc


@pytest.mark.parametrize("failed", [False, True])
def test_state_check_pauses_with_actual_readings_and_errors(monkeypatch, failed):
    pause = Mock()
    monkeypatch.setattr(urc, "request_force_pause", pause)

    def check(*, readings, errors_out):
        readings["PSU_EB_I (mA)"] = 123.0
        if failed:
            errors_out.append("Current out of range")
        return "Check result", not failed

    fft._run_state_check("State 5", check)

    pause.assert_called_once_with(
        "State 5",
        errors=["Current out of range"] if failed else [],
        readings={"PSU_EB_I (mA)": 123.0},
    )


def test_state_check_exception_is_a_failed_pause(monkeypatch):
    pause = Mock()
    monkeypatch.setattr(urc, "request_force_pause", pause)
    monkeypatch.setattr(urc, "is_aborted", lambda: False)

    def check(*, readings, errors_out):
        readings["PSU_EB_I (mA)"] = None
        raise TimeoutError("No fresh PSU data")

    fft._run_state_check("State 4", check)

    pause.assert_called_once_with("State 4", errors=["No fresh PSU data"], readings={"PSU_EB_I (mA)": None})


def test_state_check_abort_does_not_open_another_pause(monkeypatch):
    pause = Mock()
    monkeypatch.setattr(urc, "request_force_pause", pause)
    check = Mock(side_effect=urc.ScriptAbortRequested)

    with pytest.raises(urc.ScriptAbortRequested):
        fft._run_state_check("State 7", check)

    pause.assert_not_called()


def test_failed_result_without_error_output_still_pauses_as_failed(monkeypatch):
    pause = Mock()
    monkeypatch.setattr(urc, "request_force_pause", pause)

    fft._run_state_check("State 5", Mock(return_value=("Verification failed", False)))

    pause.assert_called_once_with("State 5", errors=["Verification failed"], readings={})


@pytest.mark.parametrize("standby", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_ret_report_collects_current_and_packet_results(monkeypatch, standby, failed):
    hk = SimpleNamespace(CURRENT_OPERATING_STATE=5)
    packet_result = {"passed": not failed, "details": ["Packet check failed"] if failed else []}
    psu = {"PSU_EB_I": 0.123}
    monkeypatch.setattr(urc, "pull_post_after_ret", lambda **kwargs: object())
    monkeypatch.setattr(urc, "pull_psu_after_ret", lambda **kwargs: psu)
    monkeypatch.setattr(urc, "perform_hk_check", lambda **kwargs: packet_result)
    monkeypatch.setattr(urc, "_wait_for_settled_hk", lambda *args, **kwargs: (hk, packet_result))
    monkeypatch.setattr(urc, "wait_for_fresh_psu", lambda **kwargs: psu)
    monkeypatch.setattr(urc, "get_smoothed_latest_psu", lambda _count: psu)
    monkeypatch.setattr(urc, "consumption_check", Mock(return_value=123.0))
    monkeypatch.setattr(urc, "notify_positive", Mock())
    monkeypatch.setattr(urc, "notify_negative", Mock())
    readings, errors = {}, []
    check = urc.verify_standby_ret if standby else urc.verify_safe_ret

    _, passed = check(readings=readings, errors_out=errors)

    assert passed is not failed
    assert errors == (["Packet check failed"] if failed else [])
    assert readings["PSU_EB_I (mA)"] == 123.0
    assert readings["HK check" if standby else "POST check"] is packet_result


def test_pull_post_after_ret_does_not_consume_ui_packet_queue(monkeypatch):
    post = SimpleNamespace(**{field: 0 for field in urc._POST_REQUIRED_FIELDS})
    post_queue = Queue()
    queued_post = object()
    post_queue.put(queued_post)
    monkeypatch.setattr(urc.const, "eb_post_queue", post_queue)
    monkeypatch.setattr(urc.eb_packet_utility, "get_latest_post", lambda: post)

    assert urc.pull_post_after_ret(timeout_s=0.1) is post
    assert post_queue.get_nowait() is queued_post


def test_pull_post_after_ret_can_require_fresh_packet_without_draining_ui_queue(monkeypatch):
    post = SimpleNamespace(**{field: 0 for field in urc._POST_REQUIRED_FIELDS})
    post_queue = Queue()
    queued_post = object()
    post_queue.put(queued_post)
    wait_for_fresh_post = Mock(return_value=post)
    monkeypatch.setattr(urc.const, "eb_post_queue", post_queue)
    monkeypatch.setattr(urc.eb_packet_utility, "wait_for_fresh_post", wait_for_fresh_post)
    monkeypatch.setattr(urc.eb_packet_utility, "get_post_sequence", lambda: 8)

    assert urc.pull_post_after_ret(timeout_s=0.1, after_sequence=7) is post
    wait_for_fresh_post.assert_called_once()
    assert wait_for_fresh_post.call_args.kwargs["after_sequence"] == 7
    assert 0 < wait_for_fresh_post.call_args.kwargs["timeout"] <= 0.101
    assert post_queue.get_nowait() is queued_post


def test_power_state_can_require_fresh_hk_without_consuming_gui_queue(monkeypatch):
    hk = SimpleNamespace(
        CURRENT_OPERATING_STATE=5,
        INSTR_STATUS_FLAGS=SimpleNamespace(OB_MECHANISM_BOARD_ENABLED=1, OB_DETECTOR_BOARD_ENABLED=1),
    )
    psu = {"PSU_EB_I": 0.123}
    fresh_hk = Mock(return_value=hk)
    monkeypatch.setattr(urc, "wait_for_fresh_hk", fresh_hk)
    monkeypatch.setattr(urc, "wait_for_fresh_psu", lambda **_kwargs: psu)
    monkeypatch.setattr(urc, "get_smoothed_latest_psu", lambda _count: psu)
    monkeypatch.setattr(urc, "consumption_check", lambda *_args: 123.0)
    monkeypatch.setattr(urc, "notify", Mock())
    readings, errors = {}, []

    _, passed = urc.verify_power_state(
        "State3",
        after_hk_sequence=6,
        readings=readings,
        errors_out=errors,
    )

    assert passed
    assert errors == []
    fresh_hk.assert_called_once_with(timeout=5.0, after_sequence=6)
    assert readings["CURRENT_OPERATING_STATE"] == 5


@pytest.mark.parametrize("failed", [False, True])
def test_power_state_report_uses_the_verified_samples(monkeypatch, failed):
    hk = SimpleNamespace(CURRENT_OPERATING_STATE=5, EB_TEC_DRIVE_CURRENT=10000)
    psu = {"PSU_EB_I": 0.123}
    monkeypatch.setattr(urc, "get_latest_hk", lambda: hk)
    monkeypatch.setattr(urc, "wait_for_fresh_psu", lambda **kwargs: psu)
    monkeypatch.setattr(urc, "get_smoothed_latest_psu", lambda _count: psu)
    monkeypatch.setattr(urc, "notify", Mock())

    def consumption(state, sample, errors, latest_hk):
        assert state == "State2"
        assert sample is psu
        assert latest_hk is hk
        if failed:
            errors.append("Current out of range")
        return 123.0

    monkeypatch.setattr(urc, "consumption_check", consumption)
    readings, errors = {}, []

    _, passed = urc.verify_power_state("State2", readings=readings, errors_out=errors)

    assert passed is not failed
    assert errors == (["Current out of range"] if failed else [])
    assert readings["PSU_EB_I (mA)"] == 123.0
    assert readings["CURRENT_OPERATING_STATE"] == 5
    assert readings["TEC current (A)"] == pytest.approx(0.162)


@pytest.mark.parametrize("failed", [False, True])
def test_state7_uses_windowed_psu_check_and_motion_samples(monkeypatch, failed):
    hk = SimpleNamespace(
        CURRENT_OPERATING_STATE=5,
        EB_TEC_DRIVE_CURRENT=round(config.TEC_EXPECTED_CURRENT_A / 0.0000162),
        INSTR_STATUS_FLAGS=SimpleNamespace(
            OB_MECHANISM_BOARD_ENABLED=1,
            OB_DETECTOR_BOARD_ENABLED=1,
        ),
    )
    monkeypatch.setattr(urc, "get_latest_hk", lambda: hk)
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(
        urc,
        "wait_for_fresh_psu",
        lambda **kwargs: pytest.fail("State7 should use the windowed PSU check"),
    )
    monkeypatch.setattr(
        urc,
        "consumption_check",
        lambda *_args, **_kwargs: pytest.fail("State7 should use the windowed PSU check"),
    )
    windowed_check = Mock(return_value=321.0)
    if failed:
        def fail_window(_state, window_errors, _hk, **_kwargs):
            window_errors.append("Window current out of range")
            return 321.0

        windowed_check.side_effect = fail_window
    monkeypatch.setattr(urc, "windowed_consumption_check", windowed_check)

    readings, errors = {}, []
    _, passed = urc.verify_power_state("State7", readings=readings, errors_out=errors)

    assert passed is not failed
    assert errors == (["Window current out of range"] if failed else [])
    assert readings["PSU_EB_I (mA)"] == 321.0
    windowed_check.assert_called_once_with(
        "State7",
        errors,
        hk,
        check_label="State7",
        require_motor_moving=True,
    )


def test_state7_window_uses_median_of_moving_psu_samples(monkeypatch):
    hk_packets = iter(
        (
            SimpleNamespace(MTR_FLAGS=SimpleNamespace(MOVING=0)),
            SimpleNamespace(MTR_FLAGS=SimpleNamespace(MOVING=1)),
            SimpleNamespace(MTR_FLAGS=SimpleNamespace(MOVING=1)),
        )
    )
    psu_samples = iter(
        (
            {"PSU_EB_I": 0.400},
            {"PSU_EB_I": 0.300},
            {"PSU_EB_I": 0.310},
        )
    )
    timestamps = iter((0.0, 0.1, 0.2, 0.3, 2.0))
    monkeypatch.setattr(urc, "resolve_consumption_bounds", lambda *_args: (["State7"], 300.0, 306.0))
    monkeypatch.setattr(urc, "time", SimpleNamespace(monotonic=lambda: next(timestamps)))
    monkeypatch.setattr(urc, "wait_for_fresh_psu", lambda **_kwargs: object())
    monkeypatch.setattr(urc, "get_smoothed_latest_psu", lambda _count: next(psu_samples))
    monkeypatch.setattr(urc, "get_latest_hk", lambda: next(hk_packets))

    errors = []
    median = urc.windowed_consumption_check(
        "State7",
        errors,
        duration_s=1.0,
        per_sample_timeout_s=0.1,
        min_samples=2,
        require_motor_moving=True,
        check_label="State7",
    )

    assert median == pytest.approx(305.0)
    assert errors == []


@pytest.mark.parametrize("verification", [False, True])
def test_inst_fft_pauses_for_every_state_check(monkeypatch, verification):
    pauses = []
    hk = SimpleNamespace(
        CURRENT_OPERATING_STATE=5,
        EB_TEC_DRIVE_CURRENT=0,
        TEC_SETPOINT=0xC018,
    )
    for command in (
        "ret",
        "hk_request",
        "standby",
        "set_hk_rate",
        "set_heater_configs",
        "en_mech_heater",
        "en_det_heater",
        "en_mech_board",
        "en_det_board",
        "set_motor_configs",
        "ob_homing",
        "set_tec_setpoint",
        "set_acq_configs",
        "acquisition",
        "generic_tc",
        "ob_park",
        "safe",
    ):
        monkeypatch.setattr(fft.ebtcs, command, Mock())

    def set_tec_current(_interface, _mode, current):
        hk.EB_TEC_DRIVE_CURRENT = current

    monkeypatch.setattr(fft.ebtcs, "set_tec_current", set_tec_current)
    monkeypatch.setattr(fft.eb_interface, "get_egse_interface", lambda: object())
    monkeypatch.setattr(fft, "_read_ob_trps", lambda _interface: {"MECH": 2000, "MOTOR": 2000, "DET": 2000})
    monkeypatch.setattr(fft, "_fresh_psu_sample", lambda **kwargs: {"PSU_EB_I": 0.123})
    monkeypatch.setattr(fft.ebpu, "get_latest_hk", lambda: hk)
    monkeypatch.setattr(fft.ebpu, "get_hk_sequence", lambda: 10)
    monkeypatch.setattr(fft.ebpu, "get_psu_sequence", lambda: 20)
    monkeypatch.setattr(fft.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(config, "TEC_CURRENT_SETPOINT_ADU", 10000)
    monkeypatch.setattr(config, "TEC_EXPECTED_CURRENT_A", 0.162)
    monkeypatch.setattr(config, "TEC_CURRENT_TOLERANCE_A", 0.01)
    monkeypatch.setattr(urc, "abortible_sleep", Mock())
    monkeypatch.setattr(urc, "perform_homing_check_sync", Mock())
    monkeypatch.setattr(urc, "request_repeat_acquisition", lambda _label: False)
    monkeypatch.setattr(urc, "notify_script_done", Mock())
    monkeypatch.setattr(urc, "consumption_check", lambda *_args: 123.0)
    monkeypatch.setattr(
        urc,
        "handle_script_check_failure",
        Mock(side_effect=AssertionError("State failures must use the structured pause")),
    )
    monkeypatch.setattr(
        urc,
        "request_force_pause",
        lambda label, **kwargs: pauses.append((label, kwargs)),
    )

    def verify(*args, readings, errors_out, **kwargs):
        readings["PSU_EB_I (mA)"] = 123.0
        errors_out.append("Test state failure")
        return "Test state failure", False

    for check in ("verify_safe_ret", "verify_standby_ret", "verify_power_state", "perform_acq_check_sync"):
        monkeypatch.setattr(urc, check, verify)

    fft.run_fft(verification=verification)

    reports = {label: report for label, report in pauses if "errors" in report}
    if not verification:
        assert reports == {}
        return
    assert set(reports) == {
        "State 1 - SAFE RET",
        "STANDBY RET",
        "State 2 - OB heating",
        "State 3 - OB heating and boards on",
        "State 4 - OB heating, boards on and TEC clamped",
        "State 5 - boards on and TEC clamped",
        "State 7 - all active, moving",
        "State 6 acquisition",
        "State 6 acquisition without heaters",
        "State 6 acquisition with heaters",
        "Heater config: mech ON, det OFF",
        "Heater config: mech OFF, det ON",
        "Flight heater configuration",
        "TEC ramp-up",
        "TEC ramp-down",
    }
    for report in reports.values():
        assert report["readings"]["PSU_EB_I (mA)"] == 123.0
    assert reports["State 5 - boards on and TEC clamped"]["errors"] == ["Test state failure"]
    assert reports["TEC ramp-up"]["readings"]["TEC current (A)"] == pytest.approx(0.162)
    assert reports["TEC ramp-down"]["readings"]["TEC current (A)"] == 0
