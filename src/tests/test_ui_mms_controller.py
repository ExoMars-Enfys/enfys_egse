import asyncio
from types import SimpleNamespace

import pytest

from widget_modules import menu_widget, ui_runtime_controller as urc
from widget_modules import monitoring_limits


class _DummyLogger:
    def __init__(self) -> None:
        self.records = []

    def warning(self, msg, *args):
        self.records.append(("warning", msg % args if args else msg))

    def info(self, msg, *args):
        self.records.append(("info", msg % args if args else msg))

    def error(self, msg, *args):
        self.records.append(("error", msg % args if args else msg))


def test_menu_invoke_passes_ob_port_to_ob_fft_script() -> None:
    calls = {}

    def fake_runner(port, psu_port=None, nopsu=False):
        calls["port"] = port
        calls["psu_port"] = psu_port
        calls["nopsu"] = nopsu

    state = {"ob_port": object(), "psu_port": object(), "nopsu": True}
    menu_widget._invoke_script_entry_point(fake_runner, state)

    assert calls["port"] is state["ob_port"]
    assert calls["psu_port"] is state["psu_port"]
    assert calls["nopsu"] is True


def test_mms_reasons_masks_ob_general_error(monkeypatch) -> None:
    hk = SimpleNamespace(
        INSTRUMENT_STATUS_FLAGS=(1 << 10),
        CURRENT_OPERATING_STATE=0x04,
        POST_ERROR_FLAGS=0,
        ERROR_FLAGS=1,
        ERROR_FLAGS_BITS=SimpleNamespace(OB_GENERAL_ERROR=1, RESERVED=0),
        ERRORS=None,
        MTR_ERRORS=None,
        OB_LAST_ERROR=0,
        OB_MOTOR_ERROR=0,
    )
    limits = {"eb_12v": (11.0, 13.0)}

    monkeypatch.setattr(urc, "decoded", lambda packet, field: 12.0)
    monkeypatch.setattr(urc.const, "MMS_MASK_OB_GENERAL_ERROR", True)

    reasons, tec_pre_action, ob5v_pre_action = urc.mms_reasons(hk, limits)

    assert reasons == []
    assert tec_pre_action is False
    assert ob5v_pre_action is False


@pytest.mark.parametrize("ob_hk_id", [0, 1])
def test_mms_standby_checks_ob_limits_only_after_ob_hk_arrives(monkeypatch, ob_hk_id) -> None:
    hk = SimpleNamespace(
        INSTRUMENT_STATUS_FLAGS=1 << 10,
        CURRENT_OPERATING_STATE=0x04,
        OB_HK_ID=ob_hk_id,
        POST_ERROR_FLAGS=0,
        ERROR_FLAGS=0,
    )
    monkeypatch.setattr(urc.const, "MMS_MASK_OB_LIMIT_CHECKS", False)
    monkeypatch.setattr(urc.const, "MMS_EB_ONLY_TEST_MODE", False)
    monkeypatch.setattr(
        urc, "decoded",
        lambda _packet, field: (-255.7248 if field.endswith("_TRP") else 0.0)
        if field.startswith("OB_") else None,
    )

    reasons, tec, ob5v = urc.mms_reasons(hk, monitoring_limits.mms_alarm_limits())

    assert len(reasons) == (6 if ob_hk_id else 0)
    assert all(reason.startswith("OB ") for reason in reasons)
    assert tec is False
    assert ob5v is bool(ob_hk_id)


@pytest.mark.parametrize(
    "fault, expected",
    [
        ("eb_limit", "EB +12V out of limits"),
        ("post_error", "POST Error Flags asserted"),
        ("ob_unresponsive", "HK Error Flags asserted: OB_UNRESPONSIVE"),
        ("ob_error", "OB_LAST_ERROR=0x10"),
        ("motor_error", "OB_MOTOR_ERROR=0x04"),
    ],
)
def test_mms_faults_remain_active_before_first_ob_hk(monkeypatch, fault, expected) -> None:
    hk = SimpleNamespace(
        INSTRUMENT_STATUS_FLAGS=1 << 10,
        CURRENT_OPERATING_STATE=0x04,
        OB_HK_ID=0,
        POST_ERROR_FLAGS=int(fault == "post_error"),
        ERROR_FLAGS=int(fault == "ob_unresponsive"),
        ERROR_FLAGS_BITS=SimpleNamespace(OB_UNRESPONSIVE=int(fault == "ob_unresponsive")),
        OB_LAST_ERROR=0x10 if fault == "ob_error" else 0,
        OB_MOTOR_ERROR=0x04 if fault == "motor_error" else 0,
    )
    monkeypatch.setattr(urc.const, "MMS_MASK_OB_LIMIT_CHECKS", False)
    monkeypatch.setattr(urc.const, "MMS_MASK_OB_UNRESPONSIVE", False)
    monkeypatch.setattr(urc.const, "MMS_EB_ONLY_TEST_MODE", False)
    monkeypatch.setattr(
        urc, "decoded",
        lambda _packet, field: 0.0 if field.startswith("OB_") else (
            0.0 if fault == "eb_limit" and field == "EB_MEAS_MAIN_12V" else None
        ),
    )

    reasons, _, ob5v = urc.mms_reasons(hk, monitoring_limits.mms_alarm_limits())

    assert len(reasons) == 1
    assert reasons[0].startswith(expected)
    assert ob5v is False


