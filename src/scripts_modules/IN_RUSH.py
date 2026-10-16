from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from core_modules import config, constants as const
from tek_scope_api import TekScope
from utility_modules import eb_interface, ebtcs
from utility_modules import eb_packet_utility as ebpu
from utility_modules.background_checks import set_psu_channel_voltage, switch_psu
from widget_modules import ui_runtime_controller

info_log = logging.getLogger("info_log")

DEFAULT_SCOPE_SETUP_FILE = Path(__file__).resolve().parents[2] / "rsrc" / "Scope Config" / "INRUSH.set"
PSU_ZERO_TOLERANCE_A = 0.01
PSU_ZERO_STABLE_SAMPLES = 3
PSU_ZERO_TIMEOUT_S = 15.0
SAFE_CURRENT_TIMEOUT_S = 120.0
PSU_VOLTAGE_TOLERANCE_V = 0.1
PSU_VOLTAGE_TIMEOUT_S = 5.0
TCS_COMMAND_SETTLE_S = 0.2
INRUSH_CAPTURE_PROFILES = (("4 ms/div", 0.004), ("20 us/div", 0.00002))
HEATER_CONFIGS = (0x00, 0x08A3, 0x0881, 0x08A3, 0x0881)
TEC_SETPOINT_MINUS_35C = 0xC018
HE_ACQ_CONFIG = (0x00, 0x00, 0x0000, 0x0000, 0x0000, 0x00, 0x00AE, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x02)
_CONTINUED_AFTER_FAILURE = object()


def _run_state_check(
	label: str, check: Callable[..., Any], *args: Any, **kwargs: Any
) -> None:
	errors: list[str] = []
	readings: dict[str, Any] = {}
	try:
		result = check(*args, readings=readings, errors_out=errors, **kwargs)
		if isinstance(result, tuple) and len(result) == 2:
			message, passed = result
			if not passed and not errors:
				errors.append(str(message))
	except ui_runtime_controller.ScriptAbortRequested:
		raise
	except Exception as exc:
		if ui_runtime_controller.is_aborted():
			raise
		errors.append(str(exc))

	if errors:
		if ui_runtime_controller.handle_script_check_failure(label, errors, readings):
			info_log.warning("Operator chose to continue after In-rush check failure: %s: %s", label, errors)
			return
		raise AssertionError(f"{label} failed: {'; '.join(errors)}")
	ui_runtime_controller.request_force_pause(label, errors=errors, readings=readings)


