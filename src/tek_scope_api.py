from __future__ import annotations

import csv
import math
import re
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

_SCOPE_SESSION_LOCK = threading.Lock()


class ScopeConnectionError(RuntimeError):
    """Raised when a VISA scope cannot be connected or controlled."""


class TekScope:
    """Small PyVISA interface for Tektronix MSO 4 Series single-shot captures."""

    SETUP_RECALL_TIMEOUT_MS = 60_000

    def __init__(self, resource: str, *, timeout_ms: int = 10_000) -> None:
        self.resource = str(resource).strip()
        self.timeout_ms = int(timeout_ms)
        self._manager: Any = None
        self._instrument: Any = None
        self._idn: str | None = None
        self._trigger_source: str | None = None
        self._trigger_unit: str | None = None
        self._scope_lock_acquired = False

    def connect(self) -> TekScope:
        if not self.resource:
            raise ScopeConnectionError("A VISA resource string is required")
        _SCOPE_SESSION_LOCK.acquire()
        self._scope_lock_acquired = True
        try:
            import pyvisa

            self._manager = pyvisa.ResourceManager("@py")
            self._instrument = self._manager.open_resource(self.resource)
            self._instrument.timeout = self.timeout_ms
            self._instrument.read_termination = "\n"
            self._instrument.write_termination = "\n"
            self._instrument.clear()
            self._idn = self._read_idn()
        except Exception as exc:
            self.close()
            raise ScopeConnectionError(f"Could not connect to scope at {self.resource}: {exc}") from exc
        if "TEKTRONIX" not in self._idn.upper():
            idn = self._idn
            self.close()
            raise ScopeConnectionError(f"Expected a Tektronix scope, received: {idn}")
        return self

    def _read_idn(self) -> str:
        """Read the scope identity once after clearing any stale VISA reply."""
        return self.query("*IDN?")

    def __enter__(self) -> TekScope:
        return self.connect()

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._instrument is not None:
                try:
                    self._instrument.close()
                finally:
                    self._instrument = None
            if self._manager is not None:
                try:
                    self._manager.close()
                finally:
                    self._manager = None
        finally:
            if self._scope_lock_acquired:
                self._scope_lock_acquired = False
                _SCOPE_SESSION_LOCK.release()

    def _require_connection(self) -> Any:
        if self._instrument is None:
            raise ScopeConnectionError("Scope is not connected")
        return self._instrument

    def write(self, command: str) -> None:
        try:
            self._require_connection().write(command)
        except Exception as exc:
            raise ScopeConnectionError(f"Scope command failed ({command}): {exc}") from exc

    def query(self, command: str) -> str:
        try:
            return str(self._require_connection().query(command)).strip()
        except Exception as exc:
            raise ScopeConnectionError(f"Scope query failed ({command}): {exc}") from exc

    def idn(self) -> str:
        return self._idn or self.query("*IDN?")

    def recall_setup(self, setup_file: str | Path) -> None:
        """Recall a local Tek setup archive or a setup file stored on the scope."""
        path = str(setup_file).strip()
        if not path:
            raise ScopeConnectionError("A scope setup file is required")
        if any(char in path for char in ('"', "\r", "\n")):
            raise ValueError("Scope setup path cannot contain quotes or line breaks")
        instrument = self._require_connection()
        previous_timeout = instrument.timeout
        try:
            instrument.timeout = max(int(previous_timeout), self.SETUP_RECALL_TIMEOUT_MS)
            local_path = Path(path)
            if local_path.is_file():
                if zipfile.is_zipfile(local_path):
                    with zipfile.ZipFile(local_path) as archive:
                        setup_files = [
                            entry for entry in archive.infolist()
                            if entry.filename.lower().endswith("_lrn.set")
                        ]
                        if len(setup_files) != 1:
                            raise ScopeConnectionError(
                                f"Expected one Tektronix learn file in setup archive {local_path}; "
                                f"found {len(setup_files)}"
                            )
                        commands = archive.read(setup_files[0]).decode("utf-8-sig").splitlines()
                else:
                    commands = local_path.read_text(encoding="utf-8-sig").splitlines()
                for command in commands:
                    if command.strip():
                        self.write(command)
            else:
                self.write(f'RECAll:SETUp "{path}"')
            response = self.query("*OPC?")
            if response != "1":
                raise ScopeConnectionError(
                    f"Scope did not finish recalling setup file: {path} (completion response: {response!r})"
                )
        finally:
            instrument.timeout = previous_timeout

    def configure_single_rising_trigger(self) -> dict[str, Any]:
        """Use the recalled current-probe source, whose configured units are amperes."""
        self.write("ACQuire:STATE OFF")
        self.write("TRIGger:A:TYPe EDGE")
        self.write("TRIGger:A:EDGE:SLOpe RISe")
        source = self.query("TRIGger:A:EDGE:SOUrce?").strip().upper()
        if not re.fullmatch(r"CH[1-4]", source):
            raise ScopeConnectionError(
                f"Scope setup trigger source is {source!r}; select the current-probe channel in the setup file"
            )

        active_channels = []
        for channel_number in range(1, 5):
            channel = f"CH{channel_number}"
            if self.query(f"SELect:{channel}?").strip() in {"1", "ON", "TRUE"}:
                active_channels.append(channel)
        if len(active_channels) < 2:
            raise ScopeConnectionError(
                f"Scope setup must display voltage and current channels; enabled channels: {active_channels}"
            )
        if source not in active_channels:
            raise ScopeConnectionError(f"Trigger source {source} is not enabled in the recalled setup")

        self.write(f'{source}:PROBEFUNC:EXTUNITS "A"')
        self.write(f"{source}:PROBEFUNC:EXTUNITS:STATE 1")
        self.write("ACQuire:STOPAfter SEQuence")
        self._trigger_source = source
        self._trigger_unit = "A"
        return {"trigger_source": source, "active_channels": active_channels}

    def configure_inrush_capture(self, timebase_s: float) -> None:
        """Configure the Off-to-Safe in-rush view: CH1 at 2 A/div and rising trigger."""
        timebase = float(timebase_s)
        if timebase <= 0:
            raise ValueError("In-rush timebase must be positive")
        self.write("ACQuire:STATE OFF")
        if self.query("SELect:CH1?").strip() not in {"1", "ON", "TRUE"}:
            raise ScopeConnectionError("CH1 must be enabled for the Off-to-Safe current capture")

        self.write("TRIGger:A:TYPe EDGE")
        self.write("TRIGger:A:EDGE:SOUrce CH1")
        self.write("TRIGger:A:EDGE:SLOpe RISe")
        self.write("ACQuire:STOPAfter SEQuence")
        self.write('CH1:PROBEFUNC:EXTUNITS "A"')
        self.write("CH1:PROBEFUNC:EXTUNITS:STATE 1")
        self.write("CH1:SCAle 2")
        self.write(f"HORIZONTAL:SCALE {timebase:.9g}")
        self._trigger_source = "CH1"
        self._trigger_unit = "A"

        if self.query("*OPC?") != "1":
            raise ScopeConnectionError("Oscilloscope did not complete Off-to-Safe capture configuration")

    def configure_mode_transition_capture(
        self,
        *,
        timebase_s: float | None,
        current_scale_ma_per_div: float | None,
    ) -> None:
        """Apply specified mode-transition axes, preserving any unspecified axis."""
        if timebase_s is None and current_scale_ma_per_div is None:
            raise ValueError("At least one mode-transition axis setting is required")
        if timebase_s is not None and (not math.isfinite(timebase_s) or timebase_s <= 0):
            raise ValueError("Mode-transition timebase must be finite and positive")
        if current_scale_ma_per_div is not None and (
            not math.isfinite(current_scale_ma_per_div) or current_scale_ma_per_div <= 0
        ):
            raise ValueError("Mode-transition current scale must be finite and positive")

        self.write("ACQuire:STATE OFF")
        if current_scale_ma_per_div is not None:
            if self._trigger_source is None:
                raise ScopeConnectionError(
                    "Configure the current trigger source before setting the mode-transition current scale"
                )
            source = self._trigger_source
            if self.query(f"SELect:{source}?").strip() not in {"1", "ON", "TRUE"}:
                raise ScopeConnectionError(
                    f"Mode-transition current channel {source} must be enabled to set its scale"
                )
            unit = self._trigger_unit
            if unit in {"A", "AMP", "AMPS", "AMPERE", "AMPERES"}:
                scale = current_scale_ma_per_div / 1000.0
            elif unit in {"MA", "MILLIAMP", "MILLIAMPS"}:
                scale = current_scale_ma_per_div
            else:
                raise ScopeConnectionError(
                    f"Mode-transition trigger channel {source} has unknown current units {unit!r}; "
                    "configure its probe scaling in A or mA"
                )
            self.write(f"{source}:SCAle {scale:.9g}")

        if timebase_s is not None:
            self.write(f"HORIZONTAL:SCALE {timebase_s:.9g}")
        if self.query("*OPC?") != "1":
            raise ScopeConnectionError("Oscilloscope did not complete mode-transition axis configuration")

    def set_trigger_level_current_ma(self, level_ma: float) -> float:
        """Set the edge-trigger level from mA, using the configured current-probe units."""
        if self._trigger_source is None:
            raise ScopeConnectionError("Configure the scope trigger source before setting its level")
        if float(level_ma) < 0:
            raise ValueError("Rising-current trigger level must be non-negative")

        unit = self._trigger_unit
        if unit in {"A", "AMP", "AMPS", "AMPERE", "AMPERES"}:
            level = float(level_ma) / 1000.0
        elif unit in {"MA", "MILLIAMP", "MILLIAMPS"}:
            level = float(level_ma)
        else:
            raise ScopeConnectionError(
                f"Current trigger channel {self._trigger_source} has unknown current units {unit!r}; "
                "configure its probe scaling in A or mA"
            )

        self.write(f"TRIGger:A:LEVel:{self._trigger_source} {level:.9g}")
        if self.query("*OPC?") != "1":
            raise ScopeConnectionError("Scope did not complete the trigger-level command")
        return level

    def arm_single(self) -> None:
        """Arm a single acquisition without waiting for its future trigger."""
        self.write("ACQuire:STATE OFF")
        self.write("ACQuire:STOPAfter SEQuence")
        self.write("TRIGger:A:EDGE:SLOpe RISe")
        self.write("ACQuire:STATE ON")

    def wait_for_capture(self, *, timeout_s: float = 60.0, poll_s: float = 0.1) -> None:
        """Wait for a single sequence to trigger and stop."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        observed_running = False
        while time.monotonic() < deadline:
            acquire_state = self.query("ACQuire:STATE?").strip().upper()
            if acquire_state in {"1", "ON", "RUN", "RUNNING"}:
                observed_running = True
            elif acquire_state in {"0", "OFF", "STOP", "STOPPED"}:
                trigger_state = self.query("TRIGger:STATE?").strip().upper()
                if observed_running or trigger_state in {"TRIGGER", "TRIGGERED", "SAVE"}:
                    return
            time.sleep(max(0.01, float(poll_s)))
        raise TimeoutError(f"No single-shot scope capture completed within {timeout_s:.1f} seconds")

    def save_waveforms(self, directory: str | Path, label: str, channels: list[str]) -> Path:
        """Transfer enabled channel waveforms and scaling metadata to a CSV file."""
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_.") or "capture"
        output_dir = Path(directory)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{datetime.now():%Y%m%dT%H%M%S_%f}_{safe_label}.csv"

        waveforms: dict[str, tuple[list[float], float, float, float, float, float, float, str]] = {}
        self.write("DATa:ENCdg ASCii")
        for channel in channels:
            if not re.fullmatch(r"CH[1-4]", channel.upper()):
                raise ValueError(f"Invalid scope channel: {channel}")
            self.write(f"DATa:SOUrce {channel.upper()}")
            point_count = int(float(self.query("WFMOutpre:NR_Pt?")))
            if point_count <= 0:
                raise ScopeConnectionError(f"Scope returned an invalid record length for {channel}: {point_count}")
            self.write("DATa:STARt 1")
            self.write(f"DATa:STOP {point_count}")
            response = self.query("CURVe?")
            try:
                raw_values = [float(value) for value in response.split(",") if value.strip()]
            except ValueError as exc:
                raise ScopeConnectionError(f"Could not parse waveform samples from {channel}") from exc
            if len(raw_values) != point_count:
                raise ScopeConnectionError(
                    f"Scope returned {len(raw_values)} samples for {channel}; expected {point_count}"
                )
            xincr = float(self.query("WFMOutpre:XINcr?"))
            xzero = float(self.query("WFMOutpre:XZEro?"))
            xoff = float(self.query("WFMOutpre:PT_Off?"))
            ymult = float(self.query("WFMOutpre:YMUlt?"))
            yzero = float(self.query("WFMOutpre:YZEro?"))
            yoff = float(self.query("WFMOutpre:YOFf?"))
            try:
                unit = self.query("WFMOutpre:YUNit?").strip().strip('"')
            except ScopeConnectionError:
                unit = "scope units"
            waveforms[channel.upper()] = (raw_values, xincr, xzero, xoff, ymult, yzero, yoff, unit)

        first_waveform = next(iter(waveforms.values()))
        if any(len(waveform[0]) != len(first_waveform[0]) for waveform in waveforms.values()):
            raise ScopeConnectionError("Enabled scope channels do not have matching record lengths")
        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_s", *(f"{channel}_value_{data[7]}" for channel, data in waveforms.items())])
            for index in range(len(first_waveform[0])):
                time_s = first_waveform[2] + (index - first_waveform[3]) * first_waveform[1]
                row = [time_s]
                for data in waveforms.values():
                    raw_value = data[0][index]
                    row.append((raw_value - data[6]) * data[4] + data[5])
                writer.writerow(row)
        return output_path


def find_scope() -> list[str]:
    """Discover Tektronix VISA instruments visible to the PyVISA-py backend."""
    try:
        import pyvisa

        manager = pyvisa.ResourceManager("@py")
    except Exception as exc:
        raise ScopeConnectionError(f"PyVISA with the PyVISA-py backend is required: {exc}") from exc
    found: list[str] = []
    try:
        resources = manager.list_resources("TCPIP?*")
        for resource in resources:
            scope = TekScope(resource)
            try:
                scope.connect()
                if "TEKTRONIX" in scope.idn().upper():
                    found.append(str(resource))
            except ScopeConnectionError:
                continue
            finally:
                scope.close()
    finally:
        manager.close()
    return found