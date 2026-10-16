from __future__ import annotations

import logging
import time
from typing import Any, Callable

from utility_modules import eb_interface, ebtcs
from utility_modules import eb_packet_utility as ebpu
from utility_modules.background_checks import switch_psu
from widget_modules import ui_runtime_controller

info_log = logging.getLogger("info_log")

PSU_ZERO_TOLERANCE_A = 0.01
PSU_ZERO_TIMEOUT_S = 15.0
PSU_ZERO_STABLE_SAMPLES = 3
PSU_SAMPLE_TIMEOUT_S = 1.0
TEC_SETPOINT_MINUS_35C = 0xC018
FLIGHT_HEATER_CONFIGS = (0x00, 0x079A, 0x0738, 0x079A, 0x0738)
ACQUISITION_STATE = 0x08
FIXED_POINT_ACQUISITION_TIMEOUT_S = 600.0
FIXED_POINT_POWER_CUT_DELAY_S = 100.0


def _report_check_result(label: str, errors: list[str], readings: dict[str, Any]) -> None:
	if errors:
		if ui_runtime_controller.handle_script_check_failure(label, errors, readings):
			info_log.warning("Operator chose to continue after switch-off check failure: %s: %s", label, errors)
			return
		raise AssertionError(f"{label} failed: {'; '.join(errors)}")
	ui_runtime_controller.request_force_pause(label, errors=[], readings=readings)


def _run_state_check(
	label: str, check: Callable[..., tuple[str, bool] | None], *args: Any, **kwargs: Any
) -> None:
	"""Report readings and ask whether to continue if a required state check fails."""
	errors: list[str] = []
	readings: dict[str, Any] = {}
	try:
		result = check(*args, readings=readings, errors_out=errors, **kwargs)
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

	_report_check_result(label, errors, readings)


