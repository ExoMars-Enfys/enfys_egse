from __future__ import annotations

import time

# Std library
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Added packages
from nicegui import app, ui

# Local modules
# core
from core_modules import tmstruct

# utilities
from utility_modules import hk_conversions
from utility_modules.eb_packet_utility import adu_to_temp, eb_tec_adu_to_temp

# widgets
from widget_modules import monitoring_limits, popup_widget

ValueGetter = Callable[[Any], Any]


_TMSTRUCT_HK_FIELDS = {name for name, _ in tmstruct.hk}
_TMSTRUCT_EB_HK_FIELDS = {name for name, _ in tmstruct.eb_hk}

# Standalone OB HK field -> embedded OB-in-EB HK field names.
_OB_EMBEDDED_ALIAS_OVERRIDES: dict[str, tuple[str, ...]] = {
    "CMD_CNT": ("OB_COMMAND_COUNT",),
    "PWR_STAT": ("OB_POWER_STATUS",),
    "HK_V_3V3": ("OB_3V3_VOLTAGE",),
    "HK_V_1V5": ("OB_1V5_VOLTAGE",),
    "DIGITAL_TRP": ("OB_DIGITAL_TRP",),
    "DETEC_TRP": ("OB_DETECTOR_TRP",),
    "MECH_TRP": ("OB_MECHANISM_TRP",),
    "MOTOR_TRP": ("OB_MOTOR_TRP",),
    "MTR_ABS_STEPS": ("OB_MOTOR_ABS_STEPS",),
    "MTR_REL_STEPS": ("OB_MOTOR_REL_STEPS",),
    "MTR_CURRENT": ("OB_MOTOR_CURRENT",),
    "MTR_GUARD_SELECT": ("OB_MOTOR_GUARD_TIME",),
    "MTR_CHOP": ("OB_MOTOR_RECVAL",),
    "MTR_SPEED": ("OB_SPEED", "OB_MOTOR_PWM_RATE"),
    "THRM_MECH_ON_SP": ("OB_THERMAL_MECH_MIN",),
    "THRM_MECH_OFF_SP": ("OB_THERMAL_MECH_MAX",),
    "THRM_DET_ON_SP": ("OB_THERMAL_DET_MIN",),
    "THRM_DET_OFF_SP": ("OB_THERMAL_DET_MAX",),
    "HK_SAMPLES": ("HK_NUMBER_OF_SAMPLES",),
    "HK_MECH_CUR": ("OB_MECH_CURRENT",),
    "SWIR_OFFSET": ("OB_SWIR_OFFSET",),
    "MWIR_OFFSET": ("OB_MWIR_OFFSET",),
}


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    getter: ValueGetter
    unit: str = ""
    bounds: tuple[float, float] | None = None
    alarm_bounds: tuple[float, float] | None = None
    bounds_adu: tuple[float, float] | None = None
    alarm_bounds_adu: tuple[float, float] | None = None
    decimals: int = 2
    render: str = "value"
    chip_text: str | None = None
    true_text: str = "YES"
    false_text: str = "NO"
    true_color: str = "green"
    false_color: str = "grey"
    color_map: dict[str, str] | None = None
    popup_attr: str | None = None
    popup_title: str | None = None
    popup_names: list[str] | None = None
    popup_fields: tuple[tuple[str, ValueGetter], ...] | None = None


@dataclass
class MetricPill:
    spec: MetricSpec
    chip: Any


@dataclass
class MetricsCardController:
    title: str
    pills: list[MetricPill]
    card: Any
    last_packet: Any = None

    def set_visible(self, visible: bool) -> None:
        if visible:
            self.card.classes(remove="hidden")
            return
        self.card.classes(add="hidden")

    def set_no_data(self) -> None:
        for pill in self.pills:
            pill.chip.set_text("---")
            pill.chip.set_background_color("grey")

    def update_from_packet(self, packet: Any) -> None:
        self.last_packet = packet
        display_mode = str(getattr(app.state, "hk_display_mode", "REAL")).upper()
        for pill in self.pills:
            value = _safe_get_value(pill.spec.getter, packet)

            if pill.spec.render == "bool_status":
                active = _coerce_bool(value)
                if active is None:
                    pill.chip.set_text("---")
                    pill.chip.set_background_color("grey")
                else:
                    pill.chip.set_text(pill.spec.true_text if active else pill.spec.false_text)
                    pill.chip.set_background_color(pill.spec.true_color if active else pill.spec.false_color)
                continue

            if pill.spec.render == "error_chip":
                active = _coerce_bool(value)
                pill.chip.set_text(pill.spec.chip_text or pill.spec.label)
                if active is None:
                    pill.chip.set_background_color("grey")
                else:
                    pill.chip.set_background_color("red" if active else "grey")
                continue

            if pill.spec.render == "state_chip":
                active = _coerce_bool(value)
                pill.chip.set_text(pill.spec.chip_text or pill.spec.label)
                if active is None:
                    pill.chip.set_background_color("grey")
                else:
                    pill.chip.set_background_color("green" if active else "grey")
                continue

            if pill.spec.render == "status_light":
                active = _coerce_bool(value)
                pill.chip.set_text("●")
                if active is None:
                    pill.chip.set_background_color("grey")
                else:
                    pill.chip.set_background_color("green" if active else "red")
                continue

            if pill.spec.render == "static_chip":
                pill.chip.set_text(pill.spec.chip_text or pill.spec.label)
                pill.chip.set_background_color("grey")
                continue

            if value is None:
                pill.chip.set_text("---")
                pill.chip.set_background_color("grey")
                continue

            label = _format_value(value, pill.spec.decimals, pill.spec.unit, display_mode=display_mode)
            pill.chip.set_text(label)

            warn_bounds = pill.spec.bounds_adu if display_mode == "ADU" else pill.spec.bounds
            alarm_bounds = pill.spec.alarm_bounds_adu if display_mode == "ADU" else pill.spec.alarm_bounds

            if warn_bounds is None and alarm_bounds is None:
                if pill.spec.color_map is not None:
                    mapped_color = pill.spec.color_map.get(str(value), pill.spec.color_map.get("_default", "grey"))
                    pill.chip.set_background_color(mapped_color)
                else:
                    pill.chip.set_background_color("grey")
                continue

            numeric = _coerce_float(value)
            if numeric is None:
                pill.chip.set_background_color("grey")
                continue

            if warn_bounds is not None and alarm_bounds is not None:
                warn_low, warn_high = warn_bounds
                alarm_low, alarm_high = alarm_bounds
                if warn_low <= numeric <= warn_high:
                    pill.chip.set_background_color("green")
                elif alarm_low <= numeric <= alarm_high:
                    pill.chip.set_background_color("yellow")
                else:
                    pill.chip.set_background_color("red")
                continue

            active_bounds = warn_bounds if warn_bounds is not None else alarm_bounds
            if active_bounds is None:
                pill.chip.set_background_color("grey")
                continue
            low, high = active_bounds
            in_bounds = low <= numeric <= high
            pill.chip.set_background_color("green" if in_bounds else "red")