def test_mms_reasons_adds_ob_error_details_even_without_active_bits(monkeypatch) -> None:
    hk = SimpleNamespace(
        INSTRUMENT_STATUS_FLAGS=(1 << 10),
        CURRENT_OPERATING_STATE=0x04,
        POST_ERROR_FLAGS=0,
        ERROR_FLAGS=0,
        ERROR_FLAGS_BITS=None,
        ERRORS=SimpleNamespace(UNUSED1=0, UNUSED2=0),
        MTR_ERRORS=SimpleNamespace(UNUSED=0),
        OB_LAST_ERROR=0x10,
        OB_MOTOR_ERROR=0x04,
    )
    limits = {}

    monkeypatch.setattr(urc, "decoded", lambda packet, field: 0.0)

    reasons, _, _ = urc.mms_reasons(hk, limits)

    assert "OB_LAST_ERROR=0x10 (no active bits decoded)" in reasons
    assert "OB_MOTOR_ERROR=0x04 (no active bits decoded)" in reasons


def test_mms_runs_actions_and_latches(monkeypatch) -> None:
    async def _io_bound(func):
        return func()

    logger = _DummyLogger()
    calls = {
        "abort": 0,
        "clear_pause": 0,
        "clear_force_pause": 0,
        "disable_ob5v": 0,
        "safe": 0,
        "ret": 0,
        "shutdown": 0,
    }

    class _Interface:
        def wait_for_safe_state(self, *_args, **_kwargs):
            return True

    app = SimpleNamespace(state=SimpleNamespace(eb_interface=SimpleNamespace(rs422_log_path="fake.log")))
    state = {"mode": "OB", "psu_port": object(), "mms": {}}
    hk = SimpleNamespace(CURRENT_OPERATING_STATE=0x04)

    monkeypatch.setattr(urc.run, "io_bound", _io_bound)
    monkeypatch.setattr(urc, "is_script_running", lambda: True)
    monkeypatch.setattr(urc, "request_abort", lambda: calls.__setitem__("abort", calls["abort"] + 1))
    monkeypatch.setattr(urc, "clear_pause", lambda: calls.__setitem__("clear_pause", calls["clear_pause"] + 1))
    monkeypatch.setattr(
        urc,
        "clear_force_pause",
        lambda: calls.__setitem__("clear_force_pause", calls["clear_force_pause"] + 1),
    )
    monkeypatch.setattr(
        urc, "disable_ob5v", lambda _logger: calls.__setitem__("disable_ob5v", calls["disable_ob5v"] + 1)
    )
    monkeypatch.setattr(urc.eb_interface, "get_egse_interface", lambda: _Interface())
    monkeypatch.setattr(
        urc.ebtcs, "safe", lambda *_args, **_kwargs: calls.__setitem__("safe", calls["safe"] + 1) or "OK"
    )
    monkeypatch.setattr(
        urc.ebtcs,
        "ret",
        lambda *_args, **_kwargs: calls.__setitem__("ret", calls["ret"] + 1) or "OK",
    )
    monkeypatch.setattr(
        urc.psu,
        "shutdown_psu_outputs",
        lambda *_args, **_kwargs: calls.__setitem__("shutdown", calls["shutdown"] + 1),
    )
    monkeypatch.setattr(urc.time, "sleep", lambda *_args, **_kwargs: None)

    asyncio.run(
        urc.mms(
            app=app,
            state=state,
            logger=logger,
            hk=hk,
            reasons=["EB +12V out of limits"],
            tec_pre_action=True,
            ob5v_pre_action=True,
        )
    )

    assert calls["abort"] == 1
    assert calls["clear_pause"] == 1
    assert calls["clear_force_pause"] == 1
    assert calls["disable_ob5v"] == 1
    assert calls["safe"] == 1
    assert calls["ret"] == 1
    assert calls["shutdown"] == 1

    mms_cfg = state["mms"]
    assert mms_cfg["latched"] is True
    assert mms_cfg["in_progress"] is False
    assert mms_cfg["tec_shutdown_requested"] is True
    assert mms_cfg["ob5v_disable_requested"] is True
    assert mms_cfg["mode_at_trigger"] == "OB"


