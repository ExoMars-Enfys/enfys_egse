"""Reusable, command-aware background telemetry checks.

This module intentionally contains no GUI state. Callers inject the sleep
function used while polling so scripts can remain abortible and tests can run
without the UI runtime.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable

from core_modules import config
from core_modules import constants as const  # noqa: F401 - re-exported for test monkeypatching
from core_modules import measurement_config as limits
from utility_modules import eb_packet_utility, hk_conversions, psu, tc
from utility_modules.send_cmd import cmd_repeat as repeat

event_log = logging.getLogger("event_log")
info_log = logging.getLogger("info_log")

POWER_TRANSITION_TIMEOUT_S = 6.0
POWER_TRANSITION_POLL_S = 2.0


def _power_transition_transaction(port: Any, command: int) -> Any:
    """Own the OB link from power-command ACK through a confirming HK sample."""
    result = repeat(port, tc.power_control, command)
    if result == "ERROR":
        return "ERROR"

    deadline = time.monotonic() + POWER_TRANSITION_TIMEOUT_S
    time.sleep(POWER_TRANSITION_POLL_S)
    last_response = None
    while True:
        last_response = repeat(port, tc.hk_request)
        if last_response not in (None, "ERROR"):
            info_log.info(
                "Power transition target=%s observed PWR_STAT=%s CMD_CNT=%s ERROR_BYTE=%s",
                command,
                getattr(last_response, "PWR_STAT", None),
                getattr(last_response, "CMD_CNT", None),
                getattr(last_response, "ERROR_BYTE", None),
            )
        if last_response not in (None, "ERROR") and getattr(last_response, "PWR_STAT", None) == command:
            return last_response
        if time.monotonic() >= deadline:
            return last_response
        time.sleep(POWER_TRANSITION_POLL_S)


def calculate_ob_current_profile(state: Any, motor_current: int | None = None) -> dict[str, float]:
    """Calculate rail totals from an HK response or named expected state."""
    if isinstance(state, str):
        expectation = limits.OB_STATE_EXPECTATIONS.get(state)
        if expectation is None:
            raise KeyError(f"Unknown OB current state: {state}")
        power = expectation["power"]
        heater = expectation["heater"]
        moving = expectation["moving"]
    else:
        power = getattr(state, "PWR_STAT", 0)
        heater_status = getattr(state, "THRM_STATUS", None)
        heater = (
            bool(getattr(heater_status, "DM", 0)),
            bool(getattr(heater_status, "DA", 0)),
            bool(getattr(heater_status, "MM", 0)),
            bool(getattr(heater_status, "MA", 0)),
        )
        motor_flags = getattr(state, "MTR_FLAGS", None)
        moving = bool(getattr(motor_flags, "MOVING", 0) or getattr(motor_flags, "HOMING", 0))
        if motor_current is None:
            motor_current = getattr(state, "MTR_CURRENT", None)
            if motor_current == limits.MOTOR_NOMINAL_CURRENT:
                motor_current = None

    components = ["OB5V"]
    if power & 0x01:
        components.append("MechanismBoard")
    if power & 0x02:
        components.append("DetectorBoard")
    if heater[2] or heater[3]:
        components.append("MechanismHeater")
    if heater[0] or heater[1]:
        components.append("DetectorHeater")
    # When both mechanism and detector boards are powered, their shared +5 V
    # support load is present whether or not either heater output is enabled.
    if (power & 0x03) == 0x03:
        components.append("PoweredHeatedBoards")
    expected = {f"CH{channel}": 0.0 for channel in range(1, 4)}
    for component in components:
        for channel, current_ma in limits.OB_CURRENT_COMPONENTS_MA[component].items():
            expected[channel] += current_ma

    if moving:
        if motor_current is not None:
            delta = limits.MOTOR_MOVING_CURRENT_COMPONENTS_MA.get(
                motor_current, {"CH1": limits.MOTOR_MOVING_CURRENT_MA.get(motor_current, 0.0)}
            )
            for channel, current_ma in delta.items():
                expected[channel] = expected.get(channel, 0.0) + current_ma
        else:
            for channel, current_ma in limits.OB_CURRENT_COMPONENTS_MA["Moving"].items():
                expected[channel] = expected.get(channel, 0.0) + current_ma
    return expected


def gate_script_control() -> None:
    """Honor Pause/Stop before a script action that runs outside CommandChecks."""
    try:
        from widget_modules import ui_runtime_controller
    except Exception:
        return
    if ui_runtime_controller.is_aborted():
        raise ui_runtime_controller.ScriptAbortRequested
    ui_runtime_controller.wait_while_paused()


def switch_psu(
    psu_port: Any, *, enabled: bool, psu_lock: Any = None, channel: int | None = None
) -> None:
    """Switch PSU outputs, optionally limiting the switch to one channel.

    PSU control is serially shared with the GUI monitor path. Wait (bounded) for
    the lock rather than silently skipping the switch, since a dropped switch
    would leave the PSU in the wrong state. When channel is omitted, switch all
    outputs; otherwise, switch only the specified channel.
    """
    gate_script_control()
    if psu_port is None:
        info_log.info("PSU unavailable; skipping PSU channel switch.")
        return
    if psu_lock is not None:
        try:
            acquired = psu_lock.acquire(timeout=5.0)
        except AttributeError:
            acquired = False
        if not acquired:
            event_log.error("PSU switch (enabled=%s) failed: PSU lock was busy for 5s.", enabled)
            raise TimeoutError("PSU switch failed because the PSU lock was busy for 5 seconds")
        try:
            if channel is None:
                psu.switch_all_psu_channels(psu_port, 1 if enabled else 0)
            else:
                psu.switch_psu_channel(psu_port, channel=channel, state=enabled)
        finally:
            psu_lock.release()
        return
    if channel is None:
        psu.switch_all_psu_channels(psu_port, 1 if enabled else 0)
    else:
        psu.switch_psu_channel(psu_port, channel=channel, state=enabled)


def set_psu_channel_voltage(
    psu_port: Any, *, channel: int, voltage: float, psu_lock: Any = None
) -> None:
    """Set one PSU channel voltage while serializing access with the monitor."""
    gate_script_control()
    if psu_port is None:
        raise RuntimeError("Cannot set PSU voltage: PSU is unavailable")
    if channel not in range(1, 5):
        raise ValueError(f"Invalid PSU channel: {channel}")
    if voltage < 0:
        raise ValueError(f"Invalid PSU voltage: {voltage}")

    acquired = False
    if psu_lock is not None:
        try:
            acquired = psu_lock.acquire(timeout=5.0)
        except TypeError:
            acquired = psu_lock.acquire()
        if not acquired:
            raise TimeoutError("PSU voltage change failed because the PSU lock was busy for 5 seconds")
    try:
        psu_port.write(f"V{channel} {voltage:g}\r\n".encode("utf-8"))
    finally:
        if acquired:
            psu_lock.release()


def request_hk(port: Any, checkpoint: str, *, port_lock: Any = None, transaction_runner: Any = None) -> Any:
    """Request and validate that a housekeeping response was received."""
    gate_script_control()
    if transaction_runner is not None:
        response = transaction_runner(repeat, tc.hk_request)
    else:
        lock_ctx = port_lock if port_lock is not None else nullcontext()
        with lock_ctx:
            response = repeat(port, tc.hk_request)
    if response in (None, "ERROR"):
        raise RuntimeError(f"No valid HK response received during {checkpoint}")
    return response


def request_science(
    port: Any,
    checkpoint: str,
    *,
    port_lock: Any = None,
    transaction_runner: Any = None,
    check_temperatures: bool = True,
) -> Any:
    """Request and validate SCI telemetry, optionally deferring temperature limits."""
    gate_script_control()
    if transaction_runner is not None:
        response = transaction_runner(repeat, tc.sci_request, 4, 20)
    else:
        lock_ctx = port_lock if port_lock is not None else nullcontext()
        with lock_ctx:
            response = repeat(port, tc.sci_request, 4, 20)
    return check_science(response, label=checkpoint, check_temperatures=check_temperatures)


def _read_psu_snapshot_from_queue() -> dict[str, tuple[float, float]]:
    """Return the latest valid PSU sample from the monitor cache.

    This is the EB contract: the monitor thread publishes the same sample to both
    the plotting queue and the latest-PSU cache. Validation must not drain or
    consume the live graph queue; it should read the latest cached sample only.
    """
    latest = eb_packet_utility.get_latest_psu()
    if not isinstance(latest, Mapping):
        return {}

    values: dict[str, tuple[float, float]] = {}
    for channel in range(1, 5):
        voltage = latest.get(f"CH{channel}_V") if channel != 4 else latest.get("PSU_EB_V", latest.get("CH4_V"))
        current = latest.get(f"CH{channel}_I") if channel != 4 else latest.get("PSU_EB_I", latest.get("CH4_I"))
        if voltage is None and current is None:
            continue
        values[f"CH{channel}"] = (
            float(voltage) if voltage is not None else 0.0,
            float(current) if current is not None else 0.0,
        )
    return values


def read_psu_channels(psu_port: Any, psu_lock: Any = None) -> dict[str, tuple[float, float]]:
    """Return the latest PSU telemetry sample from the queue.

    PSU verification paths must not touch the live serial port. The monitor thread is
    the only component allowed to poll the hardware; validation code must consume the
    buffered sample it publishes into ``const.psu_queue``.
    """
    if psu_port is None:
        return _read_psu_snapshot_from_queue()

    if psu_lock is not None:
        event_log.debug("PSU validation using latest cached telemetry; bypassing direct serial read.")

    return _read_psu_snapshot_from_queue()


def log_psu_snapshot(psu_port: Any, label: str, psu_lock: Any = None) -> dict[str, tuple[float, float]]:
    """Read and log PSU channels for operator review."""
    readings = read_psu_channels(psu_port, psu_lock)
    if not readings:
        event_log.info("%s: PSU snapshot unavailable.", label)
    else:
        event_log.info("%s: %s", label, readings)
    return readings


def log_thermal_status(response: Any) -> None:
    """Log the thermal response values used by the OB qualification."""
    info_log.info(
        "OB FFT thermal review: mechanism TRP=%s, motor TRP=%s",
        getattr(response, "MECH_TRP", "N/A"),
        getattr(response, "MOTOR_TRP", getattr(response, "MTR_TRP", "N/A")),
    )


def log_science_measurement(response: Any, label: str) -> None:
    """Log the science channels and temperatures used by the OB qualification."""
    event_log.info(
        "%s: SWIR H/M/L=%s/%s/%s, MWIR H/M/L=%s/%s/%s, SWIR temp=%s, heat sink=%s",
        label,
        response.SWIR_HIGH,
        response.SWIR_MED,
        response.SWIR_LOW,
        response.MWIR_HIGH,
        response.MWIR_MED,
        response.MWIR_LOW,
        response.SWIR_TEMP,
        response.HT_SINK_TEMP,
    )


def report_check(
    label: str,
    errors: list[str],
    readings: dict[str, Any] | None = None,
    *,
    notify_negative: Callable[[str], Any] | None = None,
    notify_positive: Callable[[str], Any] | None = None,
    on_failure: Callable[[str, list[str], dict[str, Any] | None], bool] | None = None,
) -> None:
    """Report a grouped check result, optionally using injected UI notifications."""
    if errors:
        numbered = [f"{index + 1}. {error.strip()}" for index, error in enumerate(errors)]
        message = f"{label} verification failed: {len(errors)} error{'s' if len(errors) != 1 else ''}:\n" + "\n".join(
            numbered
        )
        if notify_negative is not None:
            notify_negative(message)
        if on_failure is not None and on_failure(label, list(errors), readings):
            return
        raise AssertionError(message)
    message = f"{label} verification passed" + (f": {readings}" if readings else "")
    info_log.info(message)
    if notify_positive is not None:
        notify_positive(message)


def _assert_no_errors(label: str, errors: list[str]) -> None:
    if errors:
        raise AssertionError(
            f"{label} verification failed:\n" + "\n".join(f"{i + 1}. {e}" for i, e in enumerate(errors))
        )


def check_hk(
    response: Any,
    *,
    label: str = "HK",
    check_model: bool = False,
    log_result: bool = True,
) -> Any:
    """Validate that a response is a usable, error-free HK packet."""
    if response in (None, "ERROR"):
        raise RuntimeError(f"No valid HK response received during {label}")
    missing = [name for name in limits.HK_REQUIRED_FIELDS if not hasattr(response, name)]
    errors = [f"missing field {name}" for name in missing]
    if getattr(response, "ERROR_BYTE", 0):
        errors.append(f"ERROR_BYTE={response.ERROR_BYTE}")
    if getattr(response, "ERROR_MTR", 0):
        errors.append(f"ERROR_MTR={response.ERROR_MTR}")
    if getattr(response, "CMD_ID", 0):
        errors.append(f"CMD_ID={response.CMD_ID}")
    if check_model:
        expected = getattr(config, "EXP_MODEL_ID", None)
        actual = getattr(response, "MOD_ID", getattr(response, "MODEL_ID", None))
        if expected is not None and actual not in (None, expected):
            errors.append(f"model={actual}, expected {expected}")
    _assert_no_errors(label, errors)
    if log_result:
        info_log.info("%s passed: %s", label, response)
    return response


def check_science(response: Any, *, label: str = "SCI", check_temperatures: bool = True) -> Any:
    """Validate a complete SCI packet, optionally deferring temperature limits."""
    if response in (None, "ERROR"):
        raise RuntimeError(f"No valid science response received during {label}")
    missing = [name for name in limits.SCI_REQUIRED_FIELDS if not hasattr(response, name)]
    errors = [f"missing field {name}" for name in missing]
    if getattr(response, "ERROR_BYTE", 0):
        errors.append(f"ERROR_BYTE={response.ERROR_BYTE}")
    if getattr(response, "CMD_ID", None) != 0x0F:
        errors.append(f"CMD_ID={getattr(response, 'CMD_ID', None)}, expected 15")
    if check_temperatures:
        for field, (minimum, maximum) in limits.DARK_SCIENCE_TEMPERATURE_LIMITS.items():
            value = getattr(response, field, None)
            if value is not None:
                _check_science_temperature(field, value, minimum, maximum, errors)
    for field, (minimum, maximum) in limits.DARK_SCIENCE_LIMITS.items():
        value = getattr(response, field, None)
        if value is not None and not minimum <= value <= maximum:
            errors.append(f"{field}={value}, expected {minimum}..{maximum}")
    _assert_no_errors(label, errors)
    info_log.info("%s passed: %s", label, response)
    return response


def _check_science_temperature(field: str, raw: int, minimum: float, maximum: float, errors: list[str]) -> None:
    temp_c = hk_conversions.sci_temperature_to_c(field, raw)
    if not minimum <= temp_c <= maximum:
        errors.append(f"{field}={temp_c:.2f} C (raw {raw}), expected {minimum}..{maximum} C")


def check_motor_params(response: Any, expected: tuple[int, int, int, int] = limits.MOTOR_NOMINAL_PARAMS) -> None:
    """Ensure motor configuration matches the expected qualification values."""
    actual = tuple(
        getattr(response, field, None) for field in ("MTR_CURRENT", "MTR_GUARD_SELECT", "MTR_CHOP", "MTR_SPEED")
    )
    if actual != expected:
        raise AssertionError(f"Motor parameters do not match: got {actual}, expected {expected}")


def check_ob_V_rails(
    response: Any,
    errors: list[str],
    ob_3v3_lim: tuple[int, int] = limits.LIM_3V3_ADU,
    ob_1v5_lim: tuple[int, int] = limits.LIM_1V5_ADU,
) -> None:
    """Append an error if the HK ob Voltages are not in bounds."""
    ob_3v3_min, ob_3v3_max = ob_3v3_lim
    ob_1v5_min, ob_1v5_max = ob_1v5_lim
    ob_3v3 = getattr(response, "HK_V_3V3", None)
    ob_1v5 = getattr(response, "HK_V_1V5", None)
    if ob_3v3 is None:
        errors.append("HK_OB_3V3 not present in HK response")
        return
    if (ob_3v3 >> 4) < ob_3v3_min or (ob_3v3 >> 4) > ob_3v3_max:
        errors.append(f"HK_V_3V3 not in bounds: got {ob_3v3 >> 4} ADU, expected {ob_3v3_min} : {ob_3v3_max} ")

    if ob_1v5 is None:
        errors.append("HK_OB_1v5 not present in HK response")
        return
    if (ob_1v5 >> 4) < ob_1v5_min or (ob_1v5 >> 4) > ob_1v5_max:
        errors.append(f"HK_V_1v5 not in bounds: got {ob_1v5 >> 4} ADU, expected {ob_1v5_min} : {ob_1v5_max} ")


def check_ob_trps(
    response: Any,
    errors: list[str],
    lim_trp_pcb: tuple[int, int] = limits.LIM_TPR_ADU_PCB,
    lim_trp_mtr: tuple[int, int] = limits.LIM_TPR_ADU_MTR,
) -> None:
    """Append an error if the HK ob Voltages are not in bounds."""
    lim_trp_pcb_min, lim_trp_pcb_max = lim_trp_pcb
    lim_trp_mtr_min, lim_trp_mtr_max = lim_trp_mtr

    dig_trp = getattr(response, "DIGITAL_TRP", None)
    det_trp = getattr(response, "DETEC_TRP", None)
    mech_trp = getattr(response, "MECH_TRP", None)
    mtr_trp = getattr(response, "MOTOR_TRP", None)
    if dig_trp is None:
        errors.append("DIG TRP not present in HK response")
        return
    if (dig_trp >> 4) < lim_trp_pcb_min or (dig_trp >> 4) > lim_trp_pcb_max:
        errors.append(f"DIG TRP not in bounds: got {dig_trp >> 4} ADU, expected {lim_trp_pcb_min} : {lim_trp_pcb_max} ")

    if det_trp is None:
        errors.append("DET TRP not present in HK response")
        return
    if (det_trp >> 4) < lim_trp_pcb_min or (det_trp >> 4) > lim_trp_pcb_max:
        errors.append(f"DET TRP not in bounds: got {det_trp >> 4} ADU, expected {lim_trp_pcb_min} : {lim_trp_pcb_max} ")

    if mech_trp is None:
        errors.append("MECH TRP not present in HK response")
        return
    if (mech_trp >> 4) < lim_trp_pcb_min or (mech_trp >> 4) > lim_trp_pcb_max:
        errors.append(
            f"MECH TRP not in bounds: got {mech_trp >> 4} ADU, expected {lim_trp_pcb_min} : {lim_trp_pcb_max} "
        )

    if mtr_trp is None:
        errors.append("MOTOR TRP not present in HK response")
        return
    if (mtr_trp >> 4) < lim_trp_mtr_min or (mtr_trp >> 4) > lim_trp_mtr_max:
        errors.append(
            f"MOTOR TRP not in bounds: got {mtr_trp >> 4} ADU, expected {lim_trp_mtr_min} : {lim_trp_mtr_max} "
        )


def check_thermal(response: Any, command: tuple[bool, bool, bool, bool, bool]) -> None:
    """Verify heater HK state against the last heater-control command."""
    _, detector_manual, detector_auto, mechanism_manual, mechanism_auto = command
    status = getattr(response, "THRM_STATUS", None)
    mapping = {"DM": detector_manual, "DA": detector_auto, "MM": mechanism_manual, "MA": mechanism_auto}
    errors = [
        f"THRM_STATUS.{field}={int(bool(getattr(status, field, 0)))}, expected {int(expected)}"
        for field, expected in mapping.items()
        if bool(getattr(status, field, 0)) != expected
    ]
    # HMS/HDS report the corresponding physical heater output.
    for field, expected in (("HMS", mechanism_manual or mechanism_auto), ("HDS", detector_manual or detector_auto)):
        if bool(getattr(status, field, 0)) != expected:
            errors.append(f"THRM_STATUS.{field}={int(bool(getattr(status, field, 0)))}, expected {int(expected)}")
    _assert_no_errors("heater state", errors)


def check_powered(response: Any, command: int) -> None:
    """Verify board enable state against the last power-control command."""
    actual = getattr(response, "PWR_STAT", None)
    if actual != command:
        raise AssertionError(f"Power state mismatch: PWR_STAT={actual}, expected {command}")


def check_current_profile(
    readings: dict[str, tuple[float, float]],
    state: Any,
    *,
    motor_current: int | None = None,
    allow_unavailable: bool = False,
    errors: list[str] | None = None,
) -> dict[str, float]:
    """Validate OB rail currents against a named measurement profile.

    ``readings`` uses the PSU reader's ``{channel: (volts, amps)}`` format. The
    expected CH1-CH3 totals are derived from the supplied hardware state; TEC
    is therefore never included. Every rail uses a flat +/-10 mA tolerance
    (``limits.PSU_CURRENT_TOLERANCE_MA``).
    """
    expected = calculate_ob_current_profile(state, motor_current=motor_current)
    label = state if isinstance(state, str) else "OB current state"
    if not readings:
        if allow_unavailable:
            event_log.warning("%s current check skipped: PSU unavailable", label)
            return {}
        message = f"{label} current check failed: PSU readings unavailable"
        if errors is not None:
            errors.append(message)
            return {}
        raise AssertionError(message)

    validation_errors = [] if errors is None else errors
    measured: dict[str, float] = {}
    for channel, expected_ma in expected.items():
        reading = readings.get(channel)
        if reading is None:
            validation_errors.append(f"{channel} is unavailable")
            continue
        measured_ma = float(reading[1]) * 1000.0
        measured[channel] = measured_ma
        tolerance = limits.PSU_CURRENT_TOLERANCE_MA
        if abs(measured_ma - expected_ma) > tolerance:
            validation_errors.append(
                f"{channel}={measured_ma:.2f} mA, expected {expected_ma:.2f} +/- {tolerance:.2f} mA"
            )
    if errors is not None:
        return measured
    _assert_no_errors(f"{label} OB rail current", validation_errors)
    info_log.info("%s OB rail current passed: %s", label, measured)
    return measured


def check_current_profile_channel(
    readings: dict[str, tuple[float, float]],
    state: Any,
    channel: str,
    *,
    errors: list[str] | None = None,
    allow_unavailable: bool = False,
) -> float | None:
    """Validate one rail for the active state and return its measured mA value."""
    measured = check_current_profile(
        readings,
        state,
        allow_unavailable=allow_unavailable,
        errors=errors,
    )
    return measured.get(channel)


def mechanism_current_adu(response: Any) -> int | None:
    """Return the mechanism-current ADC count represented by HK_MECH_CUR.

    HK_MECH_CUR is a fixed-point field: the ADC count is stored in the upper
    12 bits. The count is the net current reported by the monitor. When the
    mechanism or detector heaters are enabled, their combined negative
    contribution is already included in that net reading and must not be
    subtracted a second time. The ADC clips negative net currents at zero.
    """
    raw = getattr(response, "HK_MECH_CUR", None)
    if raw is None:
        return None
    return int(raw) >> 4


def check_mechanism_current_zero(
    response: Any,
    errors: list[str],
    max_adu: int = limits.MECHANISM_CURRENT_ZERO_MAX_ADU,
) -> None:
    """Append an error if the net mechanism current is not near zero in ADU."""
    current_adu = mechanism_current_adu(response)
    if current_adu is None:
        errors.append("HK_MECH_CUR not present in HK response")
        return
    if current_adu > max_adu:
        errors.append(f"HK_MECH_CUR not at zero: got {current_adu} ADU, expected <= {max_adu} ADU")


def check_mechanism_idle(
    response: Any,
    errors: list[str],
    max_adu: int = limits.MECHANISM_CURRENT_IDLE_ADU,
) -> None:
    """Append an error if the net mechanism current exceeds the expected idle threshold."""
    current_adu = mechanism_current_adu(response)
    if current_adu is None:
        errors.append("HK_MECH_CUR not present in HK response")
        return
    if current_adu > max_adu:
        errors.append(f"HK_MECH_CUR above idle limit: got {current_adu} ADU, expected <= {max_adu} ADU")


def check_motor_hold_current(
    response: Any,
    errors: list[str],
    readings: dict[str, tuple[float, float]] | None = None,
    min_adu: int = limits.MECHANISM_CURRENT_ZERO_MAX_ADU + 1,
) -> None:
    """Append errors when HK or PSU telemetry shows no motor holding current."""
    current_adu = mechanism_current_adu(response)
    if current_adu is None:
        errors.append("HK_MECH_CUR not present in HK response")
    elif current_adu < min_adu:
        errors.append(f"HK_MECH_CUR hold current too low: got {current_adu} ADU, expected >= {min_adu} ADU")
    if readings is None:
        return
    reading = readings.get("CH1")
    if reading is None:
        errors.append("CH1 is unavailable for motor hold current")
        return
    measured_ma = float(reading[1]) * 1000.0
    minimum_ma = 4.0
    if measured_ma <= minimum_ma:
        errors.append(f"CH1 motor hold current too low: got {measured_ma:.2f} mA, expected > {minimum_ma:.2f} mA")


def check_motor_stopped(response: Any, checkpoint: str) -> None:
    """Validate that a motor command completed without errors or motion flags."""
    if response.ERROR_MTR != 0 or response.MTR_FLAGS.MOVING or response.MTR_FLAGS.HOMING:
        raise AssertionError(
            f"{checkpoint} did not finish cleanly: errors={response.ERROR_MTR}, flags={vars(response.MTR_FLAGS)}"
        )


def check_thermal_response(
    response: Any,
    initial_values: dict[str, Any],
    heater_name: str,
    errors: list[str],
    min_increase_adu: int = limits.HEATER_THERMAL_RESPONSE_MIN_ADU,
) -> None:
    """Append an error unless every monitored TRP rose by at least *min_increase_adu* (12-bit ADU)."""
    if not initial_values:
        event_log.warning(
            "%s heater thermal response cannot be compared because no baseline was supplied.", heater_name
        )
        return
    values = {field: getattr(response, field, None) for field in initial_values}
    if any(value is None for value in values.values()) or any(value is None for value in initial_values.values()):
        event_log.warning("%s heater thermal response cannot be compared because a TRP value is missing.", heater_name)
        return
    increases = {field: (values[field] >> 4) - (initial_values[field] >> 4) for field in initial_values}
    if not all(increase >= min_increase_adu for increase in increases.values()):
        errors.append(f"{heater_name} heater did not raise every TRP by {min_increase_adu} ADU (changes: {increases})")


def check_science_offsets(response: Any, swir_offset: int, mwir_offset: int, errors: list[str]) -> None:
    """Append an error when the requested science offsets were not applied."""
    if response.SWIR_OFFSET != swir_offset or response.MWIR_OFFSET != mwir_offset:
        errors.append(
            f"SCI offsets not applied: SWIR={response.SWIR_OFFSET}, MWIR={response.MWIR_OFFSET}; "
            f"expected SWIR={swir_offset}, MWIR={mwir_offset}"
        )


def check_ob_state(
    response: Any,
    readings: dict[str, tuple[float, float]],
    state: str,
    *,
    allow_psu_unavailable: bool = False,
    expected_motor_params: tuple[int, int, int, int] | None = None,
) -> dict[str, float]:
    """Run the same complete validation for any EB-style OB state."""
    expected = limits.OB_STATE_EXPECTATIONS.get(state)
    if expected is None:
        raise KeyError(f"Unknown OB state: {state}")
    check_hk(response, label=state)
    check_powered(response, expected["power"])
    check_thermal(response, expected["heater"])
    if expected["power"] & 0x01:
        check_motor_params(response, expected_motor_params or limits.MOTOR_NOMINAL_PARAMS)
    moving = bool(getattr(getattr(response, "MTR_FLAGS", None), "MOVING", 0))
    if state != "Moving" and moving != expected["moving"]:
        raise AssertionError(f"{state} motor moving={int(moving)}, expected {int(expected['moving'])}")
    errors: list[str] = []
    if state == "Moving":
        if moving:
            check_motor_hold_current(response, errors)
        elif response and getattr(response, "PWR_STAT", 0) & 0x01 == 0:
            check_mechanism_current_zero(response, errors)
        else:
            check_mechanism_idle(response, errors)
        measured = {channel: float(value[1]) * 1000.0 for channel, value in readings.items()}
        _assert_no_errors(f"{state} consumption", errors)
        return measured
    if moving:
        check_motor_hold_current(response, errors)
    elif expected["power"] & 0x01 == 0:
        check_mechanism_current_zero(response, errors)
    else:
        check_mechanism_idle(response, errors)
    measured = check_current_profile(
        readings,
        response,
        motor_current=getattr(response, "MTR_CURRENT", None) if moving else None,
        allow_unavailable=allow_psu_unavailable,
        errors=errors,
    )
    _assert_no_errors(f"{state} consumption", errors)
    return measured


def _movement_peak_readings(samples: list[dict[str, tuple[float, float]]]) -> dict[str, tuple[float, float]]:
    """Return the peak-current sample for each rail across the whole motion."""
    if not samples:
        return {}
    channels = sorted({channel for sample in samples for channel in sample})
    aggregate: dict[str, tuple[float, float]] = {}
    for channel in channels:
        best_sample: tuple[float, float] | None = None
        best_amplitude = -float("inf")
        for sample in samples:
            reading = sample.get(channel)
            if reading is None:
                continue
            amplitude = abs(float(reading[1]))
            if amplitude > best_amplitude:
                best_amplitude = amplitude
                best_sample = reading
        if best_sample is not None:
            aggregate[channel] = best_sample
    return aggregate


def _movement_stable_readings(samples: list[dict[str, tuple[float, float]]]) -> dict[str, tuple[float, float]]:
    """Return per-rail median readings from the stable portion of a move."""
    if not samples:
        return {}
    channels = sorted({channel for sample in samples for channel in sample})
    aggregate: dict[str, tuple[float, float]] = {}
    for channel in channels:
        readings = [sample[channel] for sample in samples if channel in sample]
        if readings:
            aggregate[channel] = (
                float(statistics.median(float(reading[0]) for reading in readings)),
                float(statistics.median(float(reading[1]) for reading in readings)),
            )
    return aggregate


def check_dark_science(hk: Any, science: Any) -> None:
    """Check dark temperatures, mechanism position, and SWIR/MWIR channels."""
    errors: list[str] = []
    configured_packets = (
        (hk, limits.DARK_HK_TEMPERATURE_LIMITS),
        (science, limits.DARK_SCIENCE_LIMITS),
    )
    for packet, configured in configured_packets:
        for field, (minimum, maximum) in configured.items():
            value = getattr(packet, field, None)
            if value is None:
                errors.append(f"{field} is missing")
            elif not minimum <= value <= maximum:
                errors.append(f"{field}={value}, expected {minimum}..{maximum}")
    for field, (minimum, maximum) in limits.DARK_SCIENCE_TEMPERATURE_LIMITS.items():
        value = getattr(science, field, None)
        if value is None:
            errors.append(f"{field} is missing")
        else:
            _check_science_temperature(field, value, minimum, maximum, errors)
    _assert_no_errors("dark science", errors)


@dataclass
class CommandChecks:
    """Issue OB commands and verify telemetry against the last command sent."""

    port: Any
    hk_request: Callable[[Any], Any] = tc.hk_request
    sleep: Callable[[float], None] = time.sleep
    current_reader: Callable[[], dict[str, tuple[float, float]]] | None = None
    # Optional factory that returns a progress-notifier object (with ``update(msg)``
    # and ``finish()`` methods) for long waits such as homing. Left ``None`` this
    # module stays GUI-free; callers inject a UI-aware factory (e.g. ``ProgressNotifier``).

    progress_factory: Callable[[str], Any] | None = None
    transaction_runner: Callable[..., Any] | None = None
    # Shared serial-port lock. When set, every HK transaction this instance issues
    # is serialized against other threads/scripts using the same lock instance.
    port_lock: Any = None
    # The OB interface is expected to boot with all heaters and board power
    # disabled. Keep these as real expected states so the first HK sample is
    # validated instead of skipping the checks because no command was sent by
    # this CommandChecks instance yet.
    last_heater: tuple[bool, bool, bool, bool, bool] = (False, False, False, False, False)
    last_power: int = 0
    last_motor_params: tuple[int, int, int, int] = limits.MOTOR_BOOT_PARAMS
    last_state_readings: dict[str, float] | None = None
    last_motion_report: dict[str, float | int] | None = None

    def _repeat(self, cmd_func: Callable[..., Any], *args: Any) -> Any:
        """Issue a command through cmd_repeat, serialized against self.port_lock."""
        self._raise_if_aborted()
        self._wait_if_paused()
        if self.transaction_runner is not None:
            return self.transaction_runner(repeat, cmd_func, *args)
        lock_ctx = self.port_lock if self.port_lock is not None else nullcontext()
        with lock_ctx:
            return repeat(self.port, cmd_func, *args)

    def hk(
        self,
        label: str,
        *,
        check_model: bool = False,
        nominal_motor: bool = False,
        expected_motor_params: tuple[int, int, int, int] | None = None,
        log_result: bool = True,
        check_power_state: bool = True,
    ) -> Any:
        self._raise_if_aborted()
        self._wait_if_paused()
        if self.transaction_runner is not None:
            hk_response = self.transaction_runner(repeat, self.hk_request)
        else:
            lock_ctx = self.port_lock if self.port_lock is not None else nullcontext()
            with lock_ctx:
                hk_response = repeat(self.port, self.hk_request)
        return self._validate_hk(
            hk_response,
            label=label,
            check_model=check_model,
            nominal_motor=nominal_motor,
            expected_motor_params=expected_motor_params,
            log_result=log_result,
            check_power_state=check_power_state,
        )

    def _validate_hk(
        self,
        hk_response: Any,
        *,
        label: str,
        check_model: bool = False,
        nominal_motor: bool = False,
        expected_motor_params: tuple[int, int, int, int] | None = None,
        log_result: bool = True,
        check_power_state: bool = True,
    ) -> Any:
        """Validate an HK response already obtained by an owned transaction."""
        response = check_hk(hk_response, label=label, check_model=check_model, log_result=log_result)
        errors: list[str] = []
        check_ob_V_rails(response, errors)
        check_ob_trps(response, errors)
        _assert_no_errors(label, errors)
        check_thermal(response, self.last_heater)
        if check_power_state:
            check_powered(response, self.last_power)
        if expected_motor_params is not None:
            check_motor_params(response, expected_motor_params)
        elif nominal_motor:
            check_motor_params(response, limits.MOTOR_NOMINAL_PARAMS)
        elif self.last_motor_params is not None:
            check_motor_params(response, self.last_motor_params)
        return response

    def heater(self, *command: bool, label: str) -> Any:
        if len(command) != 5:
            raise ValueError("heater command must contain five boolean parameters")
        self.last_heater = (command[0], command[1], command[2], command[3], command[4])
        self._repeat(tc.heater_control, *command)
        return self.hk(label)

    def power(self, command: int, *, label: str) -> Any:
        self.last_power = command
        self._raise_if_aborted()
        if self.transaction_runner is not None:
            last_response = self.transaction_runner(_power_transition_transaction, command)
        else:
            lock_ctx = self.port_lock if self.port_lock is not None else nullcontext()
            with lock_ctx:
                last_response = _power_transition_transaction(self.port, command)

        actual = getattr(last_response, "PWR_STAT", None) if last_response not in (None, "ERROR") else None
        if actual != command:
            raise AssertionError(
                f"{label}: PWR_STAT did not reach {command} within "
                f"{POWER_TRANSITION_TIMEOUT_S:.1f} s (last value {actual})"
            )
        response = self._validate_hk(last_response, label=label, log_result=False)
        info_log.info("%s: PWR_STAT reached %s", label, command)
        return response

    def set_nominal_motor_params(self) -> Any:
        self._repeat(tc.set_mtr_param, *limits.MOTOR_NOMINAL_PARAMS)
        self.last_motor_params = limits.MOTOR_NOMINAL_PARAMS
        return self.hk("nominal motor parameters", nominal_motor=True)

    def set_motor_current(self, current: int) -> Any:
        """Set and verify a motor-current setting while retaining nominal parameters."""
        params = (
            current,
            limits.MOTOR_NOMINAL_GUARD,
            limits.MOTOR_NOMINAL_CHOPPER,
            limits.MOTOR_NOMINAL_SPEED,
        )
        self._repeat(tc.set_mtr_param, *params)
        self.last_motor_params = params
        time.sleep(2)
        return self.hk(f"motor current {current}", expected_motor_params=params)

    def set_motor_speed(self, speed: int, current: int = limits.MOTOR_NOMINAL_CURRENT) -> Any:
        """Set and verify a motor-speed setting while retaining other parameters."""
        params = (
            current,
            limits.MOTOR_NOMINAL_GUARD,
            limits.MOTOR_NOMINAL_CHOPPER,
            speed,
        )
        self._repeat(tc.set_mtr_param, *params)
        self.last_motor_params = params
        time.sleep(2)
        return self.hk(f"motor speed {speed}", expected_motor_params=params)

    def current(
        self,
        profile: str,
        readings: dict[str, tuple[float, float]],
        *,
        motor_current: int | None = None,
        allow_unavailable: bool = False,
    ) -> dict[str, float]:
        """Run the named OB-only current check."""
        return check_current_profile(
            readings,
            profile,
            motor_current=motor_current,
            allow_unavailable=allow_unavailable,
        )

    def state(
        self,
        state: str,
        readings: dict[str, tuple[float, float]],
        *,
        response: Any | None = None,
        allow_psu_unavailable: bool = False,
    ) -> dict[str, float]:
        """Validate HK state and OB rail currents through one uniform API."""
        if response is None:
            response = self.hk(f"{state} state sample")
        measured = check_ob_state(
            response,
            readings,
            state,
            allow_psu_unavailable=allow_psu_unavailable,
            expected_motor_params=self.last_motor_params,
        )
        self.last_state_readings = measured
        return measured

    def move(
        self,
        *,
        negative: bool,
        steps: int,
        label: str,
        active_state: str | None = None,
        expected_motor_params: tuple[int, int, int, int] | None = None,
        max_duration_s: float | None = None,
        poll_interval_s: float | None = None,
    ) -> Any:
        effective_expected = (
            expected_motor_params
            if expected_motor_params is not None
            else self.last_motor_params or limits.MOTOR_NOMINAL_PARAMS
        )
        before = None
        if active_state is None:
            before = self.hk(
                f"{label} pre-move",
                nominal_motor=False,
                expected_motor_params=effective_expected,
                log_result=False,
            )
        started_at = time.monotonic()
        self._repeat(tc.mtr_mov_neg if negative else tc.mtr_mov_pos, steps)
        started = self.hk(
            f"{label} start",
            nominal_motor=False,
            expected_motor_params=effective_expected,
            log_result=False,
        )
        expected_direction = limits.MOTOR_DIRECTION_NEGATIVE if negative else limits.MOTOR_DIRECTION_POSITIVE
        started_moving = bool(getattr(started.MTR_FLAGS, "MOVING", 0))
        if not started_moving:
            if before is None or getattr(started, "MTR_ABS_STEPS", None) == getattr(before, "MTR_ABS_STEPS", None):
                raise AssertionError(f"{label}: motor did not start moving")
            event_log.info("%s completed before the first post-command HK sample", label)
        if started_moving and getattr(started.MTR_FLAGS, "DIR", None) != expected_direction:
            raise AssertionError(f"{label}: direction={started.MTR_FLAGS.DIR}, expected {expected_direction}")
        finished = self._wait_for_stop(
            label,
            active_state=active_state,
            expected_motor_params=effective_expected,
            initial_response=started,
            poll_interval_s=poll_interval_s,
        )
        elapsed = time.monotonic() - started_at
        if max_duration_s is not None:
            self._assert_elapsed_within_tolerance(label, elapsed, max_duration_s, tolerance_s=2.0)
        relative_steps = int(getattr(finished, "MTR_REL_STEPS", 0) or 0)
        speed_mm_s = self._movement_speed_mm_s(relative_steps, elapsed)
        self.last_motion_report = {
            "absolute_steps": int(getattr(finished, "MTR_ABS_STEPS", 0) or 0),
            "relative_steps": relative_steps,
            "elapsed_s": elapsed,
            "speed_mm_s": speed_mm_s,
        }
        event_log.info(
            "%s movement complete in %.2f s; travelled %.3f mm; speed %.3f mm/s",
            label,
            elapsed,
            self._movement_distance_mm(relative_steps),
            speed_mm_s,
        )
        self._check_stopped(finished, label)

        return finished

    def move_to_absolute_position(
        self,
        target_position: int,
        *,
        label: str,
        position_tolerance: int = limits.MOTOR_POSITION_TOLERANCE,
        expected_motor_params: tuple[int, int, int, int] | None = None,
        max_duration_s: float | None = None,
    ) -> Any:
        """Move to an absolute motor position and verify the final position."""
        before = self.hk(
            f"{label} pre-move",
            nominal_motor=expected_motor_params is None,
            expected_motor_params=expected_motor_params,
        )
        current_position = before.MTR_ABS_STEPS
        if current_position == target_position:
            return before
        negative = target_position < current_position
        finished = self.move(
            negative=negative,
            steps=abs(target_position - current_position),
            label=label,
            expected_motor_params=expected_motor_params,
            max_duration_s=max_duration_s,
        )
        if finished.MTR_ABS_STEPS == current_position:
            raise AssertionError(f"{label}: motor did not move; position remained {current_position}")
        if abs(finished.MTR_ABS_STEPS - target_position) > position_tolerance:
            raise AssertionError(f"{label}: position={finished.MTR_ABS_STEPS}, expected {target_position}")
        return finished

    def home(
        self,
        *,
        calibration: bool,
        outer: bool,
        label: str,
        active_state: str | None = None,
        expected_motor_params: tuple[int, int, int, int] | None = None,
        max_duration_s: float | None = None,
    ) -> Any:
        effective_expected = (
            expected_motor_params
            if expected_motor_params is not None
            else self.last_motor_params or limits.MOTOR_NOMINAL_PARAMS
        )
        before = self.hk(
            f"{label} pre-home",
            nominal_motor=False,
            expected_motor_params=effective_expected,
            log_result=False,
        )
        started_at = time.monotonic()
        self._repeat(tc.mtr_homing, calibration, outer)
        finished = self._wait_for_stop(
            label,
            active_state=active_state,
            timeout_s=max(limits.MOTOR_HOME_TIMEOUT_S, max_duration_s or 0),
            expected_motor_params=effective_expected,
        )
        elapsed = time.monotonic() - started_at
        if max_duration_s is not None:
            self._assert_elapsed_within_tolerance(label, elapsed, max_duration_s, tolerance_s=2.0)
        relative_steps = int(getattr(finished, "MTR_REL_STEPS", 0) or 0)
        speed_mm_s = self._movement_speed_mm_s(relative_steps, elapsed)
        event_log.info(
            "%s homing complete in %.2f s; travelled %.3f mm; speed %.3f mm/s",
            label,
            elapsed,
            self._movement_distance_mm(relative_steps),
            speed_mm_s,
        )
        self._check_stopped(finished, label)
        flags = finished.MTR_FLAGS
        stop = "OUTER" if outer else "BASE"
        errors = []
        if getattr(flags, "DIR", None) != 0:
            errors.append(f"final direction={getattr(flags, 'DIR', None)}, expected 0")
        if not getattr(flags, stop, 0):
            errors.append(f"{stop} stop is not asserted")
        expected_position = limits.MOTOR_OUTER_POSITION if outer else limits.MOTOR_BASE_POSITION
        if calibration and abs(finished.MTR_ABS_STEPS - expected_position) > limits.MOTOR_POSITION_TOLERANCE:
            errors.append(f"position={finished.MTR_ABS_STEPS}, expected {expected_position}")
        _assert_no_errors(label, errors)
        relative = getattr(finished, "MTR_REL_STEPS", finished.MTR_ABS_STEPS - before.MTR_ABS_STEPS)
        self.last_motion_report = {
            "absolute_steps": int(getattr(finished, "MTR_ABS_STEPS", 0) or 0),
            "relative_steps": int(relative or 0),
            "elapsed_s": elapsed,
            "speed_mm_s": speed_mm_s,
        }
        event_log.info(
            "%s homing completed in %.2f s; relative steps=%s; absolute steps=%s",
            label,
            elapsed,
            relative,
            finished.MTR_ABS_STEPS,
        )
        return finished

    @staticmethod
    def _movement_distance_mm(steps: int) -> float:
        """Convert absolute motor steps into travelled distance in mm."""
        return abs(float(steps)) / 320.0

    @staticmethod
    def _movement_speed_mm_s(steps: int, elapsed_s: float) -> float:
        """Compute movement speed in mm/s from step count and elapsed time."""
        elapsed = float(elapsed_s)
        if elapsed <= 0:
            return 0.0
        return CommandChecks._movement_distance_mm(steps) / elapsed

    @staticmethod
    def _assert_elapsed_within_tolerance(
        label: str,
        elapsed_s: float,
        expected_duration_s: float,
        tolerance_s: float = 2.0,
    ) -> float:
        """Check elapsed time against a nominal duration with a symmetric tolerance."""
        delta = abs(float(elapsed_s) - float(expected_duration_s))
        if delta > tolerance_s:
            raise AssertionError(
                f"{label}: elapsed {float(elapsed_s):.2f} s is outside "
                f"{float(expected_duration_s) - tolerance_s:.2f}..{float(expected_duration_s) + tolerance_s:.2f} s "
                f"(delta {delta:.2f} s)"
            )
        return float(elapsed_s)

    @staticmethod
    def _raise_if_aborted() -> None:
        """Raise the script abort exception when the UI requests a stop."""
        try:
            from widget_modules import ui_runtime_controller
        except Exception:
            return
        if ui_runtime_controller.is_aborted():
            raise ui_runtime_controller.ScriptAbortRequested

    @staticmethod
    def _wait_if_paused() -> None:
        """Block before issuing the next OB command while the UI pause is active."""
        try:
            from widget_modules import ui_runtime_controller
        except Exception:
            return
        ui_runtime_controller.wait_while_paused()

    def _wait_for_stop(
        self,
        label: str,
        *,
        active_state: str | None = None,
        timeout_s: float = limits.MOTOR_HOME_TIMEOUT_S,
        expected_motor_params: tuple[int, int, int, int] | None = None,
        initial_response: Any | None = None,
        poll_interval_s: float | None = None,
    ) -> Any:
        deadline = time.monotonic() + timeout_s
        started_at = time.monotonic()
        response = initial_response or self.hk(
            label,
            nominal_motor=expected_motor_params is None,
            expected_motor_params=expected_motor_params,
            log_result=False,
        )
        active_samples: list[dict[str, tuple[float, float]]] = []
        active_responses: list[Any] = []

        def check_active_sample(current_response: Any) -> None:
            self._raise_if_aborted()
            if active_state is None:
                return
            if not (
                getattr(current_response.MTR_FLAGS, "MOVING", 0) or getattr(current_response.MTR_FLAGS, "HOMING", 0)
            ):
                return
            readings = self.current_reader() if self.current_reader is not None else {}
            active_samples.append(readings)
            active_responses.append(current_response)

        check_active_sample(response)
        progress = self.progress_factory(f"{label}: waiting for motor to stop") if self.progress_factory else None
        try:
            while getattr(response.MTR_FLAGS, "MOVING", 0) or getattr(response.MTR_FLAGS, "HOMING", 0):
                self._raise_if_aborted()
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for motor during {label}")
                if progress is not None:
                    elapsed = time.monotonic() - started_at
                    progress.update(f"{label}: {elapsed:.0f}s elapsed (timeout {timeout_s:.0f}s)")
                self.sleep(
                    limits.MOTOR_POLL_INTERVAL_S if poll_interval_s is None else max(float(poll_interval_s), 0.01)
                )
                response = self.hk(
                    label,
                    nominal_motor=False,
                    expected_motor_params=(
                        expected_motor_params
                        if expected_motor_params is not None
                        else self.last_motor_params or limits.MOTOR_NOMINAL_PARAMS
                    ),
                    log_result=False,
                )
                check_active_sample(response)

            if active_state is not None:
                if not active_samples:
                    raise AssertionError(f"{label}: no moving HK/PSU sample captured for {active_state} validation")
                # The first moving HK can still be paired with pre-motion PSU
                # telemetry, while the last can see current decay before the HK
                # moving flag clears. Exclude both transition edges whenever a
                # middle-of-motion sample exists, then validate once using the
                # median rail readings and a response captured while moving.
                if len(active_samples) >= 3:
                    stable_samples = active_samples[1:-1]
                    stable_responses = active_responses[1:-1]
                else:
                    stable_samples = active_samples
                    stable_responses = active_responses
                aggregate = _movement_stable_readings(stable_samples)
                representative_response = stable_responses[len(stable_responses) // 2]
                self.state(
                    active_state,
                    aggregate,
                    response=representative_response,
                    allow_psu_unavailable=self.current_reader is None,
                )
                event_log.info(
                    "%s current validated from %d stable moving sample(s) (%d total)",
                    label,
                    len(stable_samples),
                    len(active_samples),
                )
        finally:
            if progress is not None:
                progress.finish()
        return response

    @staticmethod
    def _check_stopped(response: Any, label: str) -> None:
        if (
            getattr(response, "ERROR_MTR", 0)
            or getattr(response.MTR_FLAGS, "MOVING", 0)
            or getattr(response.MTR_FLAGS, "HOMING", 0)
        ):
            raise AssertionError(f"{label} did not finish cleanly")
