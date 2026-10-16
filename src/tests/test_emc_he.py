from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts_modules import EMC_HE as emc_he
from widget_modules import ui_runtime_controller


def test_emc_he_verifies_tec_setpoint_from_fresh_hk(monkeypatch):
    events = []
    hk = SimpleNamespace(TEC_SETPOINT=0xC018)

    monkeypatch.setattr(emc_he.eb_interface, "get_egse_interface", lambda: object())
    monkeypatch.setattr(emc_he.ebpu, "get_hk_sequence", lambda: 21)
    monkeypatch.setattr(emc_he.ebtcs, "set_tec_setpoint", Mock())
    monkeypatch.setattr(emc_he.ebtcs, "hk_request", Mock())

    def wait_for_hk(*, timeout, after_sequence):
        events.append((timeout, after_sequence))
        return hk

    monkeypatch.setattr(emc_he.ebpu, "wait_for_fresh_hk", wait_for_hk)

    monkeypatch.setattr(
        ui_runtime_controller,
        "notify_script_pause",
        Mock(side_effect=RuntimeError("stop after setpoint check")),
    )

    with pytest.raises(RuntimeError, match="stop after setpoint check"):
        emc_he.run_emc_he()

    assert events == [(5.0, 21)]


def test_emc_he_fails_if_no_fresh_hk_after_setpoint(monkeypatch):
    monkeypatch.setattr(emc_he.eb_interface, "get_egse_interface", lambda: object())
    monkeypatch.setattr(emc_he.ebpu, "get_hk_sequence", lambda: 21)
    monkeypatch.setattr(emc_he.ebtcs, "set_tec_setpoint", Mock())
    monkeypatch.setattr(emc_he.ebtcs, "hk_request", Mock())
    monkeypatch.setattr(emc_he.ebpu, "wait_for_fresh_hk", Mock(return_value=None))

    with pytest.raises(AssertionError, match="no fresh HK packet"):
        emc_he.run_emc_he()