@dataclass
class PacketMetricsCardController:
    state: dict[str, Any]
    cards: dict[str, Any]
    chips: dict[str, Any]

    def set_no_data(self) -> None:
        for chip in self.chips.values():
            chip.set_text("---")
            chip.set_background_color("grey")

    def set_mode(self, mode: str) -> None:
        eb_mode = mode == "EB"
        if "tc_rejected" in self.cards:
            self.cards["tc_rejected"].classes(remove="hidden" if eb_mode else None)
            if not eb_mode:
                self.cards["tc_rejected"].classes(add="hidden")
        if "post_packets" in self.cards:
            self.cards["post_packets"].classes(remove="hidden" if eb_mode else None)
            if not eb_mode:
                self.cards["post_packets"].classes(add="hidden")

    def refresh(self) -> None:
        packet_state = self.state.get("packet_counts", {})
        telemetry_last = packet_state.get("telemetry_last", {})
        eb_hk = telemetry_last.get("EB_HK", {})
        mode = str(self.state.get("mode", "EB")).upper()

        if mode == "OB":
            hk_time = self.state.get("last_ob_tm_time")
            if hk_time is None:
                ob_hk = telemetry_last.get("OB_HK", {})
                hk_time = ob_hk.get("TIME")
        else:
            hk_time = eb_hk.get("TIME")
        if hk_time is None:
            hk_time_text = "---"
        elif isinstance(hk_time, datetime):
            delta = datetime.now() - hk_time
            total_seconds = int(delta.total_seconds())
            # Format elapsed time as HH:MM:SS
            hk_time_text = time.strftime("%H:%M:%S", time.gmtime(total_seconds))
        else:
            # Fallback for numeric timestamps (epoch seconds or monotonic floats)
            try:
                hk_time_val = float(hk_time)
                # Prefer epoch-based difference where plausible
                now_epoch = time.time()
                delta_seconds = int(now_epoch - hk_time_val)
                if delta_seconds < 0:
                    # If negative, try monotonic-based difference
                    delta_seconds = int(time.monotonic() - hk_time_val)
                hk_time_text = time.strftime("%H:%M:%S", time.gmtime(max(delta_seconds, 0)))
            except Exception:
                hk_time_text = str(hk_time)

        values = {
            "tc_rejected": eb_hk.get("TCS_REJECTED", "---"),
            "hk_time": hk_time_text,
            "hk_packets": packet_state.get("hk", 0),
            "post_packets": packet_state.get("post", 0),
            "sci_packets": packet_state.get("sci", 0),
        }

        for key, chip in self.chips.items():
            value = values.get(key, "---")
            chip.set_text(str(value))
            chip.set_background_color("grey")


def _safe_get_value(getter: ValueGetter, packet: Any) -> Any:
    try:
        return getter(packet)
    except Exception:
        return None


def _first_available_value(packet: Any, field_names: str | tuple[str, ...] | list[str]) -> tuple[Any, str | None]:
    names = (field_names,) if isinstance(field_names, str) else tuple(field_names)
    for name in names:
        if hasattr(packet, name):
            value = getattr(packet, name, None)
            if value is not None:
                return value, name
    return None, None


