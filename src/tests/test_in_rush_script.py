from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts_modules import IN_RUSH as in_rush
from widget_modules import ui_runtime_controller as urc


def test_hk_request_waits_for_command_to_settle(monkeypatch):
    events = []
    monkeypatch.setattr(in_rush.time, "sleep", lambda delay: events.append(("sleep", delay)))
    monkeypatch.setattr(in_rush.ebtcs, "hk_request", lambda *args: events.append(("hk", args)))
    interface = object()

    in_rush._request_hk_after_command(interface)

    assert events == [
        ("sleep", in_rush.TCS_COMMAND_SETTLE_S),
        ("hk", (interface, 0)),
    ]


def test_scoped_transition_arms_before_command_and_saves_before_check(monkeypatch):
    events = []
    scope = Mock()
    scope.configure_mode_transition_capture.side_effect = lambda **_kwargs: events.append("configure")
    scope.arm_single.side_effect = lambda: events.append("arm")
    monkeypatch.setattr(urc, "notify", Mock())
    pause = Mock(side_effect=lambda label, **_kwargs: events.append(f"pause:{label}"))
    monkeypatch.setattr(urc, "request_force_pause", pause)
    monkeypatch.setattr(
        urc,
        "request_repeat_acquisition",
        lambda _label: events.append("repeat") or False,
    )
    monkeypatch.setattr(in_rush.ebtcs, "hk_request", lambda *_args: events.append("hk"))
    monkeypatch.setattr(in_rush, "_scope_save_checkpoint", lambda *_args: events.append("save"))

    def command():
        events.append("command")

    def check(*, readings, errors_out):
        events.append("check")
        readings["PSU_EB_I (mA)"] = 100.0
        return "OK", True

    in_rush._scoped_transition(
        scope,
        object(),
        "Mechanism board ON",
        "Table 13-58",
        "Peak current",
        None,
        ["CH1", "CH2"],
        command,
        check,
        scope_settings="Current scale: 50 mA/div; timebase: 400 us/div.",
        scope_timebase_s=0.0004,
        scope_current_scale_ma_per_div=50.0,
    )

    assert events == [
        "configure",
        "arm",
        "pause:Confirm oscilloscope settings - Mechanism board ON",
        "command",
        "save",
        "hk",
        "check",
        "pause:Mechanism board ON - HK/PSU check",
        "repeat",
    ]
    assert pause.call_args_list[0].kwargs["readings"]["Action"].endswith(
        "Resume to issue the TCS transition command."
    )
    assert pause.call_args_list[0].kwargs["readings"]["Recommended scope settings"] == (
        "Current scale: 50 mA/div; timebase: 400 us/div."
    )


def test_scoped_transition_prompts_and_aborts_after_failed_verification(monkeypatch):
    failures = []
    scope = Mock()
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(
        urc,
        "handle_script_check_failure",
        lambda label, errors, readings=None: failures.append((label, errors, readings)) or False,
    )
    monkeypatch.setattr(urc, "request_force_pause", Mock())
    monkeypatch.setattr(urc, "request_repeat_acquisition", lambda _label: False)
    monkeypatch.setattr(in_rush.ebtcs, "hk_request", Mock())

    def check(*, readings, errors_out):
        errors_out.append("PSU current mismatch")
        return "Failed", False

    with pytest.raises(AssertionError, match="PSU current mismatch"):
        in_rush._scoped_transition(
            scope,
            object(),
            "TEC current",
            "Table 13-58",
            "Peak current",
            None,
            ["CH1", "CH2"],
            Mock(),
            check,
            scope_settings="Timebase: 400 us/div.",
            scope_timebase_s=0.0004,
        )

    assert failures == [
        ("TEC current - HK/PSU check", ["PSU current mismatch"], {})
    ]
    assert urc.request_force_pause.call_args.args[0] == "Record capture results - TEC current"


def test_state_check_continues_when_operator_overrides_failure(monkeypatch):
    pause = Mock()
    failure_prompt = Mock(return_value=True)
    monkeypatch.setattr(urc, "handle_script_check_failure", failure_prompt)
    monkeypatch.setattr(urc, "request_force_pause", pause)

    def check(*, readings, errors_out):
        errors_out.append("PSU current mismatch")
        readings["PSU_EB_I (mA)"] = 321.0
        return "Failed", False

    in_rush._run_state_check("TEC current", check)

    failure_prompt.assert_called_once_with(
        "TEC current",
        ["PSU current mismatch"],
        {"PSU_EB_I (mA)": 321.0},
    )
    pause.assert_not_called()


