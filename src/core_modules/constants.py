import logging
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any

from core_modules import config

# ----Initialisation--------------------------------------------------------------------------------
DEBUG_LEVEL = logging.INFO
DEFAULT_PREFIX = datetime.now().strftime("%Y%m%dT%H%M%S")
DEFAULT_PATH = Path.cwd() / "logs" / DEFAULT_PREFIX
DEFAULT_EB_PATH = r"C:\wdir\EB_EGSE"
DEFAULT_STARTUP_MODE = "OB"

SCI_RESP_MARGIN = 0.020  # seconds
LOG_PREFIX = DEFAULT_PREFIX
LOG_PATH = DEFAULT_PATH

# ----Program Variables-----------------------------------------------------------------------------
# Set by EGSE.py do not write here.
ACK_LOG_FH: Any = None
CMD_LOG_FH: Any = None
HK_LOG_FH: Any = None
SCI_LOG_FH: Any = None

# HK queue
hk_queue = Queue(maxsize=100)
# hk_explorer_queue = Queue(maxsize=100)  # Separate queue for HK parameter explorer
eb_post_queue = Queue(maxsize=100)
psu_queue = Queue(maxsize=100)
sci_queue = Queue(maxsize=100)

# ----Monitoring Limits-----------------------------------------------------------------------------
# Real-space limits (engineering units) from TM reference table
WLIM_EB_12V = (11.0, 13.0)
ALIM_EB_12V = (10.5, 13.5)

WLIM_EB_NEG12V = (-13.0, -11.0)
ALIM_EB_NEG12V = (-13.5, -10.5)

WLIM_EB_5V = (4.5, 5.5)
ALIM_EB_5V = (4.0, 6.0)

WLIM_EB_3V3 = (3.0, 3.45)
ALIM_EB_3V3 = (3.0, 3.6)

# EB TEC rail is upper-bound monitored only per table (low side not monitored)
WLIM_EB_TEC_RAIL = (None, 3.25)
ALIM_EB_TEC_RAIL = (None, 3.5)

WLIM_EB_MCU_INTERNAL_TEMP = (-45, 125)
ALIM_EB_MCU_INTERNAL_TEMP = (-50, 135)

WLIM_EB_INTERNAL_TRP_TEMP = (-42, 70)
ALIM_EB_INTERNAL_TRP_TEMP = (-45, 90)

WLIM_EB_PSU_BOARD_TEMP = (-45, 110)
ALIM_EB_PSU_BOARD_TEMP = (-50, 120)

WLIM_3V3 = (3.0, 3.6)
ALIM_3V3 = (2.85, 3.75)

WLIM_1V5 = (1.425, 1.575)
ALIM_1V5 = (1.35, 1.65)

WLIM_TPR = (-45, +45)
ALIM_TPR = (-50, +50)

# ADU-space limits derived from conversion formulas in utility_modules/hk_conversions.py
# and utility_modules/eb_packet_utility.py
WLIM_EB_12V_ADU = (27463, 32456)
ALIM_EB_12V_ADU = (26214, 33704)

WLIM_EB_NEG12V_ADU = (28836, 34079)
ALIM_EB_NEG12V_ADU = (27525, 35389)

WLIM_EB_5V_ADU = (29445, 35988)
ALIM_EB_5V_ADU = (26173, 39260)

WLIM_EB_3V3_ADU = (39318, 45216)
ALIM_EB_3V3_ADU = (39318, 47182)

WLIM_EB_MCU_INTERNAL_TEMP_ADU = (13926, 24310)
ALIM_EB_MCU_INTERNAL_TEMP_ADU = (13621, 24921)

# EB thermistor ADU limits (decode_eb_trps conversion; temperature decreases with ADU)
WLIM_EB_INTERNAL_TRP_TEMP_ADU = (30992, 65238)
ALIM_EB_INTERNAL_TRP_TEMP_ADU = (21269, 65297)

WLIM_EB_PSU_BOARD_TEMP_ADU = (14121, 65297)
ALIM_EB_PSU_BOARD_TEMP_ADU = (11469, 65373)

WLIM_3V3_ADU = (1500, 1800)
ALIM_3V3_ADU = (1425, 1875)

