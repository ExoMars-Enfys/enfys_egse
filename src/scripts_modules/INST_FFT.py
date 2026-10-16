from __future__ import annotations

import logging
import time
from typing import Any, Callable

from core_modules import config
from utility_modules import eb_interface, ebtcs
from utility_modules import eb_packet_utility as ebpu
from widget_modules import ui_runtime_controller

info_log = logging.getLogger("info_log")

# Same +40 C limit (12-bit TRP ADU) as the OB FFT manual heater checks.
HEATER_ENABLE_MAX_TRP_ADU = 2196


# region Helper Functions
def _prompt_check_failure(label: str, message: str) -> None:
    """Ask whether INST FFT execution should continue after a failed check."""
    if ui_runtime_controller.handle_script_check_failure(label, [message]):
        return
    raise AssertionError(message)


def _run_check(label: str, check: Any, *args: Any, **kwargs: Any) -> Any:
    """Route check exceptions through the INST FFT Continue/Abort prompt."""
    try:
        return check(*args, **kwargs)
    except ui_runtime_controller.ScriptAbortRequested:
        raise
    except Exception as exc:
        _prompt_check_failure(label, str(exc))
        return None


def _run_state_check(
    label: str, check: Callable[..., tuple[str, bool] | None], *args: Any, **kwargs: Any
) -> None:
    """Report state measurements and failures in one Resume/Abort pause."""
    errors: list[str] = []
    measured: dict[str, Any] = {}
    try:
        result = check(*args, readings=measured, errors_out=errors, **kwargs)
        if result is not None:
            message, passed = result
            if not passed and not errors:
                errors.append(message)
    except ui_runtime_controller.ScriptAbortRequested:
        raise
    except Exception as exc:
        if ui_runtime_controller.is_aborted():
            raise
        errors.append(str(exc))
    ui_runtime_controller.request_force_pause(label, errors=errors, readings=measured)


def _pause_current_check(
    label: str, errors: list[str], hk: Any, current_ma: float | None, **readings: Any
) -> None:
    measured = {
        "PSU_EB_I (mA)": current_ma,
        "CURRENT_OPERATING_STATE": getattr(hk, "CURRENT_OPERATING_STATE", None),
        **readings,
    }
    ui_runtime_controller.request_force_pause(label, errors=errors, readings=measured)


def _get_tec_current_config() -> tuple[int, float, float]:
    """Return and validate the configured FFT TEC current settings."""
    setpoint_adu = int(config.TEC_CURRENT_SETPOINT_ADU)
    expected_current_a = float(config.TEC_EXPECTED_CURRENT_A)
    tolerance_a = float(config.TEC_CURRENT_TOLERANCE_A)

    if setpoint_adu < 0:
        raise ValueError(f"TEC_CURRENT_SETPOINT_ADU must be >= 0, got {setpoint_adu}")
    if expected_current_a < 0:
        raise ValueError(f"TEC_EXPECTED_CURRENT_A must be >= 0, got {expected_current_a}")
    if tolerance_a < 0:
        raise ValueError(f"TEC_CURRENT_TOLERANCE_A must be >= 0, got {tolerance_a}")

    return setpoint_adu, expected_current_a, tolerance_a


def _tec_current_in_fft_band(current_a: float, expected_current_a: float, tolerance_a: float) -> bool:
    return abs(float(current_a) - expected_current_a) <= tolerance_a


def _fresh_psu_sample(timeout: float = 2.0):
    """Get a fresh, smoothed PSU sample without draining the GUI plot queue."""
    fresh = ebpu.wait_for_fresh_psu(timeout=timeout)
    if fresh is None:
        return None
    smoothed = ebpu.get_smoothed_latest_psu(window_samples=5)
    return smoothed if isinstance(smoothed, dict) else fresh


def _tec_ramp_before_acquisition(interface: Any, tec_setpoint_adu: int, label: str) -> None:
    info_log.info("%s: commanding TEC current ramp for 60 s before acquisition", label)
    ui_runtime_controller.notify(f"{label}: TEC ramp-up for 60 s before acquisition", color="primary")
    tec_setpoint_adu = 0xFFF
    ebtcs.set_tec_current(interface, 0x00, tec_setpoint_adu)
    ebtcs.hk_request(interface, 0)
    ui_runtime_controller.abortible_sleep_with_progress(60, f"{label}: TEC ramp-up", update_interval=1.0)
    info_log.info("%s: setting TEC current to 0 before acquisition", label)
    ui_runtime_controller.notify(f"{label}: setting TEC current to 0 before acquisition", color="primary")