def test_mms_returns_early_when_latched(monkeypatch) -> None:
    async def _io_bound(func):
        return func()

    logger = _DummyLogger()
    state = {"mms": {"latched": True}}

    monkeypatch.setattr(urc.run, "io_bound", _io_bound)

    asyncio.run(
        urc.mms(
            app=SimpleNamespace(state=SimpleNamespace(eb_interface=SimpleNamespace(rs422_log_path=None))),
            state=state,
            logger=logger,
            hk=SimpleNamespace(CURRENT_OPERATING_STATE=0x02),
            reasons=["x"],
            tec_pre_action=False,
            ob5v_pre_action=False,
        )
    )

    assert state["mms"] == {"latched": True}


# ---------------------------------------------------------------------------
# Additional MMS de-duplication, fallback, and failure-path coverage
# ---------------------------------------------------------------------------


def _app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(eb_interface=SimpleNamespace(rs422_log_path=None)))


def _run_mms(state, logger, hk, *, reasons=None, tec=False, ob5v=False) -> None:
    asyncio.run(
        urc.mms(
            app=_app(),
            state=state,
            logger=logger,
            hk=hk,
            reasons=list(reasons or ["test trigger"]),
            tec_pre_action=tec,
            ob5v_pre_action=ob5v,
        )
    )


