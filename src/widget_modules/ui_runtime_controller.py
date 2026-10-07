from __future__ import annotations

# Std library
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
import math
import re
import statistics
import threading
from types import SimpleNamespace
import time
from typing import Any, Callable
from queue import Empty
import logging
import asyncio
from nicegui import app as _nicegui_app
from nicegui import core as _nicegui_core
from nicegui import ui, run
from nicegui.client import Client as _NiceGuiClient

# utilities
from utility_modules import app_theme, eb_interface, eb_packet_utility, ebtcs, hk_conversions, psu, psu_log_utility
from utility_modules.eb_packet_utility import (
    get_latest_hk,
    get_latest_psu,
    get_smoothed_latest_psu,
    set_latest_psu,
    wait_for_fresh_hk,
    wait_for_fresh_psu,
)

# core
from core_modules import tmstruct, constants as const, config
from core_modules.constants import HEATER_INCLUSIVE_STATES, MODEL_CONSUMPTION

from widget_modules import monitoring_limits

info_log = logging.getLogger("info_log")

"""This module contains backend controller functions for the UI, which are responsible for handling user interactions, updating the application state, and coordinating between different UI components and the underlying data. These controllers are designed to be bound to specific UI elements and provide a clear separation of concerns between the UI layout and the logic that drives it."""

_FORCE_PAUSE_EVENT = threading.Event()
_FORCE_PAUSE_DIALOGS: dict[str, Any] = {}
_PAUSE_BANNER_DIALOGS: dict[str, Any] = {}
_OB_SCI_CAPTURE_LOCK = threading.RLock()
_OB_SCI_CAPTURE_ID = 0
_OB_SCI_CAPTURE_ACTIVE = False
_OB_SCI_CAPTURE_CLOSING = False
_OB_SCI_CAPTURE_LABEL = ""
_OB_SCI_CAPTURE_CLOSE_REQUESTED_AT = 0.0
_OB_SCI_CAPTURE_QUIET_PERIOD_S = 1.0
_OB_SCI_STITCHED_SCANS: list[Any] = []
_SCRIPT_PSU_MA_WINDOW_SAMPLES = 5
_SCI_ACQ_TRIGGER_S = 150.0
_SCI_CONSUMPTION_CHECK_DURATION_S = 5.0
_SCI_CONSUMPTION_PER_SAMPLE_TIMEOUT_S = 0.25
_SCI_CONSUMPTION_MIN_SAMPLES = 25
_SCI_CONSUMPTION_MOVING_MIN_FRACTION = 0.50
_POST_REQUIRED_FIELDS = (
    "POST_WARNING_FLAGS",
    "POST_ERROR_FLAGS",
    "NUM_BAD_FLASH_BLOCKS",
    "NUM_BAD_SRAM_BLOCKS",
    "ASW_IMAGE_1_CRC",
    "ASW_IMAGE_2_CRC",
    "ASW_IMAGE_3_CRC",
    "ASW_IMAGE_4_CRC",
    "ASW_IMAGE_5_CRC",
    "BSW_IMAGE_CRC",
    "MEASUREMENT_TABLE_CRC",
)


# ---------------------------------------------------------------------------
# MMS helpers — limit checking
# ---------------------------------------------------------------------------
# region MMS helpers — limit checking


def append_violation(
    reasons: list[str], label: str, value: float | None, limits: tuple[float | None, float | None]
) -> bool:
    if not violates_limits(value, limits):
        return False
    low, high = limits
    reasons.append(f"{label} out of limits: value={value}, limits=({low}, {high})")
    return True


def limit_tuple(value: Any) -> tuple[float | None, float | None]:
    if isinstance(value, tuple) and len(value) == 2:
        return value
    return (None, None)


def set_eb_only_test_mode(state: dict[str, Any], enabled: bool) -> None:
    """Apply session-wide no-OB masks without changing MMS enablement or latches."""
    if enabled == const.MMS_EB_ONLY_TEST_MODE:
        return
    if enabled and state.get("mode") != "EB":
        raise RuntimeError("Select EB mode before enabling EB-only / no-OB test mode.")
    mms_cfg = state.get("mms") or {}
    if is_script_running() or mms_cfg.get("pending") or mms_cfg.get("in_progress"):
        raise RuntimeError("Wait for the running script or MMS shutdown to finish before changing test mode.")
    const.MMS_EB_ONLY_TEST_MODE = enabled
    info_log.warning(
        "EB-only / no-OB test mode %s: OB limits, OB_UNRESPONSIVE and RS485 errors %s; "
        "MMS enablement and EB protection unchanged.",
        "enabled" if enabled else "disabled",
        "masked" if enabled else "returned to normal script-mask settings",
    )


def mms_reasons(hk: Any, limits: dict[str, Any]) -> tuple[list[str], bool, bool]:
    """Evaluate MMS faults; zero OB_HK_ID marks unavailable relayed OB readings."""
    reasons: list[str] = []
    tec_pre_action = False
    ob5v_pre_action = False

    # Check OB_5V_ENABLED and SAFE mode
    instr_status_flags = int(getattr(hk, "INSTRUMENT_STATUS_FLAGS", 0))
    ob_5v_enabled = (
        instr_status_flags >> 10
    ) & 0x1  # Flags are MSB-first: HOMING_COMPLETE is bit 15, OB_5V_ENABLED bit 10
    current_state = int(
        getattr(hk, "CURRENT_OPERATING_STATE", 0) if getattr(hk, "CURRENT_OPERATING_STATE", None) is not None else 0
    )
    # OB power asserts before its first HK arrives; the zero-filled OB block
    # is not an analogue measurement. EB limits and error flags remain active.
    skip_ob_checks = (not ob_5v_enabled) or (current_state == 0x02) or (getattr(hk, "OB_HK_ID", None) == 0)

    for label, field_name, limit_key, tec_field in _MMS_FIELDS:
        if label.startswith("OB ") and (
            skip_ob_checks or const.MMS_MASK_OB_LIMIT_CHECKS or const.MMS_EB_ONLY_TEST_MODE
        ):
            continue  # Skip OB parameter checks if OB is off, in SAFE, or explicitly masked
        violated = append_violation(reasons, label, decoded(hk, field_name), limit_tuple(limits.get(limit_key)))
        tec_pre_action = tec_pre_action or (tec_field and violated)
        ob5v_pre_action = ob5v_pre_action or (label.startswith("OB ") and violated)

    if bool(getattr(hk, "POST_ERROR_FLAGS", 0)):
        reasons.append("POST Error Flags asserted")

    if bool(getattr(hk, "ERROR_FLAGS", 0)):
        ns = getattr(hk, "ERROR_FLAGS_BITS", None)
        eb_flags = sorted(k for k, v in vars(ns).items() if v == 1 and k != "RESERVED") if ns is not None else []
        if const.MMS_MASK_OB_GENERAL_ERROR:
            eb_flags = [f for f in eb_flags if f != "OB_GENERAL_ERROR"]
        if const.MMS_MASK_OB_UNRESPONSIVE or const.MMS_EB_ONLY_TEST_MODE:
            eb_flags = [f for f in eb_flags if f != "OB_UNRESPONSIVE"]
        if const.MMS_MASK_RS485_ERRORS or const.MMS_EB_ONLY_TEST_MODE:
            eb_flags = [f for f in eb_flags if f not in {"RS485_RECEIVE_ERROR", "RS485_TRANSMIT_ERROR"}]
        if eb_flags:
            reasons.append(f"HK Error Flags asserted: {', '.join(eb_flags)}")
        elif ns is None:
            reasons.append("HK Error Flags asserted")

    # OB error details — decoded from OB_LAST_ERROR byte.
    # OB_GENERAL_ERROR on EB is sticky: the OB register may already be 0 by the time
    # MMS fires.  Always include the raw byte value so operators have full context.
    ob_last_error_raw = getattr(hk, "OB_LAST_ERROR", None)
    ob_err_active = (
        sorted(k for k, v in vars(ns).items() if v == 1 and k not in {"UNUSED1", "UNUSED2"})
        if (ns := getattr(hk, "ERRORS", None)) is not None
        else []
    )
    if ob_err_active:
        reasons.append(f"OB Errors: {', '.join(ob_err_active)} (OB_LAST_ERROR=0x{ob_last_error_raw:02X})")
    elif ob_last_error_raw is not None and ob_last_error_raw != 0:
        reasons.append(f"OB_LAST_ERROR=0x{ob_last_error_raw:02X} (no active bits decoded)")

    # Motor error details — decoded from OB_MOTOR_ERROR byte.
    ob_motor_error_raw = getattr(hk, "OB_MOTOR_ERROR", None)
    mtr_err_active = (
        sorted(k for k, v in vars(ns).items() if v == 1 and k != "UNUSED")
        if (ns := getattr(hk, "MTR_ERRORS", None)) is not None
        else []
    )
    if mtr_err_active:
        reasons.append(f"OB Motor Errors: {', '.join(mtr_err_active)} (OB_MOTOR_ERROR=0x{ob_motor_error_raw:02X})")
    elif ob_motor_error_raw is not None and ob_motor_error_raw != 0:
        reasons.append(f"OB_MOTOR_ERROR=0x{ob_motor_error_raw:02X} (no active bits decoded)")

    return reasons, tec_pre_action, ob5v_pre_action


def violates_limits(value: float | None, limits: tuple[float | None, float | None]) -> bool:
    if value is None:
        return False
    low, high = limits
    return (low is not None and value < low) or (high is not None and value > high)


# endregion


# ---------------------------------------------------------------------------
# FDIR Simulator — limit checking
# ---------------------------------------------------------------------------
# region FDIR Simulator — limit checking
ObFdirParameter = monitoring_limits.ObFdirParameter
OB_FDIR_PARAMETERS = monitoring_limits.OB_FDIR_PARAMETERS


def _ob_fdir_display_mode(state: dict[str, Any]) -> str:
    """Return the active HK presentation mode used by logs and dialogs."""
    mode = str(state.get("hk_display_mode") or getattr(_nicegui_app.state, "hk_display_mode", "REAL") or "REAL").upper()
    return "ADU" if mode == "ADU" else "REAL"


def _format_ob_fdir_number(value: Any, unit: str) -> str:
    """Format one FDIR engineering value without unnecessary trailing zeros."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if not math.isfinite(numeric):
        return "N/A"
    if unit == "V":
        return f"{numeric:.3f}"
    if unit == "°C":
        return f"{numeric:.1f}"
    return f"{numeric:g}"


def _ob_fdir_real_value(parameter: ObFdirParameter, adu: int | None) -> float | None:
    """Convert one normalised standalone-OB ADU to its engineering value."""
    if adu is None:
        return None
    packet = SimpleNamespace(**{parameter.hk_field: int(adu)})
    # decode_plot_field is defined later in this module; it is available when
    # telemetry processing calls this helper after module initialisation.
    return decode_plot_field(packet, parameter.hk_field, {"hk_display_mode": "REAL"})


def _format_ob_fdir_measurement(
    state: dict[str, Any],
    parameter: ObFdirParameter,
    adu: int | None,
    severity: str,
) -> str:
    """Format value and limits in the operator-selected REAL/ADU mode."""
    is_alarm = str(severity).lower() == "alarm"
    limit_label = "alarm limits" if is_alarm else "warning limits"

    if _ob_fdir_display_mode(state) == "ADU":
        limits = parameter.alarm_limits if is_alarm else parameter.warning_limits
        value_text = "N/A" if adu is None else str(int(adu))
        return f"{parameter.hk_field}={value_text} ADU, {limit_label}=({int(limits[0])}, {int(limits[1])}) ADU"

    limits_real = parameter.alarm_limits_real if is_alarm else parameter.warning_limits_real
    real_value = _ob_fdir_real_value(parameter, adu)
    value_text = _format_ob_fdir_number(real_value, parameter.real_unit)
    low_text = _format_ob_fdir_number(limits_real[0], parameter.real_unit)
    high_text = _format_ob_fdir_number(limits_real[1], parameter.real_unit)
    return (
        f"{parameter.hk_field}={value_text} {parameter.real_unit}, "
        f"{limit_label}=({low_text}, {high_text}) {parameter.real_unit}"
    )


def _ob_fdir_simulator_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return persistent standalone-OB FDIR simulator state.

    Warning and alarm flags are independently latched, matching the two FDIR
    bitmaps reported by EB telemetry. They remain asserted after the analogue
    value returns in range and are cleared only by reset_ob_fdir_simulator().
    """
    simulator = state.setdefault("ob_fdir_simulator", {})
    simulator.setdefault("enabled", True)
    simulator.setdefault("warning_latched", set())
    simulator.setdefault("alarm_latched", set())
    simulator.setdefault("current_warning", set())
    simulator.setdefault("current_alarm", set())
    simulator.setdefault("latest_adu", {})

    # Be tolerant of state restored from JSON-like storage.
    for key in ("warning_latched", "alarm_latched", "current_warning", "current_alarm"):
        if not isinstance(simulator.get(key), set):
            simulator[key] = set(simulator.get(key) or [])
    if not isinstance(simulator.get("latest_adu"), dict):
        simulator["latest_adu"] = {}
    return simulator


def _ob_fdir_bitmask(flag_names: set[str]) -> int:
    """Build the aggregate bitmap using tmstruct.eb_fdir_flags ordering."""
    mask = 0
    for bit_index, flag_name in enumerate(_FDIR_NAMES):
        if flag_name in flag_names:
            mask |= 1 << bit_index
    return mask


def _attach_simulated_ob_fdir_fields(
    hk: Any,
    warning_latched: set[str],
    alarm_latched: set[str],
) -> None:
    """Expose simulated fields on standalone OB HK packets when mutable."""
    warning_ns = SimpleNamespace(**{name: int(name in warning_latched) for name in _FDIR_NAMES})
    alarm_ns = SimpleNamespace(**{name: int(name in alarm_latched) for name in _FDIR_NAMES})
    try:
        hk.FDIR_WARNING_FLAGS_BITS = warning_ns
        hk.FDIR_ALARM_FLAGS_BITS = alarm_ns
        hk.FDIR_WARNING_FLAGS = _ob_fdir_bitmask(warning_latched)
        hk.FDIR_ALARM_FLAGS = _ob_fdir_bitmask(alarm_latched)
    except (AttributeError, TypeError):
        # Alarm-light simulation still works even for immutable packet objects.
        pass


_OB_VOLTAGE_FDIR_FLAGS = {"FPGA_IO_POWER_SUPPLY", "FPGA_CORE_POWER_SUPPLY"}
_OB_THERMISTOR_FDIR_FLAGS = {
    "DIGITAL_BOARD_TRP",
    "DETECTOR_BOARD_TRP",
    "MECH_BOARD_TRP",
    "MOTOR_TRP",
}