def _read_ob_trps(interface: Any) -> dict[str, int] | None:
    """Request fresh HK and return the 12-bit OB mechanism/motor/detector TRP ADUs."""
    sequence = ebpu.get_hk_sequence()
    ebtcs.hk_request(interface, 0)
    hk = ebpu.wait_for_fresh_hk(timeout=5.0, after_sequence=sequence)
    if hk is None or not getattr(hk, "OB_HK_ID", 0):
        return None
    trps = {}
    for name, field in (("MECH", "OB_MECHANISM_TRP"), ("MOTOR", "OB_MOTOR_TRP"), ("DET", "OB_DETECTOR_TRP")):
        raw = getattr(hk, field, None)
        if raw is None:
            return None
        trps[name] = int(raw) >> 4
    info_log.info("OB TRPs before heater check: %s", trps)
    return trps


def _mech_heater_allowed(trps: dict[str, int] | None) -> bool:
    return trps is not None and (
        trps["MECH"] <= HEATER_ENABLE_MAX_TRP_ADU or trps["MOTOR"] <= HEATER_ENABLE_MAX_TRP_ADU
    )


def _det_heater_allowed(trps: dict[str, int] | None) -> bool:
    return trps is not None and trps["DET"] <= HEATER_ENABLE_MAX_TRP_ADU


def _skip_heater_check(label: str, trps: dict[str, int] | None) -> None:
    reason = "OB TRPs unavailable" if trps is None else f"OB above +40 C (TRPs {trps})"
    msg = f"Skipping {label}: {reason}"
    info_log.warning(msg)
    ui_runtime_controller.notify(msg, color="warning")


# endregion


