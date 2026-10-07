CMD_SPEED_DICT = {
    "Steady": 0.25,
    "Fast": 0.05,
}

# Model bitmap mapping for the OB/EB instrument. DEM is the BB2 device variant,
# which uses the 3-bit value 010.
MODEL_OPTIONS = [
    "DEV",
    "IFM",
    "DEM",
    "Firmware TB",
    "EM",
    "FM",
    "FS",
    "CMOD EGSE",
]
MODEL_BITMAPS = {
    "DEV": "000",
    "IFM": "001",
    "DEM": "010",
    "BB2": "010",
    "Firmware TB": "011",
    "EM": "100",
    "FM": "101",
    "FS": "110",
    "CMOD EGSE": "111",
}
DEFAULT_COM_PORT = 5
DEFAULT_CMD_SPEED = "Fast"  # "Steady" or "Fast"
EXP_MODEL_ID = 7

# Show the EB-only mask switch and live MMS fault-injection script in the GUI.
SHOW_MMS_TEST_TOOLS = False

# PSU Config
PSU_COM_PORT = 10
PSU_LOGGING_FREQ = 10  # in HZ

# TEC current configuration used by FFT and power-state verification.
# Change these values together when a different TEC clamp is required.
TEC_CURRENT_SETPOINT_ADU = 3550  # Commanded SET_TEC_CURRENT value (0x0E66)
TEC_EXPECTED_CURRENT_A = 0.90  # Expected measured EB_TEC_DRIVE_CURRENT, amps
TEC_CURRENT_TOLERANCE_A = 0.05  # Allowed +/- current tolerance, amps

# Scope Config (Tektronix MSO44B, direct-cabled LAN link — see tek_scope_api)
SCOPE_VISA_RESOURCE = "TCPIP0::169.254.9.67::INSTR"

CH1_OVP = 12.5
CH1_I = 0.150

CH2_OVP = 12.5
CH2_I = 0.150

CH3_OVP = 5.5
CH3_I = 0.150

ROV_HTR_OVP = 30
ROV_HTR_I = 0.05

EB_OVP = 30
EB_I = 0.5

# DAC Offset
SWIR_DAC_MIN_TH = 100
SWIR_DAC_MAX_TH = 300
MWIR_DAC_MIN_TH = 3300
MWIR_DAC_MAX_TH = 3500

MEASUREMENT_TABLES = [
    # The full range of values
    list(range(0, 8600)),
    # Guess at edges of the window.
    list(range(1300, 7600)),
    # Uneven steps for testing
    list(range(0, 1300, 10)) + list(range(1300, 7600)) + list(range(7600, 8601, 10)),
]

# Expected firmware/table CRCs reported in the EB POST packet. Keep in one
# place so image/table updates only require a change here.
POST_EXPECTED_CRC = {  # CRCs for combined patch v3.2.8 and v3.6.2
    "ASW_IMAGE_1_CRC": 0xBAF7,
    "ASW_IMAGE_2_CRC": 0xBC91,
    "ASW_IMAGE_3_CRC": 0x4FD5,
    "ASW_IMAGE_4_CRC": 0xE39A,
    "ASW_IMAGE_5_CRC": 0x440F,
    "BSW_IMAGE_CRC": 0xD2D7,
    "MEASUREMENT_TABLE_CRC": 0x7952, # msrmt table v1.1.3_7952
}


def _model_bitmap_value(model: str) -> int:
    value = MODEL_BITMAPS.get(model, model)
    if isinstance(value, str):
        if set(value).issubset({"0", "1"}):
            return int(value, 2)
        try:
            return int(value, 10)
        except ValueError:
            return int(str(value), 10)
    return int(value)


def set_expected_model_id(model: str | None) -> int:
    global EXP_MODEL_ID
    if not model:
        EXP_MODEL_ID = _model_bitmap_value("CMOD EGSE")
        return EXP_MODEL_ID
    EXP_MODEL_ID = _model_bitmap_value(model)
    return EXP_MODEL_ID