def test_scoped_transition_resets_baseline_before_repeating(monkeypatch):
    scope = Mock()
    command = Mock()
    scope.configure_mode_transition_capture.side_effect = lambda **_kwargs: None
    reset = Mock()
    repeat_choices = iter((True, False))
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(urc, "request_force_pause", Mock())
    monkeypatch.setattr(urc, "request_repeat_acquisition", lambda _label: next(repeat_choices))
    monkeypatch.setattr(in_rush, "_scope_save_checkpoint", Mock())
    monkeypatch.setattr(in_rush.ebtcs, "hk_request", Mock())

    in_rush._scoped_transition(
        scope,
        object(),
        "Mechanism heater ON",
        "Table 13-58",
        "Peak current",
        None,
        ["CH1", "CH2"],
        command,
        lambda **_kwargs: ("OK", True),
        scope_settings="Current scale: 50 mA/div; timebase: 1 ms/div.",
        scope_timebase_s=0.001,
        scope_current_scale_ma_per_div=50.0,
        repeat_reset=reset,
    )

    assert command.call_count == 2
    assert scope.arm_single.call_count == 2
    assert scope.configure_mode_transition_capture.call_count == 2
    assert sum(
        call.args[0].startswith("Confirm oscilloscope settings")
        for call in urc.request_force_pause.call_args_list
    ) == 2
    reset.assert_called_once_with()


def test_inrush_voltage_point_leaves_nominal_psu_on_and_sends_ret_automatically(monkeypatch):
    events = []
    scope = Mock()
    scope.arm_single.side_effect = lambda: events.append("arm")
    scope.configure_inrush_capture.side_effect = lambda timebase: events.append(f"configure:{timebase}")
    scope.set_trigger_level_current_ma.side_effect = lambda level: events.append(f"trigger:{level}") or 0.4
    scope.wait_for_capture.side_effect = lambda **_kwargs: events.append("capture")
    scope.save_waveforms.side_effect = lambda _directory, label, _channels: (
        events.append(f"save:{label}") or Path("capture.csv")
    )
    psu_samples = iter(
        [
            {"PSU_EB_I": 0.0},
            {"PSU_EB_I": 0.0},
            {"PSU_EB_I": 0.0},
            {"PSU_EB_I": 0.05},
            {"PSU_EB_I": 0.085},
            {"PSU_EB_I": 0.0},
            {"PSU_EB_I": 0.0},
            {"PSU_EB_I": 0.0},
            {"PSU_EB_I": 0.085},
        ]
    )
    monkeypatch.setattr(in_rush.ebpu, "get_psu_sequence", lambda: 0)
    monkeypatch.setattr(in_rush.ebpu, "wait_for_fresh_psu", lambda **_kwargs: next(psu_samples))
    monkeypatch.setattr(
        in_rush,
        "switch_psu",
        lambda _port, *, enabled, **_kwargs: events.append(f"psu:{'on' if enabled else 'off'}"),
    )
    monkeypatch.setattr(in_rush, "set_psu_channel_voltage", lambda *_args, **_kwargs: events.append("set-voltage"))
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(
        urc,
        "request_force_pause",
        lambda label, **_kwargs: events.append(f"pause:{label}"),
    )
    monkeypatch.setattr(urc, "request_repeat_acquisition", lambda _label: events.append("repeat?") or False)
    monkeypatch.setattr(
        in_rush,
        "_verify_inrush_psu_voltage",
        lambda *_args, **_kwargs: events.append("verify-voltage") or 28.0,
    )

    def check_safe_current(_state, sample, errors):
        current = sample["PSU_EB_I"]
        events.append(f"safe-current:{current}")
        if current != 0.085:
            errors.append("PSU_EB_I out of range for ['State1']")
        return current * 1000.0

    monkeypatch.setattr(urc, "consumption_check", check_safe_current)
    monkeypatch.setattr(in_rush.ebtcs, "ret", lambda *_args: events.append("ret"))
    monkeypatch.setattr(in_rush.ebtcs, "hk_request", lambda *_args: events.append("hk"))
    monkeypatch.setattr(in_rush, "_run_state_check", lambda *_args: events.append("state-check"))

    in_rush._capture_inrush_voltage_point(
        scope,
        object(),
        object(),
        None,
        ["CH1", "CH2"],
        28.0,
        "+28.0 V Off to Safe",
        leave_psu_on=True,
    )

    assert events == [
        "psu:off",
        "set-voltage",
        "configure:0.004",
        "trigger:400.0",
        "arm",
        "pause:Power on PSU CH4 - +28.0 V Off to Safe",
        "psu:on",
        "verify-voltage",
        "safe-current:0.05",
        "safe-current:0.085",
        "ret",
        "capture",
        "save:+28.0 V Off to Safe - 4 ms/div",
        "hk",
        "state-check",
        "psu:off",
        "set-voltage",
        "configure:2e-05",
        "trigger:400.0",
        "arm",
        "pause:Power on PSU CH4 - +28.0 V Off to Safe",
        "psu:on",
        "verify-voltage",
        "safe-current:0.085",
        "ret",
        "capture",
        "save:+28.0 V Off to Safe - 20 us/div",
        "hk",
        "state-check",
        "repeat?",
    ]