def _run_checked(label: str, action: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
	"""Ask whether to continue when an In-rush action raises, matching the FFT flow."""
	try:
		return action(*args, **kwargs)
	except ui_runtime_controller.ScriptAbortRequested:
		raise
	except Exception as exc:
		if ui_runtime_controller.handle_script_check_failure(label, [str(exc)]):
			info_log.warning("Operator chose to continue after In-rush failure: %s: %s", label, exc)
			return _CONTINUED_AFTER_FAILURE
		raise


def _scope_arm_checkpoint(
	scope: TekScope, label: str, table: str, trigger_level_ma: float | None = None
) -> None:
	if trigger_level_ma is not None:
		actual_level = scope.set_trigger_level_current_ma(trigger_level_ma)
		ui_runtime_controller.notify(
			f"Trigger level for {label}: {trigger_level_ma:g} mA ({actual_level:g} scope units)",
			color="primary",
		)
	scope.arm_single()
	ui_runtime_controller.notify(
		f"Oscilloscope armed for {label} ({table})", color="positive"
	)


def _scope_save_checkpoint(
	scope: TekScope, label: str, table: str, record: str, channels: list[str]
) -> Path:
	scope.wait_for_capture(timeout_s=120.0)
	output_path = scope.save_waveforms(Path(const.LOG_PATH) / "scope_captures", label, channels)
	ui_runtime_controller.notify(f"Scope waveform saved: {output_path}", color="positive")
	ui_runtime_controller.request_force_pause(
		f"Record capture results - {label}",
		readings={
			"Table": table,
			"Record": record,
			"Trace label": label,
			"Waveform CSV": str(output_path),
		},
	)
	return output_path


def _request_hk_after_command(interface: Any) -> Any:
	time.sleep(TCS_COMMAND_SETTLE_S)
	return ebtcs.hk_request(interface, 0)


def _scoped_transition(
	scope: TekScope,
	interface: Any,
	label: str,
	table: str,
	record: str,
	trigger_level_ma: float | None,
	channels: list[str],
	command: Callable[[], Any],
	check: Callable[..., Any],
	*check_args: Any,
	scope_settings: str,
	scope_timebase_s: float | None,
	scope_current_scale_ma_per_div: float | None = None,
	repeat_reset: Callable[[], Any] | None = None,
	after_capture: Callable[[], Any] | None = None,
	**check_kwargs: Any,
) -> None:
	while True:
		config_result = _run_checked(
			f"{label} - Configure oscilloscope axes",
			scope.configure_mode_transition_capture,
			timebase_s=scope_timebase_s,
			current_scale_ma_per_div=scope_current_scale_ma_per_div,
		)
		if config_result is _CONTINUED_AFTER_FAILURE:
			raise RuntimeError(f"{label}: cannot proceed without the requested oscilloscope axis settings")
		arm_result = _run_checked(
			f"{label} - Arm oscilloscope",
			_scope_arm_checkpoint,
			scope,
			label,
			table,
			trigger_level_ma,
		)
		if arm_result is _CONTINUED_AFTER_FAILURE:
			raise RuntimeError(f"{label}: cannot proceed without an armed oscilloscope")
		ui_runtime_controller.request_force_pause(
			f"Confirm oscilloscope settings - {label}",
			readings={
				"Action": (
					"The oscilloscope is armed. Confirm the probe scaling, trigger source and level, "
					"timebase, and enabled channels are correct. Use the recommended settings below. "
					"Resume to issue the TCS transition command."
				),
				"Recommended scope settings": scope_settings,
				"Enabled scope channels": channels,
			},
		)
		result = _run_checked(f"{label} - command", command)
		if result == "ERROR":
			error = RuntimeError(f"Command failed during {label}")
			if not ui_runtime_controller.handle_script_check_failure(f"{label} - command", [str(error)]):
				raise error
			info_log.warning("Operator chose to continue after In-rush command failure: %s", error)
		_run_checked(
			f"{label} - Save scope waveform",
			_scope_save_checkpoint,
			scope,
			label,
			table,
			record,
			channels,
		)
		_run_checked(f"{label} - Request HK", _request_hk_after_command, interface)
		_run_state_check(f"{label} - HK/PSU check", check, *check_args, **check_kwargs)
		if after_capture is not None:
			_run_checked(f"{label} - post-capture action", after_capture)
		if not ui_runtime_controller.request_repeat_acquisition(label):
			return
		if repeat_reset is not None:
			_run_checked(f"{label} - repeat reset", repeat_reset)


def _wait_for_psu_zero(label: str) -> None:
	sequence = ebpu.get_psu_sequence()
	deadline = time.monotonic() + PSU_ZERO_TIMEOUT_S
	zero_samples = 0
	last_current: float | None = None
	while time.monotonic() < deadline:
		if ui_runtime_controller.is_aborted():
			raise ui_runtime_controller.ScriptAbortRequested
		sample = ebpu.wait_for_fresh_psu(
			timeout=min(1.0, max(0.0, deadline - time.monotonic())),
			after_sequence=sequence,
		)
		if sample is None:
			continue
		sequence = ebpu.get_psu_sequence()
		current = sample.get("PSU_EB_I") if isinstance(sample, dict) else None
		if current is None and isinstance(sample, dict):
			current = sample.get("CH4_I")
		if current is None:
			zero_samples = 0
			continue
		last_current = float(current)
		zero_samples = zero_samples + 1 if abs(last_current) <= PSU_ZERO_TOLERANCE_A else 0
		if zero_samples >= PSU_ZERO_STABLE_SAMPLES:
			info_log.info("%s: PSU CH4 current settled at %.3f A", label, last_current)
			return
	raise TimeoutError(
		f"{label}: PSU CH4 current did not settle below {PSU_ZERO_TOLERANCE_A:.2f} A "
		f"within {PSU_ZERO_TIMEOUT_S:.0f} seconds (last reading: {last_current!r} A)"
	)


def _set_inrush_psu_voltage(psu_port: Any, psu_lock: Any, voltage: float, label: str) -> None:
	set_psu_channel_voltage(psu_port, channel=4, voltage=voltage, psu_lock=psu_lock)


def _verify_inrush_psu_voltage(voltage: float, label: str, *, after_sequence: int) -> float:
	deadline = time.monotonic() + PSU_VOLTAGE_TIMEOUT_S
	last_status: Any = None
	last_voltage: float | None = None
	while time.monotonic() < deadline:
		if ui_runtime_controller.is_aborted():
			raise ui_runtime_controller.ScriptAbortRequested
		sample = ebpu.wait_for_fresh_psu(
			timeout=min(1.0, max(0.0, deadline - time.monotonic())),
			after_sequence=after_sequence,
		)
		if sample is None:
			continue
		after_sequence = ebpu.get_psu_sequence()
		if not isinstance(sample, dict):
			continue
		last_status = sample.get("CH4_STATUS", sample.get("STATUS"))
		actual = sample.get("PSU_EB_V")
		if actual is None:
			actual = sample.get("CH4_V")
		try:
			last_voltage = float(actual) if actual is not None else None
		except (TypeError, ValueError):
			last_voltage = None
		channel_on = last_status in (1, True, "1", "ON", "on")
		if (
			channel_on
			and last_voltage is not None
			and abs(last_voltage - voltage) <= PSU_VOLTAGE_TOLERANCE_V
		):
			info_log.info("%s: PSU CH4 voltage set and verified at %.2f V", label, last_voltage)
			return last_voltage
	raise AssertionError(
		f"{label}: PSU CH4 did not report ON at {voltage:.2f} V within {PSU_VOLTAGE_TIMEOUT_S:.0f} seconds "
		f"(last status: {last_status!r}, voltage: {last_voltage!r} V)"
	)


def _wait_for_safe_current_and_send_ret(
	interface: Any, label: str, *, after_sequence: int
) -> None:
	deadline = time.monotonic() + SAFE_CURRENT_TIMEOUT_S
	last_current_ma: float | None = None
	while time.monotonic() < deadline:
		if ui_runtime_controller.is_aborted():
			raise ui_runtime_controller.ScriptAbortRequested
		sample = ebpu.wait_for_fresh_psu(
			timeout=min(1.0, max(0.0, deadline - time.monotonic())),
			after_sequence=after_sequence,
		)
		if sample is None:
			continue
		after_sequence = ebpu.get_psu_sequence()
		if not isinstance(sample, dict):
			continue

		errors: list[str] = []
		last_current_ma = ui_runtime_controller.consumption_check("State1", sample, errors)
		if errors:
			if all(error.startswith("PSU_EB_I out of range for") for error in errors):
				continue
			raise AssertionError(
				f"{label}: could not evaluate SAFE expected current: {'; '.join(errors)}"
			)
		if last_current_ma is None:
			raise AssertionError(f"{label}: SAFE expected current check returned no current reading")

		info_log.info("%s: PSU current reached SAFE expected value (%.2f mA)", label, last_current_ma)
		ui_runtime_controller.notify(
			f"{label}: PSU current reached SAFE expected value ({last_current_ma:.2f} mA); sending RET.",
			color="positive",
		)
		_run_checked(
			f"{label} - Send SAFE RET",
			ebtcs.ret,
			interface,
			0,
			0,
			0,
			0,
			0,
			0,
		)
		return
	raise TimeoutError(
		f"{label}: PSU current did not reach the SAFE expected range within "
		f"{SAFE_CURRENT_TIMEOUT_S:.0f} seconds (last reading: {last_current_ma!r} mA)"
	)


def _capture_inrush_voltage_point(
	scope: TekScope,
	interface: Any,
	psu_port: Any,
	psu_lock: Any,
	channels: list[str],
	voltage: float,
	label: str,
	*,
	leave_psu_on: bool = False,
) -> None:
	table = "Table 13-57"
	psu_is_off = False
	capture_index = 0
	while True:
		profile_name, timebase_s = INRUSH_CAPTURE_PROFILES[capture_index]
		capture_label = f"{label} - {profile_name}"
		if not psu_is_off:
			_run_checked(
				f"{label} - PSU CH4 off",
				switch_psu,
				psu_port,
				enabled=False,
				psu_lock=psu_lock,
				channel=4,
			)
			ui_runtime_controller.notify(f"{label}: PSU CH4 switched off", color="warning")
			_run_checked(f"{label} - PSU CH4 current settle", _wait_for_psu_zero, label)
		_run_checked(f"{label} - Set PSU voltage", _set_inrush_psu_voltage, psu_port, psu_lock, voltage, label)
		config_result = _run_checked(
			f"{capture_label} - Configure oscilloscope",
			scope.configure_inrush_capture,
			timebase_s,
		)
		if config_result is _CONTINUED_AFTER_FAILURE:
			raise RuntimeError(f"{capture_label}: cannot proceed without the requested oscilloscope settings")
		arm_result = _run_checked(
			f"{label} - Arm oscilloscope",
			_scope_arm_checkpoint,
			scope,
			capture_label,
			f"{table} - {profile_name}",
			400.0,
		)
		if arm_result is _CONTINUED_AFTER_FAILURE:
			raise RuntimeError(f"{capture_label}: cannot proceed without an armed oscilloscope")
		ui_runtime_controller.request_force_pause(
			f"Power on PSU CH4 - {label}",
			readings={
				"PSU target (V)": voltage,
				"Current scale": "2 A/div",
				"Timebase": profile_name,
				"Trigger": "CH1 rising edge at 400 mA",
				"Action": "Confirm the test setup is ready. Resume to switch PSU CH4 on and capture the in-rush transient; voltage will be checked after power-on.",
			},
		)
		try:
			psu_sequence = ebpu.get_psu_sequence()
			_run_checked(
				f"{label} - PSU CH4 on",
				switch_psu,
				psu_port,
				enabled=True,
				psu_lock=psu_lock,
				channel=4,
			)
			actual_voltage = _run_checked(
				f"{label} - Verify PSU voltage",
				_verify_inrush_psu_voltage,
				voltage,
				label,
				after_sequence=psu_sequence,
			)
			if actual_voltage is _CONTINUED_AFTER_FAILURE:
				repeat = False
				continue_capture = False
			else:
				continue_capture = True
			if actual_voltage is None:
				actual_voltage = voltage
			if continue_capture:
				ui_runtime_controller.notify(
					f"{label}: PSU CH4 voltage verified at {actual_voltage:.2f} V",
					color="primary",
				)
				psu_sequence = ebpu.get_psu_sequence()
				ui_runtime_controller.notify(
					f"{label}: Close the bounce-free switch now. SAFE-current monitoring is active; "
					"RET will be sent automatically when the expected current is reached.",
					color="warning",
				)
				_run_checked(
					f"{label} - Wait for SAFE current and send RET",
					_wait_for_safe_current_and_send_ret,
					interface,
					label,
					after_sequence=psu_sequence,
				)
				capture_complete = _run_checked(
					f"{capture_label} - Wait for scope capture", scope.wait_for_capture, timeout_s=120.0
				)
				if capture_complete is _CONTINUED_AFTER_FAILURE:
					repeat = False
				else:
					output_path = _run_checked(
						f"{capture_label} - Save scope waveform",
						scope.save_waveforms,
						Path(const.LOG_PATH) / "scope_captures",
						capture_label,
						channels,
					)
					if isinstance(output_path, Path):
						ui_runtime_controller.notify(f"Scope waveform saved: {output_path}", color="positive")
					_run_checked(f"{label} - Request HK", _request_hk_after_command, interface)
					_run_state_check(f"{label} - SAFE RET and POST check", ui_runtime_controller.verify_safe_ret)
					if capture_index + 1 < len(INRUSH_CAPTURE_PROFILES):
						capture_index += 1
						repeat = True
					else:
						repeat = ui_runtime_controller.request_repeat_acquisition(label)
						if repeat:
							capture_index = 0
		except ui_runtime_controller.ScriptAbortRequested:
			raise
		except Exception as exc:
			if ui_runtime_controller.handle_script_check_failure(f"{label} - In-rush step", [str(exc)]):
				info_log.warning("Operator chose to continue after In-rush failure: %s: %s", label, exc)
				repeat = False
			else:
				raise
		if repeat or not leave_psu_on:
			_run_checked(
				f"{label} - PSU CH4 off",
				switch_psu,
				psu_port,
				enabled=False,
				psu_lock=psu_lock,
				channel=4,
			)
			_run_checked(f"{label} - PSU CH4 current settle", _wait_for_psu_zero, label)
			psu_is_off = True
		if not repeat:
			break
	if leave_psu_on:
		ui_runtime_controller.notify(
			"PSU CH4 remains on at nominal +28.0 V for the mode-transition captures.",
			color="positive",
		)


def _verify_heater_transition(
	label: str,
	heater: str,
	*,
	readings: dict[str, Any] | None = None,
	errors_out: list[str] | None = None,
) -> tuple[str, bool]:
	hk_sequence = ebpu.get_hk_sequence()
	psu_sequence = ebpu.get_psu_sequence()
	_request_hk_after_command(eb_interface.get_egse_interface())
	hk = ebpu.wait_for_fresh_hk(timeout=5.0, after_sequence=hk_sequence)
	fresh_psu = ebpu.wait_for_fresh_psu(timeout=3.0, after_sequence=psu_sequence)
	sample = ebpu.get_smoothed_latest_psu(window_samples=5) if fresh_psu is not None else None
	errors: list[str] = []
	measured: dict[str, Any] = {}

	if hk is None:
		errors.append("No fresh HK packet after heater command")
	if not isinstance(sample, dict):
		errors.append("No fresh PSU sample after heater command")
	else:
		measured["PSU_EB_I (mA)"] = float(sample.get("PSU_EB_I") or 0.0) * 1000.0

	thrm = getattr(hk, "THRM_STATUS", None) if hk is not None else None
	if thrm is None:
		errors.append("THRM_STATUS is unavailable")
	else:
		mode_fields = ("MM", "MA") if heater == "mechanism" else ("DM", "DA")
		if not any(bool(getattr(thrm, field, 0)) for field in mode_fields):
			errors.append(f"{heater.title()} heater command is not reflected in THRM_STATUS")
		measured[f"{heater.title()} heater status"] = {
			"HMS": getattr(thrm, "HMS", None),
			"HDS": getattr(thrm, "HDS", None),
		}

	if hk is not None and isinstance(sample, dict):
		ui_runtime_controller.consumption_check(["Standby"], sample, errors, hk)

	if readings is not None:
		readings.update(measured)
	if errors_out is not None:
		errors_out.extend(errors)
	message = f"{label}: {measured}"
	return message, not errors


def _verify_board_flag(
	label: str,
	field: str,
	*,
	readings: dict[str, Any] | None = None,
	errors_out: list[str] | None = None,
) -> tuple[str, bool]:
	hk_sequence = ebpu.get_hk_sequence()
	psu_sequence = ebpu.get_psu_sequence()
	interface = eb_interface.get_egse_interface()
	_request_hk_after_command(interface)
	hk = ebpu.wait_for_fresh_hk(timeout=5.0, after_sequence=hk_sequence)
	fresh_psu = ebpu.wait_for_fresh_psu(timeout=3.0, after_sequence=psu_sequence)
	sample = ebpu.get_smoothed_latest_psu(window_samples=5) if fresh_psu is not None else None
	errors: list[str] = []
	measured: dict[str, Any] = {}
	if hk is None:
		errors.append("No fresh HK packet after board command")
	if not isinstance(sample, dict):
		errors.append("No fresh PSU sample after board command")
	else:
		measured["PSU_EB_I (mA)"] = float(sample.get("PSU_EB_I") or 0.0) * 1000.0
	flags = getattr(hk, "INSTR_STATUS_FLAGS", None) if hk is not None else None
	enabled = bool(getattr(flags, field, 0)) if flags is not None else False
	measured[field] = enabled
	if not enabled:
		errors.append(f"{field} is not set in fresh HK")
	if readings is not None:
		readings.update(measured)
	if errors_out is not None:
		errors_out.extend(errors)
	message = f"{label}: {measured}"
	return message, not errors


def _verify_tec_setpoint(interface: Any) -> None:
	sequence = ebpu.get_hk_sequence()
	ebtcs.set_tec_setpoint(interface, 0x00, TEC_SETPOINT_MINUS_35C)
	_request_hk_after_command(interface)
	hk = ebpu.wait_for_fresh_hk(timeout=5.0, after_sequence=sequence)
	actual = getattr(hk, "TEC_SETPOINT", None) if hk is not None else None
	errors = [] if actual == TEC_SETPOINT_MINUS_35C else [
		f"TEC_SETPOINT is {actual!r}; expected 0x{TEC_SETPOINT_MINUS_35C:04X}"
	]
	ui_runtime_controller.request_force_pause(
		"HE acquisition - verify -35 C TEC setpoint",
		errors=errors,
		readings={"TEC_SETPOINT": actual},
	)
	if errors:
		raise AssertionError(errors[0])


def _check_acquisition_completion(
	*, readings: dict[str, Any] | None = None, errors_out: list[str] | None = None
) -> tuple[str, bool]:
	errors: list[str] = []
	measured: dict[str, Any] = {}
	try:
		ui_runtime_controller.perform_acq_check_sync(readings=measured, errors_out=errors)
	except ui_runtime_controller.ScriptAbortRequested:
		raise
	except Exception as exc:
		if ui_runtime_controller.is_aborted():
			raise
		errors.append(str(exc))
	if readings is not None:
		readings.update(measured)
	if errors_out is not None:
		errors_out.extend(errors)
	return "HE acquisition completed", not errors


def _configure_heaters(interface: Any) -> None:
	ebtcs.set_heater_configs(interface, *HEATER_CONFIGS)


def _reset_to_safe_for_repeat(interface: Any) -> None:
	ebtcs.safe(interface, 0x00)
	_request_hk_after_command(interface)
	_run_state_check("Safe baseline before repeating Standby", ui_runtime_controller.verify_safe_ret)


def _disable_heater_for_repeat(interface: Any, heater: str) -> None:	
	if heater == "mechanism":
		ebtcs.en_mech_heater(interface, 0x00)
	else:
		ebtcs.en_det_heater(interface, 0x00)
	_request_hk_after_command(interface)


def _disable_board_for_repeat(interface: Any, board: str) -> None:
	if board == "mechanism":
		ebtcs.en_mech_board(interface, 0x00)
	else:
		ebtcs.en_det_board(interface, 0x00)
	_request_hk_after_command(interface)


def _reset_tec_for_repeat(interface: Any) -> None:
	ebtcs.set_tec_current(interface, 0x00, 0x000)
	_request_hk_after_command(interface)
	_run_state_check("State3 baseline before repeating TEC current", ui_runtime_controller.verify_power_state, "State3")


def _execute_inrush_sequence(
	interface: Any,
	scope: TekScope,
	psu_port: Any,
	psu_lock: Any,
	scope_channels: list[str],
	tec_setpoint: int,
	tec_expected_a: float,
	tec_tolerance_a: float,
) -> None:
	ui_runtime_controller.request_force_pause(
		"Inrush test preflight",
		readings={
			"Connections": "PSU -> LISN -> bounce-free switch -> Enfys +28 V; EGSE laptop at J01.",
			"Probes": "Differential voltage probe across +28 V/+28 V_RTN between PSU and LISN; current probe after switch near Enfys.",
			"Scope": "Confirm the recalled setup has correct probe scaling, rising current trigger, threshold, and timebase.",
			"Enabled scope channels": scope_channels,
			"Record": "Photograph setup; follow approved reset/safety procedures; numeric scope settings are not mandated by TPR-0030.",
		},
	)

	_capture_inrush_voltage_point(
		scope,
		interface,
		psu_port,
		psu_lock,
		scope_channels,
		28.0,
		"+28.0 V Off to Safe",
		leave_psu_on=True,
	)

	# Safe -> Standby
	hk_sequence = ebpu.get_hk_sequence()
	psu_sequence = ebpu.get_psu_sequence()
	_scoped_transition(
		scope,
		interface,
		"Safe to Standby",
		"Table 13-58",
		"Peak current; save and label the voltage/current trace",
		97.0,
		scope_channels,
		lambda: ebtcs.standby(interface, 5, 1),
		ui_runtime_controller.verify_standby_ret,
		scope_settings="Current scale: 50 mA/div; timebase: 400 us/div.",
		scope_timebase_s=0.0004,
		scope_current_scale_ma_per_div=50.0,
		repeat_reset=lambda: _reset_to_safe_for_repeat(interface),
		after_hk_sequence=hk_sequence,
		after_psu_sequence=psu_sequence,
	)
	ebtcs.set_hk_rate(interface, 0, 1)

	# Mechanism heater then detector heater.
	_configure_heaters(interface)
	_scoped_transition(
		scope,
		interface,
		"Mechanism heater ON",
		"Table 13-58",
		"Peak current; save and label the voltage/current trace",
		135.0,
		scope_channels,
		lambda: ebtcs.en_mech_heater(interface, 0x01),
		lambda *, readings, errors_out: _verify_heater_transition(
			"Mechanism heater ON", "mechanism", readings=readings, errors_out=errors_out
		),
		scope_settings="Current scale: 50 mA/div; timebase: 1 ms/div.",
		scope_timebase_s=0.001,
		scope_current_scale_ma_per_div=50.0,
		repeat_reset=lambda: _disable_heater_for_repeat(interface, "mechanism"),
	)
	_scoped_transition(
		scope,
		interface,
		"Detector heater ON",
		"Table 13-58",
		"Peak current; save and label the voltage/current trace",
		170.0,
		scope_channels,
		lambda: ebtcs.en_det_heater(interface, 0x01),
		lambda *, readings, errors_out: _verify_heater_transition(
			"Detector heater ON", "detector", readings=readings, errors_out=errors_out
		),
		scope_settings="Current scale: 50 mA/div; timebase: 1 ms/div.",
		scope_timebase_s=0.001,
		scope_current_scale_ma_per_div=50.0,
		repeat_reset=lambda: _disable_heater_for_repeat(interface, "detector"),
	)

	# Warm and power the boards.
	_scoped_transition(
		scope,
		interface,
		"Mechanism board ON",
		"Table 13-58",
		"Peak current; save and label the voltage/current trace",
		190.0,
		scope_channels,
		lambda: (
			ebtcs.generic_tc(interface, 0x00, 0x06, 0x64, 0xC4, 0x01, *([0x00] * 12)),
			ebtcs.en_mech_board(interface, 0x01),
		),
		lambda *, readings, errors_out: _verify_board_flag(
			"Mechanism board ON",
			"OB_MECHANISM_BOARD_ENABLED",
			readings=readings,
			errors_out=errors_out,
		),
		scope_settings="Timebase: 1 ms/div.",
		scope_timebase_s=0.001,
		repeat_reset=lambda: _disable_board_for_repeat(interface, "mechanism"),
	)
	_scoped_transition(
		scope,
		interface,
		"Detector board ON",
		"Table 13-58",
		"Peak current; save and label the voltage/current trace",
		200.0,
		scope_channels,
		lambda: ebtcs.en_det_board(interface, 0x01),
		ui_runtime_controller.verify_power_state,
		"State3",
		scope_settings="Timebase: 400 us/div.",
		scope_timebase_s=0.0004,
		repeat_reset=lambda: _disable_board_for_repeat(interface, "detector"),
	)

	# Apply the repo-configured approved TEC current rather than assuming full-scale is safe.
	ui_runtime_controller.notify(
		f"TEC current command: {tec_setpoint} ADU; expected {tec_expected_a:.3f} A +/- {tec_tolerance_a:.3f} A. Confirm this is the approved 100% test limit.",
		color="warning",
	)
	ui_runtime_controller.request_force_pause(
		"Confirm approved TEC 100% current limit",
		readings={
			"Command (ADU)": tec_setpoint,
			"Expected current (A)": tec_expected_a,
			"Tolerance (A)": tec_tolerance_a,
		},
	)
	_scoped_transition(
		scope,
		interface,
		"TEC current at configured 100% limit",
		"Table 13-58",
		"Peak current; save and label the voltage/current trace",
		200.0,
		scope_channels,
		lambda: ebtcs.set_tec_current(interface, 0x00, tec_setpoint),
		ui_runtime_controller.verify_power_state,
		"State4",
		scope_settings="Timebase: 4 ms/div.",
		scope_timebase_s=0.004,
		repeat_reset=lambda: _reset_tec_for_repeat(interface),
	)

	# Motor start and homing.
	ebtcs.set_motor_configs(interface, 0, 0x40, 0x00, 0x08, 0x00, 0x3C, 0x00, 0x00, 0x0000, 0x00)
	_scoped_transition(
		scope,
		interface,
		"Motor start / homing",
		"Table 13-58",
		"Peak current; save and label the voltage/current trace",
		370.0,
		scope_channels,
		lambda: ebtcs.ob_homing(interface, 0x01),
		ui_runtime_controller.verify_power_state,
		"State7",
		scope_settings="Current scale: 100 mA/div.",
		scope_timebase_s=None,
		scope_current_scale_ma_per_div=100.0,
		after_capture=lambda: _complete_homing_for_repeat(interface),
	)

	# Match the draft: release manual TEC current before closed-loop temperature control.
	ebtcs.set_tec_current(interface, 0x00, 0x000)
	_verify_tec_setpoint(interface)
	ebtcs.set_acq_configs(interface, *HE_ACQ_CONFIG)

	_scoped_transition(
		scope,
		interface,
		"HE acquisition start",
		"Table 13-58",
		"Peak current; save and label the voltage/current trace",
		245.0,
		scope_channels,
		lambda: ebtcs.acquisition(interface, 0x00),
		_check_acquisition_completion,
		scope_settings="Timebase: 4 ms/div.",
		scope_timebase_s=0.004,
	)

	ebtcs.safe(interface, 0x00)
	_request_hk_after_command(interface)
	_run_state_check("Return to Safe", ui_runtime_controller.verify_safe_ret)
	_capture_inrush_voltage_point(
		scope, interface, psu_port, psu_lock, scope_channels, 26.0, "+26.0 V Off to Safe"
	)
	_capture_inrush_voltage_point(
		scope, interface, psu_port, psu_lock, scope_channels, 29.4, "+29.4 V Off to Safe"
	)
	ui_runtime_controller.request_force_pause(
		"Final trace and table review",
		readings={"Action": "Confirm every saved scope trace is labelled and Tables 13-57 and 13-58 are complete."},
	)
	ui_runtime_controller.notify_script_done()


def _complete_homing_for_repeat(interface: Any) -> None:
	ui_runtime_controller.notify("Motor homing is in progress; waiting for completion.", color="primary")
	ui_runtime_controller.perform_homing_check_sync()
	_run_state_check("Homing complete - steady state", ui_runtime_controller.verify_power_state, "State4")


def run_inrush_mode_transition(
	scope_setup_file: str | Path | None = None,
	psu_port: Any = None,
	psu_lock: Any = None,
) -> None:
	"""Run TPR-0030 section 13.16 with operator-controlled failure handling."""
	try:
		_run_inrush_mode_transition(scope_setup_file, psu_port, psu_lock)
	except ui_runtime_controller.ScriptAbortRequested:
		raise
	except Exception as exc:
		if ui_runtime_controller.handle_script_check_failure("In-rush test", [str(exc)]):
			info_log.warning("Operator chose to continue after In-rush test failure: %s", exc)
			ui_runtime_controller.notify_negative(
				f"In-rush could not continue after the failure: {exc}"
			)
			return
		raise


def _run_inrush_mode_transition(
	scope_setup_file: str | Path | None,
	psu_port: Any,
	psu_lock: Any,
) -> None:
	"""Run the In-rush sequence after validating its inputs."""
	if psu_port is None:
		raise RuntimeError("In-rush test requires an available PSU connection")
	if scope_setup_file is None or not str(scope_setup_file).strip():
		scope_setup_file = DEFAULT_SCOPE_SETUP_FILE
	scope_setup_path = Path(scope_setup_file)
	if not scope_setup_path.is_file():
		raise FileNotFoundError(f"In-rush scope setup file not found: {scope_setup_path}")
	tec_setpoint = int(config.TEC_CURRENT_SETPOINT_ADU)
	tec_expected_a = float(config.TEC_EXPECTED_CURRENT_A)
	tec_tolerance_a = float(config.TEC_CURRENT_TOLERANCE_A)
	if not 0 <= tec_setpoint <= 0x0FFF or tec_expected_a < 0 or tec_tolerance_a < 0:
		raise ValueError("Configured TEC setpoint/current limits are invalid")

	interface = eb_interface.get_egse_interface()
	with TekScope(config.SCOPE_VISA_RESOURCE) as scope:
		scope.recall_setup(scope_setup_path)
		scope_setup = scope.configure_single_rising_trigger()
		info_log.info(
			"Inrush scope connected: %s; trigger=%s rising; channels=%s; setup=%s",
			scope.idn(),
			scope_setup["trigger_source"],
			scope_setup["active_channels"],
			scope_setup_path,
		)
		ui_runtime_controller.notify(
			f"Scope ready: rising-edge trigger on {scope_setup['trigger_source']}; enabled channels {scope_setup['active_channels']}",
			color="positive",
		)
		_execute_inrush_sequence(
			interface,
			scope,
			psu_port,
			psu_lock,
			scope_setup["active_channels"],
			tec_setpoint,
			tec_expected_a,
			tec_tolerance_a,
		)