def _ob_psu_protection_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return persistent OB PSU protection/action state."""
    protection = state.setdefault(
        "ob_psu_protection",
        {
            "shutdown_in_progress": False,
            "shutdown_latched": False,
            "last_shutdown_reason": [],
            "last_error": None,
            "pending_prompt_details": set(),
            "dialog": None,
            "dialog_details_label": None,
        },
    )
    protection.setdefault("shutdown_in_progress", False)
    protection.setdefault("shutdown_latched", False)
    protection.setdefault("last_shutdown_reason", [])
    protection.setdefault("last_error", None)
    protection.setdefault("pending_prompt_details", set())
    protection.setdefault("dialog", None)
    protection.setdefault("dialog_details_label", None)
    return protection


def _request_ob_psu_emergency_shutdown(
    state: dict[str, Any],
    logger: Any,
    reasons: list[str],
    *,
    automatic: bool,
) -> bool:
    """Request a non-blocking PSU emergency shutdown once per protection latch."""
    protection = _ob_psu_protection_state(state)
    active_logger = logger if logger is not None else info_log

    if protection.get("shutdown_latched") or protection.get("shutdown_in_progress"):
        return False

    psu_port = state.get("psu_port")
    if psu_port is None:
        message = "OB PSU shutdown requested, but no PSU port is available."
        protection["last_error"] = message
        active_logger.error(message)
        notify(message, color="negative")
        return False

    protection["shutdown_in_progress"] = True
    protection["last_shutdown_reason"] = list(reasons)
    protection["last_error"] = None

    dialog = protection.get("dialog")
    if dialog is not None:
        try:
            dialog.close()
        except Exception:
            pass
    protection["pending_prompt_details"] = set()

    reason_text = "; ".join(reasons) if reasons else "OB protection trigger"
    action_label = "automatic voltage-alarm shutdown" if automatic else "operator-confirmed shutdown"
    active_logger.error("OB PSU %s requested: %s", action_label, reason_text)

    def _shutdown_worker() -> None:
        try:
            lock = state.get("psu_lock")
            lock_ctx = lock if lock is not None else nullcontext()
            with lock_ctx:
                psu.shutdown_psu_outputs(psu_port)
            protection["shutdown_latched"] = True
            active_logger.error("OB PSU emergency shutdown executed: %s", reason_text)
            notify("PSU emergency shutdown executed.\n" + reason_text, color="negative")
        except Exception as exc:
            protection["last_error"] = str(exc)
            active_logger.exception("OB PSU emergency shutdown failed: %s", exc)
            notify(f"PSU emergency shutdown failed: {exc}", color="negative")
        finally:
            protection["shutdown_in_progress"] = False

    threading.Thread(target=_shutdown_worker, name="ob-psu-emergency-shutdown", daemon=True).start()
    return True


def _open_ob_psu_shutdown_dialog(state: dict[str, Any], logger: Any, details: list[str]) -> None:
    """Ask the operator whether warning/error/thermistor conditions should shut down the PSU."""
    if not details:
        return

    protection = _ob_psu_protection_state(state)
    if protection.get("shutdown_latched") or protection.get("shutdown_in_progress"):
        return

    pending: set[str] = protection["pending_prompt_details"]
    pending.update(str(detail) for detail in details if detail)
    active_logger = logger if logger is not None else info_log

    dialog = protection.get("dialog")
    details_label = protection.get("dialog_details_label")

    if dialog is None or details_label is None:
        try:
            with ui.dialog() as dialog:
                with ui.card().classes("w-[34rem] max-w-full"):
                    ui.label("OB protection condition").classes("font-bold egse-title warning-text")
                    ui.label("A thermistor condition, error, or warning has been raised. Shut down the PSU?").classes(
                        "egse-text"
                    )
                    ui.separator()
                    details_label = ui.column().classes("gap-0")
                    with ui.row().classes("w-full justify-end gap-2"):

                        def _keep_psu_on() -> None:
                            retained = sorted(protection["pending_prompt_details"])
                            protection["pending_prompt_details"] = set()
                            active_logger.warning(
                                "Operator kept PSU on after OB protection prompt: %s",
                                "; ".join(retained),
                            )
                            dialog.close()

                        def _confirm_shutdown() -> None:
                            confirmed = sorted(protection["pending_prompt_details"])
                            protection["pending_prompt_details"] = set()
                            dialog.close()
                            _request_ob_psu_emergency_shutdown(
                                state,
                                active_logger,
                                confirmed,
                                automatic=False,
                            )

                        ui.button("Keep PSU on", on_click=_keep_psu_on).props("outline")
                        ui.button("Shut down PSU", on_click=_confirm_shutdown).classes("error-text")

            protection["dialog"] = dialog
            protection["dialog_details_label"] = details_label
        except Exception as exc:
            active_logger.exception("Could not open OB PSU shutdown confirmation dialog: %s", exc)
            notify(
                "OB protection condition requires PSU shutdown confirmation:\n"
                + "\n".join(f"• {detail}" for detail in sorted(pending)),
                color="warning",
            )
            return

    details_label.clear()
    with details_label:
        _render_message_lines("\n".join(f"• {detail}" for detail in sorted(pending)), default_severity="warning")
    active_logger.warning("OB PSU shutdown confirmation requested: %s", "; ".join(sorted(pending)))
    try:
        dialog.open()
    except Exception as exc:
        active_logger.exception("Could not open OB PSU shutdown confirmation dialog: %s", exc)


def _reset_ob_psu_protection_actions(state: dict[str, Any]) -> None:
    """Reset trigger de-duplication state without changing the physical PSU state."""
    protection = _ob_psu_protection_state(state)
    protection["shutdown_in_progress"] = False
    protection["shutdown_latched"] = False
    protection["last_shutdown_reason"] = []
    protection["last_error"] = None
    protection["pending_prompt_details"] = set()
    dialog = protection.get("dialog")
    if dialog is not None:
        try:
            dialog.close()
        except Exception:
            pass

    # Also clear the MMS latch so a new trigger can fire after recovery.
    mms_cfg = state.get("mms")
    if isinstance(mms_cfg, dict):
        mms_cfg["latched"] = False
        mms_cfg["in_progress"] = False


def simulate_ob_fdir(state: dict[str, Any], hk: Any, logger: Any = None) -> list[str]:
    """Evaluate and latch standalone-OB FDIR warning/alarm conditions.

    Classification is mutually exclusive for each parameter:

    * inside warning limits: no new FDIR
    * outside warning but inside alarm limits: warning
    * outside alarm limits: alarm

    Alarm severity supersedes a previously latched warning for the same
    parameter.  Latches remain asserted until reset_ob_fdir_simulator().
    """
    simulator = _ob_fdir_simulator_state(state)
    if not bool(simulator.get("enabled", True)):
        simulator["current_warning"] = set()
        simulator["current_alarm"] = set()
        _attach_simulated_ob_fdir_fields(hk, set(), set())
        return []
    if not bool(state.get("mms", {}).get("enabled", True)):
        simulator["warning_latched"] = set()
        simulator["alarm_latched"] = set()
        simulator["current_warning"] = set()
        simulator["current_alarm"] = set()
        simulator["latest_adu"] = {}
        _attach_simulated_ob_fdir_fields(hk, set(), set())
        return []

    warning_latched: set[str] = simulator["warning_latched"]
    alarm_latched: set[str] = simulator["alarm_latched"]
    previous_warning = set(warning_latched)
    previous_alarm = set(alarm_latched)
    current_warning: set[str] = set()
    current_alarm: set[str] = set()
    latest_adu: dict[str, int] = {}

    for parameter in OB_FDIR_PARAMETERS:
        adu = _ob_adc12(getattr(hk, parameter.hk_field, None))
        if adu is None:
            continue
        latest_adu[parameter.flag_name] = adu

        outside_alarm = violates_limits(float(adu), parameter.alarm_limits)
        outside_warning = violates_limits(float(adu), parameter.warning_limits)

        if outside_alarm:
            current_alarm.add(parameter.flag_name)
            alarm_latched.add(parameter.flag_name)
            # Alarm dominates the warning presentation for this parameter.
            warning_latched.discard(parameter.flag_name)
        elif outside_warning:
            current_warning.add(parameter.flag_name)
            if parameter.flag_name not in alarm_latched:
                warning_latched.add(parameter.flag_name)

    simulator["current_warning"] = current_warning
    simulator["current_alarm"] = current_alarm
    simulator["latest_adu"] = latest_adu

    new_warning = warning_latched - previous_warning
    new_alarm = alarm_latched - previous_alarm
    active_logger = logger if logger is not None else info_log
    parameter_by_flag = {parameter.flag_name: parameter for parameter in OB_FDIR_PARAMETERS}

    warning_messages: list[str] = []
    for flag_name in sorted(new_warning):
        parameter = parameter_by_flag[flag_name]
        measurement = _format_ob_fdir_measurement(
            state,
            parameter,
            latest_adu.get(flag_name),
            "warning",
        )
        message = f"{flag_name} ({measurement})"
        warning_messages.append(message)
        active_logger.warning("OB simulated FDIR warning latched: %s", message)

    alarm_messages: list[str] = []
    for flag_name in sorted(new_alarm):
        parameter = parameter_by_flag[flag_name]
        measurement = _format_ob_fdir_measurement(
            state,
            parameter,
            latest_adu.get(flag_name),
            "alarm",
        )
        message = f"{flag_name} ({measurement})"
        alarm_messages.append(message)
        active_logger.error("OB simulated FDIR alarm latched: %s", message)

    if warning_messages:
        notify(
            "OB FDIR warning latched:\n" + "\n".join(f"• {message}" for message in warning_messages),
            color="warning",
        )
    if alarm_messages:
        notify(
            "OB FDIR alarm latched:\n" + "\n".join(f"• {message}" for message in alarm_messages),
            color="negative",
        )

    # PSU protection policy for standalone OB FDIR simulation:
    #   * voltage alarms shut down immediately;
    #   * every warning and every thermistor alarm asks the operator first.
    # Skip all protection actions when MMS is disabled.
    if not bool(state.get("mms", {}).get("enabled", True)):
        _attach_simulated_ob_fdir_fields(hk, warning_latched, alarm_latched)
        return simulated_ob_fdir_details(state)

    # Remove flags the operator has chosen to ignore.
    ignored_flags: set[str] = state.get("ob_fdir_ignored_flags") or set()
    voltage_alarm_flags = sorted((new_alarm - ignored_flags) & _OB_VOLTAGE_FDIR_FLAGS)
    if voltage_alarm_flags:
        shutdown_reasons = []
        for flag_name in voltage_alarm_flags:
            parameter = parameter_by_flag[flag_name]
            measurement = _format_ob_fdir_measurement(
                state,
                parameter,
                latest_adu.get(flag_name),
                "alarm",
            )
            shutdown_reasons.append(f"OB voltage alarm: {flag_name} ({measurement})")
        _request_ob_psu_emergency_shutdown(
            state,
            active_logger,
            shutdown_reasons,
            automatic=True,
        )
    else:
        prompt_reasons: list[str] = []
        for flag_name in sorted(new_warning - ignored_flags):
            parameter = parameter_by_flag[flag_name]
            measurement = _format_ob_fdir_measurement(
                state,
                parameter,
                latest_adu.get(flag_name),
                "warning",
            )
            prompt_reasons.append(f"OB FDIR warning: {flag_name} ({measurement})")
        for flag_name in sorted((new_alarm & _OB_THERMISTOR_FDIR_FLAGS) - ignored_flags):
            parameter = parameter_by_flag[flag_name]
            measurement = _format_ob_fdir_measurement(
                state,
                parameter,
                latest_adu.get(flag_name),
                "alarm",
            )
            prompt_reasons.append(f"OB thermistor alarm: {flag_name} ({measurement})")
        if prompt_reasons:
            _open_ob_psu_shutdown_dialog(state, active_logger, prompt_reasons)

    _attach_simulated_ob_fdir_fields(hk, warning_latched, alarm_latched)
    return simulated_ob_fdir_details(state)


def simulated_ob_fdir_details(state: dict[str, Any]) -> list[str]:
    """Return display strings for all latched standalone-OB FDIRs."""
    simulator = _ob_fdir_simulator_state(state)
    warning_latched = set(simulator.get("warning_latched") or set())
    alarm_latched = set(simulator.get("alarm_latched") or set())
    details = [f"OB FDIR Alarm: {name} (simulated, latched)" for name in sorted(alarm_latched)]
    details.extend(f"OB FDIR Warning: {name} (simulated, latched)" for name in sorted(warning_latched))
    return details


def reset_ob_fdir_simulator(state: dict[str, Any], logger: Any = None) -> None:
    """Clear all simulated OB FDIR latches after a successful Clear_Errors TC."""
    simulator = _ob_fdir_simulator_state(state)
    had_latches = bool(simulator["warning_latched"] or simulator["alarm_latched"])
    simulator["warning_latched"].clear()
    simulator["alarm_latched"].clear()
    simulator["current_warning"].clear()
    simulator["current_alarm"].clear()
    simulator["latest_adu"].clear()
    _reset_ob_psu_protection_actions(state)

    ob_light = (state.get("alarm_lights") or {}).get("ob")
    if ob_light is not None:
        reset_latches = getattr(ob_light, "reset_latches", None)
        if callable(reset_latches):
            reset_latches()
        update_from_faults = getattr(ob_light, "update_from_faults", None)
        if callable(update_from_faults):
            update_from_faults({}, source="ob_fdir_sim")

    if had_latches:
        (logger if logger is not None else info_log).info("OB simulated FDIR latches cleared.")
        notify("OB simulated FDIR latches cleared.", color="positive")


# end region


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
# region Notifications


def clear_force_pause() -> None:
    """Release a previously requested forced pause."""
    _FORCE_PAUSE_EVENT.clear()
    _close_force_pause_dialogs()


def is_force_paused() -> bool:
    """Return True when script execution is held by a forced pause."""
    return _FORCE_PAUSE_EVENT.is_set()


def notify(msg: str, color: str = "primary") -> None:
    """Send a UI notification that is safe to call from any thread.

    Uses ``loop.call_soon_threadsafe`` to enqueue the notification message
    directly into each connected client's outbox on the NiceGUI event loop
    thread.  This mirrors what ``ui.notify`` does internally but is safe to
    call from background threads (e.g. scripts running via ``run.io_bound``).
    """
    loop = _nicegui_core.loop
    if loop is None or not loop.is_running():
        info_log.info("[notify] %s", msg)
        return

    def _enqueue() -> None:
        for client in list(_NiceGuiClient.instances.values()):
            try:
                with client:
                    ui.notify(str(msg), color=color, multi_line=True)
            except Exception as exc:
                info_log.debug("notify: failed for client %s: %s", client.id, exc)

    loop.call_soon_threadsafe(_enqueue)


def notify_negative(msg) -> None:
    notify(msg, color="negative")


def notify_positive(msg) -> None:
    notify(msg, color="positive")


def notify_script_done() -> None:
    """Notify the user that the script has completed."""
    notify("Script execution complete.", color="positive")


def notify_script_pause(current: int, total: int) -> None:
    """Notify the user that the script is paused, showing progress."""
    notify(f"Script paused, command {current} of {total}", color="warning")


def begin_ob_sci_capture(label: str = "OB Science Scan") -> int:
    """Start a new OB scan capture and discard pre-scan SCI queue residue."""
    global _OB_SCI_CAPTURE_ID, _OB_SCI_CAPTURE_ACTIVE, _OB_SCI_CAPTURE_CLOSING
    global _OB_SCI_CAPTURE_LABEL, _OB_SCI_CAPTURE_CLOSE_REQUESTED_AT

    # DAC-offset calculations use the same OB SCI packet shape. They happen
    # before measurement_scan(), so clear any of their unconsumed packets at
    # this explicit acquisition boundary.
    while not const.sci_queue.empty():
        try:
            const.sci_queue.get_nowait()
        except (Empty, AttributeError):
            break

    with _OB_SCI_CAPTURE_LOCK:
        _OB_SCI_CAPTURE_ID += 1
        _OB_SCI_CAPTURE_ACTIVE = True
        _OB_SCI_CAPTURE_CLOSING = False
        _OB_SCI_CAPTURE_CLOSE_REQUESTED_AT = 0.0
        _OB_SCI_CAPTURE_LABEL = str(label).strip() or "OB Science Scan"
        capture_id = _OB_SCI_CAPTURE_ID
    info_log.info("Started OB SCI capture %s: %s", capture_id, _OB_SCI_CAPTURE_LABEL)
    return capture_id


def end_ob_sci_capture(capture_id: int) -> None:
    """Close a capture after the telemetry poller drains its remaining points."""
    global _OB_SCI_CAPTURE_CLOSING, _OB_SCI_CAPTURE_CLOSE_REQUESTED_AT
    with _OB_SCI_CAPTURE_LOCK:
        if _OB_SCI_CAPTURE_ACTIVE and capture_id == _OB_SCI_CAPTURE_ID:
            _OB_SCI_CAPTURE_CLOSING = True
            _OB_SCI_CAPTURE_CLOSE_REQUESTED_AT = time.monotonic()


def cancel_ob_sci_capture(capture_id: int) -> None:
    """Cancel a failed scan without publishing its partial points as a packet."""
    global _OB_SCI_CAPTURE_ACTIVE, _OB_SCI_CAPTURE_CLOSING, _OB_SCI_CAPTURE_CLOSE_REQUESTED_AT
    with _OB_SCI_CAPTURE_LOCK:
        if capture_id == _OB_SCI_CAPTURE_ID:
            _OB_SCI_CAPTURE_ACTIVE = False
            _OB_SCI_CAPTURE_CLOSING = False
            _OB_SCI_CAPTURE_CLOSE_REQUESTED_AT = 0.0
    info_log.info("Cancelled OB SCI capture %s", capture_id)


def _ob_sci_capture_snapshot() -> tuple[int, str] | None:
    with _OB_SCI_CAPTURE_LOCK:
        if not _OB_SCI_CAPTURE_ACTIVE:
            return None
        return _OB_SCI_CAPTURE_ID, _OB_SCI_CAPTURE_LABEL


def get_ob_sci_scans() -> list[Any]:
    """Return all stitched OB scans retained for this application process."""
    with _OB_SCI_CAPTURE_LOCK:
        return list(_OB_SCI_STITCHED_SCANS)


def _complete_ob_sci_capture_if_drained() -> tuple[int, str] | None:
    """Atomically complete a closing capture once no decoded points remain."""
    global _OB_SCI_CAPTURE_ACTIVE, _OB_SCI_CAPTURE_CLOSING, _OB_SCI_CAPTURE_CLOSE_REQUESTED_AT
    with _OB_SCI_CAPTURE_LOCK:
        if not (_OB_SCI_CAPTURE_ACTIVE and _OB_SCI_CAPTURE_CLOSING):
            return None
        if not const.sci_queue.empty():
            return None
        if time.monotonic() - _OB_SCI_CAPTURE_CLOSE_REQUESTED_AT < _OB_SCI_CAPTURE_QUIET_PERIOD_S:
            return None
        completed = (_OB_SCI_CAPTURE_ID, _OB_SCI_CAPTURE_LABEL)
        _OB_SCI_CAPTURE_ACTIVE = False
        _OB_SCI_CAPTURE_CLOSING = False
        _OB_SCI_CAPTURE_CLOSE_REQUESTED_AT = 0.0
        return completed


def replay_ob_sci_log(
    log_path: str | Path,
    *,
    info_log_path: str | Path | None = None,
    point_delay_s: float = 0.02,
    label: str = "Replayed OB Science Scan",
) -> int:
    """Replay an OB ``*_SCI.LOG`` through the live capture/queue pipeline.

    This is intended for UI integration testing: it exercises the same queue,
    capture, stitching, and plot-button path as live OB telemetry.
    """
    from analysis_modules import sci_plot

    timestamp_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.](\d{3,6})")

    def _line_time(line: str) -> datetime | None:
        match = timestamp_pattern.match(line)
        if match is None:
            return None
        fraction = match.group(2).ljust(6, "0")[:6]
        return datetime.strptime(f"{match.group(1)}.{fraction}", "%Y-%m-%d %H:%M:%S.%f")

    science_points: list[tuple[datetime, tuple[int, int, int, int, int, int, int, int, int]]] = []
    with Path(log_path).open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line_time = _line_time(line)
            parsed = sci_plot._parse_sci_log_line(line)
            if line_time is not None and parsed is not None:
                science_points.append((line_time, parsed))

    if not science_points:
        raise ValueError(f"No valid OB science measurements found in {log_path}")

    if info_log_path is None:
        sci_path = Path(log_path)
        sci_name_lower = sci_path.name.lower()
        if sci_name_lower.endswith("_sci.log"):
            expected_info_name = f"{sci_path.name[:-8]}_INFO.log".lower()
            info_log_path = next(
                (candidate for candidate in sci_path.parent.iterdir() if candidate.name.lower() == expected_info_name),
                None,
            )
            if info_log_path is not None:
                info_log.info("Auto-selected matching INFO log for OB SCI replay: %s", info_log_path)

    if info_log_path is None:
        scan_windows = [(science_points[0][0], science_points[-1][0], label)]
        info_log.warning(
            "No matching INFO log found for %s; replaying all %s SCI records as one scan",
            log_path,
            len(science_points),
        )
    else:
        explicit_windows: list[tuple[datetime, datetime, str]] = []
        legacy_windows: list[tuple[datetime, datetime, str]] = []
        explicit_open: tuple[datetime, str] | None = None
        legacy_open: tuple[datetime, str] | None = None
        started_capture_pattern = re.compile(r"started ob sci capture\s+\d+\s*:\s*(.+)", re.IGNORECASE)
        with Path(info_log_path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line_time = _line_time(line)
                if line_time is None:
                    continue
                lower_line = line.lower()
                started_match = started_capture_pattern.search(line)
                if started_match is not None:
                    explicit_open = (line_time, started_match.group(1).strip())
                elif "completed ob sci capture" in lower_line and explicit_open is not None:
                    explicit_windows.append((explicit_open[0], line_time, explicit_open[1]))
                    explicit_open = None

                if "running abu measurement scan" in lower_line:
                    # begin_ob_sci_capture is logged immediately before the
                    # actual scan command.  A final DAC-offset response can
                    # arrive in that small gap, so use the later command time
                    # as the inclusive SCI-window boundary.
                    if explicit_open is not None:
                        explicit_open = (line_time, explicit_open[1])
                    if legacy_open is not None:
                        legacy_windows.append((legacy_open[0], line_time, legacy_open[1]))
                    legacy_open = (line_time, f"{label} {len(legacy_windows) + 1}")
                elif legacy_open is not None and any(
                    marker in lower_line for marker in ("send mtr_halt", "script failed", "running clean exit")
                ):
                    legacy_windows.append((legacy_open[0], line_time, legacy_open[1]))
                    legacy_open = None

        scan_windows = explicit_windows
        if not scan_windows:
            if legacy_open is not None:
                legacy_windows.append((legacy_open[0], science_points[-1][0], legacy_open[1]))
            scan_windows = legacy_windows
        if not scan_windows:
            raise ValueError(
                f"No completed OB SCI capture or 'Running ABU Measurement Scan' windows found in {info_log_path}"
            )

    total_points = 0
    replayed_scans = 0
    for window_start, window_end, scan_label in scan_windows:
        window_points = [point for point_time, point in science_points if window_start <= point_time <= window_end]
        if not window_points:
            continue
        capture_id = begin_ob_sci_capture(scan_label)
        try:
            for parsed in window_points:
                abs_steps, swir_low, swir_med, swir_high, mwir_low, mwir_med, mwir_high, swir_offset, mwir_offset = (
                    parsed
                )
                point = SimpleNamespace(
                    CMD_CNT=total_points,
                    ABS_STEPS=abs_steps,
                    SWIR_LOW=swir_low,
                    SWIR_MED=swir_med,
                    SWIR_HIGH=swir_high,
                    MWIR_LOW=mwir_low,
                    MWIR_MED=mwir_med,
                    MWIR_HIGH=mwir_high,
                    SWIR_OFFSET=swir_offset,
                    MWIR_OFFSET=mwir_offset,
                    TIME=datetime.now(),
                )
                const.sci_queue.put(point)
                total_points += 1
                if point_delay_s > 0:
                    time.sleep(float(point_delay_s))
        except BaseException:
            cancel_ob_sci_capture(capture_id)
            raise
        else:
            end_ob_sci_capture(capture_id)

        deadline = time.monotonic() + 30.0
        while (snapshot := _ob_sci_capture_snapshot()) is not None and snapshot[0] == capture_id:
            if time.monotonic() >= deadline:
                cancel_ob_sci_capture(capture_id)
                raise TimeoutError(f"Timed out finalizing replayed OB scan {capture_id}")
            time.sleep(0.05)
        replayed_scans += 1

    if replayed_scans == 0:
        sci_range = f"{science_points[0][0]} to {science_points[-1][0]}"
        info_range = f"{scan_windows[0][0]} to {scan_windows[-1][1]}"
        raise ValueError(
            f"The INFO measurement windows do not overlap the SCI log timestamps: SCI={sci_range}; INFO={info_range}"
        )

    info_log.info(
        "Queued %s replayed OB science points across %s scan(s) from %s",
        total_points,
        replayed_scans,
        log_path,
    )
    return total_points


_LINE_SEVERITY_PATTERNS = (
    ("negative", re.compile(r"fail|error|alarm|mismatch|out of range|missing|timeout|abort", re.IGNORECASE)),
    ("warning", re.compile(r"warn|skipp", re.IGNORECASE)),
    ("positive", re.compile(r"\[pass\]|passed|\bok\b", re.IGNORECASE)),
)
_NUMBERED_LINE = re.compile(r"^\s*\d+\.\s")


def _render_message_lines(
    message: str, numbered_severity: str | None = None, default_severity: str | None = None
) -> None:
    """Render a popup message line by line, colouring pass/warning/error lines green/yellow/red."""
    for line in str(message).splitlines() or [""]:
        severity = next((name for name, pattern in _LINE_SEVERITY_PATTERNS if pattern.search(line)), None)
        if severity is None and numbered_severity and _NUMBERED_LINE.match(line):
            severity = numbered_severity
        severity = severity or (default_severity if line.strip() else None)
        classes = "egse-text whitespace-pre-wrap"
        if severity:
            classes += f" text-{severity}"
        ui.label(line or "\u00a0").classes(classes)


def _remove_force_pause_popup(popup: Any) -> None:
    """Remove either the current non-modal popup or a legacy dialog safely."""
    delete = getattr(popup, "delete", None)
    if callable(delete):
        delete()
        return
    close = getattr(popup, "close", None)
    if callable(close):
        close()


def _open_force_pause_dialogs(message: str, passed: bool | None = None) -> None:
    """Open (or refresh) a non-modal force-pause result for each client."""
    loop = _nicegui_core.loop
    if loop is None or not loop.is_running():
        info_log.info("[force-pause] %s", message)
        return

    def _open() -> None:
        for client in list(_NiceGuiClient.instances.values()):
            try:
                with client:
                    existing = _FORCE_PAUSE_DIALOGS.get(client.id)
                    if existing is not None:
                        try:
                            _remove_force_pause_popup(existing)
                        except Exception:
                            pass

                    if passed is None:
                        title = "Script Paused"
                        title_class = "warning-text"
                        accent_color = "warning"
                    elif passed:
                        title = "Check Passed — Script Paused"
                        title_class = "text-positive"
                        accent_color = "positive"
                    else:
                        title = "Check Failed — Script Paused"
                        title_class = "text-negative"
                        accent_color = "negative"

                    # A dialog adds a full-screen modal backdrop, which prevents
                    # the operator from reaching the Resume control. A fixed card
                    # keeps the result prominent while leaving the UI interactive.
                    popup_card = (
                        ui.card()
                        .classes("fixed top-4 right-4 z-[9999] w-[34rem] max-w-[calc(100vw-2rem)] shadow-2xl border-2")
                        .style(f"border-color: var(--q-{accent_color})")
                    )
                    with popup_card as popup:
                        ui.label(title).classes(f"font-bold egse-title {title_class}")
                        _render_message_lines(message, None if passed is None else accent_color)
                        ui.separator()
                        ui.label("Press Resume in Script Controls to continue.").classes("egse-text")
                    _FORCE_PAUSE_DIALOGS[client.id] = popup
                    ui.run_javascript(_FLASH_ATTENTION_JS)
            except Exception as exc:
                info_log.debug("force pause dialog: failed for client %s: %s", client.id, exc)

    loop.call_soon_threadsafe(_open)


def _close_force_pause_dialogs() -> None:
    """Close all active force-pause dialogs."""
    loop = _nicegui_core.loop
    if loop is None or not loop.is_running():
        _FORCE_PAUSE_DIALOGS.clear()
        return

    def _close() -> None:
        for client_id, dialog in list(_FORCE_PAUSE_DIALOGS.items()):
            try:
                _remove_force_pause_popup(dialog)
                client = _NiceGuiClient.instances.get(client_id)
                if client is not None:
                    with client:
                        ui.run_javascript(_RESTORE_TITLE_JS)
            except Exception:
                pass
            finally:
                _FORCE_PAUSE_DIALOGS.pop(client_id, None)

    loop.call_soon_threadsafe(_close)


_CONFIRM_EVENT = threading.Event()
_CONFIRM_RESULT: bool = False

_FLASH_ATTENTION_JS = """
if (!window.__egseOriginalTitle) { window.__egseOriginalTitle = document.title; }
document.title = '\u26a0 ACTION NEEDED \u2014 ' + window.__egseOriginalTitle;
try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    osc.start();
    osc.stop(ctx.currentTime + 0.3);
} catch (e) {}
"""

_RESTORE_TITLE_JS = """
if (window.__egseOriginalTitle) { document.title = window.__egseOriginalTitle; }
"""


def request_confirmation(
    message: str,
    title: str = "Confirm",
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    severity: str | None = None,
) -> bool:
    """Show a confirmation dialog and block until the user responds. Safe to call from a background script thread.

    Builds the dialog on the NiceGUI event loop thread (mirroring ``notify``'s
    threadsafe pattern) since UI elements cannot be created directly from a
    background thread. Returns True if confirmed, False if cancelled or the
    script is aborted while waiting.
    """
    loop = _nicegui_core.loop
    if loop is None or not loop.is_running():
        info_log.info("[confirm] %s", message)
        return False

    _CONFIRM_EVENT.clear()

    def _open_dialogs() -> None:
        for client in list(_NiceGuiClient.instances.values()):
            try:
                with client:
                    with ui.dialog() as dialog, ui.card().classes("w-[34rem] max-w-full") as card:
                        if severity:
                            card.classes("border-2").style(f"border-color: var(--q-{severity})")
                        ui.label(title).classes("font-bold egse-title" + (f" text-{severity}" if severity else ""))
                        _render_message_lines(message, severity)
                        ui.separator()
                        with ui.row().classes("w-full justify-end gap-2"):

                            def _cancel(d: Any = dialog) -> None:
                                global _CONFIRM_RESULT
                                d.close()
                                _CONFIRM_RESULT = False
                                _CONFIRM_EVENT.set()
                                ui.run_javascript(_RESTORE_TITLE_JS)

                            def _confirm(d: Any = dialog) -> None:
                                global _CONFIRM_RESULT
                                d.close()
                                _CONFIRM_RESULT = True
                                _CONFIRM_EVENT.set()
                                ui.run_javascript(_RESTORE_TITLE_JS)

                            ui.button(cancel_label, on_click=_cancel).props("outline")
                            ui.button(confirm_label, on_click=_confirm).classes("primary-text")
                    dialog.open()
                    ui.run_javascript(_FLASH_ATTENTION_JS)
            except Exception as exc:
                info_log.debug("request_confirmation: failed for client %s: %s", client.id, exc)

    loop.call_soon_threadsafe(_open_dialogs)

    while not _CONFIRM_EVENT.is_set():
        if is_aborted():
            return False
        time.sleep(0.25)

    return _CONFIRM_RESULT


_CHOICE_EVENT = threading.Event()
_CHOICE_RESULT: str | None = None


def request_choice(
    message: str,
    options: list[str],
    title: str = "Select",
    confirm_label: str = "Select",
    cancel_label: str = "Cancel",
) -> str | None:
    """Show a selection dialog and block until the user responds. Safe to call from a background script thread.

    Returns the selected option, or None if cancelled or the script is aborted while waiting.
    """
    loop = _nicegui_core.loop
    if loop is None or not loop.is_running() or not options:
        info_log.info("[choice] %s", message)
        return None

    _CHOICE_EVENT.clear()

    def _open_dialogs() -> None:
        for client in list(_NiceGuiClient.instances.values()):
            try:
                with client:
                    with ui.dialog().props("persistent") as dialog, ui.card().classes("w-[40rem] max-w-full"):
                        ui.label(title).classes("font-bold egse-title")
                        _render_message_lines(message, None)
                        selector = ui.select(options, value=options[0]).classes("w-full")
                        ui.separator()
                        with ui.row().classes("w-full justify-end gap-2"):

                            def _finish(result: str | None, d: Any = dialog) -> None:
                                global _CHOICE_RESULT
                                d.close()
                                _CHOICE_RESULT = result
                                _CHOICE_EVENT.set()
                                ui.run_javascript(_RESTORE_TITLE_JS)

                            ui.button(cancel_label, on_click=lambda: _finish(None)).props("outline")
                            ui.button(confirm_label, on_click=lambda s=selector: _finish(s.value)).classes(
                                "primary-text"
                            )
                    dialog.open()
                    ui.run_javascript(_FLASH_ATTENTION_JS)
            except Exception as exc:
                info_log.debug("request_choice: failed for client %s: %s", client.id, exc)

    loop.call_soon_threadsafe(_open_dialogs)

    while not _CHOICE_EVENT.is_set():
        if is_aborted():
            return None
        time.sleep(0.25)

    return _CHOICE_RESULT


def request_repeat_acquisition(label: str) -> bool:
    """Pause after a science acquisition; return True if the operator wants to repeat it."""
    request_force_pause(f"{label} finished. Review the science data, then press Resume to continue.")
    repeat = request_confirmation(
        f"{label} finished.\n\nRepeat this science acquisition or continue with the script?",
        title=f"{label} complete",
        confirm_label="Repeat",
        cancel_label="Continue",
    )
    if is_aborted():
        raise ScriptAbortRequested
    info_log.info("%s: operator chose to %s", label, "repeat" if repeat else "continue")
    return repeat


def handle_script_check_failure(
    label: str,
    errors: list[str],
    readings: dict[str, Any] | None = None,
) -> bool:
    """Ask the operator whether an OB FFT check failure should be overridden.

    This is the callback expected by ``scripts_modules.OB_FFT.report_check``.
    Return ``True`` to continue the script despite the failed check, or ``False``
    to let ``background_checks.report_check`` raise and stop the script.
    """
    numbered = "\n".join(f"{index + 1}. {error.strip()}" for index, error in enumerate(errors))
    message = f"{label} verification failed:\n{numbered}"
    if readings:
        reading_lines = "\n".join(f"- {name}: {value}" for name, value in readings.items())
        message += f"\n\nMeasured readings:\n{reading_lines}"
    message += "\n\nContinue the OB FFT despite this failed check?"

    continue_run = request_confirmation(
        message,
        title=f"{label} check failed",
        confirm_label="Continue",
        cancel_label="Abort",
        severity="negative",
    )
    if continue_run:
        info_log.warning("Operator overrode failed OB FFT check: %s — %s", label, errors)
    else:
        info_log.error("Operator aborted after failed OB FFT check: %s — %s", label, errors)
    return continue_run


def request_force_pause(
    label: str = "",
    errors: list[str] | None = None,
    readings: dict[str, Any] | None = None,
    *,
    passed: bool | None = None,
) -> None:
    """Report an optional check result, then pause until the user resumes or aborts.

    When ``errors`` is supplied, an empty list displays a green passed result and
    a non-empty list displays a red failed result. ``passed`` can be supplied for
    checks which already produced a boolean result. If neither is supplied, this
    retains the original message-only, neutral pause behaviour.

    Unlike ``report_check``, a failed check is deliberately not raised here: the
    persistent dialog remains open and execution stays blocked until Resume in
    Script Controls clears the pause (or the script is aborted).
    """
    check_label = str(label).strip() if label is not None else ""
    check_passed = passed if passed is not None else (not errors if errors is not None else None)

    if check_passed is None:
        message = check_label or "Execution paused. Press Resume in Script Controls to continue."
    elif check_passed:
        message = f"{check_label or 'Check'} verification passed"
        if readings:
            message += f": {readings}"
    else:
        failure_details = [str(error).strip() for error in (errors or []) if str(error).strip()]
        message = f"{check_label or 'Check'} verification failed"
        if failure_details:
            message += f": {len(failure_details)} error{'s' if len(failure_details) != 1 else ''}:\n"
            message += "\n".join(f"{index + 1}. {error}" for index, error in enumerate(failure_details))
        if readings:
            message += f"\nReadings: {readings}"

    _FORCE_PAUSE_EVENT.set()
    _open_force_pause_dialogs(message, check_passed)
    while _FORCE_PAUSE_EVENT.is_set():
        if is_aborted():
            _FORCE_PAUSE_EVENT.clear()
            _close_force_pause_dialogs()
            raise RuntimeError("Script aborted during forced pause.")
        time.sleep(0.25)


# endregion


# ---------------------------------------------------------------------------
# PSU helpers — consumption check (public)
# ---------------------------------------------------------------------------
# region PSU helpers — consumption check (public)


def consumption_check(state_names, psu_sample: dict, errors: list[str], latest_hk: Any = None) -> float | None:
    """
    Checks PSU current for the given state(s) and current OB model.
    Accepts a single state name (str) or a list of state names.
    Sums expected values for all provided states.
    Appends error to errors if out of range.
    Model is read from app.state.current_model.

    If latest_hk is provided, verify_heater_states() is called automatically and the
    active heater states (["MechHTR", "DetHTR"] subset) are appended to state_names.
    """
    from nicegui import app as _app

    model = getattr(_app.state, "current_model", None)
    if model is None:
        errors.append("No model specified for PSU consumption check.")
        return None
    if model not in MODEL_CONSUMPTION:
        errors.append(f"Unknown OB model: {model}")
        return None
    model_dict = MODEL_CONSUMPTION[model]
    if isinstance(state_names, str):
        state_names = [state_names]
    else:
        state_names = list(state_names)
    if latest_hk is not None:
        heater_states = verify_heater_states(latest_hk, errors)
        if not any(s in HEATER_INCLUSIVE_STATES for s in state_names):
            state_names += heater_states
    missing = [s for s in state_names if s not in model_dict]
    if missing:
        errors.append(f"Unknown state(s) {missing} for model '{model}'")
        return None
    if psu_sample is None:
        errors.append("No PSU sample provided for consumption check.")
        return None
    measured_current_ma = float(psu_sample.get("PSU_EB_I") or 0.0) * 1000.0
    base = sum(model_dict[s] for s in state_names)
    min_i = base - 10
    max_i = base + 10
    if not (min_i <= measured_current_ma <= max_i):
        errors.append(
            f"PSU_EB_I out of range for {state_names} ({model}): got {measured_current_ma:.2f} mA, expected {min_i}-{max_i}"
        )
    return measured_current_ma


# Per-heater history for verify_heater_states() — persists across HK poll cycles.
# Tracks the last definitive expected state (established when TRP was outside the
# hysteresis band) and the previous mode flags so that enable-transitions can be
# detected (the firmware initialises to OFF when transitioning from disabled → auto).
_heater_state_history: dict[str, dict] = {
    "Mech": {"last_expected": None, "last_trp": None, "prev_auto": False, "prev_manual": False},
    "Det": {"last_expected": None, "last_trp": None, "prev_auto": False, "prev_manual": False},
}
# endregion


# ---------------------------------------------------------------------------
# PSU helpers — moving-average smoothing
# ---------------------------------------------------------------------------
# region PSU helpers — moving-average smoothing


def get_smoothed_psu_sample(
    psu_queue: Any,
    *,
    timeout: float = 2.0,
    window_samples: int = _SCRIPT_PSU_MA_WINDOW_SAMPLES,
) -> dict[str, Any]:
    """Read PSU queue samples and return a sample with MA-smoothed current fields.

    The first sample is read with timeout. Any immediately available additional
    samples are drained up to *window_samples* to build a fixed-size moving
    average used by script-side current checks.
    """
    first_sample = psu_queue.get(timeout=timeout)
    samples = [first_sample]
    max_samples = max(1, int(window_samples))

    for _ in range(max_samples - 1):
        try:
            samples.append(psu_queue.get_nowait())
        except Empty:
            break

    if not isinstance(first_sample, dict):
        return first_sample

    averaged_sample = dict(first_sample)
    latest_sample = samples[-1] if samples else first_sample
    if isinstance(latest_sample, dict):
        averaged_sample["TIME"] = latest_sample.get("TIME", averaged_sample.get("TIME"))
        averaged_sample["STATUS"] = latest_sample.get("STATUS", averaged_sample.get("STATUS"))

    current_keys = ("PSU_ROV_HTR_I", "PSU_EB_I", "CH1_I", "CH2_I", "CH3_I", "CH4_I")
    for key in current_keys:
        values: list[float] = []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            value = sample.get(key)
            if value is None:
                continue
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
        if values:
            averaged_sample[key] = sum(values) / len(values)

    return averaged_sample


def resolve_consumption_bounds(
    state_names: str | list[str],
    errors: list[str],
    latest_hk: Any = None,
) -> tuple[list[str], float, float] | None:
    """Resolve expected PSU_EB_I bounds for the requested state(s)."""
    from nicegui import app as _app

    model = getattr(_app.state, "current_model", None)
    if model is None:
        errors.append("No model specified for PSU consumption check.")
        return None
    if model not in MODEL_CONSUMPTION:
        errors.append(f"Unknown OB model: {model}")
        return None

    model_dict = MODEL_CONSUMPTION[model]
    if isinstance(state_names, str):
        resolved_states = [state_names]
    else:
        resolved_states = list(state_names)

    if latest_hk is not None:
        heater_states = verify_heater_states(latest_hk, errors)
        if not any(s in HEATER_INCLUSIVE_STATES for s in resolved_states):
            resolved_states += heater_states

    missing = [s for s in resolved_states if s not in model_dict]
    if missing:
        errors.append(f"Unknown state(s) {missing} for model '{model}'")
        return None

    base_current_ma = sum(model_dict[s] for s in resolved_states)
    min_i = base_current_ma - 10
    max_i = base_current_ma + 10
    return resolved_states, min_i, max_i


def windowed_consumption_check(
    state_names: str | list[str],
    errors: list[str],
    latest_hk: Any = None,
    *,
    duration_s: float = _SCI_CONSUMPTION_CHECK_DURATION_S,
    per_sample_timeout_s: float = _SCI_CONSUMPTION_PER_SAMPLE_TIMEOUT_S,
    min_samples: int = _SCI_CONSUMPTION_MIN_SAMPLES,
    require_motor_moving: bool = False,
    moving_fraction_required: float = _SCI_CONSUMPTION_MOVING_MIN_FRACTION,
) -> float | None:
    """Run a repeatable PSU consumption check over a short time window.

    Samples are MA-smoothed per sample via `get_smoothed_psu_sample`.
    Pass/fail is decided by whether the window median lies within expected
    bounds. Returns the median measured current in mA.
    """
    bounds = resolve_consumption_bounds(state_names, errors, latest_hk)
    if bounds is None:
        return None
    resolved_states, min_i, max_i = bounds

    deadline = time.monotonic() + max(float(duration_s), float(per_sample_timeout_s))
    measured_ma: list[float] = []
    moving_obs_count = 0
    moving_true_count = 0

    while time.monotonic() < deadline:
        fresh = wait_for_fresh_psu(timeout=per_sample_timeout_s)
        if fresh is None:
            continue
        sample = get_smoothed_latest_psu(_SCRIPT_PSU_MA_WINDOW_SAMPLES)
        if not isinstance(sample, dict):
            continue
        measured_current_ma = float(sample.get("PSU_EB_I") or 0.0) * 1000.0
        measured_ma.append(measured_current_ma)

        if require_motor_moving:
            hk_now = get_latest_hk() or latest_hk
            if hk_now is not None:
                mtr = getattr(hk_now, "MTR_FLAGS", None)
                moving = bool(getattr(mtr, "MOVING", 0))
                moving_obs_count += 1
                if moving:
                    moving_true_count += 1

    if not measured_ma:
        errors.append("No PSU samples captured during SCI ACQ windowed current check.")
        return None

    sample_count = len(measured_ma)
    median_ma = statistics.median(measured_ma)

    if sample_count < int(min_samples):
        errors.append(
            "SCI ACQ consumption check collected too few PSU samples "
            f"({sample_count} < {int(min_samples)}) over {duration_s:.1f}s."
        )

    if not (min_i <= median_ma <= max_i):
        errors.append(
            "PSU_EB_I median out of range for "
            f"{resolved_states}: median={median_ma:.2f} mA, "
            f"expected {min_i:.1f}-{max_i:.1f} mA"
        )

    if require_motor_moving:
        if moving_obs_count <= 0:
            errors.append("SCI ACQ check could not verify motor motion (no HK MTR_FLAGS observations in window).")
        else:
            moving_fraction = moving_true_count / moving_obs_count
            if moving_fraction < float(moving_fraction_required):
                errors.append(
                    "SCI ACQ check did not run during sufficient motor motion: "
                    f"MOVING true {moving_true_count}/{moving_obs_count} "
                    f"({moving_fraction * 100:.1f}%), required >= {float(moving_fraction_required) * 100:.1f}%"
                )

    info_log.info(
        "SCI ACQ windowed consumption stats - states=%s expected=[%.1f, %.1f] mA samples=%d "
        "median=%.2f mA moving_obs=%d moving_true=%d",
        resolved_states,
        min_i,
        max_i,
        sample_count,
        median_ma,
        moving_obs_count,
        moving_true_count,
    )
    return float(median_ma)


# endregion


# ---------------------------------------------------------------------------
# PSU helpers — runtime polling / replay / cards
# ---------------------------------------------------------------------------
# region PSU helpers — runtime polling / replay / cards


def apply_psu_sample(state: dict[str, Any], psu_cards: list[Any], psu_sample: dict[str, Any]) -> None:
    update_psu_readings(state, psu_sample)
    update_psu_cards(psu_cards, psu_sample)
    sync_master = state.get("sync_ob_master_toggle_value")
    if callable(sync_master):
        sync_master()


def build_replay_psu_sample(state: dict[str, Any], psu_cards: list[Any], record: dict[str, Any]) -> dict[str, Any]:
    channels = record.get("CHANNELS", {})
    sample: dict[str, Any] = {key: None for key in _PSU_SAMPLE_KEYS if key != "status"}
    sample.update({"TIME": datetime.now(), "STATUS": bool(record.get("STATUS", True))})

    for card in psu_cards:
        channel_data: dict[str, Any] = {}
        preferred_channels, allow_fallback = card_channel_preferences(card, state["mode"])
        for channel_name in preferred_channels:
            candidate = channels.get(channel_name, {})
            if candidate.get("V") is not None or candidate.get("I") is not None:
                channel_data = candidate
                break
        if not channel_data and allow_fallback:
            for fallback_name in ("CH4", "CH3", "CH2", "CH1"):
                candidate = channels.get(fallback_name, {})
                if candidate.get("V") is not None or candidate.get("I") is not None:
                    channel_data = candidate
                    break

        voltage_key = card.channel.get("live_voltage_key")
        current_key = card.channel.get("live_current_key")
        if isinstance(voltage_key, str):
            sample[voltage_key] = channel_data.get("V")
        if isinstance(current_key, str):
            sample[current_key] = channel_data.get("I")

    return sample


def card_channel_preferences(card: Any, mode: str) -> tuple[list[str], bool]:
    by_mode = card.channel.get("replay_channel_by_mode", {})
    configured = by_mode.get(mode, by_mode.get("EB") or "CH3")
    if isinstance(configured, str):
        return [configured.upper()], True
    if isinstance(configured, list):
        channels = [str(ch).upper() for ch in configured if str(ch)]
        return channels, bool(channels)
    return ["CH3"], True


def create_poll_psu(*, state: dict[str, Any], const: Any, psu_cards: list[Any]) -> Any:
    """Create PSU polling callback bound to the current UI state and cards."""

    def poll_psu() -> None:
        replay = state["psu_replay"]
        saw_live_psu = False
        latest_live_sample = None
        # Bound per-tick queue work to keep the UI event loop responsive.
        max_live_samples_per_tick = 25
        processed = 0
        while not const.psu_queue.empty() and processed < max_live_samples_per_tick:
            saw_live_psu = True
            latest_live_sample = const.psu_queue.get()
            ingest_live_psu_sample(state, psu_cards, latest_live_sample)
            processed += 1

        if latest_live_sample is not None:
            # Draw one point per UI tick while MA includes all drained live samples.
            for card in psu_cards:
                card.push_smoothed(latest_live_sample.get("TIME"))

        if saw_live_psu:
            return

        # Replay is only used when there are no live PSU samples available.
        if not state["log_search"]["enabled"] and not replay.get("enabled"):
            return

        if not replay.get("enabled"):
            return
        records = replay.get("records") or []
        if not records:
            return

        idx = int(replay.get("index", 0))
        sample = build_replay_psu_sample(state, psu_cards, records[idx % len(records)])
        apply_psu_sample(state, psu_cards, sample)
        replay["index"] = idx + 1

    return poll_psu


# TM helpers


_NATIVE_OB_TRP_FIELDS = (
    "DIGITAL_TRP",
    "DETEC_TRP",
    "MECH_TRP",
    "MOTOR_TRP",
)

_NATIVE_OB_ADC12_FIELDS = {
    "HK_V_3V3",
    "HK_V_1V5",
    "DIGITAL_TRP",
    "DETEC_TRP",
    "MECH_TRP",
    "MOTOR_TRP",
}

_EB_OB_TRP_FIELDS = (
    "OB_DIGITAL_TRP",
    "OB_DETECTOR_TRP",
    "OB_MECHANISM_TRP",
    "OB_MOTOR_TRP",
)
_WARNING_NAMES = [name for name, _ in tmstruct.eb_warning_flags]
_FDIR_NAMES = [name for name, _ in tmstruct.eb_fdir_flags]
_OB_WARNING_FLAGS = {
    "OB_FDIR_ALARM",
    "OB_GENERAL_ERROR",
    "OB_MOTOR_ERROR",
    "OB_UNRESPONSIVE",
    "OB_STEP_COUNT_MISMATCH",
}
_OB_FDIR_FLAGS = {
    "FPGA_IO_POWER_SUPPLY",
    "FPGA_CORE_POWER_SUPPLY",
    "DIGITAL_BOARD_TRP",
    "DETECTOR_BOARD_TRP",
    "MECH_BOARD_TRP",
    "MOTOR_TRP",
}
_EB_WARNING_FLAGS = {
    "GENERAL_ERROR",
    "EB_FDIR_ALARM",
    "WATCHDOG_TIMEOUT_DETECTED",
    "NO_RET_RECEIVED",
    "NO_HEALTHY_ASW_IMAGE",
    "PATCH_WRITING_ERROR",
    "RS422_RECEIVE_ERROR",
    "RS422_TRANSMIT_ERROR",
    "RS485_RECEIVE_ERROR",
    "RS485_TRANSMIT_ERROR",
}
_EB_FDIR_FLAGS = {
    "EB_PLUS_12V_SUPPLY",
    "EB_MINUS_12V_SUPPLY",
    "EB_PLUS_5V_SUPPLY",
    "EB_PLUS_3V3_SUPPLY",
    "PROCESSOR_INTERNAL_TEMPERATURE",
    "INTERNAL_TRP_TEMPERATURE",
    "PSU_BOARD_TEMPERATURE",
}


def create_set_psu_card_profiles(*, ch1_card: Any, ch2_card: Any, ch3_card: Any, ch4_card: Any) -> Any:
    """Create mode-dependent PSU card profile callback bound to PSU card controllers."""

    def set_psu_card_profiles(mode: str) -> None:
        ob_mode = mode == "OB"
        ch1_card.apply_profile(
            title=" CH1 +12V I",
            visible=ob_mode,
            live_voltage_key="CH1_V",
            live_current_key="CH1_I",
            replay_channels=["CH1"],
            show_enabled_toggle=False,
        )
        ch2_card.apply_profile(
            title=" CH2 -12V I",
            visible=ob_mode,
            live_voltage_key="CH2_V",
            live_current_key="CH2_I",
            replay_channels=["CH2"],
            show_enabled_toggle=False,
        )
        ch3_card.apply_profile(
            title=" CH3 +5V I" if ob_mode else " CH3 ROVHTR I",
            visible=True,
            live_voltage_key="CH3_V" if ob_mode else "PSU_ROV_HTR_V",
            live_current_key="CH3_I" if ob_mode else "PSU_ROV_HTR_I",
            replay_channels=["CH3"],
            show_enabled_toggle=(not ob_mode),
        )
        ch4_card.apply_profile(
            title=" CH4 ROVHTR I" if ob_mode else " CH4 +28V I",
            visible=True,
            live_voltage_key="CH4_V" if ob_mode else "PSU_EB_V",
            live_current_key="CH4_I" if ob_mode else "PSU_EB_I",
            replay_channels=["CH4"],
            show_enabled_toggle=True,
            show_lisn_toggle=(not ob_mode),
        )

    return set_psu_card_profiles


def create_set_psu_log_path(*, state: dict[str, Any], logger: Any) -> Any:
    """Create PSU replay-log setter callback bound to current state."""

    def set_psu_log_path(psu_log_path: str | None) -> bool:
        replay = state["psu_replay"]
        if not psu_log_path:
            reset_psu_replay(replay)
            return False

        records = psu_log_utility.load_psu_channel_samples(psu_log_path)
        now = datetime.now()
        replay["enabled"] = bool(records)
        replay["source_path"] = psu_log_path
        replay["records"] = records
        replay["index"] = 0
        replay["hk_anchor"] = now if records else None
        replay["latest_hk_time"] = now if records else None
        replay["psu_anchor"] = records[0]["TIME"] if records else None

        if records:
            logger.info("Loaded %d PSU replay samples from %s", len(records), psu_log_path)
            return True

        logger.warning("No valid PSU replay samples found in %s", psu_log_path)
        return False

    return set_psu_log_path


def ingest_live_psu_sample(state: dict[str, Any], psu_cards: list[Any], psu_sample: dict[str, Any]) -> None:
    """Feed live PSU samples into MA buffers without plotting each one."""
    set_latest_psu(psu_sample)
    update_psu_readings(state, psu_sample)
    update_psu_cards(psu_cards, psu_sample, plot_sample=False)
    sync_master = state.get("sync_ob_master_toggle_value")
    if callable(sync_master):
        sync_master()


def reset_psu_replay(replay: dict[str, Any]) -> None:
    replay["enabled"] = False
    replay["source_path"] = None
    replay["records"] = []
    replay["index"] = 0
    replay["hk_anchor"] = None
    replay["latest_hk_time"] = None
    replay["psu_anchor"] = None


def update_psu_cards(psu_cards: list[Any], psu_sample: dict[str, Any], *, plot_sample: bool = True) -> None:
    for card in psu_cards:
        status_key = card.channel.get("status_key")
        if isinstance(status_key, str) and status_key in psu_sample and psu_sample.get(status_key) is not None:
            card.set_enabled_from_psu(bool(psu_sample.get(status_key)))
        current_key = card.channel.get("live_current_key")
        if isinstance(current_key, str):
            if plot_sample:
                card.push_sample(psu_sample.get("TIME"), psu_sample.get(current_key))
            else:
                card.ingest_sample(psu_sample.get(current_key))


def update_psu_readings(state: dict[str, Any], psu_sample: dict[str, Any]) -> None:
    readings = state["last_psu_readings"]
    for key in _PSU_SAMPLE_KEYS:
        source_key = "STATUS" if key == "status" else key
        readings[key] = psu_sample.get(source_key)


# endregion


# ---------------------------------------------------------------------------
# Script checks — HK / POST verification
# ---------------------------------------------------------------------------
# region Script checks — HK / POST verification


async def perform_acq_check(
    acq_mode: int = 0,
    acq_duration_s: int = 0,
    acq_timeout_s: float | None = None,
    acq_sample_time_ms: int = 0,
) -> None:
    """Async wrapper that runs the synchronous acquisition check in an executor."""
    await run.io_bound(lambda: perform_acq_check_sync(acq_mode, acq_duration_s, acq_timeout_s, acq_sample_time_ms))


def perform_acq_check_sync(
    acq_mode: int = 0,
    acq_duration_s: int = 0,
    acq_timeout_s: float | None = None,
    acq_sample_time_ms: int = 0,
    check_state6: bool = True,
    *,
    readings: dict[str, Any] | None = None,
    errors_out: list[str] | None = None,
) -> None:
    """Synchronous acquisition wait helper. Blocks until acquisition completes or timeout/abort.

    Completion is determined by firmware state: waits for CURRENT_OPERATING_STATE to
    enter 0x08 (Acquisition), then waits for it to leave 0x08 (back to Standby/Safe).
    This is correct regardless of acquisition mode, sample spacing, or packet count.

    For mode 2 (Fixed-point) acquisitions the firmware runs a setup phase (ADC
    initialisation / pre-scan) *inside* the 0x08 state before data collection begins.
    The firmware enforces a minimum spacing of 250 ms even when a smaller value is
    configured.  The timeout is therefore:

        timeout = acq_duration_s + setup_overhead + 120 s safety margin

    where setup_overhead = (effective_spacing_ms / 250) * 360 s  (6 min at the 250 ms
    minimum, scaling linearly for larger spacings).
    effective_spacing_ms = max(acq_sample_time_ms, 250) when acq_sample_time_ms > 0,
    otherwise 250 ms is assumed.

    All other modes default to 300 s.

    Args:
        acq_mode: acquisition mode (2 = Fixed-point; others use the default 300 s timeout).
        acq_duration_s: ACQ_DURATION field from SET_ACQ_CONFIGS, in seconds.
        acq_timeout_s: override the computed timeout (seconds).
        acq_sample_time_ms: ACQ_SAMPLE_TIME field from SET_ACQ_CONFIGS, in ms.
            Used to compute effective spacing; values below 250 are clamped to 250.
        check_state6: run the t+150s State 6 power consumption check.
        readings: optional output for the measured State 6 and completion readings.
        errors_out: optional output for State 6 power-check errors.

    This can be called from synchronous script code (e.g. inside `run_fft`).
    For async callers, use `await perform_acq_check()` which delegates to `run.io_bound`.
    """
    if acq_timeout_s is None:
        if acq_mode == 2 and acq_duration_s > 0:
            effective_spacing_ms = max(acq_sample_time_ms, 250) if acq_sample_time_ms > 0 else 250
            # Setup overhead: firmware ADC setup inside 0x08 takes ~4 min at 250 ms
            # minimum spacing; scale linearly for larger spacings.
            setup_overhead_s = int(effective_spacing_ms / 250 * 360)
            safety_s = 120
            acq_timeout_s = acq_duration_s + setup_overhead_s + safety_s
            info_log.debug(
                "Mode 2 acquisition: effective_spacing=%d ms, setup_overhead=%d s,"
                " timeout=%d s (duration %d s + setup %d s + safety %d s)",
                effective_spacing_ms,
                setup_overhead_s,
                acq_timeout_s,
                acq_duration_s,
                setup_overhead_s,
                safety_s,
            )
        else:
            acq_timeout_s = 300
    _ACQ_STATE = 0x08
    start_time = time.monotonic()
    _acq_150s_checked = not check_state6
    received_start = eb_packet_utility.get_sci_received_count()
    sent_start: int | None = None
    progress = ProgressNotifier(f"Acquisition wait: 0s / {acq_timeout_s:.0f}s")

    info_log.debug("Starting acquisition wait: waiting for CURRENT_OPERATING_STATE=0x08...")

    # --- Phase 1: wait for the EB to enter Acquisition state ---
    while True:
        if is_aborted():
            progress.finish()
            notify_negative("Acquisition aborted by user.")
            raise RuntimeError("Acquisition aborted by user.")
        if time.monotonic() - start_time > acq_timeout_s:
            progress.finish()
            notify_negative("Timeout waiting for acquisition to start.")
            raise TimeoutError("Timeout waiting for acquisition to start.")
        latest_hk = get_latest_hk()
        if latest_hk is not None and getattr(latest_hk, "CURRENT_OPERATING_STATE", None) == _ACQ_STATE:
            sci_count = getattr(latest_hk, "SCIENCE_PACKETS_SENT", 0)
            sent_start = int(sci_count or 0)
            info_log.info(
                "Acquisition started (CURRENT_OPERATING_STATE=0x08), initial SCIENCE_PACKETS_SENT=%s",
                sci_count,
            )
            break
        progress.update(
            f"Acquisition wait: waiting to start ({time.monotonic() - start_time:.0f}s / {acq_timeout_s:.0f}s)"
        )
        time.sleep(1)

    # --- Phase 2: wait for the EB to leave Acquisition state ---
    while True:
        if is_aborted():
            progress.finish()
            notify_negative("Acquisition aborted by user.")
            raise RuntimeError("Acquisition aborted by user.")
        if time.monotonic() - start_time > acq_timeout_s:
            progress.finish()
            notify_negative("Timeout waiting for acquisition to complete.")
            raise TimeoutError("Timeout waiting for acquisition to complete.")

        latest_hk = get_latest_hk()
        if latest_hk is None:
            time.sleep(1)
            continue

        # One-shot PSU current check at t+150s from ACQ sequence start.
        # This aligns with the SCI flow timing where cooldown/homing/dark/start-move
        # typically occupy most of the first ~150 s before science data collection.
        if not _acq_150s_checked and time.monotonic() - start_time >= _SCI_ACQ_TRIGGER_S:
            _acq_150s_checked = True
            try:
                errors: list[str] = []
                ch4_current_ma = windowed_consumption_check(
                    ["State6"],
                    errors,
                    latest_hk,
                )
                thrm = getattr(latest_hk, "THRM_STATUS", None)
                _record_state_check_report(readings, errors_out, errors, latest_hk, ch4_current_ma)
                if errors:
                    count = len(errors)
                    numbered = [f"{i + 1}. {err.strip()}" for i, err in enumerate(errors)]
                    info_log.warning(
                        "SCI ACQ t+150s check \u2014 %d error%s: %s",
                        count,
                        "s" if count != 1 else "",
                        "; ".join(numbered),
                    )
                    notify_negative(
                        f"SCI ACQ t+150s check failed ({count} error{'s' if count != 1 else ''}):\n"
                        + "\n".join(numbered)
                    )
                else:
                    msg = (
                        f"Power State 6 : SCI ACQ (windowed) \u2014 PSU_EB_I median: {ch4_current_ma:.2f} mA, "
                        f"CURRENT_OPERATING_STATE: {getattr(latest_hk, 'CURRENT_OPERATING_STATE', None)}, "
                        f"THRM_STATUS.HMS: {getattr(thrm, 'HMS', 0)}, THRM_STATUS.HDS: {getattr(thrm, 'HDS', 0)}, "
                        f"TEC_SETPOINT: {getattr(latest_hk, 'TEC_SETPOINT', None)}"
                    )
                    info_log.info(msg)
                    notify_positive(msg)
            except Exception as exc:
                info_log.warning("Acquisition t+150s PSU current check failed: %s", exc)
                if errors_out is not None:
                    errors_out.append(f"Acquisition t+150s PSU current check failed: {exc}")

        cos = getattr(latest_hk, "CURRENT_OPERATING_STATE", None)
        if cos != _ACQ_STATE:
            sci_count_end = getattr(latest_hk, "SCIENCE_PACKETS_SENT", "N/A")
            acq_complete_msg = (
                (f"Acquisition complete — CURRENT_OPERATING_STATE=0x{cos:02X}, SCIENCE_PACKETS_SENT={sci_count_end}")
                if cos is not None
                else (f"Acquisition complete — SCIENCE_PACKETS_SENT={sci_count_end}")
            )
            info_log.info(
                "Acquisition complete: CURRENT_OPERATING_STATE=0x%02X, SCIENCE_PACKETS_SENT=%s at %s",
                cos if cos is not None else 0,
                sci_count_end,
                getattr(latest_hk, "TIME", None),
            )
            notify_positive(acq_complete_msg)
            if readings is not None:
                readings["Completion operating state"] = cos
                readings["SCIENCE_PACKETS_SENT"] = sci_count_end
                readings["State 6 power check"] = "Performed" if check_state6 and _acq_150s_checked else "Not performed"
            _warn_if_sci_packets_missing(sent_start, sci_count_end, received_start)
            # Do not consume from const.sci_queue here: the telemetry poll loop
            # owns draining it into the packet viewer/state, and stealing a
            # packet for logging would make it disappear from the GUI.
            progress.finish()
            return

        info_log.info(
            "Acquisition in progress (CURRENT_OPERATING_STATE=0x08, SCIENCE_PACKETS_SENT=%s)",
            getattr(latest_hk, "SCIENCE_PACKETS_SENT", "N/A"),
        )
        progress.update(
            f"Acquisition in progress: {time.monotonic() - start_time:.0f}s / {acq_timeout_s:.0f}s "
            f"(SCI this acq: EB sent {_sci_delta(sent_start, getattr(latest_hk, 'SCIENCE_PACKETS_SENT', None))}, "
            f"received {eb_packet_utility.get_sci_received_count() - received_start})"
        )
        time.sleep(10)


def _sci_delta(sent_start: int | None, sent_now: Any) -> int | str:
    try:
        return max(0, int(sent_now) - int(sent_start))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "N/A"


def _warn_if_sci_packets_missing(
    sent_start: int | None, sent_end: Any, received_start: int, grace_s: float = 15.0
) -> None:
    """Warn when the EB HK counter says SCI packets were sent but fewer arrived in the RS422 log."""
    sent = _sci_delta(sent_start, sent_end)
    if not isinstance(sent, int) or sent == 0:
        return
    # The RS422 log is parsed on the telemetry poll, so give late packets a chance to be decoded.
    deadline = time.monotonic() + grace_s
    received = eb_packet_utility.get_sci_received_count() - received_start
    while received < sent and time.monotonic() < deadline and not is_aborted():
        time.sleep(1)
        received = eb_packet_utility.get_sci_received_count() - received_start
    if received >= sent:
        info_log.info("SCI packets this acquisition: EB sent %d, received %d", sent, received)
        return
    msg = (
        f"Warning: EB reported {sent} SCI packet{'s' if sent != 1 else ''} sent this acquisition "
        f"(SCIENCE_PACKETS_SENT {sent_start} -> {sent_end}) but only {received} arrived in the RS422 log. "
        "Known EB software issue - science data for this acquisition may be missing."
    )
    info_log.warning(msg)
    notify(msg, color="warning")


def perform_hk_check(hk: Any = None, post: Any = None, hk_type: str = "hk") -> dict:
    """
    Perform HK or POST check as in ebgui, returning a result dict.
    Args:
        hk: Housekeeping packet object (for regular HK)
        post: POST packet object (for post HK)
        hk_type: 'hk' for regular HK, 'post' for POST check
    Returns:
        dict with keys: 'passed' (bool), 'details' (list of str)
    """
    result = {"passed": True, "details": []}

    if hk_type == "hk":
        if hk is None:
            result["passed"] = False
            result["details"].append("No HK data available.")
            return result
        # Values are already decoded, just check ranges

        # Extract, convert, and check for None before formatting
        try:
            eb_12v_raw = hk.EB_MEAS_MAIN_12V
            eb_neg12v_raw = hk.EB_MEAS_MAIN_NEG12V
            eb_5v_raw = hk.EB_MEAS_5V
            eb_3v3_raw = hk.EB_MEAS_3V3
            eb_tec_v_raw = hk.EB_MEAS_TEC_RAIL
            eb_0v_raw = hk.EB_0V_ADC_READING
            eb_tec_i_raw = hk.EB_TEC_DRIVE_CURRENT
        except Exception as e:
            result["passed"] = False
            result["details"].append(f"Missing HK field: {e}")
            return result

        value_fields = [
            (eb_12v_raw, "EB_MEAS_MAIN_12V"),
            (eb_neg12v_raw, "EB_MEAS_MAIN_NEG12V"),
            (eb_5v_raw, "EB_MEAS_5V"),
            (eb_3v3_raw, "EB_MEAS_3V3"),
            (eb_tec_v_raw, "EB_MEAS_TEC_RAIL"),
            (eb_0v_raw, "EB_0V_ADC_READING"),
            (eb_tec_i_raw, "EB_TEC_DRIVE_CURRENT"),
        ]
        for val, name in value_fields:
            if val is None:
                result["passed"] = False
                result["details"].append(f"{name}. Got: None, Expected: numeric HK value")

        # Only check ranges if all values are present
        if all(val is not None for val, _ in value_fields):
            eb_12v = eb_12v_raw * 0.000400543
            eb_neg12v = eb_neg12v_raw * -0.00038147
            eb_5v = eb_5v_raw * 0.000152829
            eb_3v3 = eb_3v3_raw * 0.0000763
            eb_tec_v = eb_tec_v_raw * 0.0000763
            eb_0v = eb_0v_raw * 0.0000763
            eb_tec_i = eb_tec_i_raw * 0.0000162
            checks = [
                (11.0 <= eb_12v <= 13.0, f"EB 12V out of range. Got: {eb_12v:.2f} V, Expected: 11.00 to 13.00 V"),
                (
                    -13.0 <= eb_neg12v <= -11.0,
                    f"EB -12V out of range. Got: {eb_neg12v:.2f} V, Expected: -13.00 to -11.00 V",
                ),
                (4.5 <= eb_5v <= 5.5, f"EB 5V out of range. Got: {eb_5v:.2f} V, Expected: 4.50 to 5.50 V"),
                (2.8 <= eb_3v3 <= 3.8, f"EB 3V3 out of range. Got: {eb_3v3:.2f} V, Expected: 2.80 to 3.80 V"),
                (-0.5 <= eb_tec_v <= 0.5, f"EB TEC V out of range. Got: {eb_tec_v:.2f} V, Expected: -0.50 to 0.50 V"),
                (-0.5 <= eb_0v <= 0.5, f"EB 0V out of range. Got: {eb_0v:.2f} V, Expected: -0.50 to 0.50 V"),
                (
                    -0.1 <= eb_tec_i <= 0.1,
                    f"EB TEC I out of range. Got: {eb_tec_i:.4f} A, Expected: -0.1000 to 0.1000 A",
                ),
            ]
            for ok, msg in checks:
                if not ok:
                    result["passed"] = False
                    result["details"].append(msg)

        scalar_checks = (
            ("TCS_REJECTED", 0),
            ("INSTRUMENT_STATUS_FLAGS", 25604),
            ("ERROR_FLAGS", 0),
            ("WARNING_FLAGS", 0),
            ("FDIR_ALARM_FLAGS", 0),
            ("FDIR_WARNING_FLAGS", 0),
        )
        for field, expected in scalar_checks:
            actual = getattr(hk, field, None)
            if actual != expected:
                result["passed"] = False
                result["details"].append(f"{field} mismatch. Got: {actual}, Expected: {expected}")
        return result

    elif hk_type == "post":
        if post is None:
            result["passed"] = False
            result["details"].append("No POST data available.")
            return result
        # Convert POST voltages/temps if present
        try:
            tm_12v = post.TM_12V * 0.000400543 if hasattr(post, "TM_12V") else None
            tm_neg12v = post.TM_NEG12V * -0.00038147 if hasattr(post, "TM_NEG12V") else None
            tm_5v = post.TM_5V * 0.000152829 if hasattr(post, "TM_5V") else None
            tm_3v3 = post.TM_3V3 * 0.0000763 if hasattr(post, "TM_3V3") else None
            eb_processor_temp = (
                post.EB_PROCESSOR_TEMP * 0.01637198 - 273 if hasattr(post, "EB_PROCESSOR_TEMP") else None
            )
            tec_detector_temp = (
                post.TEC_DETECTOR_TEMP * -0.001830011 + 51.27039922 if hasattr(post, "TEC_DETECTOR_TEMP") else None
            )
        except Exception as e:
            result["passed"] = False
            result["details"].append(f"POST conversion error: {e}")
            return result

        # POST checks as in ebgui
        all_post_passed = (
            getattr(post, "POST_WARNING_FLAGS", None) == 0
            and getattr(post, "POST_ERROR_FLAGS", None) == 0
            and getattr(post, "NUM_BAD_FLASH_BLOCKS", None) == 0
            and getattr(post, "NUM_BAD_SRAM_BLOCKS", None) == 0
            and all(getattr(post, field, None) == expected for field, expected in config.POST_EXPECTED_CRC.items())
        )
        if not all_post_passed:
            result["passed"] = False
            # Add details for each failed check
            post_zero_checks = (
                "POST_WARNING_FLAGS",
                "POST_ERROR_FLAGS",
                "NUM_BAD_FLASH_BLOCKS",
                "NUM_BAD_SRAM_BLOCKS",
            )
            for field in post_zero_checks:
                actual = getattr(post, field, None)
                if actual != 0:
                    result["details"].append(f"{field} mismatch. Got: {actual}, Expected: 0")
            for field, expected in config.POST_EXPECTED_CRC.items():
                actual = getattr(post, field, None)
                if actual != expected:
                    actual_text = f"0x{actual:04X}" if isinstance(actual, int) else str(actual)
                    expected_text = f"0x{expected:04X}" if isinstance(expected, int) else str(expected)
                    result["details"].append(f"{field} mismatch. Got: {actual_text}, Expected: {expected_text}")
            # Keep converted engineering values separate from actual POST failures.
            # They are diagnostic context only and must not inflate the error count.
            post_engineering_values = (
                ("TM_12V", tm_12v, const.WLIM_EB_12V, "V"),
                ("TM_NEG12V", tm_neg12v, const.WLIM_EB_NEG12V, "V"),
                ("TM_5V", tm_5v, const.WLIM_EB_5V, "V"),
                ("TM_3V3", tm_3v3, const.WLIM_EB_3V3, "V"),
                ("EB_PROCESSOR_TEMP", eb_processor_temp, const.WLIM_EB_MCU_INTERNAL_TEMP, "C"),
                ("TEC_DETECTOR_TEMP", tec_detector_temp, const.WLIM_TPR, "C"),
            )
            diagnostics = []
            for field, actual, expected_range, unit in post_engineering_values:
                if actual is None:
                    continue
                low, high = expected_range
                status = "PASS" if low <= actual <= high else "OUT OF RANGE"
                diagnostics.append(
                    f"{field}: Got: {actual:.2f} {unit}, Expected: {low:.2f} to {high:.2f} {unit} [{status}]"
                )
            if diagnostics:
                result["diagnostics"] = diagnostics
        return result

    else:
        result["passed"] = False
        result["details"].append(f"Unknown hk_type: {hk_type}")
        return result


async def perform_homing_check(homing_timeout_s: float = 60.0) -> None:
    """Async wrapper that runs the synchronous homing check in an executor."""
    await run.io_bound(lambda: perform_homing_check_sync(homing_timeout_s))


def perform_homing_check_sync(homing_timeout_s: float = 90) -> None:
    """Synchronous homing wait helper. Blocks until HOMING_COMPLETE or timeout/abort.

    This can be called from synchronous script code (e.g. inside `run_emc_init`).
    For async callers, use `await perform_homing_check()` which delegates to `run.io_bound`.
    """
    start_time = time.monotonic()
    info_log.debug("Starting homing wait loop for HOMING_COMPLETE flag...")
    progress = ProgressNotifier(f"Homing wait: 0s / {homing_timeout_s:.0f}s")

    homing_complete = 0
    while homing_complete == 0:
        latest_hk = get_latest_hk()

        # Allow user to abort the script while waiting
        if is_aborted():
            info_log.warning("Homing wait aborted by user.")
            progress.finish()
            notify_negative("Homing aborted by user.")
            raise RuntimeError("Homing aborted by user.")

        info_log.debug("No HK packet yet while waiting for HOMING_COMPLETE; continuing to wait.")

        # Prefer the decoded bitfield object `INSTR_STATUS_FLAGS` (set by parser).
        # Fall back to the numeric `INSTRUMENT_STATUS_FLAGS` if necessary.
        if latest_hk is None:
            homing_complete = 0
        elif hasattr(latest_hk, "INSTR_STATUS_FLAGS"):
            homing_complete = int(getattr(latest_hk.INSTR_STATUS_FLAGS, "HOMING_COMPLETE", 0) or 0)
        elif hasattr(latest_hk, "INSTRUMENT_STATUS_FLAGS"):
            # Fallback: flags are MSB-first, so HOMING_COMPLETE is bit 15.
            try:
                homing_complete = (int(getattr(latest_hk, "INSTRUMENT_STATUS_FLAGS", 0)) >> 15) & 0x1
            except Exception:
                homing_complete = 0
        else:
            homing_complete = 0

        hk_time = getattr(latest_hk, "TIME", None)
        if homing_complete == 1:
            info_log.info("HOMING_COMPLETE detected in HK at %s", hk_time)
            progress.finish()
            return
        if time.monotonic() - start_time > homing_timeout_s:
            info_log.error("Timeout waiting for HOMING_COMPLETE flag in HK telemetry (waited %ss)", homing_timeout_s)
            progress.finish()
            notify_negative("Timeout waiting for HOMING_COMPLETE flag in HK telemetry.")
            raise TimeoutError("Timeout waiting for HOMING_COMPLETE flag in HK telemetry.")
        progress.update(f"Homing wait: {time.monotonic() - start_time:.0f}s / {homing_timeout_s:.0f}s")
        time.sleep(1)  # Sleep briefly to avoid busy-waiting


# endregion


# ---------------------------------------------------------------------------
# Script checks — heater state verification
# ---------------------------------------------------------------------------
# region Script checks — heater state verification


def verify_heater_states(
    latest_hk: Any,
    errors: list[str],
) -> list[str]:
    if latest_hk is None:
        errors.append("No HK packet available for heater state check.")
        return []

    thrm = getattr(latest_hk, "THRM_STATUS", None)
    if thrm is None:
        errors.append("THRM_STATUS not available in HK packet.")
        return []

    mm = bool(getattr(thrm, "MM", 0))
    ma = bool(getattr(thrm, "MA", 0))
    dm = bool(getattr(thrm, "DM", 0))
    da = bool(getattr(thrm, "DA", 0))
    act_hms = int(getattr(thrm, "HMS", 0))
    act_hds = int(getattr(thrm, "HDS", 0))

    info_log.info(
        "Heater check \u2014 THRM_STATUS: MM=%s MA=%s HMS=%s | DM=%s DA=%s HDS=%s",
        int(mm),
        int(ma),
        act_hms,
        int(dm),
        int(da),
        act_hds,
    )

    # --- Step 1: TC command check (informational only) ---
    if not mm and not ma:
        info_log.info("Heater check \u2014 Mech heater not commanded ON (THRM_STATUS.MM=0, MA=0)")
    if not dm and not da:
        info_log.info("Heater check \u2014 Det heater not commanded ON (THRM_STATUS.DM=0, DA=0)")

    # --- Step 2: Temperature / status check ---
    # Firmware bang-bang hysteresis algorithm:
    #   HMS/HDS turns ON  when BOTH current AND previous TRP < ON_SP  (OB_THERMAL_*_MIN)
    #   HMS/HDS turns OFF when BOTH current AND previous TRP > OFF_SP (OB_THERMAL_*_MAX)
    #   Otherwise (TRP in band): maintain PREV_STATUS — state is history-dependent.
    # _heater_state_history persists the last definitive expected state across poll cycles
    # so that the hysteresis band case can still be verified.  When transitioning from
    # disabled → auto the firmware initialises PREV_STATUS to OFF, which is reflected here.
    def _check_htr(label, mode_manual, mode_auto, trp_attr, on_sp_attr, off_sp_attr, act):
        h = _heater_state_history[label]
        if mode_manual:
            # Manual mode: heater must always be physically ON; record known state
            if not h["prev_manual"]:
                info_log.info("%s heater: Manual mode enabled — recording expected=ON", label)
            h.update(last_expected=True, prev_auto=False, prev_manual=True)
            if not act:
                errors.append(f"{label} heater in Manual mode but HMS/HDS={act} (expected 1)")
        elif mode_auto:
            # TRP packs the 12-bit ADC value into the upper bits of the 16-bit
            # field; the ON/OFF setpoints are already plain 12-bit values.
            raw_trp = getattr(latest_hk, trp_attr, None)
            trp = raw_trp >> 4 if raw_trp is not None else None
            on_sp = getattr(latest_hk, on_sp_attr, None)
            off_sp = getattr(latest_hk, off_sp_attr, None)
            # Detect disabled→auto transition (firmware PREV_STATUS initialises to OFF)
            newly_enabled = not h["prev_auto"] and not h["prev_manual"]
            if newly_enabled:
                info_log.info(
                    "%s heater: Auto mode just enabled — TRP=%s [ON_SP=%s, OFF_SP=%s] — firmware initialises to OFF",
                    label,
                    trp,
                    on_sp,
                    off_sp,
                )
                if h["last_expected"] is None:
                    h["last_expected"] = False  # firmware default at first enable
            h.update(prev_auto=True, prev_manual=False)
            if trp is None or on_sp is None or off_sp is None:
                info_log.warning(
                    "%s heater Auto mode — fields missing (%s=%s, %s=%s, %s=%s), skipping check",
                    label,
                    trp_attr,
                    trp,
                    on_sp_attr,
                    on_sp,
                    off_sp_attr,
                    off_sp,
                )
                return
            h["last_trp"] = trp
            info_log.info(
                "%s heater Auto mode \u2014 TRP=%s [ON_SP=%s, OFF_SP=%s] last_expected=%s",
                label,
                trp,
                on_sp,
                off_sp,
                h["last_expected"],
            )
            if trp < on_sp:
                # Below ON threshold — heater must be ON; record boundary crossing
                h["last_expected"] = True
                if not act:
                    errors.append(f"{label} heater: TRP={trp} < ON_SP={on_sp} \u2192 expected ON, got HMS/HDS={act}")
            elif trp > off_sp:
                # Above OFF threshold — heater must be OFF; record boundary crossing
                h["last_expected"] = False
                if act:
                    errors.append(f"{label} heater: TRP={trp} > OFF_SP={off_sp} \u2192 expected OFF, got HMS/HDS={act}")
            else:
                # TRP in hysteresis band [ON_SP, OFF_SP] — use recorded history
                exp = h["last_expected"]
                if newly_enabled:
                    # First poll for this heater (EGSE started mid-test, or heater was
                    # re-enabled while TRP was already in band).  We have no reliable
                    # history of which boundary was last crossed, so we cannot verify
                    # the state — use the actual HMS/HDS as the baseline for future polls.
                    info_log.info(
                        "%s heater: auto first detected with TRP=%s in band [%s, %s],"
                        " HMS/HDS=%s — using as history baseline (cannot verify initial state)",
                        label,
                        trp,
                        on_sp,
                        off_sp,
                        act,
                    )
                    h["last_expected"] = bool(act)
                elif exp is True:
                    if not act:
                        errors.append(
                            f"{label} heater: TRP={trp} in band [{on_sp}, {off_sp}],"
                            f" last boundary \u2192 ON, but HMS/HDS={act}"
                        )
                    else:
                        info_log.info(
                            "%s heater: TRP=%s in band [%s, %s] \u2014 last boundary \u2192 ON, HMS/HDS=%s (OK)",
                            label,
                            trp,
                            on_sp,
                            off_sp,
                            act,
                        )
                elif exp is False:
                    if act:
                        errors.append(
                            f"{label} heater: TRP={trp} in band [{on_sp}, {off_sp}],"
                            f" last boundary \u2192 OFF, but HMS/HDS={act}"
                        )
                    else:
                        info_log.info(
                            "%s heater: TRP=%s in band [%s, %s] \u2014 last boundary \u2192 OFF, HMS/HDS=%s (OK)",
                            label,
                            trp,
                            on_sp,
                            off_sp,
                            act,
                        )
                else:
                    # No history yet (e.g. TRP has been in band since enable) — cannot verify
                    info_log.info(
                        "%s heater: TRP=%s in band [%s, %s] \u2014 no boundary history, HMS/HDS=%s",
                        label,
                        trp,
                        on_sp,
                        off_sp,
                        act,
                    )
        else:
            # Heater not commanded — reset history ready for the next enable event
            h.update(last_expected=None, last_trp=None, prev_auto=False, prev_manual=False)

    _check_htr("Mech", mm, ma, "OB_MECHANISM_TRP", "OB_THERMAL_MECH_MIN", "OB_THERMAL_MECH_MAX", act_hms)
    _check_htr("Det", dm, da, "OB_DETECTOR_TRP", "OB_THERMAL_DET_MIN", "OB_THERMAL_DET_MAX", act_hds)

    # Return active heater state names for consumption_check
    states: list[str] = []
    if act_hms:
        states.append("MechHTR")
    if act_hds:
        states.append("DetHTR")
    return states


# Central script runtime control (play / pause / abort)
_SCRIPT_CONTROL = {
    "running": False,
    "pause_event": threading.Event(),
    "abort_event": threading.Event(),
    "current_script": None,
}
# endregion


# ---------------------------------------------------------------------------
# Script checks — RET / state verification
# ---------------------------------------------------------------------------
# region Script checks — RET / state verification


def is_valid_post_packet(post: Any) -> bool:
    return all(hasattr(post, field_name) for field_name in _POST_REQUIRED_FIELDS)


def pull_post_after_ret(timeout_s: float = 6.0, poll_s: float = 0.2) -> Any | None:
    """Return a valid POST packet after RET, retrying queue reads and RS422 refresh."""
    deadline = time.monotonic() + max(0.1, timeout_s)
    while time.monotonic() < deadline:
        while not const.eb_post_queue.empty():
            post = const.eb_post_queue.get()
            if is_valid_post_packet(post):
                return post

        rs422_log_path = getattr(getattr(_nicegui_app.state, "eb_interface", None), "rs422_log_path", None)
        if rs422_log_path:
            try:
                eb_packet_utility.read_pkt(rs422_log_path, latest_only=True)
            except Exception:
                pass

        time.sleep(max(0.05, poll_s))

    return None


def pull_psu_after_ret(timeout_s: float = 6.0, poll_timeout_s: float = 0.5) -> dict[str, Any] | None:
    """Return a fresh PSU monitor sample after RET without draining the plot queue."""
    sample = wait_for_fresh_psu(timeout=timeout_s)
    if isinstance(sample, dict):
        # Use the recent cache window for current smoothing, but preserve freshness.
        smoothed = get_smoothed_latest_psu(_SCRIPT_PSU_MA_WINDOW_SAMPLES)
        return smoothed if isinstance(smoothed, dict) else sample
    cached = get_latest_psu()
    return cached if isinstance(cached, dict) else None


def _record_state_check_report(
    readings: dict[str, Any] | None,
    errors_out: list[str] | None,
    errors: list[str],
    hk: Any = None,
    current_ma: float | None = None,
) -> None:
    """Expose the samples used by a check without fetching different telemetry."""
    if errors_out is not None:
        errors_out.extend(errors)
    if readings is None:
        return
    readings["PSU_EB_I (mA)"] = current_ma
    if hk is not None:
        for field in ("CURRENT_OPERATING_STATE", "TEC_SETPOINT"):
            readings[field] = getattr(hk, field, None)
        tec_raw = getattr(hk, "EB_TEC_DRIVE_CURRENT", None)
        readings["TEC current (A)"] = tec_raw * 0.0000162 if tec_raw is not None else None
        for flag_group, fields in (
            ("INSTR_STATUS_FLAGS", ("OB_MECHANISM_BOARD_ENABLED", "OB_DETECTOR_BOARD_ENABLED")),
            ("THRM_STATUS", ("HMS", "HDS")),
            ("MTR_FLAGS", ("MOVING",)),
        ):
            flags = getattr(hk, flag_group, None)
            for field in fields:
                readings[f"{flag_group}.{field}"] = getattr(flags, field, None)


def verify_power_state(
    state: str,
    *,
    readings: dict[str, Any] | None = None,
    errors_out: list[str] | None = None,
) -> tuple[str, bool]:
    """Verify the OB is in the expected power state.

    Fetches the latest HK and PSU samples from their queues, runs a PSU
    consumption check for *state*, then performs additional HK field checks
    appropriate for that state:

    State2  — OB Heating:           heater verification (via consumption_check)
    State3  — OB Heating + Boards:  State2 checks + mech/det boards enabled
    State4  — OB Heating + TEC:      State3 checks + configured TEC current check
    State5  — Boards + TEC:          boards enabled + configured TEC current check
    State7  — All Active:           State4 checks + motor moving

    Returns (msg, passed) — the same contract as verify_safe_ret / verify_standby_ret.
    Optional readings/errors_out containers receive the actual check measurements
    and errors for structured script pause reports.
    """
    _BOARD_STATES = {"State3", "State4", "State5", "State7"}
    _TEC_STATES = {"State4", "State5", "State7"}

    errors: list[str] = []
    ch4_current_ma: float | None = None

    latest_hk = get_latest_hk()
    fresh_psu = wait_for_fresh_psu(timeout=2.0)
    latest_psu = get_smoothed_latest_psu(_SCRIPT_PSU_MA_WINDOW_SAMPLES) if fresh_psu is not None else None
    if latest_hk is None:
        errors.append(f"Missing HK data for {state} verification")
    if latest_psu is None:
        errors.append(f"Missing fresh PSU monitor data for {state} verification")

    # --- PSU consumption + heater check ---
    if latest_psu is not None:
        ch4_current_ma = consumption_check(state, latest_psu, errors, latest_hk)

    if latest_hk is not None:
        instr = getattr(latest_hk, "INSTR_STATUS_FLAGS", None)

        # --- Boards enabled (State3 / State4 / State5 / State7) ---
        if state in _BOARD_STATES:
            mech_board = bool(getattr(instr, "OB_MECHANISM_BOARD_ENABLED", 0))
            det_board = bool(getattr(instr, "OB_DETECTOR_BOARD_ENABLED", 0))
            if not mech_board:
                errors.append("Mech board not enabled (INSTR_STATUS_FLAGS.OB_MECHANISM_BOARD_ENABLED=0)")
            if not det_board:
                errors.append("Det board not enabled (INSTR_STATUS_FLAGS.OB_DETECTOR_BOARD_ENABLED=0)")

        # --- TEC clamped at configured current +/- tolerance (State4 / State5 / State7) ---
        if state in _TEC_STATES:
            tec_target_a = float(config.TEC_EXPECTED_CURRENT_A)
            tec_tolerance_a = float(config.TEC_CURRENT_TOLERANCE_A)
            tec_current = getattr(latest_hk, "EB_TEC_DRIVE_CURRENT", 0) * 0.0000162
            if abs(tec_current - tec_target_a) > tec_tolerance_a:
                # TEC may still be ramping — poll for up to 30 s before failing.
                info_log.debug(
                    "%s TEC current %.3f A outside %.2f +/- %.2f A; waiting up to 30 s...",
                    state,
                    tec_current,
                    tec_target_a,
                    tec_tolerance_a,
                )
                _tec_ramped = False
                for _ in range(30):
                    time.sleep(1)
                    _poll_hk = get_latest_hk()
                    if _poll_hk is not None:
                        tec_current = getattr(_poll_hk, "EB_TEC_DRIVE_CURRENT", 0) * 0.0000162
                        if abs(tec_current - tec_target_a) <= tec_tolerance_a:
                            latest_hk = _poll_hk  # use the fresher HK for remaining checks
                            _tec_ramped = True
                            break
                if not _tec_ramped:
                    errors.append(
                        f"TEC current not clamped at {tec_target_a:.2f} A: {tec_current:.3f} A "
                        f"(allowed {tec_target_a - tec_tolerance_a:.2f} to {tec_target_a + tec_tolerance_a:.2f} A)"
                    )

        # --- Motor moving (State7) ---
        if state == "State7":
            mtr = getattr(latest_hk, "MTR_FLAGS", None)
            if not bool(getattr(mtr, "MOVING", 0)):
                errors.append("Motor not moving (MTR_FLAGS.MOVING=0)")

    cos = getattr(latest_hk, "CURRENT_OPERATING_STATE", None) if latest_hk is not None else None
    _record_state_check_report(readings, errors_out, errors, latest_hk, ch4_current_ma)

    if errors:
        count = len(errors)
        numbered = [f"{i + 1}. {err.strip()}" for i, err in enumerate(errors)]
        info_log.error(
            "%s verification failed: %d error%s — PSU_EB_I: %s mA",
            state,
            count,
            "s" if count != 1 else "",
            f"{ch4_current_ma:.2f}" if ch4_current_ma is not None else "N/A",
        )
        msg = f"{state} verification failed: {count} error{'s' if count != 1 else ''}:\n" + "\n".join(numbered)
        notify(msg, color="negative")
        return msg, False
    else:
        msg = f"Power {state} OK — PSU_EB_I: {ch4_current_ma:.2f} mA, CURRENT_OPERATING_STATE: {cos}"
        info_log.info(msg)
        notify(msg, color="positive")
        return msg, True


def verify_safe_ret(
    *, readings: dict[str, Any] | None = None, errors_out: list[str] | None = None
) -> tuple[str, bool]:
    """Verify SAFE RET, optionally collecting readings/errors for a pause report."""
    errors = []
    # ?RET and first check
    # This block performs the SAFE RET verification after issuing a RET and HK request.
    latest_post = pull_post_after_ret(timeout_s=6.0)
    latest_psu = pull_psu_after_ret(timeout_s=6.0)

    if latest_post is None:
        errors.append("POST packet availability. Got: no POST packet, Expected: POST packet after RET")
    if latest_psu is None:
        errors.append("PSU sample availability. Got: no PSU sample, Expected: PSU sample after RET")

    ch4_current_ma = None
    if latest_psu is not None:
        consumption_check("State1", latest_psu, errors)
        ch4_current_ma = float(latest_psu.get("PSU_EB_I") or 0.0) * 1000.0

    result = None
    if latest_post is not None:
        result = perform_hk_check(hk=None, post=latest_post, hk_type="post")

        # Check the POST packet for all required fields and limits
        if not (result and result.get("passed", False)):
            if result and "details" in result and result["details"]:
                errors.extend(result["details"])
            else:
                errors.append(f"POST Packet Check failed with unknown error: {result}")
    _record_state_check_report(readings, errors_out, errors, current_ma=ch4_current_ma)
    if readings is not None:
        readings["POST check"] = result
    if errors:
        count = len(errors)
        numbered = [f"{i + 1}. {err.strip()}" for i, err in enumerate(errors)]
        info_log.error(f"PSU_EB_I: {ch4_current_ma if ch4_current_ma is not None else 'N/A'} mA")
        msg = f"SAFE RET verification failed: {count} error{'s' if count != 1 else ''}:\n" + "\n".join(numbered)
        diagnostics = result.get("diagnostics", []) if isinstance(result, dict) else []
        if diagnostics:
            msg += "\n\nPOST diagnostics (informational):\n" + "\n".join(f"- {line}" for line in diagnostics)
        notify_negative(msg)
        return msg, False
    else:
        msg = f"Power State 1 - SAFE mode: EB PSU I : {ch4_current_ma if ch4_current_ma is not None else 'N/A'} mA, \nPOST Packet Check Result: {result}"
        info_log.info(msg)
        notify_positive(msg)
        return msg, True


def _wait_for_settled_hk(after_hk_sequence: int | None, timeout: float = 5.0):
    """Poll HK packets newer than *after_hk_sequence* until the flag check passes.

    A HK packet that arrives right after STANDBY can still reflect a
    transitional state (e.g. INSTRUMENT_STATUS_FLAGS briefly reading 1028
    instead of the settled 25604) before a later packet catches up. Keep
    accepting newer packets within the timeout budget instead of failing on
    whichever one happens to arrive first.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    seq = after_hk_sequence
    latest_hk = None
    result = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        candidate = wait_for_fresh_hk(timeout=remaining, after_sequence=seq)
        if candidate is None:
            break
        latest_hk = candidate
        seq = eb_packet_utility.get_hk_sequence()
        result = perform_hk_check(hk=candidate, post=None, hk_type="hk")
        if result.get("passed", False):
            break
    return latest_hk, result


