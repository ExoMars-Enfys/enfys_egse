from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts_modules import INADVERTENT_SWITCH_OFF as switch_off
from widget_modules import ui_runtime_controller as urc


def test_boot_to_safe_sends_ret_and_hk_immediately_after_psu_on(monkeypatch):
    events = []
    switch = Mock(side_effect=lambda *args, **kwargs: events.append("psu-on"))
    ret = Mock(side_effect=lambda *_args: events.append("ret"))
    hk_request = Mock(side_effect=lambda *_args: events.append("hk-request"))
    monkeypatch.setattr(switch_off, "switch_psu", switch)
    monkeypatch.setattr(switch_off.ebtcs, "ret", ret)
    monkeypatch.setattr(switch_off.ebtcs, "hk_request", hk_request)
    monkeypatch.setattr(
        urc,
        "notify",
        Mock(side_effect=lambda *_args, **_kwargs: events.append("notify")),
    )
    monkeypatch.setattr(
        urc,
        "abortible_sleep_with_progress",
        Mock(side_effect=lambda *_args, **_kwargs: events.append("wait")),
    )
    monkeypatch.setattr(
        urc,
        "request_force_pause",
        Mock(side_effect=lambda *_args, **_kwargs: events.append("pause")),
    )
    monkeypatch.setattr(switch_off.ebpu, "get_post_sequence", Mock(return_value=12))
    verify_safe_ret = Mock(return_value=("SAFE RET passed", True))
    monkeypatch.setattr(urc, "verify_safe_ret", verify_safe_ret)
    psu_port = object()
    interface = object()

    switch_off._boot_to_safe(interface, psu_port, None, "Power cycle")

    switch.assert_called_once_with(psu_port, enabled=True, psu_lock=None, channel=4)
    ret.assert_called_once_with(interface, 0, 0, 0, 0, 0, 0)
    hk_request.assert_called_once_with(interface, 0)
    assert events[:4] == ["psu-on", "ret", "hk-request", "notify"]
    assert events[4:6] == ["wait", "wait"]
    assert verify_safe_ret.call_count == 1
    assert verify_safe_ret.call_args.kwargs["after_post_sequence"] == 12