def test_inrush_repeat_prompt_happens_before_power_off(monkeypatch):
    events = []
    scope = Mock()
    scope.arm_single.side_effect = lambda: events.append("arm")
    scope.wait_for_capture.side_effect = lambda **_kwargs: events.append("capture")
    scope.save_waveforms.return_value = Path("capture.csv")
    psu_samples = iter(
        [
            {"PSU_EB_I": 0.0}, {"PSU_EB_I": 0.0}, {"PSU_EB_I": 0.0},
            {"PSU_EB_I": 0.085},
            {"PSU_EB_I": 0.0}, {"PSU_EB_I": 0.0}, {"PSU_EB_I": 0.0},
            {"PSU_EB_I": 0.085},
            {"PSU_EB_I": 0.0}, {"PSU_EB_I": 0.0}, {"PSU_EB_I": 0.0},
        ]
    )
    repeat_choices = iter((False,))
    monkeypatch.setattr(in_rush.ebpu, "get_psu_sequence", lambda: 0)
    monkeypatch.setattr(in_rush.ebpu, "wait_for_fresh_psu", lambda **_kwargs: next(psu_samples))
    monkeypatch.setattr(
        in_rush,
        "switch_psu",
        lambda _port, *, enabled, **_kwargs: events.append(f"psu:{'on' if enabled else 'off'}"),
    )
    monkeypatch.setattr(in_rush, "set_psu_channel_voltage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(in_rush, "_verify_inrush_psu_voltage", lambda *_args, **_kwargs: 26.0)
    monkeypatch.setattr(urc, "consumption_check", lambda _state, _sample, _errors: 85.0)
    scope.configure_inrush_capture.side_effect = lambda timebase: events.append(f"configure:{timebase}")
    scope.set_trigger_level_current_ma.return_value = 0.4
    monkeypatch.setattr(urc, "notify", Mock())
    monkeypatch.setattr(urc, "request_force_pause", Mock())
    monkeypatch.setattr(
        urc,
        "request_repeat_acquisition",
        lambda _label: events.append("repeat?") or next(repeat_choices),
    )
    monkeypatch.setattr(in_rush.ebtcs, "ret", Mock())
    monkeypatch.setattr(in_rush.ebtcs, "hk_request", Mock())
    monkeypatch.setattr(in_rush, "_run_state_check", Mock())

    in_rush._capture_inrush_voltage_point(
        scope,
        object(),
        object(),
        None,
        ["CH1", "CH2"],
        26.0,
        "+26.0 V Off to Safe",
    )

    repeat_indices = [index for index, event in enumerate(events) if event == "repeat?"]
    off_indices = [index for index, event in enumerate(events) if event == "psu:off"]
    assert len(repeat_indices) == 1
    assert events.count("configure:0.004") == 1
    assert events.count("configure:2e-05") == 1
    assert events.count("capture") == 2
    assert repeat_indices[0] < off_indices[-1]


def test_voltage_verification_ignores_off_and_transient_readbacks(monkeypatch):
    samples = iter(
        [
            {"CH4_STATUS": 0, "CH4_V": -0.01},
            {"CH4_STATUS": 1, "CH4_V": -0.01},
            {"CH4_STATUS": 1, "CH4_V": 28.0},
        ]
    )
    monkeypatch.setattr(in_rush.ebpu, "get_psu_sequence", lambda: 1)
    monkeypatch.setattr(in_rush.ebpu, "wait_for_fresh_psu", lambda **_kwargs: next(samples))
    monkeypatch.setattr(urc, "is_aborted", lambda: False)

    voltage = in_rush._verify_inrush_psu_voltage(28.0, "28 V capture", after_sequence=0)

    assert voltage == 28.0


def test_default_inrush_setup_file_is_repository_resource():
    assert in_rush.DEFAULT_SCOPE_SETUP_FILE == (
        Path(in_rush.__file__).resolve().parents[2] / "rsrc" / "Scope Config" / "INRUSH.set"
    )
    assert in_rush.DEFAULT_SCOPE_SETUP_FILE.is_file()