def verify_standby_ret(
    after_hk_sequence: int | None = None,
    after_psu_sequence: int | None = None,
    *,
    readings: dict[str, Any] | None = None,
    errors_out: list[str] | None = None,
) -> tuple[str, bool]:
    """Verify STANDBY RET, optionally collecting readings/errors for a pause report."""
    errors = []
    # Wait for telemetry newer than the markers captured before STANDBY. This
    # accepts data that arrived during the normal settling delay instead of
    # accidentally demanding another packet after verification starts, and
    # retries across packets so a transient mismatch doesn't fail the check.
    latest_hk, hk_result = _wait_for_settled_hk(after_hk_sequence, timeout=5.0)
    if latest_hk is None:
        hk_age = eb_packet_utility.get_latest_hk_age_s()
        age_text = "no HK cached" if hk_age is None else f"latest HK is {hk_age:.2f} s old"
        errors.append(
            f"HK packet availability. Got: no HK newer than the STANDBY marker within 5.0 s ({age_text}), "
            "Expected: HK packet after STANDBY"
        )
    fresh_psu = wait_for_fresh_psu(timeout=2.0, after_sequence=after_psu_sequence)
    latest_psu = get_smoothed_latest_psu(_SCRIPT_PSU_MA_WINDOW_SAMPLES) if fresh_psu is not None else None
    if latest_psu is None:
        psu_age = eb_packet_utility.get_latest_psu_age_s()
        age_text = "no PSU sample cached" if psu_age is None else f"latest PSU sample is {psu_age:.2f} s old"
        errors.append(
            f"PSU sample availability. Got: no PSU sample newer than the STANDBY marker within 2.0 s ({age_text}), "
            "Expected: PSU monitor sample after STANDBY"
        )

    ch4_current_ma = None
    if latest_psu is not None:
        consumption_check("Standby", latest_psu, errors)
        ch4_current_ma = float(latest_psu.get("PSU_EB_I") or 0.0) * 1000.0

    result = hk_result
    if latest_hk is not None:
        # Check the HK packet for all required fields and limits
        if not (result and result.get("passed", False)):
            if result and "details" in result and result["details"]:
                errors.extend(result["details"])
            else:
                errors.append(f"HK Check failed with unknown error: {result}")
    _record_state_check_report(readings, errors_out, errors, latest_hk, ch4_current_ma)
    if readings is not None:
        readings["HK check"] = result
    if errors:
        count = len(errors)
        numbered = [f"{i + 1}. {err.strip()}" for i, err in enumerate(errors)]
        info_log.info(f"PSU_EB_I: {ch4_current_ma if ch4_current_ma is not None else 'N/A'} mA")
        msg = f"STANDBY RET verification failed: {count} error{'s' if count != 1 else ''} :\n" + "\n".join(numbered)
        notify_negative(msg)
        return msg, False
    else:
        msg = f"Standby mode: EB PSU I : {ch4_current_ma if ch4_current_ma is not None else 'N/A'} mA, \nHK Check Result: {result}"
        info_log.info(msg)
        notify_positive(msg)
        return msg, True


