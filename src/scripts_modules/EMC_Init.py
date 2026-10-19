# from __future__ import annotations

import logging
import time
from queue import Empty

from core_modules import constants as const
from utility_modules import eb_interface, ebtcs
from utility_modules.eb_packet_utility import get_latest_hk, get_smoothed_latest_psu, wait_for_fresh_psu
from widget_modules import ui_runtime_controller

info_log = logging.getLogger("info_log")


def run_emc_init(verification: bool = True) -> None:
    interface = eb_interface.get_egse_interface()
    # RET command (SAFE mode)
    ebtcs.ret(interface, 0, 0, 0, 0, 0, 0)
    ebtcs.hk_request(interface, 0)
    if verification:
        msg, passed = ui_runtime_controller.verify_safe_ret()
        if not passed:
            raise AssertionError(f"SAFE RET verification failed:\n{msg}")

    time.sleep(2)
    # Transition to Standby and use automatic ASW
    ebtcs.standby(interface, 5, 1)
    ebtcs.ret(interface, 0, 0, 0, 0, 0, 0)
    ebtcs.hk_request(interface, 0)
    ebtcs.set_hk_rate(interface, 0, 1)
    time.sleep(2)
    if verification:
        msg, passed = ui_runtime_controller.verify_standby_ret()
        if not passed:
            raise AssertionError(f"STANDBY RET verification failed:\n{msg}")
        else:
            info_log.info("STANDBY RET verification passed:\n%s", msg)

    time.sleep(1)
    # Configure Heaters for ON during test (Upper - 2245 +55 ) (Lower - 2211 +45 )
    ebtcs.set_heater_configs(interface, 0x00, 0x08C5, 0x08A3, 0x08C5, 0x08A3)
    # Activate both heaters
    ebtcs.en_mech_heater(interface, 0x1)
    ebtcs.en_det_heater(interface, 0x1)
    # Generic TC for heater status (see script)
    ebtcs.generic_tc(interface, 0x0, 0x6, 0x64, 0xC4, 0x01, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0)
    ebtcs.hk_request(interface, 0)
    time.sleep(1)
    if verification:
        errors = []
        try:
            latest_hk = get_latest_hk()
            latest_psu = get_smoothed_latest_psu(5) if wait_for_fresh_psu(timeout=2.0) is not None else None
        except Empty:
            errors.append("Missing HK or PSU queue data (mech ON, det ON)")
            latest_hk = None
            latest_psu = None
        ch4_current_ma = None
        if latest_psu is not None:
            ch4_current_ma = ui_runtime_controller.consumption_check("State2", latest_psu, errors)
        if latest_hk is not None:
            if hasattr(latest_hk, "THRM_STATUS") and getattr(latest_hk.THRM_STATUS, "HMS", 0) != 1:
                errors.append(
                    f"Mechanism heater is not ON (THRM_STATUS.HMS={getattr(latest_hk.THRM_STATUS, 'HMS', 0)})"
                )
            if hasattr(latest_hk, "THRM_STATUS") and getattr(latest_hk.THRM_STATUS, "HDS", 0) != 1:
                errors.append(f"Detector heater is not ON (THRM_STATUS.HDS={getattr(latest_hk.THRM_STATUS, 'HDS', 0)})")
        if errors:
            count = len(errors)
            numbered = [f"{i + 1}. {err.strip()}" for i, err in enumerate(errors)]
            info_log.info(f"PSU_EB_I: {ch4_current_ma if ch4_current_ma is not None else 'N/A'} mA")
            msg = (
                f"Heater config verification failed (mech ON, det ON): {count} error{'s' if count != 1 else ''} :\n"
                + "\n".join(numbered)
            )
            ui_runtime_controller.notify_negative(msg)
            raise AssertionError(msg)
        else:
            msg = (
                f"Power State 2 : OB HEATING - PSU_EB_I: {ch4_current_ma if ch4_current_ma is not None else 0.0:.2f} mA, "
                f"CURRENT_OPERATING_STATE: {getattr(latest_hk, 'CURRENT_OPERATING_STATE', None)}, "
                f"THRM_STATUS.HMS: {getattr(getattr(latest_hk, 'THRM_STATUS', None), 'HMS', 0)}, "
                f"THRM_STATUS.HDS: {getattr(getattr(latest_hk, 'THRM_STATUS', None), 'HDS', 0)}"
            )
            info_log.info(msg)
            ui_runtime_controller.notify_positive(msg)

    # Enable Mechanism and Detector boards
    ebtcs.en_mech_board(interface, 0x1)
    ebtcs.en_det_board(interface, 0x1)
    # Set Motor Parameters to defaults
    ebtcs.set_motor_configs(interface, 0, 0x40, 0x00, 0x08, 0x00, 0x00, 0x3C, 0x00, 0x0000, 0x00)
    # Perform Homing Cal to Base then Drive to Outer
    ebtcs.ob_homing(interface, 0x01)

    # Wait for HOMING_COMPLETE flag in HK, with 1 minute timeout, using global cache
    info_log.info("Test 1")
    # Call the synchronous homing check helper so this blocking script
    # waits correctly for homing to complete.
    ui_runtime_controller.perform_homing_check_sync()

    # TODO: Check OBHOMED flag in HK (user should verify before proceeding)
    ui_runtime_controller.notify_script_pause(13, 13)
    # End of EMC_Init
    ui_runtime_controller.notify_script_done()