WLIM_1V5_ADU = (1425, 1575)
ALIM_1V5_ADU = (1350, 1650)

# OB thermistor ADU limits (adu_to_temp conversion)
WLIM_TPR_ADU = (1849, 2212)
ALIM_TPR_ADU = (1825, 2229)

# ----Bus Voltage Settings-------------------------------------------------------------
# Bus voltage settings for different modes and channels (Min, Nominal, Max)
BUS_VOLTAGES = {
    "OB": {
        "CH1": {"MIN": 11.2, "NOM": 12.0, "MAX": 13.2},
        "CH2": {"MIN": 11.2, "NOM": 12.0, "MAX": 13.2},
        "CH3": {"MIN": 4.8, "NOM": 5.0, "MAX": 5.5},
    },
    "EB": {
        "CH3": {"MIN": 26.0, "NOM": 28.0, "MAX": 29.4},
        "CH4": {"MIN": 26.0, "NOM": 28.0, "MAX": 29.4},
    },
}

# ----Power State Limits based on Model-------------------------------------------------------------
# Keep the selectable model list in config so the UI and protocol config stay aligned.
MODELS = list(config.MODEL_OPTIONS)

# States whose MODEL_CONSUMPTION values already include heater (OB Heating) power.
# When one of these states is used in consumption_check, heater sub-states (MechHTR/DetHTR)
# must NOT be added again from verify_heater_states — they are already baked in.
# State2 = OB Heating
# State3 = OB Heating + Powered On
# State4 = OB Heating + Powered On + TEC at 0.9A
# State7 = All Active (OB Heating + Powered On + TEC at 0.9A + Moving)
HEATER_INCLUSIVE_STATES: frozenset[str] = frozenset({"State2", "State3", "State4", "State7"})

# Power consumption (mA) per state for each model
# State Map :
# State1 : Safe
# State2 : OB Heating
# State3 : OB Heating + Powered On
# State4 : OB Heating + Powered On + TEC at 0.9A
# State5 : Powered On + TEC at 0.9A
# State6 : ACQ
# State7 : All Active - OB Heating + Powered On + TEC at 0.9A + Moving

MODEL_CONSUMPTION = {
    "IFM": {
            "State1": 87,
            "State2": 160,
            "State3": 179,
            "State4": 327,
            "State5": 266,
            "State6": 207,
            "State7": 380,
            "Standby": 101,
            "Mech": 4,
            "Det": 12,
            "DetHTR": 21,
            "MechHTR": 42,
            "Moving": 53,
            "TEC_0_9A": 130,
            "TEC35": 100,
            "TEC65": 140,
        },
    "DEM": {
        "State1": 87,
        "State2": 160,
        "State3": 179,
        "State4": 327,
        "State5": 266,
        "State6": 207,
        "State7": 380,
        "Standby": 101,
        "Mech": 4,
        "Det": 12,
        "DetHTR": 21,
        "MechHTR": 42,
        "Moving": 53,
        "TEC_0_9A": 130,
        "TEC35": 100,
        "TEC65": 140,
    },
    "EM": {  # Consumption Dictionary for EMC
        "State1": 84,  # 90 - EMC | 84 - FFT |
        "State2": 163,  # 170 - EMC | 163 - FFT |
        "State3": 180,  # 190 - EMC | 180 - FFT |
        "State4": 310,  # 320 - EMC | 310 - FFT |
        "State5": 240,  # 255 - EMC | 240 - FFT |
        "State6": 200,  # pretty much same (200)
        "State7": 360,  # 370 - EMC | 360 - FFT |
        "Standby": 103,  # 110 - EMC | 103 - FFT |
        "Mech": 4,
        "Det": 10,
        "DetHTR": 19,
        "MechHTR": 38,
        "Moving": 50,
        "TEC_0_9A": 130,  # changed to 120 for max, changed to 140 for min
        "TEC35": 100,
        "TEC65": 140,  #! need to probably check and update
    },
    "PFM": {
        "State1": 84,  # 90 - EMC | 84 - FFT
        "State2": 163,  # 170 - EMC | 163 - FFT
        "State3": 180,  # 190 - EMC | 180 - FFT
        "State4": 310,  # 320 - EMC | 310 - FFT
        "State5": 230,  # 255 - EMC | 230 - FFT
        "State6": 200,  # pretty much same
        "State7": 360,  # 370 - EMC | 360 - FFT
        "Standby": 103,  # 110 - EMC | 103 - FFT
        "Mech": 4,
        "Det": 10,
        "DetHTR": 19,
        "MechHTR": 38,
        "Moving": 50,
        "TEC_0_9A": 130,
        "TEC35": 100,
        "TEC65": 140,  #! need to probably check and update
    },
    "FS": {
        "State1": 85,
        "State2": 105,
        "State3": 125,
        "State4": 145,
        "State5": 165,
        "State6": 185,
        "State7": 205,
        "Standby": 95,
    },
}