# endregion


# ---------------------------------------------------------------------------
# Script controls — pause / abort / run state
# ---------------------------------------------------------------------------
# region Script controls — pause / abort / run state


class ScriptAbortRequested(Exception):
    """Raised when a script wait is interrupted by the Stop control."""


def wait_while_paused(poll_s: float = 0.25) -> None:
    """Block the calling (script) thread while Pause is active; still honors Stop.

    This is the single shared gate scripts/checks should call before doing any
    work (sending a command, sleeping, taking a reading) so Pause stops the whole
    script rather than only the OB command path.
    """
    while is_paused() or is_force_paused():
        if is_aborted():
            raise ScriptAbortRequested
        time.sleep(max(float(poll_s), 0.01))


def abortible_sleep(seconds: float, poll_s: float = 0.1) -> None:
    """Sleep while allowing Stop to interrupt and Pause to hold the countdown."""
    deadline = time.monotonic() + max(float(seconds), 0.0)
    interval = max(float(poll_s), 0.01)
    while True:
        if is_aborted():
            raise ScriptAbortRequested
        if is_paused() or is_force_paused():
            pause_started = time.monotonic()
            wait_while_paused(interval)
            deadline += time.monotonic() - pause_started
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(interval, remaining))


class ProgressNotifier:
    """A spinner notification that can be updated/dismissed from any thread.

    Creation and updates are marshalled onto the NiceGUI event loop via
    ``call_soon_threadsafe``, so this is safe to use from script worker threads.
    """

    def __init__(self, message: str, *, color: str = "warning") -> None:
        self._handles: dict[Any, Any] = {}
        self._create(message, color)

    def _run_on_clients(self, action: Callable[[Any], None]) -> None:
        loop = _nicegui_core.loop
        if loop is None or not loop.is_running():
            return

        def _enqueue() -> None:
            for client in list(_NiceGuiClient.instances.values()):
                try:
                    with client:
                        action(client)
                except Exception as exc:
                    info_log.debug("ProgressNotifier: action failed for client %s: %s", client.id, exc)

        loop.call_soon_threadsafe(_enqueue)

    def _create(self, message: str, color: str) -> None:
        if _nicegui_core.loop is None or not _nicegui_core.loop.is_running():
            info_log.info("[progress] %s", message)
            return

        def _create_for_client(client: Any) -> None:
            self._handles[client.id] = ui.notification(
                message, color=color, multi_line=True, spinner=True, timeout=None, close_button=False
            )

        self._run_on_clients(_create_for_client)

    def update(self, message: str) -> None:
        """Update the progress text on all open spinner notifications."""
        info_log.debug("[progress] %s", message)

        def _update_for_client(client: Any) -> None:
            handle = self._handles.get(client.id)
            if handle is not None:
                handle.message = message

        self._run_on_clients(_update_for_client)

    def finish(self) -> None:
        """Dismiss the spinner notifications."""

        def _dismiss_for_client(client: Any) -> None:
            handle = self._handles.pop(client.id, None)
            if handle is not None:
                handle.dismiss()

        self._run_on_clients(_dismiss_for_client)