def _ob_field_aliases(*field_names: str) -> tuple[str, ...]:
    """Return ordered OB field aliases available in tmstruct.

    Names are resolved using standalone OB HK fields, known embedded OB-in-EB
    aliases, and OB_ prefixed variants when present in ``tmstruct.eb_hk``.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _append_if_known(name: str) -> None:
        if name in seen:
            return
        if name in _TMSTRUCT_HK_FIELDS or name in _TMSTRUCT_EB_HK_FIELDS:
            candidates.append(name)
            seen.add(name)

    def _append(name: str) -> None:
        if name in seen:
            return
        candidates.append(name)
        seen.add(name)

    for field_name in field_names:
        _append_if_known(field_name)

        override_names = _OB_EMBEDDED_ALIAS_OVERRIDES.get(field_name, ())
        for override in override_names:
            _append_if_known(override)
            _append(override)

        prefixed = field_name if field_name.startswith("OB_") else f"OB_{field_name}"
        _append_if_known(prefixed)
        _append(prefixed)

    # Fall back to requested names if tmstruct changes unexpectedly.
    return tuple(candidates) if candidates else tuple(field_names)


def _decoded_ob_value(packet: Any, field_names: str | tuple[str, ...] | list[str]) -> float | None:
    raw, resolved_name = _first_available_value(packet, field_names)
    if raw is None or resolved_name is None:
        return None

    display_mode = str(getattr(app.state, "hk_display_mode", "REAL")).upper()
    if display_mode == "ADU":
        try:
            return float(raw) if resolved_name.startswith("OB_") else float(int(raw) >> 4)
        except (TypeError, ValueError):
            return None

    try:
        converted = hk_conversions.decode_field(packet, resolved_name)
    except Exception:
        converted = None
    if converted is not None:
        try:
            return float(converted)
        except (TypeError, ValueError):
            return None

    try:
        raw_value = float(raw) if resolved_name.startswith("OB_") else float(int(raw) >> 4)
        if resolved_name == "HK_V_3V3":
            return raw_value * 4.05 / 4095.0 * 2.0
        if resolved_name == "HK_V_1V5":
            return raw_value * 4.05 / 4095.0
        if resolved_name in {
            "DIGITAL_TRP",
            "DETEC_TRP",
            "MECH_TRP",
            "MOTOR_TRP",
            "OB_DIGITAL_TRP",
            "OB_DETECTOR_TRP",
            "OB_MECHANISM_TRP",
            "OB_MOTOR_TRP",
        }:
            return float(adu_to_temp(int(raw_value)))
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None

    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _format_value(value: Any, decimals: int, unit: str, *, display_mode: str = "REAL") -> str:
    mode = str(display_mode or "REAL").upper()
    if isinstance(value, bool):
        text = "YES" if value else "NO"
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if mode == "ADU":
            text = str(int(value))
        else:
            text = f"{value:.{decimals}f}"
    else:
        text = str(value)

    if mode == "ADU":
        return text
    return f"{text} {unit}".strip()


def _flag_true(packet: Any, attr_name: str, bit_name: str) -> bool | None:
    namespace = getattr(packet, attr_name, None)
    if namespace is None:
        return None
    bit = getattr(namespace, bit_name, None)
    if bit is None:
        return None
    return bool(bit)


def _has_any_asserted(packet: Any, attr_name: str, fields: list[str]) -> bool | None:
    namespace = getattr(packet, attr_name, None)
    if namespace is None:
        return None
    return any(bool(getattr(namespace, field, 0)) for field in fields)


def decoded(packet: Any, field_name: str) -> float | None:
    """Return a field in ADU or engineering units.

    EB fields continue to use ``hk_conversions.decode_field``. Native OB fields
    use their standalone OB HK names and fall back to local conversions when
    ``hk_conversions`` does not yet define those names.
    """
    raw = getattr(packet, field_name, None)
    if raw is None:
        return None

    display_mode = str(getattr(app.state, "hk_display_mode", "REAL")).upper()
    if display_mode == "ADU":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    # Prefer the central conversion table when it knows the field.
    try:
        converted = hk_conversions.decode_field(packet, field_name)
    except Exception:
        converted = None
    if converted is not None:
        try:
            return float(converted)
        except (TypeError, ValueError):
            return None

    # Standalone OB HK fallback conversions. These names come directly from
    # core_modules.tmstruct.hk. Native fields pack the 12-bit ADC value into
    # the upper bits of a 16-bit field, so it must be shifted out first.
    try:
        raw_value = float(int(raw) >> 4)
        if field_name == "HK_V_3V3":
            return raw_value * 4.05 / 4095.0 * 2.0
        if field_name == "HK_V_1V5":
            return raw_value * 4.05 / 4095.0
        if field_name in {"DIGITAL_TRP", "DETEC_TRP", "MECH_TRP", "MOTOR_TRP"}:
            return float(adu_to_temp(int(raw_value)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    return None


def _hex_attr(packet: Any, field_name: str | tuple[str, ...], width: int = 2) -> str | None:
    """Format an integer packet attribute as a zero-padded hexadecimal value."""
    names = (field_name,) if isinstance(field_name, str) else field_name
    for name in names:
        value = getattr(packet, name, None)
        if value is not None:
            try:
                return f"0x{int(value):0{width}X}"
            except (TypeError, ValueError):
                continue
    return None


def _tec_temp(packet: Any) -> float | None:
    raw = getattr(packet, "EB_PELTIER_TEMP", None)
    if raw is None:
        return None
    display_mode = str(getattr(app.state, "hk_display_mode", "REAL")).upper()
    if display_mode == "ADU":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return eb_tec_adu_to_temp(int(raw))


def _tec_drive_current(packet: Any) -> float | None:
    raw = getattr(packet, "EB_TEC_DRIVE_CURRENT", None)
    if raw is None:
        return None
    display_mode = str(getattr(app.state, "hk_display_mode", "REAL")).upper()
    if display_mode == "ADU":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return float(raw) * 0.0000162


def _ob_thermal_setpoint(packet: Any, field_names: str | tuple[str, ...]) -> float | None:
    raw, resolved_name = _first_available_value(packet, field_names)
    if raw is None or resolved_name is None:
        return None
    display_mode = str(getattr(app.state, "hk_display_mode", "REAL")).upper()
    if display_mode == "ADU":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    try:
        return float(adu_to_temp(int(raw) >> 4 if not resolved_name.startswith("OB_") else int(raw)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _state_name(packet: Any) -> str | None:
    value = getattr(packet, "CURRENT_OPERATING_STATE", None)
    if value is None:
        return None
    return {0x00: "INITIALISING", 0x02: "SAFE", 0x04: "STANDBY", 0x08: "ACQ"}.get(int(value), str(value))


def _ob_warm(packet: Any) -> bool | None:
    det = _flag_true(packet, "INSTR_STATUS_FLAGS", "DETECTOR_WARM")
    mech = _flag_true(packet, "INSTR_STATUS_FLAGS", "MECHANISM_WARM")
    if det is None or mech is None:
        return None
    return bool(det and mech)


def _parked(packet: Any) -> bool | None:
    parked_flag = _flag_true(packet, "INSTR_STATUS_FLAGS", "MECHANISM_PARKED")
    if parked_flag is None:
        return None
    return not parked_flag


def _direction(packet: Any) -> str | None:
    namespace = getattr(packet, "MTR_FLAGS", None)
    if namespace is None or not hasattr(namespace, "DIR"):
        return None
    moving = _flag_true(packet, "MTR_FLAGS", "MOVING")
    if moving is not True:
        return None
    return "TO BASE" if int(getattr(namespace, "DIR", 0)) == 0 else "TO OUTER"


def _stop(packet: Any) -> str | None:
    namespace = getattr(packet, "MTR_FLAGS", None)
    if namespace is None:
        return None
    if bool(getattr(namespace, "BASE", 0)):
        return "BASE"
    if bool(getattr(namespace, "OUTER", 0)):
        return "OUTER"
    return "Not At Stop"


def _ns_bool(packet: Any, attr_name: str, bit_name: str) -> bool | None:
    namespace = getattr(packet, attr_name, None)
    if namespace is None or not hasattr(namespace, bit_name):
        return None
    return bool(getattr(namespace, bit_name))


def _eb_hk_specs() -> list[MetricSpec]:
    warning_names = [name for name, _ in tmstruct.eb_warning_flags]
    fdir_names = [name for name, _ in tmstruct.eb_fdir_flags]

    return [
        MetricSpec(
            key="eb_operating_state",
            label="OP",
            getter=_state_name,
            color_map={"INITIALISING": "grey", "SAFE": "blue", "STANDBY": "green", "ACQ": "purple", "_default": "grey"},
        ),
        MetricSpec(
            key="eb_has_errors",
            label="ERR",
            getter=lambda hk: bool(getattr(hk, "ERROR_FLAGS", 0)),
            render="bool_status",
            true_text="ERROR",
            false_text="ERROR",
            true_color="red",
            false_color="green",
            popup_attr="ERROR_FLAGS_BITS",
            popup_title="Error Flags Bitmap",
            popup_names=warning_names,
        ),
        MetricSpec(
            key="eb_has_warnings",
            label="WRN",
            getter=lambda hk: bool(getattr(hk, "WARNING_FLAGS", 0)),
            render="bool_status",
            true_text="WARN",
            false_text="WARN",
            true_color="yellow",
            false_color="green",
            popup_attr="WARNING_FLAGS_BITS",
            popup_title="Warning Flags Bitmap",
            popup_names=warning_names,
        ),
        MetricSpec(
            key="eb_fdir_alarm",
            label="FDIR ALM",
            getter=lambda hk: bool(getattr(hk, "FDIR_ALARM_FLAGS", 0)),
            render="bool_status",
            true_text="FDIR ALARM",
            false_text="FDIR ALARM",
            true_color="red",
            false_color="green",
            popup_attr="FDIR_ALARM_FLAGS_BITS",
            popup_title="FDIR Alarm Flags Bitmap",
            popup_names=fdir_names,
        ),
        MetricSpec(
            key="eb_fdir_warning",
            label="FDIR WRN",
            getter=lambda hk: bool(getattr(hk, "FDIR_WARNING_FLAGS", 0)),
            render="bool_status",
            true_text="FDIR WARN",
            false_text="FDIR WARN",
            true_color="yellow",
            false_color="green",
            popup_attr="FDIR_WARNING_FLAGS_BITS",
            popup_title="FDIR Warning Flags Bitmap",
            popup_names=fdir_names,
        ),
        MetricSpec(
            key="eb_12v",
            label="+12V",
            getter=lambda hk: decoded(hk, "EB_MEAS_MAIN_12V"),
            unit="V",
            **monitoring_limits.metric_limit_kwargs("eb_12v"),
        ),
        MetricSpec(
            key="eb_neg12v",
            label="-12V",
            getter=lambda hk: decoded(hk, "EB_MEAS_MAIN_NEG12V"),
            unit="V",
            **monitoring_limits.metric_limit_kwargs("eb_neg12v"),
        ),
        MetricSpec(
            key="eb_5v",
            label="+5V",
            getter=lambda hk: decoded(hk, "EB_MEAS_5V"),
            unit="V",
            **monitoring_limits.metric_limit_kwargs("eb_5v"),
        ),
        MetricSpec(
            key="eb_3v3",
            label="+3V3",
            getter=lambda hk: decoded(hk, "EB_MEAS_3V3"),
            unit="V",
            **monitoring_limits.metric_limit_kwargs("eb_3v3"),
        ),
        MetricSpec(
            key="eb_mcu_temp",
            label="MCU TEMP",
            getter=lambda hk: decoded(hk, "EB_MCU_INTERNAL_TEMP"),
            unit="°C",
            **monitoring_limits.metric_limit_kwargs("eb_mcu_temp"),
        ),
        MetricSpec(
            key="eb_internal_temp",
            label="INTRNL TEMP",
            getter=lambda hk: decoded(hk, "EB_INTERNAL_TRP_TEMP"),
            unit="°C",
            **monitoring_limits.metric_limit_kwargs("eb_internal_temp"),
        ),
        MetricSpec(
            key="eb_psu_temp",
            label="PSU TEMP",
            getter=lambda hk: decoded(hk, "EB_PSU_BOARD_TEMP"),
            unit="°C",
            **monitoring_limits.metric_limit_kwargs("eb_psu_temp"),
        ),
        MetricSpec(key="setpoint", label="SETPOINT", getter=lambda hk: getattr(hk, "TEC_SETPOINT", None)),
        MetricSpec(
            key="drive_i",
            label="Drive I",
            getter=_tec_drive_current,
            unit="A",
            decimals=4,
        ),
        MetricSpec(key="temp", label="TEMP", getter=_tec_temp, unit="°C"),
        MetricSpec(key="dac", label="DAC OUT", getter=lambda hk: getattr(hk, "EB_TEC_DAC_OUTPUT", None)),
        MetricSpec(
            key="eb_tec_at_setpoint",
            label="At Set",
            getter=lambda hk: _flag_true(hk, "INSTR_STATUS_FLAGS", "TEC_AT_SETPOINT"),
            render="bool_status",
            true_text="AT SET",
            false_text="AT SET",
            true_color="green",
            false_color="grey",
        ),
    ]


def _ob_hk_specs(state: dict[str, Any] | None = None) -> list[MetricSpec]:
    """Metrics sourced directly from the standalone OB ``tmstruct.hk`` packet."""

    def _ob_sci_value(packet: Any, field_name: str) -> Any:
        value, _ = _first_available_value(packet, _ob_field_aliases(field_name))
        if value is not None:
            return value
        if not isinstance(state, dict):
            return None

        candidates = [state.get("latest_ob_sci")]
        recent_packets = state.get("ob_sci_packets")
        if isinstance(recent_packets, list) and recent_packets:
            candidates.append(recent_packets[-1])

        for candidate in candidates:
            if isinstance(candidate, dict):
                value = candidate.get(field_name)
            else:
                value = getattr(candidate, field_name, None) if candidate is not None else None
            if value is not None:
                return value
        return None

    return [
        # OB analogue housekeeping
        MetricSpec(
            key="3v3",
            label="+3.3V",
            getter=lambda hk: _decoded_ob_value(hk, _ob_field_aliases("HK_V_3V3")),
            unit="V",
            **monitoring_limits.metric_limit_kwargs("ob_3v3"),
        ),
        MetricSpec(
            key="1v5",
            label="+1.5V",
            getter=lambda hk: _decoded_ob_value(hk, _ob_field_aliases("HK_V_1V5")),
            unit="V",
            **monitoring_limits.metric_limit_kwargs("ob_1v5"),
        ),
        MetricSpec(
            key="dig",
            label="DIG:",
            getter=lambda hk: _decoded_ob_value(hk, _ob_field_aliases("DIGITAL_TRP")),
            unit="°C",
            **monitoring_limits.metric_limit_kwargs("ob_trp"),
        ),
        MetricSpec(
            key="det",
            label="DET:",
            getter=lambda hk: _decoded_ob_value(hk, _ob_field_aliases("DETEC_TRP")),
            unit="°C",
            **monitoring_limits.metric_limit_kwargs("ob_trp"),
        ),
        MetricSpec(
            key="mech",
            label="MECH:",
            getter=lambda hk: _decoded_ob_value(hk, _ob_field_aliases("MECH_TRP")),
            unit="°C",
            **monitoring_limits.metric_limit_kwargs("ob_trp"),
        ),
        MetricSpec(
            key="mtr",
            label="MTR",
            getter=lambda hk: _decoded_ob_value(hk, _ob_field_aliases("MOTOR_TRP")),
            unit="°C",
            **monitoring_limits.metric_limit_kwargs("ob_trp"),
        ),
        MetricSpec(
            key="cmd_cnt",
            label="CMD CNT",
            getter=lambda hk: _first_available_value(hk, _ob_field_aliases("CMD_CNT"))[0],
        ),
        MetricSpec(key="pwr_stat", label="PWR STAT", getter=lambda hk: _hex_attr(hk, _ob_field_aliases("PWR_STAT"))),
        MetricSpec(
            key="mech_pwr",
            label="MECH PWR",
            getter=lambda hk: _status_mask_set(
                hk,
                _ob_field_aliases("PWR_STAT"),
                0x01,
            ),
            render="bool_status",
            true_text="MECH ON",
            false_text="MECH OFF",
            true_color="green",
            false_color="grey",
        ),
        MetricSpec(
            key="det_pwr",
            label="DET PWR",
            getter=lambda hk: _status_mask_set(
                hk,
                _ob_field_aliases("PWR_STAT"),
                0x02,
            ),
            render="bool_status",
            true_text="DET ON",
            false_text="DET OFF",
            true_color="green",
            false_color="grey",
        ),
        MetricSpec(
            key="hk_mech_cur",
            label="MECH CUR",
            getter=lambda hk: _decoded_ob_value(hk, _ob_field_aliases("HK_MECH_CUR")),
            unit="mA",
            decimals=2,
        ),
        # Motor status and configuration
        MetricSpec(
            key="ob_motor_moving",
            label="MOV",
            getter=lambda hk: _flag_true(hk, "MTR_FLAGS", "MOVING"),
            render="bool_status",
            true_text="Moving",
            false_text="Stationary",
            true_color="green",
            false_color="grey",
        ),
        MetricSpec(
            key="ob_direction",
            label="DIR",
            getter=_direction,
            color_map={"TO BASE": "purple", "TO OUTER": "blue", "_default": "grey"},
        ),
        MetricSpec(
            key="ob_stop",
            label="STOP",
            getter=_stop,
            color_map={"BASE": "purple", "OUTER": "blue", "Not At Stop": "grey", "_default": "grey"},
        ),
        MetricSpec(
            key="ob_steps",
            label="ABS STEPS",
            getter=lambda hk: _first_available_value(hk, _ob_field_aliases("MTR_ABS_STEPS"))[0],
        ),
        MetricSpec(
            key="mtr_rel_steps",
            label="REL STEPS",
            getter=lambda hk: _first_available_value(hk, _ob_field_aliases("MTR_REL_STEPS"))[0],
        ),
        MetricSpec(
            key="ob_mech_cal",
            label="CAL",
            getter=lambda hk: _flag_true(hk, "MTR_FLAGS", "CAL"),
            render="state_chip",
            chip_text="CAL",
        ),
        MetricSpec(
            key="mtr_params",
            label="MTR PARAMS",
            getter=lambda hk: True,
            render="static_chip",
            popup_title="Motor Parameters",
            popup_fields=(
                ("Current", lambda hk: _hex_attr(hk, _ob_field_aliases("MTR_CURRENT"))),
                ("Guard", lambda hk: _hex_attr(hk, _ob_field_aliases("MTR_GUARD_SELECT"))),
                ("Chop", lambda hk: _hex_attr(hk, _ob_field_aliases("MTR_CHOP"))),
                ("Speed", lambda hk: _hex_attr(hk, _ob_field_aliases("MTR_SPEED"))),
            ),
        ),
        # Heater state bitfield
        MetricSpec(
            key="mech_htr_status",
            label="MECH HTR",
            getter=lambda hk: _ns_bool(hk, "THRM_STATUS", "HMS"),
            render="bool_status",
            true_text="ON",
            false_text="OFF",
            true_color="green",
            false_color="grey",
            popup_title="Mechanism Heater Status",
            popup_fields=(
                ("Manual", lambda hk: _ns_bool(hk, "THRM_STATUS", "MM")),
                ("Automatic", lambda hk: _ns_bool(hk, "THRM_STATUS", "MA")),
                ("ON setpoint", lambda hk: _ob_thermal_setpoint(hk, _ob_field_aliases("THRM_MECH_ON_SP"))),
                ("OFF setpoint", lambda hk: _ob_thermal_setpoint(hk, _ob_field_aliases("THRM_MECH_OFF_SP"))),
            ),
        ),
        MetricSpec(
            key="mech_manual",
            label="MANUAL",
            getter=lambda hk: _ns_bool(hk, "THRM_STATUS", "MM"),
            render="state_chip",
            chip_text="MANUAL",
        ),
        MetricSpec(
            key="mech_auto",
            label="AUTO",
            getter=lambda hk: _ns_bool(hk, "THRM_STATUS", "MA"),
            render="state_chip",
            chip_text="AUTO",
        ),
        MetricSpec(
            key="det_htr_status",
            label="DET HTR",
            getter=lambda hk: _ns_bool(hk, "THRM_STATUS", "HDS"),
            render="bool_status",
            true_text="ON",
            false_text="OFF",
            true_color="green",
            false_color="grey",
            popup_title="Detector Heater Status",
            popup_fields=(
                ("Manual", lambda hk: _ns_bool(hk, "THRM_STATUS", "DM")),
                ("Automatic", lambda hk: _ns_bool(hk, "THRM_STATUS", "DA")),
                ("Science", lambda hk: _ns_bool(hk, "THRM_STATUS", "S")),
                ("ON setpoint", lambda hk: _ob_thermal_setpoint(hk, _ob_field_aliases("THRM_DET_ON_SP"))),
                ("OFF setpoint", lambda hk: _ob_thermal_setpoint(hk, _ob_field_aliases("THRM_DET_OFF_SP"))),
            ),
        ),
        MetricSpec(
            key="det_manual",
            label="MANUAL",
            getter=lambda hk: _ns_bool(hk, "THRM_STATUS", "DM"),
            render="state_chip",
            chip_text="MANUAL",
        ),
        MetricSpec(
            key="det_auto",
            label="AUTO",
            getter=lambda hk: _ns_bool(hk, "THRM_STATUS", "DA"),
            render="state_chip",
            chip_text="AUTO",
        ),
        MetricSpec(
            key="det_sci",
            label="SCI",
            getter=lambda hk: _ns_bool(hk, "THRM_STATUS", "S"),
            render="bool_status",
            true_text="SCI TOGGLE",
            false_text="SCI TOGGLE",
            true_color="green",
            false_color="grey",
        ),
        MetricSpec(
            key="mech_htr_min_sp",
            label="ON SP",
            getter=lambda hk: _ob_thermal_setpoint(hk, _ob_field_aliases("THRM_MECH_ON_SP")),
            unit="°C",
        ),
        MetricSpec(
            key="mech_htr_max_sp",
            label="OFF SP",
            getter=lambda hk: _ob_thermal_setpoint(hk, _ob_field_aliases("THRM_MECH_OFF_SP")),
            unit="°C",
        ),
        MetricSpec(
            key="det_htr_min_sp",
            label="ON SP",
            getter=lambda hk: _ob_thermal_setpoint(hk, _ob_field_aliases("THRM_DET_ON_SP")),
            unit="°C",
        ),
        MetricSpec(
            key="det_htr_max_sp",
            label="OFF SP",
            getter=lambda hk: _ob_thermal_setpoint(hk, _ob_field_aliases("THRM_DET_OFF_SP")),
            unit="°C",
        ),
        # OB error bitfields
        MetricSpec(
            key="ob_errors",
            label="OB ERRORS",
            chip_text="ERR",
            getter=lambda hk: any(_ns_bool(hk, "ERRORS", name) for name in ("IPI", "IOS", "ICR", "MOR", "TMO", "IPA")),
            render="error_chip",
            popup_title="OB Errors",
            popup_fields=tuple(
                (name, lambda hk, name=name: _ns_bool(hk, "ERRORS", name))
                for name in ("IPI", "IOS", "ICR", "MOR", "TMO", "IPA")
            ),
        ),
        MetricSpec(
            key="mtr_errors",
            label="MTR ERRORS",
            chip_text="MTR ERR",
            getter=lambda hk: any(_ns_bool(hk, "MTR_ERRORS", name) for name in ("CD", "AB", "ABS", "DSE")),
            render="error_chip",
            popup_title="Motor Errors",
            popup_fields=tuple(
                (name, lambda hk, name=name: _ns_bool(hk, "MTR_ERRORS", name)) for name in ("CD", "AB", "ABS", "DSE")
            ),
        ),
    ]


def _render_metric_grid(
    *,
    specs: list[MetricSpec],
    columns: int,
    pills: list[MetricPill],
) -> None:
    # These pills already contain their description inside the chip.
    label_hidden_pills = {
        "eb_operating_state",
        "eb_has_errors",
        "eb_has_warnings",
        "eb_fdir_alarm",
        "eb_fdir_warning",
        "ob_enabled",
        "ob_home",
        "ob_parked",
        "ob_warm",
        "ob_mech_pwr",
        "ob_det_pwr",
        "ob_acq_cfg_set",
        "ob_motor_moving",
        "ob_mech_cal",
        "mech_pwr",
        "det_pwr",
        "mtr_params",
        "mech_manual",
        "mech_auto",
        "det_manual",
        "det_auto",
        "det_sci",
        "mech_power_on",
        "detector_power_on",
    }

    metric_grid = ui.grid(columns=columns).classes("w-full gap-1")
    if columns == 2:
        metric_grid.style("grid-template-columns: repeat(2, max-content); justify-content: start;")

    with metric_grid:
        for spec in specs:
            if spec.render == "error_chip":
                with (
                    ui.row()
                    .classes("w-full items-center justify-center gap-1")
                    .style("flex-wrap: nowrap; white-space: nowrap;")
                ):
                    if spec.key not in label_hidden_pills:
                        ui.label(spec.label).classes("egse-metric-label whitespace-nowrap")
                    chip = ui.chip("---", color="grey").props("dense").classes("w-fit egse-metric-value")

                pills.append(
                    MetricPill(
                        spec=spec,
                        chip=chip,
                    )
                )
                continue

            # Label and value chip are now in the same horizontal row.
            with (
                ui.row()
                .classes("w-full items-center justify-center gap-1")
                .style("flex-wrap: nowrap; white-space: nowrap;")
            ):
                if spec.key not in label_hidden_pills:
                    ui.label(spec.label).classes("egse-metric-label whitespace-nowrap")

                chip = ui.chip("---", color="grey").props("dense").classes("w-fit egse-metric-value")

                pills.append(
                    MetricPill(
                        spec=spec,
                        chip=chip,
                    )
                )


def _bind_metric_popups(controller: MetricsCardController) -> None:
    for pill in controller.pills:
        spec = pill.spec
        if not spec.popup_attr and not spec.popup_fields:
            continue
        pill.chip.props("clickable")
        pill.chip.classes(add="cursor-pointer")

        def _show_popup(_: Any = None, pill_spec: MetricSpec = spec) -> None:
            if pill_spec.popup_fields:
                packet = controller.last_packet
                details = [(name, _safe_get_value(getter, packet)) for name, getter in pill_spec.popup_fields]
                popup_widget.show_details_popup(
                    title=pill_spec.popup_title or pill_spec.label,
                    details=details,
                )
                return
            popup_widget.show_flag_popup(
                title=pill_spec.popup_title or pill_spec.label,
                packet=controller.last_packet,
                attr_name=pill_spec.popup_attr or "",
                ordered_names=pill_spec.popup_names,
            )

        pill.chip.on("click", _show_popup)


def create_metrics_card(title: str, specs: list[MetricSpec]) -> MetricsCardController:
    pills: list[MetricPill] = []

    with ui.card().classes("w-full min-w-0").style("padding: 0.5rem;") as card:
        title_lbl = ui.label(title)
        title_lbl.classes("font-bold mb-2 egse-medium-text")
        _render_metric_grid(specs=specs, columns=5, pills=pills)

    controller = MetricsCardController(title=title, pills=pills, card=card)
    controller.set_no_data()
    return controller


def create_default_eb_metrics_card() -> MetricsCardController:
    specs = _eb_hk_specs()
    spec_map = {spec.key: spec for spec in specs}
    pills: list[MetricPill] = []
    with ui.card().classes("w-full min-w-0").style("padding: 0.5rem;") as card:
        eb_lbl = ui.label("EB STATUS")
        eb_lbl.classes("font-bold mb-2 egse-medium-text")

        _render_metric_grid(
            specs=[
                spec_map[k]
                for k in ("eb_operating_state", "eb_has_errors", "eb_has_warnings", "eb_fdir_alarm", "eb_fdir_warning")
            ],
            columns=5,
            pills=pills,
        )
        _render_metric_grid(
            specs=[
                spec_map[k]
                for k in ("eb_12v", "eb_neg12v", "eb_5v", "eb_3v3", "eb_mcu_temp", "eb_internal_temp", "eb_psu_temp")
            ],
            columns=7,
            pills=pills,
        )
        # _render_metric_grid(
        #     specs=[spec_map[k] for k in ("eb_mcu_temp", "eb_internal_temp", "eb_psu_temp")],
        #     columns=3,
        #     pills=pills,
        # )

        ui.space()
        tec_lbl = ui.label("TEC STATUS")
        tec_lbl.classes("font-bold mb-2 egse-medium-text")
        _render_metric_grid(
            specs=[spec_map[k] for k in ("setpoint", "drive_i", "temp", "dac", "eb_tec_at_setpoint")],
            columns=5,
            pills=pills,
        )

    controller = MetricsCardController(title="EB STATUS", pills=pills, card=card)
    controller.set_no_data()
    _bind_metric_popups(controller)
    return controller


def create_default_ob_metrics_card(state: dict[str, Any] | None = None) -> MetricsCardController:
    specs = _ob_hk_specs(state)
    spec_map = {spec.key: spec for spec in specs}
    pills: list[MetricPill] = []

    with ui.card().classes("w-full min-w-0").style("padding: 0.5rem;") as card:
        with ui.row().classes("w-full gap-4 items-start min-w-0 flex-nowrap"):
            with ui.column().classes("flex-1 min-w-0 gap-1"):
                ob_lbl = ui.label("OB STATUS")
                ob_lbl.classes("font-bold mb-2 egse-medium-text")
                _render_metric_grid(
                    specs=[
                        spec_map[k]
                        for k in (
                            "cmd_cnt",
                            "pwr_stat",
                            "3v3",
                            "1v5",
                            "dig",
                            "det",
                            "mech",
                            "mtr",
                            "ob_errors",
                            "mtr_errors",
                        )
                    ],
                    columns=10,
                    pills=pills,
                )

        with ui.row().classes("w-full items-start gap-4").style("flex-wrap: nowrap;"):
            with ui.column().classes("min-w-0 gap-1").style("flex: 1 1 0%;"):
                motor_lbl = ui.label("MECH STATUS")
                motor_lbl.classes("font-bold mb-2 egse-medium-text")
                _render_metric_grid(
                    specs=[
                        spec_map[k]
                        for k in (
                            "ob_motor_moving",
                            "ob_direction",
                            "ob_stop",
                            "ob_steps",
                            "mtr_rel_steps",
                            "mtr_params",
                            "mech_pwr",
                            "hk_mech_cur",
                            "mech_htr_status",
                        )
                    ],
                    columns=9,
                    pills=pills,
                )

            with ui.column().classes("gap-1").style("flex: 0 0 18rem;"):
                det_lbl = ui.label("DET STATUS")
                det_lbl.classes("font-bold mb-2 egse-medium-text")
                _render_metric_grid(
                    specs=[spec_map[k] for k in ("det_pwr", "det_htr_status")],
                    columns=2,
                    pills=pills,
                )

    controller = MetricsCardController(title="OB STATUS", pills=pills, card=card)
    controller.set_no_data()
    _bind_metric_popups(controller)
    return controller


def create_packet_metrics_card(state: dict[str, Any]) -> PacketMetricsCardController:
    cards: dict[str, Any] = {}
    chips: dict[str, Any] = {}

    with ui.card().classes("w-full min-w-0").style("padding: 0.5rem;"):
        ui.label("PACKET METRICS").classes("font-bold mb-2 egse-medium-text")
        with ui.row().classes("w-full gap-2"):
            fields = [
                ("tc_rejected", "TCs RJCTD"),
                ("hk_time", "Time Since Last HK"),
                ("hk_packets", "HK"),
                ("post_packets", "POST"),
                ("sci_packets", "SCI"),
            ]
            for key, label in fields:
                with ui.column().classes("flex-1 min-w-0 items-center gap-1") as card_col:
                    ui.label(label).classes("text-center egse-metric-label")
                    chips[key] = (
                        ui.chip("---", color="grey")
                        .props("dense")
                        .classes("w-full egse-metric-value")
                        .style("justify-content: center;")
                    )
                cards[key] = card_col

    controller = PacketMetricsCardController(state=state, cards=cards, chips=chips)
    controller.set_no_data()
    controller.set_mode(state.get("mode", "EB"))
    return controller


def _status_mask_set(
    packet: Any,
    field_name: str | tuple[str, ...],
    mask: int,
) -> bool | None:
    names = (field_name,) if isinstance(field_name, str) else field_name
    for name in names:
        value = getattr(packet, name, None)
        if value is not None:
            try:
                return bool(int(value) & mask)
            except (TypeError, ValueError):
                continue
    return None