def _patch_direct_mms_runner(monkeypatch) -> None:
    async def _io_bound(func):
        return func()

    monkeypatch.setattr(urc.run, "io_bound", _io_bound)
    monkeypatch.setattr(urc.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(urc, "is_script_running", lambda: False)


def test_mms_returns_early_when_already_in_progress(monkeypatch) -> None:
    async def _unexpected_io_bound(_func):
        raise AssertionError("io_bound must not run for an in-progress MMS")

    state = {"mms": {"in_progress": True}}
    monkeypatch.setattr(urc.run, "io_bound", _unexpected_io_bound)

    _run_mms(state, _DummyLogger(), SimpleNamespace(CURRENT_OPERATING_STATE=0x04))

    assert state == {"mms": {"in_progress": True}}


def test_mms_without_running_script_skips_abort_and_handles_missing_psu(monkeypatch) -> None:
    _patch_direct_mms_runner(monkeypatch)
    logger = _DummyLogger()
    state = {"mode": "OB", "mms": {}}

    monkeypatch.setattr(urc, "request_abort", lambda: (_ for _ in ()).throw(AssertionError("unexpected abort")))
    monkeypatch.setattr(urc, "clear_pause", lambda: (_ for _ in ()).throw(AssertionError("unexpected clear")))
    monkeypatch.setattr(urc, "clear_force_pause", lambda: (_ for _ in ()).throw(AssertionError("unexpected clear")))
    monkeypatch.setattr(urc.eb_interface, "get_egse_interface", lambda: object())
    monkeypatch.setattr(urc.ebtcs, "safe", lambda *_args, **_kwargs: "ERROR")

    _run_mms(state, logger, SimpleNamespace(CURRENT_OPERATING_STATE=0x04))

    assert state["mms"]["latched"] is True
    assert state["mms"]["in_progress"] is False
    assert any("PSU emergency shutdown skipped" in message for level, message in logger.records if level == "warning")


def test_mms_safe_command_failure_still_attempts_psu_shutdown(monkeypatch) -> None:
    _patch_direct_mms_runner(monkeypatch)
    calls = {"ret": 0, "shutdown": 0}
    state = {"mode": "OB", "psu_port": object(), "mms": {}}

    monkeypatch.setattr(urc.eb_interface, "get_egse_interface", lambda: object())
    monkeypatch.setattr(urc.ebtcs, "safe", lambda *_args, **_kwargs: "ERROR")
    monkeypatch.setattr(urc.ebtcs, "ret", lambda *_args, **_kwargs: calls.__setitem__("ret", calls["ret"] + 1))
    monkeypatch.setattr(
        urc.psu,
        "shutdown_psu_outputs",
        lambda *_args, **_kwargs: calls.__setitem__("shutdown", calls["shutdown"] + 1),
    )

    _run_mms(state, _DummyLogger(), SimpleNamespace(CURRENT_OPERATING_STATE=0x04))

    assert calls["ret"] == 0
    assert calls["shutdown"] == 1
    assert state["mms"]["latched"] is True


def test_mms_ret_command_failure_still_attempts_psu_shutdown(monkeypatch) -> None:
    _patch_direct_mms_runner(monkeypatch)
    calls = {"shutdown": 0}
    state = {"mode": "OB", "psu_port": object(), "mms": {}}

    monkeypatch.setattr(urc.eb_interface, "get_egse_interface", lambda: object())
    monkeypatch.setattr(urc.ebtcs, "safe", lambda *_args, **_kwargs: "OK")
    monkeypatch.setattr(urc.ebtcs, "ret", lambda *_args, **_kwargs: "ERROR")
    monkeypatch.setattr(
        urc.psu,
        "shutdown_psu_outputs",
        lambda *_args, **_kwargs: calls.__setitem__("shutdown", calls["shutdown"] + 1),
    )

    _run_mms(state, _DummyLogger(), SimpleNamespace(CURRENT_OPERATING_STATE=0x04))

    assert calls["shutdown"] == 1
    assert state["mms"]["latched"] is True


def test_mms_safe_state_skips_ob5v_disable_pre_action(monkeypatch) -> None:
    _patch_direct_mms_runner(monkeypatch)
    logger = _DummyLogger()
    calls = {"disable": 0}
    state = {"mode": "OB", "mms": {}}

    monkeypatch.setattr(
        urc,
        "disable_ob5v",
        lambda _logger: calls.__setitem__("disable", calls["disable"] + 1),
    )
    monkeypatch.setattr(urc.eb_interface, "get_egse_interface", lambda: object())
    monkeypatch.setattr(urc.ebtcs, "safe", lambda *_args, **_kwargs: "ERROR")

    _run_mms(
        state,
        logger,
        SimpleNamespace(CURRENT_OPERATING_STATE=0x02),
        ob5v=True,
    )

    assert calls["disable"] == 0
    assert any("OB 5V disable skipped" in message for level, message in logger.records if level == "info")


def test_mms_unconfirmed_safe_state_still_shuts_down_psu(monkeypatch) -> None:
    _patch_direct_mms_runner(monkeypatch)
    logger = _DummyLogger()
    calls = {"shutdown": 0}

    class _Interface:
        def wait_for_safe_state(self, *_args, **_kwargs):
            return False

    state = {"mode": "OB", "psu_port": object(), "mms": {}}
    monkeypatch.setattr(urc.eb_interface, "get_egse_interface", lambda: _Interface())
    monkeypatch.setattr(urc.ebtcs, "safe", lambda *_args, **_kwargs: "OK")
    monkeypatch.setattr(urc.ebtcs, "ret", lambda *_args, **_kwargs: "OK")
    monkeypatch.setattr(
        urc.psu,
        "shutdown_psu_outputs",
        lambda *_args, **_kwargs: calls.__setitem__("shutdown", calls["shutdown"] + 1),
    )

    _run_mms(state, logger, SimpleNamespace(CURRENT_OPERATING_STATE=0x04))

    assert calls["shutdown"] == 1
    assert any("SAFE state was not confirmed" in message for level, message in logger.records if level == "warning")


def test_mms_psu_shutdown_exception_is_logged_but_trigger_stays_latched(monkeypatch) -> None:
    _patch_direct_mms_runner(monkeypatch)
    logger = _DummyLogger()
    state = {"mode": "OB", "psu_port": object(), "mms": {}}

    monkeypatch.setattr(urc.eb_interface, "get_egse_interface", lambda: object())
    monkeypatch.setattr(urc.ebtcs, "safe", lambda *_args, **_kwargs: "ERROR")
    monkeypatch.setattr(
        urc.psu,
        "shutdown_psu_outputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("PSU failed")),
    )

    _run_mms(state, logger, SimpleNamespace(CURRENT_OPERATING_STATE=0x04))

    assert state["mms"]["latched"] is True
    assert state["mms"]["in_progress"] is False
    assert any("PSU failed" in message for level, message in logger.records if level == "error")


def test_mms_io_bound_failure_records_error_and_clears_in_progress(monkeypatch) -> None:
    async def _failing_io_bound(_func):
        raise RuntimeError("worker failed")

    state = {"mms": {}}
    monkeypatch.setattr(urc.run, "io_bound", _failing_io_bound)

    with pytest.raises(RuntimeError, match="worker failed"):
        _run_mms(state, _DummyLogger(), SimpleNamespace(CURRENT_OPERATING_STATE=0x04))

    assert state["mms"]["last_error"] == "worker failed"
    assert state["mms"]["in_progress"] is False