def abortible_sleep_with_progress(seconds: float, label: str, update_interval: float = 5.0) -> None:
    """Sleep like ``abortible_sleep`` but show a live-updating progress notification.

    Intended for long waits (heater thermal response, settle delays, etc.) so the
    operator can see what the script is doing instead of a silent multi-second gap.
    """
    total = max(float(seconds), 0.0)
    if total <= 0:
        return
    progress = ProgressNotifier(f"{label}: 0s / {total:.0f}s")
    deadline = time.monotonic() + total
    start = time.monotonic()
    interval = max(float(update_interval), 0.1)
    try:
        while True:
            if is_aborted():
                raise ScriptAbortRequested
            if is_paused() or is_force_paused():
                pause_started = time.monotonic()
                wait_while_paused(interval)
                paused_for = time.monotonic() - pause_started
                deadline += paused_for
                start += paused_for
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            elapsed = time.monotonic() - start
            percent = min(100, int(elapsed / total * 100))
            progress.update(f"{label}: {elapsed:.0f}s / {total:.0f}s ({percent}%)")
            time.sleep(min(interval, remaining))
    finally:
        progress.finish()


def clear_abort() -> None:
    _SCRIPT_CONTROL["abort_event"].clear()


def clear_pause() -> None:
    _SCRIPT_CONTROL["pause_event"].clear()
    _hide_pause_banner()