def _run_check(label: str, check: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
	try:
		return check(*args, **kwargs)
	except ui_runtime_controller.ScriptAbortRequested:
		raise
	except Exception as exc:
		if ui_runtime_controller.is_aborted():
			raise
		if ui_runtime_controller.handle_script_check_failure(label, [str(exc)]):
			info_log.warning("Operator chose to continue after switch-off check failure: %s: %s", label, exc)
			return None
		raise


def _verify_psu_off(psu_port: Any, psu_lock: Any, label: str) -> None:
	sequence = ebpu.get_psu_sequence()
	switch_psu(psu_port, enabled=False, psu_lock=psu_lock, channel=4)
	ui_runtime_controller.notify(f"{label}: PSU outputs switched off; checking current", color="warning")

	deadline = time.monotonic() + PSU_ZERO_TIMEOUT_S
	readings: dict[str, Any] = {}
	latest_currents: dict[str, float] = {}
	latest_sample_error: str | None = None
	consecutive_zero_samples = 0

	while time.monotonic() < deadline:
		if ui_runtime_controller.is_aborted():
			raise ui_runtime_controller.ScriptAbortRequested
		remaining = deadline - time.monotonic()
		sample = ebpu.wait_for_fresh_psu(
			timeout=min(PSU_SAMPLE_TIMEOUT_S, max(0.0, remaining)),
			after_sequence=sequence,
		)
		if sample is None:
			continue
		sequence = ebpu.get_psu_sequence()
		if not isinstance(sample, dict):
			latest_sample_error = "PSU sample was not a channel-current mapping"
			consecutive_zero_samples = 0
			continue

		current_keys = ("CH1_I", "CH2_I", "CH3_I", "CH4_I", "PSU_EB_I", "PSU_ROV_HTR_I")
		present_keys = [key for key in current_keys if key in sample and sample[key] is not None]
		if not present_keys:
			latest_sample_error = "PSU sample did not contain any channel current readings"
			consecutive_zero_samples = 0
			continue

		currents: dict[str, float] = {}
		sample_errors: list[str] = []
		for key in present_keys:
			try:
				current_a = float(sample[key])
			except (TypeError, ValueError):
				sample_errors.append(f"{key} is not a valid current reading: {sample[key]!r}")
				continue
			currents[key] = current_a

		if sample_errors or len(currents) != len(present_keys):
			latest_sample_error = "; ".join(sample_errors)
			consecutive_zero_samples = 0
			continue

		latest_currents = currents
		readings = {f"{key} (A)": current for key, current in currents.items()}
		if all(abs(current) <= PSU_ZERO_TOLERANCE_A for current in currents.values()):
			consecutive_zero_samples += 1
			if consecutive_zero_samples >= PSU_ZERO_STABLE_SAMPLES:
				break
		else:
			consecutive_zero_samples = 0

	errors: list[str] = []
	if consecutive_zero_samples < PSU_ZERO_STABLE_SAMPLES:
		if not latest_currents and latest_sample_error is None:
			errors.append("No fresh PSU sample received after switching outputs off")
		elif latest_sample_error is not None and not latest_currents:
			errors.append(latest_sample_error)
		else:
			errors.append(
				f"PSU current did not settle to 0 A within {PSU_ZERO_TIMEOUT_S:.0f} seconds "
				f"({consecutive_zero_samples}/{PSU_ZERO_STABLE_SAMPLES} consecutive zero samples)"
			)
			if latest_sample_error is not None:
				errors.append(latest_sample_error)
			for key, current_a in latest_currents.items():
				if abs(current_a) > PSU_ZERO_TOLERANCE_A:
					errors.append(f"{key} is {current_a:.3f} A; expected 0 A")

	_report_check_result(label, errors, readings)
	info_log.info("%s: PSU current verified at zero: %s", label, readings)


def _boot_to_safe(interface: Any, psu_port: Any, psu_lock: Any, label: str) -> None:
	post_sequence = ebpu.get_post_sequence()
	switch_psu(psu_port, enabled=True, psu_lock=psu_lock, channel=4)
	ebtcs.ret(interface, 0, 0, 0, 0, 0, 0)
	ebtcs.hk_request(interface, 0)
	ui_runtime_controller.notify(f"{label}: PSU outputs on; RET and HK request sent", color="primary")
	ui_runtime_controller.abortible_sleep_with_progress(2, f"{label}: PSU settling", update_interval=1)
	ui_runtime_controller.abortible_sleep_with_progress(2, f"{label}: POST packet settling", update_interval=1)
	ui_runtime_controller.request_force_pause("Pause for POST packet checks")
	_run_state_check(
		f"{label} - SAFE RET and POST",
		ui_runtime_controller.verify_safe_ret,
		after_post_sequence=post_sequence,
	)


def _enter_standby(interface: Any, label: str) -> None:
	hk_sequence = ebpu.get_hk_sequence()
	psu_sequence = ebpu.get_psu_sequence()
	ebtcs.standby(interface, 5, 1)
	ebtcs.ret(interface, 0, 0, 0, 0, 0, 0)
	ebtcs.hk_request(interface, 0)
	ebtcs.set_hk_rate(interface, 0, 1)
	ui_runtime_controller.abortible_sleep_with_progress(5, f"{label}: STANDBY settling", update_interval=1)
	_run_state_check(
		f"{label} - STANDBY RET and regular packet checks",
		ui_runtime_controller.verify_standby_ret,
		after_hk_sequence=hk_sequence,
		after_psu_sequence=psu_sequence,
	)


def _verify_tec_setpoint(interface: Any, label: str) -> None:
	sequence = ebpu.get_hk_sequence()
	ebtcs.set_tec_setpoint(interface, 0x00, TEC_SETPOINT_MINUS_35C)
	time.sleep(2)
	ebtcs.hk_request(interface, 0)
	hk = ebpu.wait_for_fresh_hk(timeout=5.0, after_sequence=sequence)
	errors: list[str] = []
	readings: dict[str, Any] = {}
	actual = getattr(hk, "TEC_SETPOINT", None) if hk is not None else None
	readings["TEC_SETPOINT"] = actual
	if actual is None:
		errors.append("No fresh HK TEC_SETPOINT reading received")
	elif int(actual) != TEC_SETPOINT_MINUS_35C:
		errors.append(f"TEC_SETPOINT is 0x{int(actual):04X}; expected 0x{TEC_SETPOINT_MINUS_35C:04X}")
	_report_check_result(label, errors, readings)


def _wait_for_motion_then_switch_off(
	label: str,
	after_hk_sequence: int,
	psu_port: Any,
	psu_lock: Any,
) -> None:
	deadline = time.monotonic() + 30.0
	sequence = after_hk_sequence
	moving_since: float | None = None
	latest_flags: Any = None

	while time.monotonic() < deadline:
		if ui_runtime_controller.is_aborted():
			raise ui_runtime_controller.ScriptAbortRequested
		ui_runtime_controller.wait_while_paused()
		hk = ebpu.wait_for_fresh_hk(timeout=min(1.0, max(0.0, deadline - time.monotonic())), after_sequence=sequence)
		if hk is None:
			continue
		sequence = ebpu.get_hk_sequence()
		latest_flags = getattr(hk, "MTR_FLAGS", None)
		moving = bool(getattr(latest_flags, "MOVING", 0) or getattr(latest_flags, "HOMING", 0))
		if moving:
			now = time.monotonic()
			if moving_since is None:
				moving_since = now
				ui_runtime_controller.notify(f"{label}: motion detected; confirming continuous motion", color="positive")
			elif now - moving_since >= 5.0:
				ui_runtime_controller.notify(
					f"{label}: motion verified for 5 seconds; switching PSU off",
					color="warning",
				)
				_verify_psu_off(psu_port, psu_lock, f"{label} - switch-off during acquisition preparation")
				return
		else:
			moving_since = None
		ui_runtime_controller.abortible_sleep(0.05)

	message = "No continuous motor motion for 5 seconds within 30 seconds"
	_report_check_result(
		f"{label} - motion check",
		[message],
		{"MTR_FLAGS": latest_flags},
	)


def _wait_for_fixed_point_then_switch_off(
	label: str,
	after_hk_sequence: int,
	psu_port: Any,
	psu_lock: Any,
) -> None:
	acquisition_started = time.monotonic()
	ui_runtime_controller.abortible_sleep_with_progress(
		FIXED_POINT_POWER_CUT_DELAY_S,
		f"{label}: waiting before fixed-point power cut",
		update_interval=1.0,
	)
	deadline = acquisition_started + FIXED_POINT_ACQUISITION_TIMEOUT_S
	sequence = after_hk_sequence
	latest_hk: Any = None

	while time.monotonic() < deadline:
		if ui_runtime_controller.is_aborted():
			raise ui_runtime_controller.ScriptAbortRequested
		ui_runtime_controller.wait_while_paused()
		hk = ebpu.wait_for_fresh_hk(
			timeout=min(1.0, max(0.0, deadline - time.monotonic())),
			after_sequence=sequence,
		)
		if hk is None:
			continue
		sequence = ebpu.get_hk_sequence()
		latest_hk = hk
		flags = getattr(hk, "MTR_FLAGS", None)
		moving = bool(getattr(flags, "MOVING", 0) or getattr(flags, "HOMING", 0))
		operating_state = getattr(hk, "CURRENT_OPERATING_STATE", None)
		if operating_state == ACQUISITION_STATE and not moving:
			ui_runtime_controller.notify(
				f"{label}: acquisition is active and motor is stationary; switching PSU off",
				color="warning",
			)
			_verify_psu_off(psu_port, psu_lock, f"{label} - switch-off during fixed-point acquisition")
			return
		ui_runtime_controller.abortible_sleep(0.05)

	_report_check_result(
		f"{label} - fixed-point sample check",
		[f"No stationary motor sample received during acquisition within {FIXED_POINT_ACQUISITION_TIMEOUT_S:.0f} seconds"],
		{
			"CURRENT_OPERATING_STATE": getattr(latest_hk, "CURRENT_OPERATING_STATE", None),
			"MTR_FLAGS": getattr(latest_hk, "MTR_FLAGS", None),
		},
	)


def _prepare_warm_boards_and_home(interface: Any, label: str) -> None:
	ebtcs.en_mech_board(interface, 0x1)
	ebtcs.en_det_board(interface, 0x1)
	ui_runtime_controller.abortible_sleep_with_progress(3, f"{label}: boards powering on", update_interval=1)
	ebtcs.set_motor_configs(interface, 0, 0x40, 0x00, 0x08, 0x00, 0x3C, 0x00, 0x00, 0x0000, 0x00)

	ebtcs.ob_homing(interface, 0x01)
	ui_runtime_controller.notify(f"{label}: homing started", color="primary")
	_run_check(f"{label} - homing check", ui_runtime_controller.perform_homing_check_sync)
	ui_runtime_controller.request_force_pause(f"{label} - homing complete")


def _configure_flight_heaters(interface: Any) -> None:
	ebtcs.en_mech_heater(interface, 0x00)
	ebtcs.en_det_heater(interface, 0x00)
	ebtcs.set_heater_configs(interface, *FLIGHT_HEATER_CONFIGS)
	ebtcs.en_mech_heater(interface, 0x01)
	ebtcs.en_det_heater(interface, 0x01)


def _interrupt_during_acquisition(
	interface: Any,
	psu_port: Any,
	psu_lock: Any,
	label: str,
	*acq_config: int,
	fixed_point: bool = False,
) -> None:
	_configure_flight_heaters(interface)
	ebtcs.set_acq_configs(interface, *acq_config)
	ebtcs.set_hk_rate(interface, 0, 1)
	ui_runtime_controller.notify(f"{label}: acquisition configured; starting preparation movement", color="primary")
	hk_sequence = ebpu.get_hk_sequence()
	ebtcs.acquisition(interface, 0x00)
	if fixed_point:
		_wait_for_fixed_point_then_switch_off(label, hk_sequence, psu_port, psu_lock)
	else:
		_wait_for_motion_then_switch_off(label, hk_sequence, psu_port, psu_lock)


def run_inadvertent_switch_off(psu_port: Any = None, psu_lock: Any = None) -> None:
	"""Run the four inadvertent switch-off power-cycle tests."""
	interface = eb_interface.get_egse_interface()

	# First boot: verify POST before the first uncommanded power loss.
	_boot_to_safe(interface, psu_port, psu_lock, "Power cycle 1")
	_verify_psu_off(psu_port, psu_lock, "Power cycle 1 - verify zero consumption")

	# Second boot: verify POST and regular STANDBY telemetry before power loss.
	_boot_to_safe(interface, psu_port, psu_lock, "Power cycle 2")
	_enter_standby(interface, "Power cycle 2")
	_verify_psu_off(psu_port, psu_lock, "Power cycle 2 - verify zero consumption")

	# Third boot: interrupt a normal acquisition after continuous motion is confirmed.
	_boot_to_safe(interface, psu_port, psu_lock, "Power cycle 3")
	_enter_standby(interface, "Power cycle 3")
	_prepare_warm_boards_and_home(interface, "Normal acquisition")
	_verify_tec_setpoint(interface, "Normal acquisition - TEC setpoint")
	_interrupt_during_acquisition(
		interface,
		psu_port,
		psu_lock,
		"Normal acquisition",
		0, 0, 0, 0, 0, 1, 249, 0, 1, 42, 0, 0, 0, 12,
	)

	# Fourth boot: wait for a stationary fixed-point sample before power loss.
	_boot_to_safe(interface, psu_port, psu_lock, "Power cycle 4")
	_enter_standby(interface, "Power cycle 4")
	_prepare_warm_boards_and_home(interface, "Fixed-point acquisition")
	_verify_tec_setpoint(interface, "Fixed-point acquisition - TEC setpoint")
	_interrupt_during_acquisition(
		interface,
		psu_port,
		psu_lock,
		"Fixed-point acquisition",
		0x01, 0x00, 0x0000, 0x00FA, 0x0078, 0x0000, 0x0, 0x00, 0x1, 0x1, 0x1, 0x1, 0x00, 0x01,
		fixed_point=True,
	)

	ui_runtime_controller.notify_script_done()