def run_fft(verification: bool = True) -> None:
    """Run INST FFT, pausing on each verified state with its errors and readings."""
    interface = eb_interface.get_egse_interface()
    tec_setpoint_adu, tec_expected_current_a, tec_current_tolerance_a = _get_tec_current_config()
    info_log.info(
        "FFT TEC config: command=%d ADU (0x%04X), expected=%.3f A +/- %.3f A",
        tec_setpoint_adu,
        tec_setpoint_adu,
        tec_expected_current_a,
        tec_current_tolerance_a,
    )
    # FFT.txt sequence

    # region Boot Checks
    # ?RET and first check - State 1
    interface = eb_interface.get_egse_interface()
    # RET command (SAFE mode)
    ebtcs.ret(interface, 0, 0, 0, 0, 0, 0)
    ebtcs.hk_request(interface, 0)
    if verification:
        _run_state_check("State 1 - SAFE RET", ui_runtime_controller.verify_safe_ret)

    time.sleep(2)
    ui_runtime_controller.request_force_pause("Pause for POST packet checks")
    # Transition to Standby and use automatic ASW. Capture telemetry sequence
    # markers before the command so packets/samples arriving during the settling
    # delay still count as fresh for this transition.
    standby_hk_sequence = ebpu.get_hk_sequence()
    standby_psu_sequence = ebpu.get_psu_sequence()
    ebtcs.standby(
        interface, 5, 1
    )  #! Change according to what image you want to start. For EQM at 3.6.2 image 5 (25.09.26)
    ebtcs.ret(interface, 0, 0, 0, 0, 0, 0)
    ebtcs.hk_request(interface, 0)
    ebtcs.set_hk_rate(interface, 0, 1)
    time.sleep(5)
    if verification:
        _run_state_check(
            "STANDBY RET",
            ui_runtime_controller.verify_standby_ret,
            after_hk_sequence=standby_hk_sequence,
            after_psu_sequence=standby_psu_sequence,
        )
    else:
        ui_runtime_controller.request_force_pause("Click to continue once ready.")
    # endregion

    # region Heater Checks
    time.sleep(1)
    ebtcs.set_heater_configs(interface, 0x00, 0x08A3, 0x0881, 0x08A3, 0x0881)

    # ?Enable Mech Heater - Standby + Mech HTR (only at or below +40 C)
    trps = _read_ob_trps(interface)
    if not _mech_heater_allowed(trps):
        _skip_heater_check("mech heater check", trps)
    else:
        ebtcs.en_mech_heater(interface, 0x1)
        ebtcs.hk_request(interface, 0)
        time.sleep(3.5)
        if verification:
            errors = []
            latest_hk = ebpu.get_latest_hk()
            latest_psu = _fresh_psu_sample(timeout=2.0)
            if latest_hk is None:
                errors.append("Missing HK data (mech ON, det OFF)")
            if latest_psu is None:
                errors.append("Missing fresh PSU monitor data (mech ON, det OFF)")
            ch4_current_ma = (
                ui_runtime_controller.consumption_check(["Standby"], latest_psu, errors, latest_hk)
                if latest_psu is not None
                else None
            )
            _pause_current_check("Heater config: mech ON, det OFF", errors, latest_hk, ch4_current_ma)
        ebtcs.en_mech_heater(interface, 0x0)

    # ?Enable Det Heater - Standby + Det HTR (only at or below +40 C)
    trps = _read_ob_trps(interface)
    if not _det_heater_allowed(trps):
        _skip_heater_check("det heater check", trps)
    else:
        ebtcs.en_det_heater(interface, 0x1)
        ebtcs.hk_request(interface, 0)
        time.sleep(3.5)
        if verification:
            errors = []
            latest_hk = ebpu.get_latest_hk()
            latest_psu = _fresh_psu_sample(timeout=2.0)
            if latest_hk is None:
                errors.append("Missing HK data (mech OFF, det ON)")
            if latest_psu is None:
                errors.append("Missing fresh PSU monitor data (mech OFF, det ON)")
            ch4_current_ma = (
                ui_runtime_controller.consumption_check(["Standby"], latest_psu, errors, latest_hk)
                if latest_psu is not None
                else None
            )
            _pause_current_check("Heater config: mech OFF, det ON", errors, latest_hk, ch4_current_ma)
        ebtcs.en_det_heater(interface, 0x0)

    # ?Turn on both heaters - State 2 - OB Heating (only if both may be enabled)
    trps = _read_ob_trps(interface)
    if not (_mech_heater_allowed(trps) and _det_heater_allowed(trps)):
        _skip_heater_check("State2 dual heater check", trps)
    else:
        ebtcs.en_mech_heater(interface, 0x1)
        ebtcs.en_det_heater(interface, 0x1)
        ebtcs.hk_request(interface, 0)
        time.sleep(3.5)
        if verification:
            _run_state_check("State 2 - OB heating", ui_runtime_controller.verify_power_state, "State2")

    # ?Set Heater Configs to flight
    ebtcs.en_mech_heater(interface, 0x0)
    ebtcs.en_det_heater(interface, 0x0)
    ebtcs.set_heater_configs(interface, 0x00, 0x079A, 0x0738, 0x079A, 0x0738)
    ebtcs.en_mech_heater(interface, 0x1)
    ebtcs.en_det_heater(interface, 0x1)
    ebtcs.hk_request(interface, 0)
    time.sleep(3.5)
    if verification:
        errors = []
        latest_hk = ebpu.get_latest_hk()
        latest_psu = _fresh_psu_sample(timeout=2.0)
        if latest_hk is None:
            errors.append("Missing HK data (flight HTR configs)")
        if latest_psu is None:
            errors.append("Missing fresh PSU monitor data (flight HTR configs)")
        # Heaters are in automatic mode with flight configs — actual HMS/HDS depends on
        # temperature, so verify_heater_states will add only the physically active ones.
        ch4_current_ma = (
            ui_runtime_controller.consumption_check(["Standby"], latest_psu, errors, latest_hk)
            if latest_psu is not None
            else None
        )
        _pause_current_check("Flight heater configuration", errors, latest_hk, ch4_current_ma)
    else:
        ui_runtime_controller.request_force_pause("Click to continue once ready.")
    # endregion

    # region Mechanism Checks and Homing
    # ?Turn on the mechanism board, set configs and home - State 2 + Mech Board ON + Moving
    ebtcs.en_mech_board(interface, 0x1)
    ui_runtime_controller.abortible_sleep(5)
    ebtcs.set_motor_configs(interface, 0, 0x40, 0x00, 0x08, 0x00, 0x3C, 0x00, 0x00, 0x0000, 0x00)
    # ebtcs.generic_tc(interface,0x0, 0x2, 0x00, 0x00, 0x00, 0xC8, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)

    ebtcs.ob_homing(interface, 0x01)
    _run_check("Initial homing", ui_runtime_controller.perform_homing_check_sync)
    ui_runtime_controller.request_force_pause("Click to continue once ready.")
    # endregion

    # region TEC tests
    # ?Turn on TEC - State 2 + Mech Board ON + TEC clamped at configured current
    ebtcs.set_tec_current(interface, 0x00, tec_setpoint_adu)

    ebtcs.hk_request(interface, 0)
    # Verification: TEC current must settle at configured expected current +/- tolerance and PSU current must match
    if verification:
        errors = []
        tec_ramped = False
        timeout_count = 0
        latest_hk = None
        latest_psu = None
        while not tec_ramped and timeout_count < 30:
            time.sleep(1)
            timeout_count += 1
            latest_hk = ebpu.get_latest_hk()
            latest_psu = _fresh_psu_sample(timeout=2.0)
            if latest_psu is None:
                continue
            if latest_hk is None:
                continue
            tec_current = getattr(latest_hk, "EB_TEC_DRIVE_CURRENT", 0) * 0.0000162
            if not _tec_current_in_fft_band(tec_current, tec_expected_current_a, tec_current_tolerance_a):
                continue  # Keep waiting for the clamped current band
            tec_ramped = True
        if not tec_ramped:
            errors.append(
                f"Timeout waiting for TEC current to settle at {tec_expected_current_a:.2f} A "
                f"+/- {tec_current_tolerance_a:.2f} A"
            )
        ch4_current_ma = (
            ui_runtime_controller.consumption_check(["Standby", "Mech", "TEC_0_9A"], latest_psu, errors, latest_hk)
            if latest_psu is not None
            else None
        )
        tec_raw = getattr(latest_hk, "EB_TEC_DRIVE_CURRENT", None)
        _pause_current_check(
            "TEC ramp-up",
            errors,
            latest_hk,
            ch4_current_ma,
            **{
                "TEC current (A)": tec_raw * 0.0000162 if tec_raw is not None else None,
                "TEC target (A)": tec_expected_current_a,
                "TEC tolerance (A)": tec_current_tolerance_a,
                "TEC command (ADU)": tec_setpoint_adu,
            },
        )

    # ?Turn off TEC - State 2 + Mech Board
    ebtcs.set_tec_current(interface, 0x00, 0x000)
    # Verification: TEC current = 0 and PSU current as expected
    ebtcs.hk_request(interface, 0)
    time.sleep(3.5)
    if verification:
        errors = []
        tec_off = False
        timeout_count = 0
        latest_hk = None
        latest_psu = None
        while not tec_off and timeout_count < 30:
            time.sleep(1)
            timeout_count += 1
            latest_hk = ebpu.get_latest_hk()
            latest_psu = _fresh_psu_sample(timeout=2.0)
            if latest_psu is None:
                continue
            if latest_hk is None:
                continue
            tec_current = getattr(latest_hk, "EB_TEC_DRIVE_CURRENT", 0) * 0.0000162
            if tec_current >= 0.01:
                continue  # Keep waiting
            tec_off = True
        if not tec_off:
            errors.append("Timeout waiting for TEC current to reach 0 A")
        ch4_current_ma = (
            ui_runtime_controller.consumption_check(["Standby", "Mech"], latest_psu, errors, latest_hk)
            if latest_psu is not None
            else None
        )
        tec_raw = getattr(latest_hk, "EB_TEC_DRIVE_CURRENT", None)
        _pause_current_check(
            "TEC ramp-down",
            errors,
            latest_hk,
            ch4_current_ma,
            **{"TEC current (A)": tec_raw * 0.0000162 if tec_raw is not None else None},
        )
    # endregion

    # region Full Dark SCI Scan @-35oC
    # ?Set TEC setpoint to -35oC, enable detectors, and start acquisition
    ebtcs.set_tec_setpoint(interface, 0x0, 0xC018)
    ebtcs.en_det_board(interface, 0x1)
    time.sleep(1)
    if verification:
        errors = []
        latest_hk = ebpu.get_latest_hk()
        if latest_hk is not None:
            if hasattr(latest_hk, "TEC_SETPOINT") and getattr(latest_hk, "TEC_SETPOINT", None) != 0xC018:
                errors.append(f"TEC setpoint is not -35C (HK TEC_SETPOINT={getattr(latest_hk, 'TEC_SETPOINT', None)})")
        if errors:
            msg = "\n".join(errors)
            ui_runtime_controller.notify_negative(msg)
            _prompt_check_failure("TEC setpoint -35C", msg)

    # ?State 6 SCI ACQ
    ebtcs.set_acq_configs(
        interface, 0, 0, 0, 0, 0, 1, 249, 0, 1, 42, 0, 0, 0, 12
    )  #! This is a baseline ACQ that ABU verified with table 12 - This can be changed if needed later on
    ebtcs.set_hk_rate(interface, 0, 1)
    time.sleep(3)
    check_state6 = True
    ui_runtime_controller.request_force_pause(
                "1st Dark Scan. Click to continue once ready."
            )
    while True:
        ebtcs.acquisition(interface, 0x0)
        time.sleep(3)
        if verification:
            _run_state_check("State 6 acquisition", ui_runtime_controller.perform_acq_check_sync, check_state6=check_state6)
        check_state6 = False
        if not ui_runtime_controller.request_repeat_acquisition("Full Dark SCI Scan @-35C"):
            break
    # endregion

    ebtcs.set_hk_rate(interface, 0, 1)
    # endregion

    # region Full Light Scan @-35oC
    # ?State 6 SCI ACQ with heaters OFF and baffle hat off
    ebtcs.en_mech_heater(interface, 0x0)
    ebtcs.en_det_heater(interface, 0x0)
    ebtcs.set_heater_configs(interface, 0x00, 0x079A, 0x0738, 0x079A, 0x0738)
    ebtcs.en_mech_heater(interface, 0x1)
    ebtcs.en_det_heater(interface, 0x1)
    ui_runtime_controller.request_force_pause(
        "Remove Baffle Hat and continue with acquisition. Click to continue once ready."
    )
    while True:
        ebtcs.acquisition(interface, 0x0)
        ebtcs.hk_request(interface, 0)
        time.sleep(3.5)
        if verification:
            _run_state_check(
                "State 6 acquisition without heaters",
                ui_runtime_controller.perform_acq_check_sync,
                check_state6=False,
            )
        if not ui_runtime_controller.request_repeat_acquisition("Full Light Scan @-35C (hat off)"):
            break
    # endregion

    
    # region Setup for Fixed point Dark SCI Scan
    # ?State3 - OB Heating + Powered On
    ebtcs.set_hk_rate(interface, 0, 1)
    ebtcs.en_mech_heater(interface, 0x0)
    ebtcs.en_det_heater(interface, 0x0)
    ebtcs.set_heater_configs(interface, 0x00, 0x08D3, 0x08A3, 0x08D3, 0x08A3)
    ebtcs.en_mech_heater(interface, 0x1)
    ebtcs.en_det_heater(interface, 0x1)
    ebtcs.generic_tc(interface, 0x0, 0x6, 0x64, 0xC4, 0x01, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0)

    ebtcs.en_mech_board(interface, 0x1)
    ebtcs.en_det_board(interface, 0x1)
    ebtcs.hk_request(interface, 0)
    time.sleep(3.5)
    if verification:
        _run_state_check("State 3 - OB heating and boards on", ui_runtime_controller.verify_power_state, "State3")

    # ? State4 - OB Heating + Powered On + TEC clamped at configured current
    ebtcs.set_tec_current(interface, 0x00, tec_setpoint_adu)
    ebtcs.hk_request(interface, 0)
    time.sleep(3.5)
    if verification:
        _run_state_check(
            "State 4 - OB heating, boards on and TEC clamped", ui_runtime_controller.verify_power_state, "State4"
        )

    # ?State 7 - All Active (OB Heating + Powered On + TEC clamped at configured current + Moving)
    ebtcs.set_motor_configs(interface, 0, 0x40, 0x00, 0x08, 0x00, 0x3C, 0x00, 0x00, 0x0000, 0x00)
    # ebtcs.generic_tc(interface,0x0, 0x2, 0x00, 0x00, 0x00, 0xC8, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)

    ebtcs.ob_homing(interface, 0x01)
    ebtcs.hk_request(interface, 0)
    time.sleep(3.5)
    if verification:
        _run_state_check("State 7 - all active, moving", ui_runtime_controller.verify_power_state, "State7")

    _run_check("State 7 homing", ui_runtime_controller.perform_homing_check_sync)
    ui_runtime_controller.request_force_pause("Click to continue once ready.")

    time.sleep(3.5)
    ebtcs.en_mech_heater(interface, 0x0)
    ebtcs.en_det_heater(interface, 0x0)

    # ?State 5 - Powered On + TEC clamped at configured current (heaters off, mech board on)
    ebtcs.set_heater_configs(interface, 0x00, 0x079A, 0x0738, 0x079A, 0x0738)
    ebtcs.en_mech_heater(interface, 0x1)
    ebtcs.en_det_heater(interface, 0x1)
    ebtcs.hk_request(interface, 0)
    time.sleep(3.5)
    if verification:
        _run_state_check("State 5 - boards on and TEC clamped", ui_runtime_controller.verify_power_state, "State5")

    ebtcs.set_tec_current(interface, 0x00, 0x000)

    ebtcs.set_tec_setpoint(interface, 0x0, 0xC018)
    ebtcs.hk_request(interface, 0)
    time.sleep(3.5)
    if verification:
        errors = []
        latest_hk = ebpu.get_latest_hk()
        if latest_hk is not None:
            if hasattr(latest_hk, "TEC_SETPOINT") and getattr(latest_hk, "TEC_SETPOINT", None) != 0xC018:
                errors.append(f"TEC setpoint is not -35C (HK TEC_SETPOINT={getattr(latest_hk, 'TEC_SETPOINT', None)})")
        if errors:
            msg = "\n".join(errors)
            ui_runtime_controller.notify_negative(msg)
            _prompt_check_failure("TEC setpoint -35C", msg)
    # endregion

    # region Fixed point Dark SCI Scan @-35oC with Heaters
    # ?State 6 SCI ACQ with heaters ON
    ebtcs.en_mech_heater(interface, 0x0)
    ebtcs.en_det_heater(interface, 0x0)
    ebtcs.set_heater_configs(interface, 0x00, 0x08A3, 0x0881, 0x08A3, 0x0881)
    ebtcs.en_mech_heater(interface, 0x1)
    ebtcs.en_det_heater(interface, 0x1)
    ebtcs.set_acq_configs(
        interface,
        0x01,
        0x00,
        0x0000,
        0x00FA,
        0x0078,
        0x0000,
        0x0,
        0x00,
        0x1,
        0x1,
        0x1,
        0x1,
        0x00,
        0x01,  # MT1 , spacing 250ms, duration 120s, start position 5 ()
    )  #! Todo Check the measurement tables for the correct fixed point measurement
    ebtcs.set_hk_rate(interface, 0, 1)
    ui_runtime_controller.request_force_pause(
                "Fixed Point Dark Scan.Put the Baffle Hat back on and Click to continue once ready."
            )
    while True:
        ebtcs.acquisition(interface, 0x0)
        ebtcs.hk_request(interface, 0)
        time.sleep(3.5)
        if verification:
            _run_state_check(
                "State 6 acquisition with heaters",
                ui_runtime_controller.perform_acq_check_sync,
                acq_mode=2,
                acq_duration_s=0x0078,
                acq_sample_time_ms=0x0064,
                check_state6=False,
            )
        if not ui_runtime_controller.request_repeat_acquisition("Fixed point Dark SCI Scan @-35C with heaters"):
            break
    # endregion

    # region Park and Power down
    ebtcs.ob_park(interface, 0x00)
    _run_check("Final homing", ui_runtime_controller.perform_homing_check_sync)

    ui_runtime_controller.request_force_pause("Click to continue once ready.")
    ebtcs.safe(interface, 0)
    ebtcs.ret(interface, 0, 0, 0, 0, 0, 0)
    # End of FFT
    ui_runtime_controller.notify_script_done()
    # endregion