def finish_script() -> None:
    """Clear running state and reset control events."""
    _SCRIPT_CONTROL["running"] = False
    _SCRIPT_CONTROL["pause_event"].clear()
    _SCRIPT_CONTROL["abort_event"].clear()
    _SCRIPT_CONTROL["current_script"] = None
    _hide_pause_banner()


def get_script_control() -> dict:
    """Return the runtime script control dictionary.

    Keys: `running` (bool), `pause_event` (threading.Event),
    `abort_event` (threading.Event), `current_script` (optional name).
    """
    return _SCRIPT_CONTROL


def is_aborted() -> bool:
    return _SCRIPT_CONTROL["abort_event"].is_set()


def is_paused() -> bool:
    return _SCRIPT_CONTROL["pause_event"].is_set()


def is_script_running() -> bool:
    return bool(_SCRIPT_CONTROL.get("running"))


def request_abort() -> None:
    _SCRIPT_CONTROL["abort_event"].set()


def request_pause() -> None:
    _SCRIPT_CONTROL["pause_event"].set()
    _show_pause_banner()


def _show_pause_banner() -> None:
    """Show a persistent, unmissable 'Script Paused' banner (unlike a transient notify)."""
    loop = _nicegui_core.loop
    if loop is None or not loop.is_running():
        return

    def _open() -> None:
        for client in list(_NiceGuiClient.instances.values()):
            try:
                with client:
                    if client.id in _PAUSE_BANNER_DIALOGS:
                        continue
                    popup_card = (
                        ui.card()
                        .classes("fixed top-4 right-4 z-[9999] w-[24rem] max-w-[calc(100vw-2rem)] shadow-2xl border-2")
                        .style("border-color: var(--q-warning)")
                    )
                    with popup_card:
                        ui.label("Script Paused").classes("font-bold egse-title warning-text")
                        ui.label("The running script will not send any more commands until resumed.").classes(
                            "egse-text"
                        )
                    _PAUSE_BANNER_DIALOGS[client.id] = popup_card
                    ui.run_javascript(_FLASH_ATTENTION_JS)
            except Exception as exc:
                info_log.debug("pause banner: failed for client %s: %s", client.id, exc)

    loop.call_soon_threadsafe(_open)


def _hide_pause_banner() -> None:
    """Dismiss the persistent 'Script Paused' banner for all clients."""
    loop = _nicegui_core.loop
    if loop is None or not loop.is_running():
        _PAUSE_BANNER_DIALOGS.clear()
        return

    def _close() -> None:
        for client_id, popup in list(_PAUSE_BANNER_DIALOGS.items()):
            try:
                _remove_force_pause_popup(popup)
                client = _NiceGuiClient.instances.get(client_id)
                if client is not None:
                    with client:
                        ui.run_javascript(_RESTORE_TITLE_JS)
            except Exception:
                pass
            finally:
                _PAUSE_BANNER_DIALOGS.pop(client_id, None)

    loop.call_soon_threadsafe(_close)


def start_script(script_name: str | None = None) -> None:
    """Mark a script as running and clear control events."""
    _SCRIPT_CONTROL["running"] = True
    _SCRIPT_CONTROL["current_script"] = script_name
    _SCRIPT_CONTROL["pause_event"].clear()
    _SCRIPT_CONTROL["abort_event"].clear()
    # Ensure any UI-forced pause is released when starting a new script
    _FORCE_PAUSE_EVENT.clear()


def toggle_pause() -> None:
    if is_force_paused():
        # If a UI-forced pause was active, clear it first
        clear_force_pause()
        return
    if is_paused():
        clear_pause()
    else:
        request_pause()


# endregion


# ---------------------------------------------------------------------------
# Theme helpers
# ---------------------------------------------------------------------------
# region Theme helpers


def apply_theme_to_ui(
    *,
    ui: Any,
    app: Any,
    theme: str,
    css_path: Path,
    theme_state: dict[str, str],
    theme_plots: list[Any],
    logo_images: list[Any],
) -> None:
    theme_state["value"] = theme
    css_vars = app_theme.load_css_vars(css_path, theme=theme)
    app.state.theme_vars = css_vars
    app.state.theme_palette = app_theme.get_theme_palette(css_vars, theme)
    plot_labels = {f"plot_{idx}": plot for idx, plot in enumerate(theme_plots)}
    app_theme.apply_theme(
        ui,
        theme,
        css_vars,
        plot_labels,
        list(plot_labels.keys()),
        logo_images,
        css_vars.get("logo-light-src", "/rsrc/Enfys_logo.png"),
        css_vars.get("logo-dark-src", "/rsrc/Enfys_logo_-_FINAL_-_WHITE.png"),
    )


def create_set_theme(
    *,
    ui: Any,
    app: Any,
    state: dict[str, Any],
    css_path: Path,
    theme_state: dict[str, str],
    theme_plots: list[Any],
    logo_images: list[Any] | None = None,
) -> Any:
    """Create a theme setter callback bound to current UI controllers."""

    def set_theme(theme: str) -> None:
        if theme not in ("dark", "light"):
            return
        apply_theme_to_ui(
            ui=ui,
            app=app,
            theme=theme,
            css_path=css_path,
            theme_state=theme_state,
            theme_plots=theme_plots,
            logo_images=logo_images or [],
        )

    return set_theme


# PSU helpers


_PSU_SAMPLE_KEYS = (
    "status",
    "CH1_STATUS",
    "CH2_STATUS",
    "CH3_STATUS",
    "CH4_STATUS",
    "PSU_ROV_HTR_V",
    "PSU_ROV_HTR_I",
    "PSU_EB_V",
    "PSU_EB_I",
    "CH1_V",
    "CH1_I",
    "CH2_V",
    "CH2_I",
    "CH3_V",
    "CH3_I",
    "CH4_V",
    "CH4_I",
)
# endregion


# ---------------------------------------------------------------------------
# TM / HK helpers — decoding, alarm lights, plot cards
# ---------------------------------------------------------------------------
# region TM / HK helpers — decoding, alarm lights, plot cards


def active_flag_names(flag_ns: Any, ordered_names: list[str]) -> list[str]:
    if flag_ns is None:
        return []
    return [
        name
        for name in ordered_names
        if not name.startswith("UNUSED") and not name.startswith("RESERVED") and bool(getattr(flag_ns, name, 0))
    ]


def any_flag(flag_ns: Any) -> bool:
    return bool(flag_ns) and any(bool(value) for value in flag_ns.__dict__.values())


def _ob_adc12(raw: Any) -> int | None:
    """Normalise a standalone OB ADC field to a 12-bit ADU value.

    Native ``tmstruct.hk`` packets normally expose the ADC as an already
    normalised 0..4095 value.  The fallback also accepts a 16-bit left-aligned
    representation used by some older decoders.
    """
    try:
        raw_value = int(raw)
    except (TypeError, ValueError):
        return None

    if 0 <= raw_value <= 0x0FFF:
        return raw_value
    if 0 <= raw_value <= 0xFFFF:
        return (raw_value >> 4) & 0x0FFF
    return raw_value & 0x0FFF


def decode_plot_field(
    packet: Any,
    field_name: str,
    state: dict[str, Any] | None = None,
) -> float | None:
    raw = getattr(packet, field_name, None)
    if raw is None:
        return None

    display_mode = str(
        (state or {}).get("hk_display_mode") or getattr(_nicegui_app.state, "hk_display_mode", "REAL")
    ).upper()

    # Native standalone OB analogue fields must be normalised before conversion.
    if field_name in _NATIVE_OB_ADC12_FIELDS:
        adu = _ob_adc12(raw)
        if adu is None:
            return None

        if display_mode == "ADU":
            return float(adu)

        try:
            if field_name == "HK_V_3V3":
                value = adu * 4.05 / 4095.0 * 2.0

            elif field_name == "HK_V_1V5":
                value = adu * 4.05 / 4095.0

            elif field_name in {
                "DIGITAL_TRP",
                "DETEC_TRP",
                "MECH_TRP",
                "MOTOR_TRP",
            }:
                # Endpoint ADC values normally cannot produce a valid
                # thermistor resistance/temperature.
                if adu <= 0 or adu >= 4095:
                    return None

                value = float(eb_packet_utility.adu_to_temp(adu))

            else:
                return None

        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            return None

        return float(value) if math.isfinite(float(value)) else None

    # Existing EB conversion path.
    if display_mode == "ADU":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    try:
        value = hk_conversions.decode_field(packet, field_name)
        if value is None:
            return None
        value = float(value)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None

    return value if math.isfinite(value) else None


def decode_tuple(
    packet: Any,
    field_names: tuple[str, ...],
    state: dict[str, Any] | None = None,
) -> list[float] | None:
    values: list[float] = []

    for field_name in field_names:
        value = decode_plot_field(packet, field_name, state)
        if value is None:
            return None
        values.append(value)

    return values