# Total PSU EB input-current expectations from the current-consumption analysis
# workbook. FM uses its PFM projection where the workbook does not list a direct
# FM measurement. Values are in mA; the workbook specifies a +/-10 mA tolerance.
PSU_CONSUMPTION_TOLERANCE_MA = 10.0
PSU_CONSUMPTION_VOLTAGE_TOLERANCE_V = 0.5
PSU_MODEL_CONSUMPTION_BY_VOLTAGE_MA = {
    26.0: {
        "EM": {
            "State1": 89.0,
            "State2": 173.0,
            "State3": 191.0,
            "State4": 330.0,
            "State5": 263.0,
            "State6": 216.0,
            "State7": 385.0,
        },
        "FM": {
            "State1": 89.28,
            "State2": 178.82,
            "State3": 196.92,
            "State4": 333.84,
            "State5": 269.94,
            "State6": 222.98,
            "State7": 408.11,
        },
    },
    28.0: {
        "EM": {
            "State1": 85.0,
            "Standby": 103.0,
            "State2": 163.22,
            "State3": 180.04,
            "State4": 310.0,
            "State5": 244.76,
            "State6": 203.9,
            "State7": 356.22,
        },
        "FM": {
            "State1": 85.28,
            "Standby": 109.38,
            "State2": 169.04,
            "State3": 185.96,
            "State4": 313.84,
            "State5": 251.70,
            "State6": 210.88,
            "State7": 379.33,
        },
    },
    29.4: {
        "EM": {
            "State1": 83.0,
            "State2": 157.0,
            "State3": 173.0,
            "State4": 296.0,
            "State5": 237.0,
            "State6": 194.0,
            "State7": 344.0,
        },
        "FM": {
            "State1": 83.28,
            "State2": 162.82,
            "State3": 178.92,
            "State4": 299.84,
            "State5": 243.94,
            "State6": 200.98,
            "State7": 367.11,
        },
    },
}

# ----MMS Configuration-----------------------------------------------------------------------------
# Session-only GUI mode for EB-only/no-OB tests. Applies the three no-OB masks
# without disabling MMS or changing the individual script masks below.
MMS_EB_ONLY_TEST_MODE: bool = False

# When True, OB_GENERAL_ERROR in the EB ERROR_FLAGS is excluded from MMS trigger conditions.
# OB_GENERAL_ERROR is a sticky bit that may linger after the OB error has already cleared.
# Set to False to allow OB_GENERAL_ERROR to trigger MMS actions.
MMS_MASK_OB_GENERAL_ERROR: bool = False

# When True, all OB voltage/temperature limit checks (OB FPGA rails and OB thermistors)
# are excluded from MMS trigger conditions. Intended for EB-only runs without an OB
# attached, where every OB channel reads out of limits.
MMS_MASK_OB_LIMIT_CHECKS: bool = False

# When True, OB_UNRESPONSIVE in the EB ERROR_FLAGS is excluded from MMS trigger
# conditions. Intended for EB-only runs where the EB flags the missing OB.
MMS_MASK_OB_UNRESPONSIVE: bool = False

# When True, RS485_RECEIVE_ERROR and RS485_TRANSMIT_ERROR in the EB ERROR_FLAGS
# are excluded from MMS trigger conditions. RS485 is the EB<->OB interface, so
# these bits can assert when running without an OB attached.
MMS_MASK_RS485_ERRORS: bool = False
