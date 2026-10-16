from datetime import datetime
from types import SimpleNamespace

import pytest

from core_modules import config
from widget_modules import ui_runtime_controller as urc


def test_acquisition_completion_does_not_check_science_point_temperatures(monkeypatch) -> None:
    hk_packets = iter(
        (
            SimpleNamespace(CURRENT_OPERATING_STATE=0x08, SCIENCE_PACKETS_SENT=0),
            SimpleNamespace(CURRENT_OPERATING_STATE=0x00, SCIENCE_PACKETS_SENT=1),
        )
    )

    class _Progress:
        def __init__(self, _message) -> None:
            pass

        def update(self, _message) -> None:
            pass

        def finish(self) -> None:
            pass

    monkeypatch.setattr(urc, "get_latest_hk", lambda: next(hk_packets))
    monkeypatch.setattr(urc.eb_packet_utility, "get_sci_received_count", lambda: 0)
    monkeypatch.setattr(urc, "_warn_if_sci_packets_missing", lambda *_args: None)
    monkeypatch.setattr(urc.eb_packet_utility, "get_sci_packets_since", lambda *_args: pytest.fail("SCI temps checked"))
    monkeypatch.setattr(urc, "ProgressNotifier", _Progress)
    monkeypatch.setattr(urc, "is_aborted", lambda: False)
    monkeypatch.setattr(urc.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(urc.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(urc, "notify_positive", lambda _message: None)

    urc.perform_acq_check_sync(acq_timeout_s=1, check_state6=False)


def test_acquisition_report_preserves_state6_failure(monkeypatch) -> None:
    hk_packets = iter(
        (
            SimpleNamespace(CURRENT_OPERATING_STATE=0x08, SCIENCE_PACKETS_SENT=0),
            SimpleNamespace(CURRENT_OPERATING_STATE=0x00, SCIENCE_PACKETS_SENT=1),
        )
    )
    times = iter((0.0, 0.0, 151.0, 151.0))
    monkeypatch.setattr(urc, "get_latest_hk", lambda: next(hk_packets))
    monkeypatch.setattr(urc.eb_packet_utility, "get_sci_received_count", lambda: 0)
    monkeypatch.setattr(urc, "_warn_if_sci_packets_missing", lambda *_args: None)
    monkeypatch.setattr(urc, "ProgressNotifier", lambda *_args: SimpleNamespace(finish=lambda: None))
    monkeypatch.setattr(urc, "is_aborted", lambda: False)
    monkeypatch.setattr(urc.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(urc, "notify_positive", lambda _message: None)
    monkeypatch.setattr(urc, "notify_negative", lambda _message: None)

    def consumption(_states, errors, _hk):
        errors.append("State 6 current out of range")
        return 321.0

    monkeypatch.setattr(urc, "windowed_consumption_check", consumption)
    readings, errors = {}, []

    urc.perform_acq_check_sync(acq_timeout_s=300, readings=readings, errors_out=errors)

    assert errors == ["State 6 current out of range"]
    assert readings["PSU_EB_I (mA)"] == 321.0
    assert readings["SCIENCE_PACKETS_SENT"] == 1
    assert readings["State 6 power check"] == "Performed"


@pytest.mark.parametrize(
    ("model", "voltage", "state", "expected_ma"),
    [
        ("EM", 26.0, "State1", 89.0),
        ("FM", 26.0, "State7", 408.11),
        ("EM", 28.0, "State4", 310.0),
        ("FM", 28.0, "Standby", 109.38),
        ("EM", 29.4, "State3", 173.0),
        ("FM", 29.4, "State6", 200.98),
    ],
)
def test_voltage_specific_consumption_bounds_use_workbook_values(
    monkeypatch, model, voltage, state, expected_ma
):
    monkeypatch.setattr(urc._nicegui_app.state, "current_model", model, raising=False)
    errors = []

    bounds = urc.resolve_consumption_bounds(state, errors, psu_voltage_v=voltage)

    assert bounds is not None
    assert bounds[0] == [state]
    assert bounds[1:] == pytest.approx((expected_ma - 10, expected_ma + 10))
    assert errors == []


def test_voltage_specific_consumption_check_uses_measured_psu_output(monkeypatch):
    monkeypatch.setattr(urc._nicegui_app.state, "current_model", "FM", raising=False)
    errors = []

    measured = urc.consumption_check(
        "State1",
        {"PSU_EB_V": 26.0, "PSU_EB_I": 0.098},
        errors,
    )

    assert measured == pytest.approx(98.0)
    assert errors == []


def test_unknown_or_off_voltage_uses_existing_model_consumption(monkeypatch):
    monkeypatch.setattr(urc._nicegui_app.state, "current_model", "EM", raising=False)
    errors = []

    bounds = urc.resolve_consumption_bounds("Standby", errors, psu_voltage_v=-0.01)

    assert bounds == (["Standby"], 93.0, 113.0)
    assert errors == []


def test_windowed_consumption_uses_voltage_from_captured_samples(monkeypatch):
    monkeypatch.setattr(urc._nicegui_app.state, "current_model", "FM", raising=False)
    monkeypatch.setattr(urc, "wait_for_fresh_psu", lambda **_kwargs: {"PSU_EB_V": 26.0})
    monkeypatch.setattr(
        urc,
        "get_smoothed_latest_psu",
        lambda _window: {"PSU_EB_V": 26.0, "PSU_EB_I": 0.098},
    )
    monotonic_values = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(urc.time, "monotonic", lambda: next(monotonic_values))
    errors = []

    measured = urc.windowed_consumption_check(
        "State1",
        errors,
        duration_s=1.0,
        per_sample_timeout_s=0.01,
        min_samples=1,
    )

    assert measured == pytest.approx(98.0)
    assert errors == []


def test_plot_samples_retain_real_and_adu_values_for_each_timestamp() -> None:
    class _PlotCard:
        def __init__(self) -> None:
            self.samples = []

        def set_stream_enabled(self, _enabled: bool) -> None:
            pass

        def push(self, *args, **kwargs) -> None:
            self.samples.append((args, kwargs))

    trp_card = _PlotCard()
    voltage_card = _PlotCard()
    timestamp = datetime(2026, 9, 29, 12, 0, 0)
    hk = SimpleNamespace(
        TIME=timestamp,
        DIGITAL_TRP=2048,
        DETEC_TRP=2100,
        MECH_TRP=2200,
        MOTOR_TRP=2300,
        HK_V_3V3=2000,
        HK_V_1V5=2500,
    )

    state = {
        "mode": "OB",
        "hk_display_mode": "REAL",
        "psu_replay": {},
        "latest_hk_packet": hk,
        "latest_sci_temps": {"mode": "OB", "values": {"SWIR": ("SWIR_TEMP", 6282)}},
    }
    urc.update_plot_cards(
        state,
        hk,
        trp_card,
        voltage_card,
    )

    trp_args, trp_kwargs = trp_card.samples[0]
    assert trp_args[0] == [timestamp]
    assert trp_kwargs["display_values"]["ADU"][0] == [2048.0]
    assert trp_kwargs["display_values"]["REAL"][0][0] != trp_kwargs["display_values"]["ADU"][0][0]
    assert all(value[0] != value[0] for value in trp_kwargs["display_values"]["REAL"][4:])

    sci_timestamp = timestamp.replace(second=1)
    urc._push_sci_temperature_plot(
        state,
        SimpleNamespace(TIME=sci_timestamp, SWIR_TEMP=6282, HT_SINK_TEMP=6282),
        "OB",
        trp_card,
    )
    sci_args, sci_kwargs = trp_card.samples[1]
    assert sci_args[0] == [sci_timestamp]
    assert sci_kwargs["display_values"]["ADU"][4] == [392.0]
    assert sci_kwargs["display_values"]["REAL"][4][0] > 0

    voltage_args, voltage_kwargs = voltage_card.samples[0]
    assert voltage_args[0] == [timestamp]
    assert voltage_kwargs["display_values"]["ADU"][:2] == [[2000.0], [2500.0]]


class _DummyLogger:
    def __init__(self) -> None:
        self.warns = []
        self.errors = []

    def warning(self, msg, *args):
        self.warns.append(msg % args if args else msg)

    def error(self, msg, *args):
        self.errors.append(msg % args if args else msg)


def test_ob_alarm_details_filter_only_ob_related_flags() -> None:
    hk = SimpleNamespace(
        WARNING_FLAGS_BITS=SimpleNamespace(OB_UNRESPONSIVE=1, GENERAL_ERROR=1),
        FDIR_ALARM_FLAGS_BITS=SimpleNamespace(DIGITAL_BOARD_TRP=1, EB_PLUS_5V_SUPPLY=1),
        FDIR_WARNING_FLAGS_BITS=SimpleNamespace(MECH_BOARD_TRP=1, EB_TEC_SUPPLY=1),
        ERRORS=SimpleNamespace(IPI=1),
        MTR_ERRORS=SimpleNamespace(DSE=1),
    )

    details = urc.ob_alarm_details(hk)

    assert "OB Warning: OB_UNRESPONSIVE" in details
    assert "OB Warning: GENERAL_ERROR" not in details
    assert "OB FDIR Alarm: DIGITAL_BOARD_TRP" in details
    assert "OB FDIR Alarm: EB_PLUS_5V_SUPPLY" not in details
    assert "OB FDIR Warning: MECH_BOARD_TRP" in details
    assert "OB FDIR Warning: EB_TEC_SUPPLY" not in details
    assert "OB Error flags active" in details
    assert "OB Motor error flags active" in details


def test_eb_alarm_details_filter_only_eb_related_flags_and_tcs_rejected() -> None:
    hk = SimpleNamespace(
        TCS_REJECTED=2,
        WARNING_FLAGS_BITS=SimpleNamespace(GENERAL_ERROR=1, OB_UNRESPONSIVE=1),
        FDIR_ALARM_FLAGS_BITS=SimpleNamespace(EB_PLUS_5V_SUPPLY=1, MECH_BOARD_TRP=1),
        FDIR_WARNING_FLAGS_BITS=SimpleNamespace(PSU_BOARD_TEMPERATURE=1, MOTOR_TRP=1),
    )

    details = urc.eb_alarm_details(hk)

    assert "TCS Rejected: 2" in details
    assert "EB Warning: GENERAL_ERROR" in details
    assert "EB Warning: OB_UNRESPONSIVE" not in details
    assert "EB FDIR Alarm: EB_PLUS_5V_SUPPLY" in details
    assert "EB FDIR Alarm: MECH_BOARD_TRP" not in details
    assert "EB FDIR Warning: PSU_BOARD_TEMPERATURE" in details
    assert "EB FDIR Warning: MOTOR_TRP" not in details


def test_log_new_hk_alarm_details_logs_raise_once() -> None:
    state = {}
    logger = _DummyLogger()

    urc.log_new_hk_alarm_details(
        state,
        logger,
        channel="ob",
        details=["OB Warning: OB_UNRESPONSIVE", "OB FDIR Alarm: DIGITAL_BOARD_TRP"],
    )

    assert any("warning raised" in msg for msg in logger.warns)
    assert any("alarm raised" in msg for msg in logger.errors)

    # Re-logging same details should not produce duplicate raise messages.
    warn_count = len(logger.warns)
    error_count = len(logger.errors)
    urc.log_new_hk_alarm_details(
        state,
        logger,
        channel="ob",
        details=["OB Warning: OB_UNRESPONSIVE", "OB FDIR Alarm: DIGITAL_BOARD_TRP"],
    )
    assert len(logger.warns) == warn_count
    assert len(logger.errors) == error_count


def test_perform_hk_check_detects_non_zero_error_and_fdir_fields() -> None:
    hk = SimpleNamespace(
        EB_MEAS_MAIN_12V=30000,
        EB_MEAS_MAIN_NEG12V=30000,
        EB_MEAS_5V=30000,
        EB_MEAS_3V3=43000,
        EB_MEAS_TEC_RAIL=0,
        EB_0V_ADC_READING=0,
        EB_TEC_DRIVE_CURRENT=0,
        TCS_REJECTED=0,
        INSTRUMENT_STATUS_FLAGS=25604,
        ERROR_FLAGS=1,
        WARNING_FLAGS=0,
        FDIR_ALARM_FLAGS=2,
        FDIR_WARNING_FLAGS=4,
    )

    result = urc.perform_hk_check(hk=hk, hk_type="hk")

    assert result["passed"] is False
    assert any("ERROR_FLAGS mismatch. Got: 1, Expected: 0" in detail for detail in result["details"])
    assert any("FDIR_ALARM_FLAGS mismatch. Got: 2, Expected: 0" in detail for detail in result["details"])
    assert any("FDIR_WARNING_FLAGS mismatch. Got: 4, Expected: 0" in detail for detail in result["details"])


# ---------------------------------------------------------------------------
# Additional alarm lifecycle and HK/POST validation coverage
# ---------------------------------------------------------------------------


def _valid_hk() -> SimpleNamespace:
    return SimpleNamespace(
        EB_MEAS_MAIN_12V=round(12.0 / 0.000400543),
        EB_MEAS_MAIN_NEG12V=round(-12.0 / -0.00038147),
        EB_MEAS_5V=round(5.0 / 0.000152829),
        EB_MEAS_3V3=round(3.3 / 0.0000763),
        EB_MEAS_TEC_RAIL=0,
        EB_0V_ADC_READING=0,
        EB_TEC_DRIVE_CURRENT=0,
        TCS_REJECTED=0,
        INSTRUMENT_STATUS_FLAGS=25604,
        ERROR_FLAGS=0,
        WARNING_FLAGS=0,
        FDIR_ALARM_FLAGS=0,
        FDIR_WARNING_FLAGS=0,
    )


def _valid_post(**overrides) -> SimpleNamespace:
    values = {
        "POST_WARNING_FLAGS": 0,
        "POST_ERROR_FLAGS": 0,
        "NUM_BAD_FLASH_BLOCKS": 0,
        "NUM_BAD_SRAM_BLOCKS": 0,
        **config.POST_EXPECTED_CRC,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_alarm_detail_helpers_handle_missing_namespaces() -> None:
    hk = SimpleNamespace(
        TCS_REJECTED=0,
        WARNING_FLAGS_BITS=None,
        FDIR_ALARM_FLAGS_BITS=None,
        FDIR_WARNING_FLAGS_BITS=None,
        ERRORS=None,
        MTR_ERRORS=None,
    )

    assert urc.ob_alarm_details(hk) == []
    assert urc.eb_alarm_details(hk) == []


def test_alarm_detail_is_logged_again_after_clear_and_retrigger() -> None:
    state = {}
    logger = _DummyLogger()
    detail = "OB FDIR Alarm: DIGITAL_BOARD_TRP"

    urc.log_new_hk_alarm_details(state, logger, channel="ob", details=[detail])
    urc.log_new_hk_alarm_details(state, logger, channel="ob", details=[])
    urc.log_new_hk_alarm_details(state, logger, channel="ob", details=[detail])

    assert sum("alarm raised" in message for message in logger.errors) == 2


def test_perform_hk_check_reports_missing_hk() -> None:
    result = urc.perform_hk_check(hk=None, hk_type="hk")

    assert result == {"passed": False, "details": ["No HK data available."]}


def test_perform_hk_check_accepts_valid_hk() -> None:
    result = urc.perform_hk_check(hk=_valid_hk(), hk_type="hk")

    assert result == {"passed": True, "details": []}


def test_perform_hk_check_failure_reports_actual_and_expected_range() -> None:
    hk = _valid_hk()
    hk.EB_MEAS_MAIN_12V = round(14.0 / 0.000400543)

    result = urc.perform_hk_check(hk=hk, hk_type="hk")

    assert result["passed"] is False
    assert any(
        "EB 12V out of range. Got:" in detail and "Expected: 11.00 to 13.00 V" in detail for detail in result["details"]
    )


def test_perform_hk_check_reports_missing_required_field() -> None:
    hk = _valid_hk()
    del hk.EB_MEAS_MAIN_12V

    result = urc.perform_hk_check(hk=hk, hk_type="hk")

    assert result["passed"] is False
    assert any("Missing HK field" in detail for detail in result["details"])


def test_perform_hk_check_reports_none_value_without_conversion_error() -> None:
    hk = _valid_hk()
    hk.EB_MEAS_3V3 = None

    result = urc.perform_hk_check(hk=hk, hk_type="hk")

    assert result["passed"] is False
    assert "EB_MEAS_3V3. Got: None, Expected: numeric HK value" in result["details"]


def test_perform_post_check_reports_missing_post() -> None:
    result = urc.perform_hk_check(post=None, hk_type="post")

    assert result == {"passed": False, "details": ["No POST data available."]}


def test_perform_post_check_accepts_valid_crc_and_status_fields() -> None:
    result = urc.perform_hk_check(post=_valid_post(), hk_type="post")

    assert result == {"passed": True, "details": []}


def test_perform_post_check_reports_each_failed_status_or_crc() -> None:
    post = _valid_post(
        POST_WARNING_FLAGS=1,
        POST_ERROR_FLAGS=2,
        NUM_BAD_FLASH_BLOCKS=3,
        NUM_BAD_SRAM_BLOCKS=4,
        ASW_IMAGE_1_CRC=0,
        MEASUREMENT_TABLE_CRC=0,
    )

    result = urc.perform_hk_check(post=post, hk_type="post")

    assert result["passed"] is False
    assert any("POST_WARNING_FLAGS mismatch. Got: 1, Expected: 0" in detail for detail in result["details"])
    assert any("POST_ERROR_FLAGS mismatch. Got: 2, Expected: 0" in detail for detail in result["details"])
    assert any("NUM_BAD_FLASH_BLOCKS mismatch. Got: 3, Expected: 0" in detail for detail in result["details"])
    assert any("NUM_BAD_SRAM_BLOCKS mismatch. Got: 4, Expected: 0" in detail for detail in result["details"])
    assert any("ASW_IMAGE_1_CRC mismatch. Got: 0x0000, Expected:" in detail for detail in result["details"])
    assert any("MEASUREMENT_TABLE_CRC mismatch. Got: 0x0000, Expected:" in detail for detail in result["details"])


def test_perform_post_failure_diagnostics_are_separate_from_errors() -> None:
    # Raw ADU values that convert to nominal 12 V, -12 V, 5 V, 3.3 V, 25 C and 0 C.
    post = _valid_post(
        MEASUREMENT_TABLE_CRC=0,
        TM_12V=29959,
        TM_NEG12V=31457,
        TM_5V=32716,
        TM_3V3=43250,
        EB_PROCESSOR_TEMP=18202,
        TEC_DETECTOR_TEMP=28016,
    )

    result = urc.perform_hk_check(post=post, hk_type="post")

    assert result["passed"] is False
    assert len(result["details"]) == 1
    assert "MEASUREMENT_TABLE_CRC mismatch" in result["details"][0]
    diagnostics = result.get("diagnostics", [])
    assert any(
        "TM_12V: Got:" in detail and "Expected: 11.00 to 13.00 V" in detail and "[PASS]" in detail
        for detail in diagnostics
    )
    assert any(
        "TM_NEG12V: Got:" in detail and "Expected: -13.00 to -11.00 V" in detail and "[PASS]" in detail
        for detail in diagnostics
    )
    assert any(
        "TM_5V: Got:" in detail and "Expected: 4.50 to 5.50 V" in detail and "[PASS]" in detail
        for detail in diagnostics
    )
    assert any(
        "TM_3V3: Got:" in detail and "Expected: 3.00 to 3.45 V" in detail and "[PASS]" in detail
        for detail in diagnostics
    )
    assert any(
        "EB_PROCESSOR_TEMP: Got:" in detail and "Expected: -45.00 to 125.00 C" in detail and "[PASS]" in detail
        for detail in diagnostics
    )
    assert any(
        "TEC_DETECTOR_TEMP: Got:" in detail and "Expected: -45.00 to 45.00 C" in detail and "[PASS]" in detail
        for detail in diagnostics
    )


def test_perform_post_check_reports_conversion_error() -> None:
    post = _valid_post(TM_12V="not-numeric")

    result = urc.perform_hk_check(post=post, hk_type="post")

    assert result["passed"] is False
    assert any("POST conversion error" in detail for detail in result["details"])


def test_perform_hk_check_rejects_unknown_check_type() -> None:
    result = urc.perform_hk_check(hk_type="unknown")

    assert result == {"passed": False, "details": ["Unknown hk_type: unknown"]}