def decoded(packet: Any, field_name: str) -> float | None:
    value = hk_conversions.decode_field(packet, field_name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def drain_packet_queue(queue: Any, handler: Any) -> None:
    while not queue.empty():
        handler(queue.get())


# MMS helpers


_MMS_FIELDS = (
    ("EB +12V", "EB_MEAS_MAIN_12V", "eb_12v", False),
    ("EB -12V", "EB_MEAS_MAIN_NEG12V", "eb_neg12v", False),
    ("EB +5V", "EB_MEAS_5V", "eb_5v", False),
    ("EB +3V3", "EB_MEAS_3V3", "eb_3v3", False),
    ("EB MCU Temp", "EB_MCU_INTERNAL_TEMP", "eb_mcu_temp", False),
    ("EB Internal TRP", "EB_INTERNAL_TRP_TEMP", "eb_internal_trp_temp", False),
    ("EB PSU TRP", "EB_PSU_BOARD_TEMP", "eb_psu_trp_temp", False),
    ("EB TEC rail", "EB_MEAS_TEC_RAIL", "eb_tec_rail_v", True),
    ("OB FPGA I/O", "OB_3V3_VOLTAGE", "ob_fpga_io_v", False),
    ("OB FPGA Core", "OB_1V5_VOLTAGE", "ob_fpga_core_v", False),
    ("OB DIG TRP", "OB_DIGITAL_TRP", "ob_digital_trp", False),
    ("OB DET TRP", "OB_DETECTOR_TRP", "ob_detector_trp", False),
    ("OB MECH TRP", "OB_MECHANISM_TRP", "ob_mechanism_trp", False),
    ("OB MOTOR TRP", "OB_MOTOR_TRP", "ob_motor_trp", False),
)


def eb_alarm_details(hk: Any) -> list[str]:
    warning_bits = active_flag_names(getattr(hk, "WARNING_FLAGS_BITS", None), _WARNING_NAMES)
    fdir_alarm_bits = active_flag_names(getattr(hk, "FDIR_ALARM_FLAGS_BITS", None), _FDIR_NAMES)
    fdir_warning_bits = active_flag_names(getattr(hk, "FDIR_WARNING_FLAGS_BITS", None), _FDIR_NAMES)

    details: list[str] = []
    tcs_rejected = int(getattr(hk, "TCS_REJECTED", 0) or 0)
    if tcs_rejected > 0:
        details.append(f"TCS Rejected: {tcs_rejected}")
    details.extend([f"EB Warning: {flag}" for flag in warning_bits if flag in _EB_WARNING_FLAGS])
    details.extend([f"EB FDIR Alarm: {flag}" for flag in fdir_alarm_bits if flag in _EB_FDIR_FLAGS])
    details.extend([f"EB FDIR Warning: {flag}" for flag in fdir_warning_bits if flag in _EB_FDIR_FLAGS])
    return details


def log_new_hk_alarm_details(state: dict[str, Any], logger: Any, channel: str, details: list[str]) -> None:
    active_details = state.setdefault("hk_active_alarm_details", {"ob": set(), "eb": set()})
    previous = set(active_details.get(channel, set()))
    current = set(details)

    for detail in sorted(current - previous):
        detail_lower = detail.lower()
        if "error" in detail_lower or "alarm" in detail_lower:
            logger.error("HK %s alarm raised: %s", channel.upper(), detail)
        else:
            logger.warning("HK %s warning raised: %s", channel.upper(), detail)

    active_details[channel] = current


def ob_alarm_details(hk: Any) -> list[str]:
    warning_bits = active_flag_names(getattr(hk, "WARNING_FLAGS_BITS", None), _WARNING_NAMES)
    fdir_alarm_bits = active_flag_names(getattr(hk, "FDIR_ALARM_FLAGS_BITS", None), _FDIR_NAMES)
    fdir_warning_bits = active_flag_names(getattr(hk, "FDIR_WARNING_FLAGS_BITS", None), _FDIR_NAMES)

    details = [f"OB Warning: {flag}" for flag in warning_bits if flag in _OB_WARNING_FLAGS]
    details.extend([f"OB FDIR Alarm: {flag}" for flag in fdir_alarm_bits if flag in _OB_FDIR_FLAGS])
    details.extend([f"OB FDIR Warning: {flag}" for flag in fdir_warning_bits if flag in _OB_FDIR_FLAGS])
    if any_flag(getattr(hk, "ERRORS", None)):
        details.append("OB Error flags active")
    if any_flag(getattr(hk, "MTR_ERRORS", None)):
        details.append("OB Motor error flags active")
    return details


def update_hk_alarm_lights(state: dict[str, Any], hk: Any, logger: Any) -> None:
    alarm_lights = state.get("alarm_lights") or {}
    mode = str(state.get("mode", "EB")).upper()

    # In standalone OB mode, FDIR bitmaps are simulated from native OB ADCs.
    # Keep packet-reported errors and simulated FDIR latches as separate light
    # sources so Clear_Errors can reset only the simulator source.
    ob_details = ob_alarm_details(hk)
    simulated_details = simulated_ob_fdir_details(state) if mode == "OB" else []
    if mode == "OB":
        ob_details = [detail for detail in ob_details if "OB FDIR " not in detail]
    eb_details = eb_alarm_details(hk)

    active_logger = logger if logger is not None else info_log
    active_details = state.setdefault("hk_active_alarm_details", {"ob": set(), "eb": set()})
    previous_ob_details = set(active_details.get("ob", set()))
    newly_raised_ob_details = sorted(set(ob_details) - previous_ob_details)

    # Simulated latches are logged with their raw ADU and limits when first
    # asserted by simulate_ob_fdir(); avoid emitting a second generic log here.
    log_new_hk_alarm_details(state, active_logger, "ob", ob_details)
    log_new_hk_alarm_details(state, active_logger, "eb", eb_details)

    # Native OB warnings and error flags never shut the PSU down automatically.
    # They ask the operator once when newly asserted. Simulated FDIR warnings
    # and thermistor alarms are handled directly by simulate_ob_fdir().
    if mode == "OB" and newly_raised_ob_details:
        _open_ob_psu_shutdown_dialog(state, active_logger, newly_raised_ob_details)

    if "ob" in alarm_lights:
        alarm_lights["ob"].update_from_faults({detail: True for detail in ob_details}, source="hk")
        alarm_lights["ob"].update_from_faults(
            {detail: True for detail in simulated_details},
            source="ob_fdir_sim",
        )
    if "eb" in alarm_lights:
        alarm_lights["eb"].update_from_faults({detail: True for detail in eb_details}, source="hk")


def update_packet_viewer(
    mode: str, packet_viewer_controllers: dict[str, Any], hk: Any, packet_list_controller: Any = None
) -> None:
    packet_type = "OB_HK" if mode == "OB" else "EB_HK"
    viewer = packet_viewer_controllers.get(packet_type)
    if viewer is None:
        return
    # Route each HK packet popped from const.hk_queue directly to the active HK viewer profile.
    viewer.update_from_packet(packet_type, hk)

    # Add to packet list if available
    if packet_list_controller is not None:
        packet_data = hk.__dict__
        label = f"TM: {packet_type}"
        packet_list_controller.add_packet(packet_type, packet_data, label)


def _record_sci_temperatures(
    state: dict[str, Any], mode: str, fields: dict[str, str], get_field: Callable[[str], Any]
) -> None:
    """Remember the latest raw SCI temperatures so HK-rate plots can carry them forward."""
    temps = {key: (field, get_field(field)) for key, field in fields.items() if get_field(field) is not None}
    if temps:
        state["latest_sci_temps"] = {"mode": mode, "values": temps}


def _push_sci_temperature_plot(state: dict[str, Any], packet: Any, mode: str, trp_card: Any) -> None:
    """Plot SCI temperatures once, at the timestamp of their SCI packet."""
    timestamp = packet.get("TIME") if isinstance(packet, dict) else getattr(packet, "TIME", None)
    if timestamp is None:
        return

    hk_packet = state.get("latest_hk_packet")
    hk_fields = _NATIVE_OB_TRP_FIELDS if mode == "OB" else _EB_OB_TRP_FIELDS
    sci_fields = (
        ("SWIR_TEMP", "HT_SINK_TEMP", None)
        if mode == "OB"
        else ("SWIR_END_TEMP", "HEATSINK_END_TEMP", "MWIR_END_TEMP")
    )
    display_values: dict[str, list[list[float]]] = {}
    for display_mode in ("REAL", "ADU"):
        display_state = {**state, "hk_display_mode": display_mode}
        values = []
        for field in hk_fields:
            value = decode_plot_field(hk_packet, field, display_state) if hk_packet is not None else None
            values.append([float(value) if value is not None else float("nan")])
        for field in sci_fields:
            raw = None if field is None else (packet.get(field) if isinstance(packet, dict) else getattr(packet, field, None))
            try:
                value = (
                    float(int(raw) >> 4)
                    if display_mode == "ADU"
                    else hk_conversions.sci_temperature_to_c(field, raw)
                )
            except (TypeError, ValueError, ZeroDivisionError):
                value = float("nan")
            values.append([float(value)])
        display_values[display_mode] = values

    selected_display_mode = "ADU" if str(state.get("hk_display_mode", "REAL")).upper() == "ADU" else "REAL"
    trp_card.push([timestamp], display_values[selected_display_mode], display_values=display_values)


def update_plot_cards(
    state: dict[str, Any],
    hk: Any,
    trp_card: Any,
    voltage_card: Any,
) -> None:
    time_value = getattr(hk, "TIME", None)
    if time_value is None:
        return

    replay = state["psu_replay"]
    replay["latest_hk_time"] = time_value
    if replay.get("hk_anchor") is None:
        replay["hk_anchor"] = time_value

    mode = str(state.get("mode", "EB")).upper()
    display_states = {
        display_mode: {**state, "hk_display_mode": display_mode}
        for display_mode in ("REAL", "ADU")
    }
    selected_display_mode = "ADU" if str(state.get("hk_display_mode", "REAL")).upper() == "ADU" else "REAL"

    def _values_for_display(fields: tuple[str, ...], display_mode: str) -> list[list[float]]:
        values = []
        for field in fields:
            value = decode_plot_field(hk, field, display_states[display_mode])
            values.append([float(value) if value is not None else float("nan")])
        return values

    if mode == "OB":
        # Receiving a standalone OB HK packet proves that the OB stream is alive.
        trp_card.set_stream_enabled(True)
        voltage_card.set_stream_enabled(True)

        trp_display_values = {
            display_mode: _values_for_display(_NATIVE_OB_TRP_FIELDS, display_mode)
            + [[float("nan")]] * 3
            for display_mode in ("REAL", "ADU")
        }
        if any(math.isfinite(values[0]) for mode_values in trp_display_values.values() for values in mode_values):
            trp_values = trp_display_values[selected_display_mode]
            trp_card.push(
                [time_value],
                trp_values,
                display_values=trp_display_values,
            )

        voltage_display_values = {
            display_mode: _values_for_display(("HK_V_3V3", "HK_V_1V5"), display_mode)
            + [[float("nan")]]
            for display_mode in ("REAL", "ADU")
        }
        if any(
            math.isfinite(values[0])
            for mode_values in voltage_display_values.values()
            for values in mode_values[:2]
        ):
            # The voltage card has three series: OB 3V3, OB 1V5 and EB 3V3.
            # Do not update the EB series from a standalone OB packet.
            voltage_card.push(
                [time_value],
                voltage_display_values[selected_display_mode],
                display_values=voltage_display_values,
            )

        return

    # EB mode uses the combined EB HK packet.
    instr_flags = getattr(hk, "INSTR_STATUS_FLAGS", None)

    if instr_flags is not None:
        ob_enabled = bool(getattr(instr_flags, "OB_5V_ENABLED", 0))
    else:
        # Fallback for packets containing only the numeric flag field.
        try:
            numeric_flags = int(getattr(hk, "INSTRUMENT_STATUS_FLAGS", 0))
            ob_enabled = bool((numeric_flags >> 10) & 0x1)
        except (TypeError, ValueError):
            ob_enabled = False

    # OB_5V_ENABLED asserts before the OB reports; until then the relayed OB HK is all zeros with OB_HK_ID == 0.
    ob_enabled = ob_enabled and bool(getattr(hk, "OB_HK_ID", 0))

    trp_card.set_stream_enabled(ob_enabled)
    voltage_card.set_stream_enabled(ob_enabled)

    if not ob_enabled:
        return

    trp_display_values = {
        display_mode: _values_for_display(_EB_OB_TRP_FIELDS, display_mode)
        + [[float("nan")]] * 3
        for display_mode in ("REAL", "ADU")
    }
    if any(math.isfinite(values[0]) for mode_values in trp_display_values.values() for values in mode_values):
        trp_values = trp_display_values[selected_display_mode]
        trp_card.push(
            [time_value],
            trp_values,
            display_values=trp_display_values,
        )

    voltage_display_values = {
        display_mode: _values_for_display(("OB_3V3_VOLTAGE", "OB_1V5_VOLTAGE", "EB_MEAS_3V3"), display_mode)
        for display_mode in ("REAL", "ADU")
    }
    if any(math.isfinite(values[0]) for mode_values in voltage_display_values.values() for values in mode_values):
        voltage_card.push(
            [time_value],
            voltage_display_values[selected_display_mode],
            display_values=voltage_display_values,
        )


# endregion


# ---------------------------------------------------------------------------
# TM polling — create_poll_tm and supporting helpers
# ---------------------------------------------------------------------------
# region TM polling — create_poll_tm and supporting helpers


def create_poll_tm(
    *,
    app: Any,
    state: dict[str, Any],
    const: Any,
    logger: Any,
    eb_metrics_card: Any,
    ob_metrics_card: Any,
    packet_metrics_card: Any,
    packet_viewer_controllers: dict[str, Any],
    trp_card: Any,
    voltage_card: Any,
    hk_explorer_card: Any = None,
) -> Any:
    """Create TM polling callback bound to current controllers and state."""

    async def poll_tm() -> None:
        # Independent packet counters
        counts = state.setdefault("packet_counts", {"hk": 0, "post": 0, "sci": 0})
        mode = state.get("mode", "EB")
        if mode == "EB":
            rs422_log_path = getattr(app.state.eb_interface, "rs422_log_path", None)
            log_selected = bool(rs422_log_path)
            if not log_selected:
                eb_metrics_card.set_no_data()
                ob_metrics_card.set_no_data()
                packet_metrics_card.set_no_data()
                return

            if not state["log_search"]["enabled"]:
                return

            # EB mode packets are sourced from the RS422 decode path.
            # Only re-read when file mtime changes. A parse can take longer as the
            # session log grows, so never allow timer ticks to stack parse jobs.
            if state.get("_rs422_parse_busy", False):
                return
            if eb_interface.rs422_log_changed(Path(rs422_log_path)):
                state["_rs422_parse_busy"] = True
                try:
                    # read_pkt scans the RS422 log. Logs grow throughout a run, so doing
                    # this on the UI event loop eventually stalls every NiceGUI timer
                    # (including the PSU display). Parse it in the I/O worker pool.
                    await run.io_bound(eb_packet_utility.read_pkt, rs422_log_path, True)
                except Exception as e:
                    logger.error(f"Failed to read packet log: {e}")
                finally:
                    state["_rs422_parse_busy"] = False

        processed_hk = False
        last_hk_time = state.setdefault("latest_hk_time", None)

        # Bound work per tick so a backlog cannot monopolize the GUI event loop.
        processed_hk_count = 0
        max_hk_packets_per_tick = 5
        while not const.hk_queue.empty():
            try:
                if const.hk_queue.qsize() <= 1:
                    break
                const.hk_queue.get_nowait()
            except (Empty, AttributeError):
                break
        while not const.hk_queue.empty() and processed_hk_count < max_hk_packets_per_tick:
            processed_hk = True
            hk = const.hk_queue.get()
            processed_hk_count += 1
            now = datetime.now()
            if mode == "OB":
                state["last_ob_tm_time"] = getattr(hk, "TIME", now)
            # Calculate time since last HK
            if last_hk_time is not None:
                hk_delta = (now - last_hk_time).total_seconds()
            else:
                hk_delta = 0.0
            state["hk_time_since_last"] = hk_delta
            state["latest_hk_time"] = now
            last_hk_time = now

            # Only regular HK packets (TM_TYPE_ID=0x1) advance the HK counter;
            # response HK packets (0x2) still update the UI but aren't counted.
            if getattr(hk, "TM_TYPE_ID", 0x1) == 0x1:
                counts["hk"] = int(counts.get("hk", 0)) + 1

            if not hasattr(hk, "TIME"):
                hk.TIME = now

            if mode == "OB":
                simulate_ob_fdir(state, hk, logger)

            eb_metrics_card.update_from_packet(hk)
            ob_metrics_card.update_from_packet(hk)
            update_hk_alarm_lights(state, hk, logger)

            # MMS runs continuously while in EB mode and latches on first trigger.
            mms_cfg = state.get("mms") or {}
            if state.get("mode") == "EB" and bool(mms_cfg.get("enabled", True)):
                reasons, tec_pre_action, ob5v_pre_action = mms_reasons(hk, mms_cfg.get("limits") or {})
                if reasons:
                    if mms_cfg.get("latched") or mms_cfg.get("in_progress") or mms_cfg.get("pending"):
                        pass
                    else:
                        mms_cfg["pending"] = True

                        async def _schedule_mms_call() -> None:
                            try:
                                await mms(
                                    app,
                                    state,
                                    logger,
                                    hk,
                                    list(reasons),
                                    tec_pre_action,
                                    ob5v_pre_action,
                                )
                            except Exception as exc:
                                logger.exception("Failed to execute MMS actions: %s", exc)
                            finally:
                                current_cfg = state.setdefault("mms", {})
                                current_cfg["pending"] = False

                        try:
                            asyncio.create_task(_schedule_mms_call())
                        except Exception as exc:
                            mms_cfg["pending"] = False
                            logger.exception("Could not schedule MMS task: %s", exc)

            state["latest_hk_packet"] = hk
            update_plot_cards(state, hk, trp_card, voltage_card)
            if hk_explorer_card is not None:
                hk_explorer_card.push_data({"EB_HK": hk})
            packet_list_controller = state.get("packet_list_controller")
            update_packet_viewer(mode, packet_viewer_controllers, hk, packet_list_controller)

        # In static/mock-log runs, HK may stop updating. Keep replay time moving so
        # PSU log playback can continue even without fresh HK packets.
        replay = state["psu_replay"]
        if replay.get("enabled") and replay.get("hk_anchor") is not None and not processed_hk:
            replay["latest_hk_time"] = datetime.now()

        # Process one POST packet per cycle, matching ebgui behaviour and dedupe by identity
        if not const.eb_post_queue.empty():
            post = const.eb_post_queue.get()
            required_post_fields = (
                "POST_WARNING_FLAGS",
                "POST_ERROR_FLAGS",
                "NUM_BAD_FLASH_BLOCKS",
                "NUM_BAD_SRAM_BLOCKS",
                "ASW_IMAGE_1_CRC",
                "ASW_IMAGE_2_CRC",
                "ASW_IMAGE_3_CRC",
                "ASW_IMAGE_4_CRC",
                "ASW_IMAGE_5_CRC",
                "BSW_IMAGE_CRC",
                "MEASUREMENT_TABLE_CRC",
            )
            if not all(hasattr(post, field_name) for field_name in required_post_fields):
                logger.debug("Ignoring non-POST packet in POST queue")
            else:
                post_identity = tuple(getattr(post, field_name, None) for field_name in required_post_fields)
                last_post_identity = state.setdefault("last_post_identity", {"value": None})
                if post_identity == last_post_identity.get("value"):
                    logger.debug("Ignoring duplicate POST packet in POST queue")
                else:
                    last_post_identity["value"] = post_identity
                    counts["post"] = int(counts.get("post", 0)) + 1
                    state.setdefault("packet_counts", counts)
                    state["last_post"] = post
                    # Update POST viewer if present
                    if "EB_POST" in packet_viewer_controllers:
                        try:
                            packet_viewer_controllers["EB_POST"].update_from_packet(post)
                        except Exception:
                            logger.debug("Failed to update EB_POST packet viewer")

                    # Add to packet list if available
                    packet_list_controller = state.get("packet_list_controller")
                    if packet_list_controller is not None:
                        post_data = (
                            post.__dict__
                            if hasattr(post, "__dict__")
                            else (dict(post) if isinstance(post, dict) else {})
                        )
                        label = "TM: Post"
                        packet_list_controller.add_packet("EB_POST", post_data, label)

        # Process SCI packets from TM queues.
        # EB SCI and OB SCI have different packet shapes, so we handle them separately.
        if not const.sci_queue.empty():
            # EB packets are complete multi-point products, so retain its legacy
            # latest-packet behaviour. Each OB packet is one scan point; draining
            # that queue here would punch holes in the plotted OB scan.
            if mode != "OB":
                while not const.sci_queue.empty():
                    try:
                        if const.sci_queue.qsize() <= 1:
                            break
                        const.sci_queue.get_nowait()
                    except (Empty, AttributeError):
                        break
            if mode == "OB":
                new_ob_sci_packets = 0
                latest_ob_sci = None
                ob_sci_packets = state.setdefault("ob_sci_packets", [])
                ob_recent_identities = state.setdefault("ob_sci_recent_identities", [])
                ob_recent_identity_set = state.setdefault("ob_sci_recent_identity_set", set())
                max_ob_recent_identities = int(state.get("ob_sci_dedupe_window", 128) or 128)

                processed_sci_count = 0
                max_sci_packets_per_tick = 1000 if _ob_sci_capture_snapshot() is not None else 5
                while not const.sci_queue.empty() and processed_sci_count < max_sci_packets_per_tick:
                    candidate = const.sci_queue.get()
                    processed_sci_count += 1
                    if not hasattr(candidate, "__dict__") and not isinstance(candidate, dict):
                        logger.debug("Ignoring non-object packet in SCI queue")
                        continue

                    latest_sci = candidate
                    if isinstance(latest_sci, dict):
                        latest_sci.setdefault("TIME", datetime.now())
                        state["last_ob_tm_time"] = latest_sci.get("TIME")

                        def _field(name: str) -> Any:
                            return latest_sci.get(name)

                    else:
                        if not hasattr(latest_sci, "TIME"):
                            latest_sci.TIME = datetime.now()
                        state["last_ob_tm_time"] = getattr(latest_sci, "TIME", datetime.now())

                        def _field(name: str) -> Any:
                            return getattr(latest_sci, name, None)

                    # OB SCI identity based on command counter and key measurement values.
                    sci_identity = (
                        "OB_SCI",
                        _field("CMD_CNT"),
                        _field("ABS_STEPS") or _field("MTR_ABS_STEPS") or _field("MOTOR_ABS_STEPS"),
                        _field("SWIR_HIGH"),
                        _field("SWIR_MED"),
                        _field("SWIR_LOW"),
                        _field("MWIR_HIGH"),
                        _field("MWIR_MED"),
                        _field("MWIR_LOW"),
                    )
                    capture = _ob_sci_capture_snapshot()
                    # Do not deduplicate an explicit scan capture. Repeated
                    # points at the same motor position and intensity can be
                    # legitimate measurements and must remain in the stitched
                    # packet. Dedupe is retained for the ordinary latest-value
                    # OB viewer outside capture sessions.
                    if capture is None:
                        if sci_identity in ob_recent_identity_set:
                            logger.debug("Ignoring duplicate OB science packet in SCI queue")
                            continue

                        ob_recent_identities.append(sci_identity)
                        ob_recent_identity_set.add(sci_identity)
                        if len(ob_recent_identities) > max_ob_recent_identities:
                            expired = ob_recent_identities.pop(0)
                            if expired not in ob_recent_identities:
                                ob_recent_identity_set.discard(expired)

                    latest_ob_sci = latest_sci
                    state["latest_ob_sci"] = latest_sci
                    _record_sci_temperatures(state, "OB", {"SWIR": "SWIR_TEMP", "HT_SINK": "HT_SINK_TEMP"}, _field)
                    _push_sci_temperature_plot(state, latest_sci, "OB", trp_card)
                    state_ob_metrics_card = state.get("ob_metrics_card")
                    if (
                        state_ob_metrics_card is not None
                        and getattr(state_ob_metrics_card, "last_packet", None) is not None
                    ):
                        state_ob_metrics_card.update_from_packet(state_ob_metrics_card.last_packet)
                    if capture is not None:
                        capture_id, capture_label = capture
                        if state.get("ob_sci_capture_id") != capture_id:
                            ob_sci_packets.clear()
                            state["ob_sci_capture_id"] = capture_id
                            state["ob_sci_capture_label"] = capture_label
                            state["ob_sci_capture_active"] = True
                            state["ob_sci_stitched_packet"] = None
                            state["ob_sci_capture_seen"] = 0
                            state["ob_sci_capture_rejected"] = 0
                        state["ob_sci_capture_seen"] = int(state.get("ob_sci_capture_seen", 0)) + 1
                        required_intensity_fields = (
                            "SWIR_HIGH",
                            "SWIR_MED",
                            "SWIR_LOW",
                            "MWIR_HIGH",
                            "MWIR_MED",
                            "MWIR_LOW",
                        )
                        missing_plot_fields = [name for name in required_intensity_fields if _field(name) is None]
                        if missing_plot_fields:
                            state["ob_sci_capture_rejected"] = int(state.get("ob_sci_capture_rejected", 0)) + 1
                            logger.debug(
                                "Ignoring non-measurement OB SCI packet during capture; missing %s",
                                ", ".join(missing_plot_fields),
                            )
                        else:
                            if isinstance(latest_sci, dict):
                                point_data = dict(latest_sci)
                            else:
                                point_data = dict(vars(latest_sci))
                            abs_steps = next(
                                (
                                    point_data.get(name)
                                    for name in ("ABS_STEPS", "MTR_ABS_STEPS", "MOTOR_ABS_STEPS", "CMD_CNT")
                                    if point_data.get(name) is not None
                                ),
                                len(ob_sci_packets),
                            )
                            point_data["ABS_STEPS"] = abs_steps
                            ob_sci_packets.append(SimpleNamespace(**point_data))
                            max_ob_sci_packets = int(state.get("max_ob_sci_packets", 4096) or 4096)
                            if len(ob_sci_packets) > max_ob_sci_packets:
                                del ob_sci_packets[:-max_ob_sci_packets]
                    counts["sci"] = int(counts.get("sci", 0)) + 1
                    new_ob_sci_packets += 1

                if new_ob_sci_packets > 0 and latest_ob_sci is not None and "OB_SCI" in packet_viewer_controllers:
                    try:
                        packet_viewer_controllers["OB_SCI"].update_from_packet(latest_ob_sci)

                        packet_list_controller = state.get("packet_list_controller")
                        if packet_list_controller is not None:
                            sci_data = (
                                latest_ob_sci.__dict__
                                if hasattr(latest_ob_sci, "__dict__")
                                else (dict(latest_ob_sci) if isinstance(latest_ob_sci, dict) else {})
                            )
                            packet_list_controller.add_packet("OB_SCI", sci_data, "TM: OB Science")
                    except Exception:
                        logger.debug("Failed to update OB_SCI packet viewer")
            else:
                new_sci_packets = 0
                required_sci_fields = (
                    "PACKET_NUMBER",
                    "SCI_POINT_COUNT",
                    "SCI_PACKET_CRITICALITY",
                )
                sci_packets = state.setdefault("sci_packets", [])
                sci_packet_identities = state.setdefault("sci_packet_identities", set())
                processed_sci_count = 0
                max_sci_packets_per_tick = 5
                while not const.sci_queue.empty() and processed_sci_count < max_sci_packets_per_tick:
                    latest_sci = const.sci_queue.get()
                    processed_sci_count += 1
                    if not all(hasattr(latest_sci, field_name) for field_name in required_sci_fields):
                        logger.debug("Ignoring non-SCI packet in SCI queue")
                        continue
                    if not hasattr(latest_sci, "TIME"):
                        latest_sci.TIME = datetime.now()
                    # Build identity tuple (packet_number, criticality, point_count, first_abs_step, last_abs_step)
                    packet_number = getattr(latest_sci, "PACKET_NUMBER", None)
                    criticality = getattr(latest_sci, "SCI_PACKET_CRITICALITY", None)
                    point_count = int(getattr(latest_sci, "SCI_POINT_COUNT", 0) or 0)
                    first_abs_step = None
                    last_abs_step = None
                    sci_points = getattr(latest_sci, "SCI_POINTS", None)
                    if sci_points:
                        first_abs_step = getattr(sci_points[0], "ABS_STEPS", None)
                        last_abs_step = getattr(sci_points[-1], "ABS_STEPS", None)
                    elif hasattr(latest_sci, "ABS_STEPS"):
                        first_abs_step = getattr(latest_sci, "ABS_STEPS", None)
                        last_abs_step = first_abs_step
                    sci_identity = (packet_number, criticality, point_count, first_abs_step, last_abs_step)
                    if sci_identity in sci_packet_identities:
                        logger.debug("Ignoring duplicate science packet in SCI queue")
                        continue
                    sci_packets.append(latest_sci)
                    sci_packet_identities.add(sci_identity)
                    _record_sci_temperatures(
                        state,
                        "EB",
                        {"SWIR": "SWIR_END_TEMP", "HT_SINK": "HEATSINK_END_TEMP", "MWIR": "MWIR_END_TEMP"},
                        lambda name, packet=latest_sci: getattr(packet, name, None),
                    )
                    _push_sci_temperature_plot(state, latest_sci, "EB", trp_card)
                    counts["sci"] = int(counts.get("sci", 0)) + 1
                    new_sci_packets += 1

                # Sort and trim buffer as legacy GUI
                try:
                    sci_packets.sort(key=lambda p: int(getattr(p, "PACKET_NUMBER", 0)))
                except Exception:
                    pass

                max_sci_packets = 12
                if len(sci_packets) > max_sci_packets:
                    del sci_packets[:-max_sci_packets]

                # Rebuild identity set from retained packets
                state["sci_packet_identities"] = {
                    (
                        getattr(p, "PACKET_NUMBER", None),
                        getattr(p, "SCI_PACKET_CRITICALITY", None),
                        int(getattr(p, "SCI_POINT_COUNT", 0) or 0),
                        (
                            getattr(p.SCI_POINTS[0], "ABS_STEPS", None)
                            if getattr(p, "SCI_POINTS", None)
                            else getattr(p, "ABS_STEPS", None)
                        ),
                        (
                            getattr(p.SCI_POINTS[-1], "ABS_STEPS", None)
                            if getattr(p, "SCI_POINTS", None)
                            else getattr(p, "ABS_STEPS", None)
                        ),
                    )
                    for p in sci_packets
                }

                if new_sci_packets > 0:
                    if "EB_SCI" in packet_viewer_controllers and sci_packets:
                        try:
                            latest_sci = sci_packets[-1]
                            state["latest_ob_sci"] = latest_sci
                            state_ob_metrics_card = state.get("ob_metrics_card")
                            if (
                                state_ob_metrics_card is not None
                                and getattr(state_ob_metrics_card, "last_packet", None) is not None
                            ):
                                state_ob_metrics_card.update_from_packet(state_ob_metrics_card.last_packet)
                            packet_viewer_controllers["EB_SCI"].update_from_packet(latest_sci)

                            # Add to packet list if available
                            packet_list_controller = state.get("packet_list_controller")
                            if packet_list_controller is not None:
                                sci_data = (
                                    latest_sci.__dict__
                                    if hasattr(latest_sci, "__dict__")
                                    else (dict(latest_sci) if isinstance(latest_sci, dict) else {})
                                )
                                packet_num = getattr(latest_sci, "PACKET_NUMBER", "")
                                label = f"TM: Science (pkt {packet_num})" if packet_num else "TM: Science"
                                packet_list_controller.add_packet("EB_SCI", sci_data, label)
                        except Exception:
                            logger.debug("Failed to update SCI packet viewer")

        if mode == "OB":
            completed_capture = _complete_ob_sci_capture_if_drained()
            if completed_capture is not None:
                capture_id, capture_label = completed_capture
                if state.get("ob_sci_capture_id") == capture_id:
                    points = list(state.get("ob_sci_packets") or [])
                else:
                    state["ob_sci_packets"] = []
                    state["ob_sci_capture_id"] = capture_id
                    state["ob_sci_capture_label"] = capture_label
                    points = []
                normalized_points = [SimpleNamespace(**point) if isinstance(point, dict) else point for point in points]
                stitched_packet = SimpleNamespace(
                    SCI_PACKET_CRITICALITY="OB",
                    PACKET_NUMBER=capture_id,
                    SCI_POINT_COUNT=len(normalized_points),
                    SCI_POINTS=normalized_points,
                    CAPTURE_LABEL=capture_label,
                    TIME=datetime.now(),
                )
                state["ob_sci_stitched_packet"] = stitched_packet
                state["ob_sci_capture_active"] = False
                with _OB_SCI_CAPTURE_LOCK:
                    _OB_SCI_STITCHED_SCANS.append(stitched_packet)
                    state["ob_sci_scans"] = _OB_SCI_STITCHED_SCANS
                info_log.info(
                    "Completed OB SCI capture %s with %s points: %s",
                    capture_id,
                    len(normalized_points),
                    capture_label,
                )
                notify(
                    f"OB science scan captured: {len(normalized_points)} points "
                    f"({int(state.get('ob_sci_capture_rejected', 0))} rejected)",
                    color="positive" if normalized_points else "warning",
                )

        packet_metrics_card.refresh()

    return poll_tm


def disable_ob5v(logger: Any) -> None:
    try:
        interface = eb_interface.get_egse_interface()
        status = ebtcs.en_ob5v(interface, 0)
        if status == "ERROR":
            logger.warning("MMS pre-action: OB 5V disable command failed over EB link.")
            return
        logger.warning("MMS pre-action: OB 5V disable command sent over EB link.")
    except Exception as exc:
        logger.error(f"MMS pre-action failed while disabling OB 5V over EB link: {exc}")


async def mms(
    app: Any,
    state: dict[str, Any],
    logger: Any,
    hk: Any,
    reasons: list[str],
    tec_pre_action: bool,
    ob5v_pre_action: bool,
):
    mms_cfg = state.setdefault("mms", {})
    if not bool(mms_cfg.get("enabled", True)):
        return
    if mms_cfg.get("latched"):
        return
    if mms_cfg.get("in_progress"):
        return

    mms_cfg["in_progress"] = True

    def _run_mms_actions() -> None:
        mms_cfg = state.setdefault("mms", {})
        if not bool(mms_cfg.get("enabled", True)):
            return
        if mms_cfg.get("latched"):
            return

        # Abort any running script before taking safety actions.
        if is_script_running():
            request_abort()
            clear_pause()
            clear_force_pause()
            logger.warning("MMS action: running script aborted.")

        if tec_pre_action:
            mms_cfg["tec_shutdown_requested"] = True
            logger.warning("MMS pre-action: TEC current should be forced to 0 (TC not yet implemented).")

        # Only attempt OB 5V disable when EB is not already in SAFE.
        current_state = int(
            getattr(hk, "CURRENT_OPERATING_STATE", 0) if getattr(hk, "CURRENT_OPERATING_STATE", None) is not None else 0
        )
        if ob5v_pre_action and current_state != 0x02:
            mms_cfg["ob5v_disable_requested"] = True
            disable_ob5v(logger)
        elif ob5v_pre_action:
            logger.info("MMS pre-action: OB 5V disable skipped — EB already in SAFE state.")

        logger.warning("MMS trigger detected: attempting SET SAFE then PSU shutdown. Reasons: %s", "; ".join(reasons))
        safe_confirmed = False
        try:
            interface = eb_interface.get_egse_interface()
            safe_status = ebtcs.safe(interface, 0)
            if safe_status != "ERROR":
                time.sleep(2)
                ret_status = ebtcs.ret(interface, 0, 0, 0, 0, 0, 0)
            else:
                ret_status = "ERROR"

            if safe_status != "ERROR" and ret_status != "ERROR":
                logger.warning("MMS action: SAFE and RET commands sent via ebtcs.")
                rs422_log_path = getattr(app.state.eb_interface, "rs422_log_path", None)
                safe_confirmed = interface.wait_for_safe_state(rs422_log_path, timeout_s=10.0, poll_s=0.5)
                logger.warning(
                    "MMS action: %s",
                    "SAFE operating state confirmed from HK."
                    if safe_confirmed
                    else "SAFE operating state could not be confirmed from HK.",
                )
            else:
                logger.warning("MMS action: SAFE/RET command sequence failed.")
        except Exception as exc:
            logger.error(f"MMS action failed while sending SET SAFE command: {exc}")

        psu_port = state.get("psu_port")

        # Latch MMS state first so trigger information is recorded even if shutdown fails.
        mms_cfg["latched"] = True
        mms_cfg["mode_at_trigger"] = state.get("mode")
        mms_cfg["triggered_at"] = datetime.now().isoformat(timespec="seconds")
        mms_cfg["reasons"] = reasons

        # Always attempt PSU emergency shutdown if PSU port is available, regardless of SAFE confirmation.
        if psu_port is not None:
            if not safe_confirmed:
                logger.warning("MMS action: SAFE state was not confirmed; proceeding with PSU shutdown anyway.")
            lock = state.get("port_lock")
            lock_ctx = lock if lock is not None else nullcontext()
            try:
                with lock_ctx:
                    psu.shutdown_psu_outputs(psu_port)
                logger.warning(
                    "MMS action: PSU emergency shutdown executed (all channels OFF). Reasons: %s",
                    "; ".join(reasons) if reasons else "none",
                )
            except Exception as exc:
                logger.error(f"MMS action failed during PSU emergency shutdown: {exc}")
        else:
            logger.warning("MMS action: PSU emergency shutdown skipped because PSU port is unavailable.")

    try:
        await run.io_bound(_run_mms_actions)
    except Exception as exc:
        mms_cfg["last_error"] = str(exc)
        raise
    finally:
        mms_cfg["in_progress"] = False


# endregion


# ---------------------------------------------------------------------------
# UI controllers — general
# ---------------------------------------------------------------------------
# region UI controllers — general


def create_set_mode(*, app: Any, state: dict[str, Any]) -> Any:
    """Create a mode setter callback bound to current state and app."""

    def set_mode(mode: str) -> None:
        if state.get("ob_replay") is not None and mode != "OB":
            info_log.error("EB mode is unavailable during offline OB replay")
            ui.notify("Offline replay supports OB logs only", type="negative")
            return
        if mode not in ("EB", "OB"):
            return
        previous_mode = state.get("mode")
        state["mode"] = mode
        app.state.egse_mode = mode
        for refresh in state["plot_refreshers"]:
            try:
                refresh(mode)
            except Exception:
                info_log.exception("plot_refresher failed during mode switch to %s", mode)
        if previous_mode != mode:
            # Update PSU channels based on the new mode
            psu_port = state.get("psu_port")
            psu_lock = state.get("psu_lock")
            psu_mode_state = state.get("psu_mode_state")
            if isinstance(psu_mode_state, dict):
                psu_mode_state["ebmode"] = mode == "EB"
            if psu_port:
                ebmode = mode == "EB"
                voltage_mode = state.get("voltage_mode", "NOM")

                def _apply_psu_channels() -> None:
                    # Mark the command in-flight so the monitor thread backs off, and
                    # bound the lock wait so a stuck lock can't freeze PSU telemetry
                    # forever (see psu.init_psu_comms for the failure mode).
                    psu.set_psu_command_in_flight(psu_mode_state, True)
                    try:
                        if psu_lock is not None:
                            acquired = psu_lock.acquire(timeout=5.0)
                            if not acquired:
                                info_log.error("PSU mode switch to %s failed: PSU lock was busy for 5s.", mode)
                                return
                            try:
                                psu.setChannels(psu_port, ebmode, voltage_mode)
                            finally:
                                psu_lock.release()
                        else:
                            psu.setChannels(psu_port, ebmode, voltage_mode)
                    except Exception:
                        info_log.exception("PSU mode switch to %s failed", mode)
                    finally:
                        psu.set_psu_command_in_flight(psu_mode_state, False)

                # Run off the event loop: psu_lock can be briefly held by the PSU
                # monitor thread or another script action, and this callback often
                # runs directly on the UI event loop (not via run.io_bound).
                threading.Thread(target=_apply_psu_channels, name="ob-eb-mode-psu-apply", daemon=True).start()
            # Run mode change resetters
            for reset in state.get("mode_change_resetters", []):
                try:
                    reset()
                except Exception:
                    info_log.exception("mode_change_resetter failed during mode switch to %s", mode)
        sync_packet_tabs = state.get("sync_packet_tabs")
        if callable(sync_packet_tabs):
            try:
                sync_packet_tabs(mode)
            except Exception:
                info_log.exception("sync_packet_tabs failed during mode switch to %s", mode)

    return set_mode


def dispatch_ob_tc(state: dict[str, Any], command: Any, *args: Any, **kwargs: Any) -> Any:
    """Send an OB TC using the shared OB port lock when available."""

    def _port_is_available(port: Any) -> bool:
        if port is None or not callable(getattr(port, "write", None)):
            return False
        is_open = getattr(port, "is_open", True)
        return bool(is_open)

    ob_port = state.get("ob_port")
    worker = state.get("ob_worker")
    worker_port = getattr(worker, "port", None) if worker is not None else None
    active_port = worker_port if worker is not None else ob_port
    if not _port_is_available(active_port):
        ui.notify("OB port unavailable", color="negative")
        return "ERROR"

    if worker is not None:
        return worker.submit(command, *args, priority=1, **kwargs)
    lock = state.get("port_lock")
    lock_ctx = lock if lock is not None else nullcontext()
    with lock_ctx:
        return command(ob_port, *args, **kwargs)


def dispatch_eb_tc(state: dict[str, Any], command: Any, *args: Any, **kwargs: Any) -> Any:
    """Send an EB TC using the shared EB interface when available."""
    from utility_modules import eb_interface

    interface = state.get("eb_interface")
    if interface is None:
        interface = eb_interface.get_egse_interface()
        state["eb_interface"] = interface
    if interface is None:
        ui.notify("EB interface unavailable", color="negative")
        return

    return command(interface, *args, **kwargs)


# endregion


# ---------------------------------------------------------------------------
# UI controllers — SCI packet navigation
# ---------------------------------------------------------------------------
# region UI controllers — SCI packet navigation


def render_sci_plotly_figures(
    sci_packets: list[Any],
    title_prefix: str = "SCI Buffer",
    adc_right_shift: int = 0,
) -> list[Any]:
    """Build interactive SCI figures for EB packets or accumulated OB points.

    The plotting module expects attribute-based packet objects. OB telemetry can
    arrive as either dictionaries or decoded objects, so normalize mappings here
    and keep the widget independent of packet representation.
    """
    from analysis_modules import sci_plot

    normalized_packets = [SimpleNamespace(**packet) if isinstance(packet, dict) else packet for packet in sci_packets]
    return sci_plot.render_sci_packets_plotly_figures(
        sci_packets=normalized_packets,
        title_prefix=title_prefix,
    )


def create_sci_navigation_state(max_packets: int = 12) -> dict[str, Any]:
    """Create backend-owned SCI navigation state for packet/point swiping."""
    return {
        "packets": [],
        "identities": set(),
        "packet_index": 0,
        "point_index": 0,
        "max_packets": max_packets,
    }


def sci_add_packet(sci_state: dict[str, Any], raw_packet: Any) -> Any | None:
    """Normalize, dedupe, and insert an SCI packet into backend state."""
    packet_obj = raw_packet
    if not hasattr(packet_obj, "__dict__") and isinstance(packet_obj, dict):
        packet_obj = SimpleNamespace(**packet_obj)
    if not hasattr(packet_obj, "__dict__"):
        return None

    packet_obj = eb_packet_utility.merge_sci_data_packet(packet_obj)

    def _packet_sort_key(packet: Any) -> int:
        try:
            return int(getattr(packet, "PACKET_NUMBER", 0))
        except (TypeError, ValueError):
            return 0

    def _packet_identity(packet: Any) -> tuple[Any, ...]:
        packet_number = getattr(packet, "PACKET_NUMBER", None)
        criticality = getattr(packet, "SCI_PACKET_CRITICALITY", None)
        point_count = int(getattr(packet, "SCI_POINT_COUNT", 0) or 0)
        first_abs_step = None
        last_abs_step = None
        sci_points = getattr(packet, "SCI_POINTS", None)
        if sci_points:
            first_abs_step = getattr(sci_points[0], "ABS_STEPS", None)
            last_abs_step = getattr(sci_points[-1], "ABS_STEPS", None)
        elif hasattr(packet, "ABS_STEPS"):
            first_abs_step = getattr(packet, "ABS_STEPS", None)
            last_abs_step = first_abs_step
        return (packet_number, criticality, point_count, first_abs_step, last_abs_step)

    identities = sci_state.setdefault("identities", set())
    identity = _packet_identity(packet_obj)
    if identity in identities:
        return packet_obj

    packets = sci_state.setdefault("packets", [])
    packets.append(packet_obj)
    packets.sort(key=_packet_sort_key)

    max_packets = int(sci_state.get("max_packets", 12) or 12)
    if len(packets) > max_packets:
        sci_state["packets"] = packets[-max_packets:]
        packets = sci_state["packets"]

    sci_state["identities"] = {_packet_identity(packet) for packet in packets}
    sci_set_packet_index(sci_state, len(packets) - 1)
    return packet_obj


def sci_current_packet(sci_state: dict[str, Any]) -> Any | None:
    packets = sci_state.get("packets") or []
    if not packets:
        return None
    idx = int(sci_state.get("packet_index", 0)) % len(packets)
    return packets[idx]


def sci_set_packet_index(sci_state: dict[str, Any], packet_index: int) -> Any | None:
    packets = sci_state.get("packets") or []
    if not packets:
        sci_state["packet_index"] = 0
        sci_state["point_index"] = 0
        return None
    sci_state["packet_index"] = packet_index % len(packets)
    sci_state["point_index"] = 0
    return sci_current_packet(sci_state)


def sci_set_point_index(sci_state: dict[str, Any], point_index: int) -> int:
    packet = sci_current_packet(sci_state)
    if packet is None:
        sci_state["point_index"] = 0
        return 0
    point_count = int(getattr(packet, "SCI_POINT_COUNT", 0) or 0)
    if point_count <= 0:
        sci_state["point_index"] = 0
        return 0
    sci_state["point_index"] = min(max(point_index, 0), point_count - 1)
    return sci_state["point_index"]


def sci_shift_packet_index(sci_state: dict[str, Any], delta: int) -> Any | None:
    current = int(sci_state.get("packet_index", 0))
    return sci_set_packet_index(sci_state, current + delta)


def sci_shift_point_index(sci_state: dict[str, Any], delta: int) -> int:
    packet = sci_current_packet(sci_state)
    if packet is None:
        sci_state["point_index"] = 0
        return 0
    point_count = int(getattr(packet, "SCI_POINT_COUNT", 0) or 0)
    if point_count <= 0:
        sci_state["point_index"] = 0
        return 0
    current = int(sci_state.get("point_index", 0))
    sci_state["point_index"] = (current + delta) % point_count
    return sci_state["point_index"]


# endregion