@pytest.mark.parametrize(
    ("sample", "should_fail"),
    [
        ({"CH1_I": 0.0, "CH2_I": 0.002, "CH3_I": 0.0, "CH4_I": 0.0}, False),
        ({"CH1_I": 0.0, "CH2_I": 0.02, "CH3_I": 0.0, "CH4_I": 0.0}, True),
    ],
)
def test_psu_off_requires_fresh_zero_current_sample(monkeypatch, sample, should_fail):
    pause = Mock()
    switch = Mock()
    clock = {"now": 0.0, "sequence": 12}
    monkeypatch.setattr(switch_off.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(switch_off.ebpu, "get_psu_sequence", lambda: clock["sequence"])

    def wait_for_psu(**_kwargs):
        clock["now"] += 1.0
        clock["sequence"] += 1
        return sample

    monkeypatch.setattr(switch_off.ebpu, "wait_for_fresh_psu", wait_for_psu)
    monkeypatch.setattr(switch_off, "switch_psu", switch)
    monkeypatch.setattr(urc, "is_aborted", lambda: False)
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(urc, "request_force_pause", pause)
    check_failure = Mock(return_value=False)
    monkeypatch.setattr(urc, "handle_script_check_failure", check_failure)
    psu_port = object()

    if should_fail:
        with pytest.raises(AssertionError, match="expected 0 A"):
            switch_off._verify_psu_off(psu_port, None, "PSU off")
        check_failure.assert_called_once()
        assert "PSU current did not settle" in check_failure.call_args.args[1][0]
        pause.assert_not_called()
    else:
        switch_off._verify_psu_off(psu_port, None, "PSU off")
        assert pause.call_args.kwargs["errors"] == []
        check_failure.assert_not_called()
    switch.assert_called_once_with(psu_port, enabled=False, psu_lock=None, channel=4)


def test_state_check_failure_asks_to_continue_and_continues_when_approved(monkeypatch):
    failure_prompt = Mock(return_value=True)
    passing_pause = Mock()
    monkeypatch.setattr(urc, "handle_script_check_failure", failure_prompt)
    monkeypatch.setattr(urc, "request_force_pause", passing_pause)

    def failed_check(*, readings, errors_out):
        readings["Measured current (A)"] = 0.2
        errors_out.append("Current was expected to be zero")
        return "Current check failed", False

    switch_off._run_state_check("PSU off", failed_check)

    failure_prompt.assert_called_once_with(
        "PSU off",
        ["Current was expected to be zero"],
        {"Measured current (A)": 0.2},
    )
    passing_pause.assert_not_called()


def test_psu_off_failure_continues_when_operator_approves(monkeypatch):
    clock = {"now": 0.0, "sequence": 1}

    def monotonic():
        clock["now"] += 1.0
        return clock["now"]

    monkeypatch.setattr(switch_off.time, "monotonic", monotonic)
    monkeypatch.setattr(switch_off.ebpu, "get_psu_sequence", lambda: clock["sequence"])
    monkeypatch.setattr(switch_off.ebpu, "wait_for_fresh_psu", lambda **_kwargs: {"CH4_I": 0.2})
    monkeypatch.setattr(switch_off, "switch_psu", Mock())
    monkeypatch.setattr(urc, "is_aborted", lambda: False)
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(
        urc,
        "handle_script_check_failure",
        Mock(return_value=True),
    )
    monkeypatch.setattr(urc, "request_force_pause", Mock())

    switch_off._verify_psu_off(object(), None, "PSU off")

    urc.handle_script_check_failure.assert_called_once()
    assert "CH4_I is 0.200 A" in urc.handle_script_check_failure.call_args.args[1][-1]


def test_psu_off_waits_for_current_to_settle_before_verifying(monkeypatch):
    pause = Mock()
    switch = Mock()
    clock = {"now": 0.0, "sequence": 12, "calls": 0}
    nonzero = {"CH4_I": 0.2}
    zero = {"CH4_I": 0.0}
    monkeypatch.setattr(switch_off.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(switch_off.ebpu, "get_psu_sequence", lambda: clock["sequence"])

    def wait_for_psu(**_kwargs):
        clock["now"] += 1.0
        clock["sequence"] += 1
        clock["calls"] += 1
        return nonzero if clock["calls"] == 1 else zero

    monkeypatch.setattr(switch_off.ebpu, "wait_for_fresh_psu", wait_for_psu)
    monkeypatch.setattr(switch_off, "switch_psu", switch)
    monkeypatch.setattr(urc, "is_aborted", lambda: False)
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(urc, "request_force_pause", pause)

    switch_off._verify_psu_off(object(), None, "PSU off")

    assert clock["calls"] == 4
    assert pause.call_args.kwargs["errors"] == []
    assert pause.call_args.kwargs["readings"] == {"CH4_I (A)": 0.0}


def test_motion_detection_switches_psu_off_without_operator_pause(monkeypatch):
    clock = {"now": 0.0, "sequence": 4}
    events = []

    def wait_for_hk(**_kwargs):
        clock["now"] += 1.0
        clock["sequence"] += 1
        return SimpleNamespace(MTR_FLAGS=SimpleNamespace(MOVING=1, HOMING=0))

    def switch_off_psu(psu_port, psu_lock, label):
        events.append(("psu-off", psu_port, psu_lock, label))

    monkeypatch.setattr(switch_off.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(switch_off.ebpu, "wait_for_fresh_hk", wait_for_hk)
    monkeypatch.setattr(switch_off.ebpu, "get_hk_sequence", lambda: clock["sequence"])
    monkeypatch.setattr(urc, "is_aborted", lambda: False)
    monkeypatch.setattr(urc, "wait_while_paused", Mock())
    monkeypatch.setattr(urc, "abortible_sleep", Mock())
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(urc, "request_force_pause", Mock())
    monkeypatch.setattr(switch_off, "_verify_psu_off", switch_off_psu)
    psu_port, psu_lock = object(), object()

    switch_off._wait_for_motion_then_switch_off("Acquisition", 4, psu_port, psu_lock)

    assert events == [
        ("psu-off", psu_port, psu_lock, "Acquisition - switch-off during acquisition preparation")
    ]
    assert clock["now"] == 6.0
    urc.request_force_pause.assert_not_called()


def test_motion_must_remain_continuous_for_five_seconds(monkeypatch):
    clock = {"now": 0.0, "sequence": 4}
    events = []
    statuses = [True, True, False, True, True, True, True, True, True]

    def wait_for_hk(**_kwargs):
        clock["now"] += 1.0
        clock["sequence"] += 1
        moving = statuses.pop(0)
        return SimpleNamespace(MTR_FLAGS=SimpleNamespace(MOVING=int(moving), HOMING=0))

    monkeypatch.setattr(switch_off.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(switch_off.ebpu, "wait_for_fresh_hk", wait_for_hk)
    monkeypatch.setattr(switch_off.ebpu, "get_hk_sequence", lambda: clock["sequence"])
    monkeypatch.setattr(urc, "is_aborted", lambda: False)
    monkeypatch.setattr(urc, "wait_while_paused", Mock())
    monkeypatch.setattr(urc, "abortible_sleep", Mock())
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(urc, "request_force_pause", Mock())
    monkeypatch.setattr(
        switch_off,
        "_verify_psu_off",
        lambda *_args: events.append(("psu-off", clock["now"])),
    )

    switch_off._wait_for_motion_then_switch_off("Acquisition", 4, object(), None)

    assert events == [("psu-off", 9.0)]
    urc.request_force_pause.assert_not_called()


def test_fixed_point_switches_off_only_when_acquisition_is_stationary(monkeypatch):
    clock = {"sequence": 10}
    events = []
    hk_samples = iter(
        (
            SimpleNamespace(CURRENT_OPERATING_STATE=4, MTR_FLAGS=SimpleNamespace(MOVING=0, HOMING=0)),
            SimpleNamespace(CURRENT_OPERATING_STATE=8, MTR_FLAGS=SimpleNamespace(MOVING=1, HOMING=0)),
            SimpleNamespace(CURRENT_OPERATING_STATE=8, MTR_FLAGS=SimpleNamespace(MOVING=0, HOMING=0)),
        )
    )

    def wait_for_hk(**_kwargs):
        events.append(("hk",))
        clock["sequence"] += 1
        return next(hk_samples)

    monkeypatch.setattr(switch_off.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(switch_off.ebpu, "wait_for_fresh_hk", wait_for_hk)
    monkeypatch.setattr(switch_off.ebpu, "get_hk_sequence", lambda: clock["sequence"])
    monkeypatch.setattr(urc, "is_aborted", lambda: False)
    monkeypatch.setattr(urc, "wait_while_paused", Mock())
    monkeypatch.setattr(urc, "abortible_sleep", Mock())
    monkeypatch.setattr(
        urc,
        "abortible_sleep_with_progress",
        lambda seconds, label, update_interval: events.append(
            ("delay", seconds, label, update_interval)
        ),
    )
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(urc, "request_force_pause", Mock())
    monkeypatch.setattr(
        switch_off,
        "_verify_psu_off",
        lambda _port, _lock, label: events.append(("psu-off", label, clock["sequence"])),
    )

    switch_off._wait_for_fixed_point_then_switch_off("Fixed point", 10, object(), None)

    assert events == [
        ("delay", 100.0, "Fixed point: waiting before fixed-point power cut", 1.0),
        ("hk",),
        ("hk",),
        ("hk",),
        ("psu-off", "Fixed point - switch-off during fixed-point acquisition", 13),
    ]
    urc.request_force_pause.assert_not_called()


def test_acquisition_setup_configures_flight_heaters_and_enables_them_before_acq(monkeypatch):
    events = []
    interface = object()
    for command in (
        "en_mech_heater",
        "en_det_heater",
        "set_heater_configs",
        "hk_request",
        "set_acq_configs",
        "set_hk_rate",
        "acquisition",
    ):
        monkeypatch.setattr(
            switch_off.ebtcs,
            command,
            Mock(side_effect=lambda *args, _name=command, **_kwargs: events.append((_name, args))),
            )
        monkeypatch.setattr(switch_off.ebpu, "get_hk_sequence", lambda: 17)
        state_check = Mock(side_effect=AssertionError("No power-state check is expected before ACQ"))
        monkeypatch.setattr(switch_off, "_run_state_check", state_check)
    monkeypatch.setattr(
        switch_off,
        "_wait_for_motion_then_switch_off",
        lambda label, sequence, psu_port, psu_lock: events.append(
            ("motion", (label, sequence, psu_port, psu_lock))
        ),
    )
    monkeypatch.setattr(urc, "notify", Mock())
    acq_config = (0, 1, 2)
    psu_port, psu_lock = object(), object()

    switch_off._interrupt_during_acquisition(
        interface, psu_port, psu_lock, "Acquisition test", *acq_config
    )

    assert events == [
        ("en_mech_heater", (interface, 0x00)),
        ("en_det_heater", (interface, 0x00)),
        ("set_heater_configs", (interface, *switch_off.FLIGHT_HEATER_CONFIGS)),
        ("en_mech_heater", (interface, 0x01)),
        ("en_det_heater", (interface, 0x01)),
        ("set_acq_configs", (interface, *acq_config)),
        ("set_hk_rate", (interface, 0, 1)),
        ("acquisition", (interface, 0x00)),
        ("motion", ("Acquisition test", 17, psu_port, psu_lock)),
    ]
    state_check.assert_not_called()


def test_board_and_homing_preparation_does_not_check_state_before_heaters(monkeypatch):
    events = []
    interface = object()
    for command in ("en_mech_board", "en_det_board", "set_motor_configs", "ob_homing"):
        monkeypatch.setattr(
            switch_off.ebtcs,
            command,
            Mock(side_effect=lambda *args, _name=command, **_kwargs: events.append((_name, args))),
        )
    monkeypatch.setattr(
        urc,
        "abortible_sleep_with_progress",
        lambda *_args, **_kwargs: events.append(("wait", ())),
    )
    monkeypatch.setattr(urc, "notify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        switch_off,
        "_run_check",
        lambda label, check, *_args, **_kwargs: events.append(("homing-check", (label,))),
    )
    monkeypatch.setattr(urc, "request_force_pause", lambda label, **_kwargs: events.append(("pause", (label,))))
    monkeypatch.setattr(switch_off, "_run_state_check", lambda *_args, **_kwargs: pytest.fail("Premature power-state check"))

    switch_off._prepare_warm_boards_and_home(interface, "Acquisition test")

    assert [event[0] for event in events] == [
        "en_mech_board",
        "en_det_board",
        "wait",
        "set_motor_configs",
        "ob_homing",
        "homing-check",
        "pause",
    ]


def test_runner_executes_all_four_power_cycles_in_order(monkeypatch):
    events = []
    interface = object()
    monkeypatch.setattr(switch_off.eb_interface, "get_egse_interface", lambda: interface)
    monkeypatch.setattr(
        switch_off,
        "_boot_to_safe",
        lambda actual_interface, psu_port, psu_lock, label: events.append(
            ("boot", actual_interface, psu_port, psu_lock, label)
        ),
    )
    monkeypatch.setattr(
        switch_off,
        "_enter_standby",
        lambda actual_interface, label: events.append(("standby", actual_interface, label)),
    )
    monkeypatch.setattr(
        switch_off,
        "_prepare_warm_boards_and_home",
        lambda actual_interface, label: events.append(("prepare", actual_interface, label)),
    )
    monkeypatch.setattr(
        switch_off,
        "_verify_tec_setpoint",
        lambda actual_interface, label: events.append(("tec", actual_interface, label)),
    )
    monkeypatch.setattr(
        switch_off,
        "_interrupt_during_acquisition",
        lambda actual_interface, psu_port, psu_lock, label, *config, fixed_point=False: events.append(
            ("acquisition", actual_interface, psu_port, psu_lock, label, config, fixed_point)
        ),
    )
    monkeypatch.setattr(
        switch_off,
        "_verify_psu_off",
        lambda actual_port, actual_lock, label: events.append(
            ("psu-off", actual_port, actual_lock, label)
        ),
    )
    monkeypatch.setattr(urc, "notify_script_done", lambda: events.append(("done",)))

    psu_port, psu_lock = object(), object()
    switch_off.run_inadvertent_switch_off(psu_port, psu_lock)

    assert [event[0] for event in events] == [
        "boot",
        "psu-off",
        "boot",
        "standby",
        "psu-off",
        "boot",
        "standby",
        "prepare",
        "tec",
        "acquisition",
        "boot",
        "standby",
        "prepare",
        "tec",
        "acquisition",
        "done",
    ]
    assert events[0] == ("boot", interface, psu_port, psu_lock, "Power cycle 1")
    assert events[1] == ("psu-off", psu_port, psu_lock, "Power cycle 1 - verify zero consumption")
    assert events[2] == ("boot", interface, psu_port, psu_lock, "Power cycle 2")
    assert events[3] == ("standby", interface, "Power cycle 2")
    assert events[4] == ("psu-off", psu_port, psu_lock, "Power cycle 2 - verify zero consumption")
    assert events[5] == ("boot", interface, psu_port, psu_lock, "Power cycle 3")
    assert events[6] == ("standby", interface, "Power cycle 3")
    assert events[7] == ("prepare", interface, "Normal acquisition")
    assert events[8] == ("tec", interface, "Normal acquisition - TEC setpoint")
    normal_acquisition = events[9]
    assert normal_acquisition[1:5] == (interface, psu_port, psu_lock, "Normal acquisition")
    assert normal_acquisition[5] == (0, 0, 0, 0, 0, 1, 249, 0, 1, 42, 0, 0, 0, 12)
    assert normal_acquisition[6] is False
    assert events[10] == ("boot", interface, psu_port, psu_lock, "Power cycle 4")
    assert events[11] == ("standby", interface, "Power cycle 4")
    assert events[12] == ("prepare", interface, "Fixed-point acquisition")
    assert events[13] == ("tec", interface, "Fixed-point acquisition - TEC setpoint")
    fixed_point_acquisition = events[14]
    assert fixed_point_acquisition[1:5] == (
        interface,
        psu_port,
        psu_lock,
        "Fixed-point acquisition",
    )
    assert fixed_point_acquisition[5] == (
        0x01, 0x00, 0x0000, 0x00FA, 0x0078, 0x0000, 0x0, 0x00,
        0x1, 0x1, 0x1, 0x1, 0x00, 0x01,
    )
    assert fixed_point_acquisition[6] is True