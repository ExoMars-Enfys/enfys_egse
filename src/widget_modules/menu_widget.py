from __future__ import annotations

# Std library
import asyncio
import inspect
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module, reload
from pathlib import Path
from typing import Any

# Added packages
from nicegui import app, run, ui

# Local modules
from core_modules import config
from core_modules import constants as const
from utility_modules import eb_interface, ebtcs
from utility_modules.desktop_launcher import destroy_desktop_window
from widget_modules import file_dialog_window_widget, ui_runtime_controller

try:
    from tek_scope_api import ScopeConnectionError, TekScope, find_scope

    _SCOPE_API_AVAILABLE = True
except ImportError:  # tek_scope_api is an optional local package
    _SCOPE_API_AVAILABLE = False


@dataclass
class MenuController:
    button: Any
    card: Any
    _state: dict[str, bool]

    def open(self) -> None:
        self._state["open"] = True
        self.card.style(
            "transform: translateX(0); opacity: 1; pointer-events: auto;"
            "transition: transform .2s ease, opacity .2s ease;"
        )

    def close(self) -> None:
        self._state["open"] = False
        self.card.style(
            "transform: translateX(-120%); opacity: 0; pointer-events: none;"
            "transition: transform .2s ease, opacity .2s ease;"
        )

    def toggle(self, *_: Any) -> None:
        if self._state["open"]:
            self.close()
        else:
            self.open()


def _discover_eb_scripts() -> dict[str, tuple[str, Any]]:
    """Find menu scripts exposing one callable ``run_*`` entry point."""
    scripts: dict[str, tuple[str, Any]] = {}
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts_modules"

    for script_path in sorted(scripts_dir.glob("*.py"), key=lambda path: path.stem.lower()):
        if script_path.stem.startswith("_") or not script_path.stem.isidentifier():
            continue
        if script_path.stem.lower() == "mms_mask_live_test" and not config.SHOW_MMS_TEST_TOOLS:
            continue

        try:
            module = import_module(f"scripts_modules.{script_path.stem}")
        except Exception:
            continue

        entry_point_names = sorted(
            name for name in dir(module) if name.startswith("run_") and callable(getattr(module, name))
        )
        if len(entry_point_names) != 1:
            continue

        scripts[script_path.stem.lower()] = (script_path.stem, getattr(module, entry_point_names[0]))

    return scripts


def _script_uses_ebtc(script_runner: Any) -> bool:
    """Return whether a script entry point has imported the EBTC helper module."""
    return any(
        getattr(value, "__name__", "") == "utility_modules.ebtcs" for value in script_runner.__globals__.values()
    )


def _invoke_script_entry_point(script_runner: Any, state: dict[str, Any]) -> None:
    """Call a script using the active OB/PSU port state when the function requires it."""
    try:
        params = inspect.signature(script_runner).parameters
    except (TypeError, ValueError):
        script_runner()
        return

    kwargs: dict[str, Any] = {}
    for name, parameter in params.items():
        if name == "port":
            kwargs[name] = state.get("ob_port")
        elif name == "psu_port":
            kwargs[name] = state.get("psu_port")
        elif name == "nopsu":
            kwargs[name] = bool(state.get("nopsu", False))
        elif name == "psu_lock":
            kwargs[name] = state.get("psu_lock")
        elif name == "worker":
            kwargs[name] = state.get("ob_worker")
        elif name == "gui_state":
            kwargs[name] = state
        elif name in state and parameter.default is parameter.empty:
            kwargs[name] = state.get(name)

    if kwargs:
        script_runner(**kwargs)
        return
    script_runner()


async def _send_safe_tc(state: dict[str, Any]) -> None:
    def send() -> Any:
        interface = eb_interface.get_egse_interface()
        return ebtcs.safe(interface, 0)

    try:
        result = await run.io_bound(send)
    except Exception:
        state["logger"].exception("Failed to send SAFE TC")
        ui.notify("Failed to send SAFE TC", type="negative")
        return
    if result == "ERROR":
        ui.notify("Failed to send SAFE TC", type="negative")
        return
    ui.notify("SAFE TC sent", type="positive")


def create_menu(
    state: dict[str, Any],
    *,
    set_mode_fn: Any | None = None,
    set_psu_log_path_fn: Any | None = None,
    get_psu_sample_count_fn: Any | None = None,
) -> MenuController:
    """Creates the menu for the application. The menu contains buttons to start/stop EB EGSE tools, select a log file, and take a log snapshot."""
    menu_state = {"open": False}
    state.setdefault("egse_tools_started", bool(getattr(app.state.eb_interface, "egse_started", False)))
    with ui.element("div").classes("relative inline-block"):
        menu_button = ui.button(icon="menu").props("flat dense round").classes("self-start rounded-full")

        with ui.card().classes("absolute left-0 top-10 z-30 shadow-xl rounded-xl w-max max-w-none") as menu_card:
            with ui.column().classes("w-full gap-2 whitespace-nowrap"):
                replay = state.get("ob_replay")
                if replay is not None:
                    menu_card.style("max-height: 80vh; overflow-y: auto;")
                    ui.label("OFFLINE OB REPLAY - no hardware").classes("text-warning")
                    replay_path = ui.input("OB run folder or CMD log", value=str(replay.source)).classes("w-full")
                    ui.label().bind_text_from(replay, "progress").classes("egse-metric-label")
                    ui.label().bind_text_from(replay, "last_error", backward=lambda value: value or "").classes(
                        "text-negative"
                    )

                    async def load_replay() -> None:
                        if ui_runtime_controller.is_script_running():
                            ui.notify("Stop the running script before loading/restarting replay", type="warning")
                            return
                        cyclic = state.get("cyclic_hk")
                        if cyclic is not None and cyclic.enabled:
                            ui.notify("Disable cyclic HK before loading/restarting replay", type="warning")
                            return

                        def load() -> None:
                            with state["port_lock"], state["psu_lock"]:
                                replay.load(replay_path.value)

                        try:
                            await run.io_bound(load)
                        except Exception as exc:
                            state["logger"].exception("Could not load OB replay")
                            ui.notify(str(exc), type="negative")
                            return
                        ui_runtime_controller.reset_ob_fdir_simulator(state)
                        model_select.set_value(replay.model_name)
                        config.set_expected_model_id(replay.model_name)
                        state["model"] = replay.model_name
                        ui.notify("Offline recording loaded; run the same script as the recorded run", type="positive")

                    ui.button("Load / restart OB replay", on_click=load_replay).classes("w-full")
                # --- SAFE TC handler ---
                async def send_safe_tc():
                    await _send_safe_tc(state)

                with ui.row().classes("items-center justify-start gap-2"):
                    ui.label("OB").classes("egse-metric-label")
                    mode_switch = ui.switch(
                        value=(state["mode"] == "EB"),
                        on_change=lambda e: _call_set_mode(set_mode_fn, "EB" if e.value else "OB"),
                    )
                    if replay is not None:
                        mode_switch.disable()
                    ui.label("EB").classes("egse-metric-label")

                    def _current_theme() -> str:
                        return getattr(app.state, "theme_state", {}).get("value", "dark")

                    def _theme_icon(theme: str) -> str:
                        # Show the icon for the target theme when pressed.
                        return "light_mode" if theme == "dark" else "brightness_2"

                    def _theme_icon_color(_theme: str) -> str:
                        # Keep icon color sourced from active theme tokens.
                        return "var(--text-color)"

                    theme_button = ui.button(icon=_theme_icon(_current_theme())).props("flat dense round")
                    theme_button.props("text-color=grey-7")
                    theme_button.style(f"color: {_theme_icon_color(_current_theme())};")

                    def _toggle_theme() -> None:
                        next_theme = "light" if _current_theme() == "dark" else "dark"
                        getattr(app.state, "set_theme", lambda _theme: None)(next_theme)
                        theme_button.props(f"icon={_theme_icon(next_theme)}")
                        theme_button.props("text-color=grey-7")
                        theme_button.style(f"color: {_theme_icon_color(next_theme)};")

                    theme_button.on_click(_toggle_theme)
                    ui.label("Real").classes("egse-metric-label")
                    ui.switch(
                        value=(str(state.get("hk_display_mode", "REAL")).upper() == "ADU"),
                        on_change=lambda e: _set_hk_display_mode(state, "ADU" if e.value else "REAL"),
                    )
                    ui.label("ADU").classes("egse-metric-label")
                model_options = const.MODELS
                model_labels = list(model_options)

                def on_model_change(e):
                    state["model"] = e.value
                    app.state.current_model = e.value
                    config.set_expected_model_id(e.value)

                def _on_mms_toggle(e: Any) -> None:
                    enabled = bool(e.value)
                    mms_cfg = state.setdefault("mms", {})
                    mms_cfg["enabled"] = enabled
                    if not enabled:
                        # Clear all latches so no pending protection actions fire while disabled.
                        mms_cfg["latched"] = False
                        mms_cfg["in_progress"] = False
                        mms_cfg["pending"] = False
                        ui_runtime_controller.reset_ob_fdir_simulator(state)
                    ui.notify(f"MMS {'enabled' if enabled else 'disabled'}", type="warning" if enabled else "info")

                with ui.row().classes("items-center justify-start gap-2 w-full"):
                    ui.label("MMS").classes("egse-metric-label")
                    ui.switch(
                        value=bool(state.get("mms", {}).get("enabled", True)),
                        on_change=_on_mms_toggle,
                    )
                    lbl_mms_state = ui.label("Enabled" if state.get("mms", {}).get("enabled", True) else "Disabled")
                    lbl_mms_state.classes("egse-metric-label")
                    ui.button(
                        "Reset Latch",
                        on_click=lambda: (
                            ui_runtime_controller.reset_ob_fdir_simulator(state),
                            ui.notify("Protection latch cleared — PSU can be reopened", type="info"),
                        ),
                        color="orange",
                    ).props("unelevated").classes("whitespace-nowrap px-3")

                if config.SHOW_MMS_TEST_TOOLS:
                    with ui.row().classes("items-center gap-2 w-full"):
                        def _on_eb_only_toggle(e: Any) -> None:
                            try:
                                ui_runtime_controller.set_eb_only_test_mode(state, bool(e.value))
                            except RuntimeError as exc:
                                eb_only_switch.set_value(const.MMS_EB_ONLY_TEST_MODE)
                                state["logger"].warning("Could not change EB-only test mode: %s", exc)
                                ui.notify(str(exc), type="negative")
                                return
                            ui.notify(
                                "EB-only / no-OB test mode: OB checks masked; EB MMS protection unchanged."
                                if e.value else "EB-only test mode off: normal MMS checks restored.",
                                type="warning",
                            )

                        eb_only_switch = ui.switch(
                            "EB-only / no-OB test mode",
                            value=const.MMS_EB_ONLY_TEST_MODE,
                            on_change=_on_eb_only_toggle,
                        )
                    ui.label(
                        "Test mode masks OB limits, OB_UNRESPONSIVE and RS485 errors only. "
                        "Keep MMS ON. Disable test mode before normal OB operation."
                    ).classes("egse-metric-label text-warning")

                with ui.row().classes("items-center gap-2 w-full"):
                    model_select = ui.select(
                        model_labels,
                        value=state.get("model", model_labels[0]),
                        label="Select Model",
                        on_change=on_model_change,
                    ).classes("flex-1")
                    if replay is not None:
                        model_select.disable()
                    ui.select(
                        options=["MIN", "NOM", "MAX"],
                        value=state.get("voltage_mode", "NOM"),
                        label="Bus V",
                        on_change=lambda e: _on_voltage_mode_change(e, state),
                    ).classes("w-24")
                # Initialize state and app.state with default model if not set
                if "model" not in state:
                    state["model"] = model_labels[0]
                if not hasattr(app.state, "current_model"):
                    app.state.current_model = state["model"]

                # --- Unified two-column button layout ---
                with ui.row().classes("gap-2 w-full no-wrap") as eb_only_buttons_row:
                    with ui.column().classes("gap-2 w-full"):
                        ui.button(
                            "Start",
                            on_click=lambda: _start_egse_tools(state, None),
                        ).classes("w-full whitespace-nowrap rounded-full")
                        ui.button(
                            "Log",
                            on_click=lambda: _select_rs422_log_async(state),
                        ).classes("w-full whitespace-nowrap rounded-full")
                        ui.button(
                            "TXT Script",
                            on_click=lambda: _run_txt_script(state),
                        ).classes("w-full whitespace-nowrap rounded-full")
                    with ui.column().classes("gap-2 w-full"):
                        ui.button(
                            "Stop",
                            on_click=lambda: _stop_egse_tools(state, None),
                        ).classes("w-full whitespace-nowrap rounded-full")
                        ui.button(
                            "PSU Log",
                            on_click=lambda: _select_psu_log(
                                state,
                                set_psu_log_path_fn=set_psu_log_path_fn,
                                get_psu_sample_count_fn=get_psu_sample_count_fn,
                            ),
                        ).classes("w-full whitespace-nowrap rounded-full")
                        ui.button(
                            "Send SAFE TC",
                            on_click=send_safe_tc,
                        ).classes("w-full whitespace-nowrap rounded-full")

                ui.button(
                    "Scope Settings",
                    on_click=_open_scope_settings_dialog,
                ).classes("w-full whitespace-nowrap rounded-full")

                # --- Script selection and controls ---
                script_options = sorted(_discover_eb_scripts().items())
                script_labels = [label for _, (label, _) in script_options]
                script_keys = {label: key for key, (label, _) in script_options}
                selected_script = ui.select(
                    script_labels,
                    value=script_labels[0] if script_labels else None,
                    label="Select Script",
                ).classes("w-full")

                def _refresh_script_options() -> None:
                    nonlocal script_labels, script_keys
                    current_label = selected_script.value
                    script_options = sorted(_discover_eb_scripts().items())
                    script_labels = [label for _, (label, _) in script_options]
                    script_keys = {label: key for key, (label, _) in script_options}
                    next_value = (
                        current_label
                        if current_label in script_labels
                        else (script_labels[0] if script_labels else None)
                    )
                    selected_script.set_options(script_labels, value=next_value)
                    ui.notify(f"Scripts refreshed ({len(script_labels)} found)")

                def get_selected_key():
                    val = selected_script.value
                    if val in script_keys:
                        return script_keys[val]
                    return script_keys[script_labels[0]] if script_labels else ""

                ui.add_css(".q-tooltip { font-size: 1.1rem !important; }")

                with ui.row().classes("w-full justify-end gap-2") as script_buttons_row:

                    def _pause_click(e: Any = None) -> None:
                        _pause_selected_script(state, get_selected_key())

                    def _resume_click(e: Any = None) -> None:
                        _resume_selected_script(state, get_selected_key())

                    def _stop_click(e: Any = None) -> None:
                        _abort_selected_script(state, get_selected_key())

                    ui.button(
                        icon="arrow_forward",
                        on_click=lambda: (_run_selected_script(state, get_selected_key(), script_buttons_row)),
                    ).props("flat round dense").classes("rounded-full w-16 h-12").tooltip("Run selected script")
                    ui.button(icon="pause", on_click=_pause_click).props("flat round dense").classes(
                        "rounded-full w-16 h-12"
                    ).tooltip("Pause running script")
                    ui.button(icon="play_arrow", on_click=_resume_click).props("flat round dense").classes(
                        "rounded-full w-16 h-12"
                    ).tooltip("Resume paused script")
                    ui.button(icon="stop_circle", on_click=_stop_click).props("flat round dense").classes(
                        "rounded-full w-16 h-12"
                    ).tooltip("Stop running script")
                    ui.button(icon="refresh", on_click=_refresh_script_options).props("flat round dense").classes(
                        "rounded-full w-16 h-12"
                    ).tooltip("Refresh EB scripts")

                # (Removed duplicate Run Text Script button)

                ui.button("Log Snapshot", on_click=lambda: _log_snapshot(state)).classes(
                    "w-full whitespace-nowrap rounded-full w-36 h-12"
                )
                ui.button(
                    "Shutdown", color="negative", on_click=lambda: stop_and_shutdown(state, state["stop_event"])
                ).classes("w-full whitespace-nowrap rounded-full w-36 h-12")

    def _sync_egse_tools_buttons(mode: str) -> None:
        """Show EB-only tool buttons in EB mode and hide them in OB mode."""
        if str(mode).upper() == "EB":
            eb_only_buttons_row.classes(remove="hidden")
        else:
            eb_only_buttons_row.classes(add="hidden")

    if isinstance(state.get("plot_refreshers"), list):
        state["plot_refreshers"].append(_sync_egse_tools_buttons)
    _sync_egse_tools_buttons(state.get("mode", "EB"))

    controller = MenuController(button=menu_button, card=menu_card, _state=menu_state)
    controller.close()
    menu_button.on_click(controller.toggle)
    return controller


def _call_set_mode(set_mode_fn: Any | None, mode: str) -> None:
    handler = set_mode_fn if callable(set_mode_fn) else getattr(app.state, "set_egse_mode", None)
    if callable(handler):
        handler(mode)


async def _on_voltage_mode_change(e: Any, state: dict[str, Any]) -> None:
    """Handle voltage mode change and apply to PSU without blocking the UI event loop."""
    from contextlib import nullcontext

    from nicegui import run

    from utility_modules import psu

    new_mode = e.value
    state["voltage_mode"] = new_mode
    psu_mode_state = state.get("psu_mode_state")
    if isinstance(psu_mode_state, dict):
        psu_mode_state["voltage_mode"] = new_mode

    port = state.get("psu_port")
    psu_lock = state.get("psu_lock")
    if not port:
        return

    def _apply() -> None:
        lock_ctx = psu_lock if psu_lock is not None else nullcontext()
        with lock_ctx:
            psu.apply_voltage_mode(port, new_mode, state.get("mode", "OB"))

    # psu_lock can be briefly held by the PSU monitor thread or a running script;
    # acquiring it directly here would freeze the whole UI event loop until free.
    await run.io_bound(_apply)


def _set_hk_display_mode(state: dict[str, Any], mode: str) -> None:
    mode_upper = str(mode or "REAL").upper()
    if mode_upper not in {"ADU", "REAL"}:
        mode_upper = "REAL"

    state["hk_display_mode"] = mode_upper
    app.state.hk_display_mode = mode_upper

    for controller in (state.get("packet_viewer_controllers") or {}).values():
        try:
            controller.refresh()
        except Exception:
            continue

    for key in ("eb_metrics_card", "ob_metrics_card"):
        controller = state.get(key)
        if controller is None:
            continue
        packet = getattr(controller, "last_packet", None)
        if packet is not None:
            try:
                controller.update_from_packet(packet)
            except Exception:
                continue

    for key in ("trp_card", "voltage_card"):
        plot_controller = state.get(key)
        if plot_controller is None:
            continue
        try:
            plot_controller.set_display_mode(mode_upper)
        except Exception:
            continue


def _log_psu_snapshot(state: dict[str, Any]) -> None:
    """Logs the current PSU readings to the info log."""
    logger = state["logger"]
    readings = state["last_psu_readings"]
    status_value = readings["status"]
    if status_value is None:
        logger.info("PSU status: no PSU readings available yet")
        return
    logger.info(
        "PSU status: STATUS=%s | ROV_HTR: V=%.2f, I=%.1f mA | EB: V=%.2f, I=%.1f mA",
        status_value,
        float(readings["PSU_ROV_HTR_V"]) if readings["PSU_ROV_HTR_V"] is not None else float("nan"),
        (float(readings["PSU_ROV_HTR_I"]) * 1000) if readings["PSU_ROV_HTR_I"] is not None else float("nan"),
        float(readings["PSU_EB_V"]) if readings["PSU_EB_V"] is not None else float("nan"),
        (float(readings["PSU_EB_I"]) * 1000) if readings["PSU_EB_I"] is not None else float("nan"),
    )


def _log_snapshot(state: dict[str, Any]) -> None:
    """Takes a snapshot of the current logs and PSU readings, and logs them to the info log."""
    state["logger"].info('Log Snapshot at "%s"', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _log_psu_snapshot(state)


def _select_psu_log(
    state: dict[str, Any],
    *,
    set_psu_log_path_fn: Any | None = None,
    get_psu_sample_count_fn: Any | None = None,
) -> None:
    """Open a PSU log picker and register selected replay file in app state."""
    logger = state["logger"]
    try:
        file_path = file_dialog_window_widget.select_file_dialog(
            title="Select PSU Log",
            filetypes=[("PSU Log Files", "*_PSU.log"), ("Log Files", "*.log"), ("All Files", "*.*")],
        )
    except Exception as exc:
        logger.error("Error while selecting PSU log file: %s", exc)
        ui.notify("Failed to open PSU log picker", color="negative")
        return

    if not file_path:
        return

    set_psu_log_path = (
        set_psu_log_path_fn if callable(set_psu_log_path_fn) else getattr(app.state, "set_psu_log_path", None)
    )
    if not callable(set_psu_log_path):
        ui.notify("PSU log setter not available", color="negative")
        return

    try:
        if set_psu_log_path(file_path):
            sample_count = 0
            get_sample_count = (
                get_psu_sample_count_fn
                if callable(get_psu_sample_count_fn)
                else getattr(app.state, "get_psu_replay_sample_count", None)
            )
            if callable(get_sample_count):
                try:
                    raw_count = get_sample_count()
                    if isinstance(raw_count, (int, float, str)):
                        sample_count = int(raw_count)
                except Exception:
                    sample_count = 0
            message = f"PSU log selected ({sample_count} samples)" if sample_count > 0 else "PSU log selected"
            ui.notify(message)
        else:
            ui.notify("No valid PSU samples found in selected log", color="negative")
    except Exception as exc:
        logger.error("Error while loading PSU replay log '%s': %s", file_path, exc)
        ui.notify("Failed to load PSU log", color="negative")


def _open_scope_settings_dialog() -> None:
    """Dialog to find, test, and set the scope's VISA resource string."""
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Scope Connection").classes("font-bold egse-title")
        ui.separator()
        resource_input = ui.input("VISA Resource", value=getattr(config, "SCOPE_VISA_RESOURCE", "")).classes("w-full")
        setup_file_input = ui.input(
            "Scope Setup File (optional)",
            value=getattr(app.state, "scope_setup_file", ""),
            placeholder="Only required for TEC_test; In-rush uses its bundled setup",
        ).classes("w-full")
        status_label = ui.label("").classes("text-sm")

        async def _find() -> None:
            if not _SCOPE_API_AVAILABLE:
                ui.notify("tek_scope_api not installed", color="negative")
                return
            status_label.set_text("Scanning local network...")
            try:
                resources = await run.io_bound(find_scope)
            except Exception as exc:
                status_label.set_text("")
                ui.notify(f"Scope discovery failed: {exc}", color="negative")
                return
            if not resources:
                status_label.set_text("No scope found on local subnet")
                ui.notify("No scope found", color="warning")
                return
            resource_input.set_value(resources[0])
            status_label.set_text(f"Found {len(resources)} scope(s)")
            ui.notify(f"Found scope: {resources[0]}", type="positive")

        async def _test() -> None:
            if not _SCOPE_API_AVAILABLE:
                ui.notify("tek_scope_api not installed", color="negative")
                return
            resource = resource_input.value.strip()
            if not resource:
                ui.notify("Enter a VISA resource string first", color="warning")
                return

            def _connect_and_idn() -> str:
                scope = TekScope(resource).connect()
                try:
                    return scope.idn()
                finally:
                    scope.close()

            status_label.set_text("Testing connection...")
            try:
                idn = await run.io_bound(_connect_and_idn)
                status_label.set_text(f"Connected: {idn}")
                ui.notify("Scope connection OK", type="positive")
            except ScopeConnectionError as exc:
                status_label.set_text("")
                ui.notify(f"Connection failed: {exc}", color="negative")

        def _apply() -> None:
            resource = resource_input.value.strip()
            setup_file = setup_file_input.value.strip()
            if not resource:
                ui.notify("Enter a VISA resource string first", color="warning")
                return
            config.SCOPE_VISA_RESOURCE = resource
            if setup_file:
                app.state.scope_setup_file = setup_file
            else:
                app.state.scope_setup_file = ""
            ui.notify("Scope connection selected", type="positive")
            dialog.close()

        with ui.row().classes("w-full gap-2"):
            ui.button("Find Scope", on_click=_find).classes("flex-1")
            ui.button("Test", on_click=_test).classes("flex-1")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Apply", on_click=_apply, color="primary")
    dialog.open()


async def _run_txt_script(state: dict[str, Any], buttons_row: Any = None) -> None:
    """Pick a .txt script file and typecast it to CmdTool."""
    if state.get("txt_script_busy", False):
        ui.notify("A text script is already being sent", type="warning")
        return

    state["txt_script_busy"] = True
    try:
        file_path = await run.io_bound(
            lambda: file_dialog_window_widget.select_file_dialog(
                title="Select Text Script",
                filetypes=[("Text Scripts", "*.txt"), ("All Files", "*.*")],
            )
        )
    except Exception as exc:
        state["txt_script_busy"] = False
        state["logger"].error("Text script picker error: %s", exc)
        ui.notify("Failed to open text script picker", color="negative")
        return

    if not file_path:
        state["txt_script_busy"] = False
        return

    try:
        interface = eb_interface.get_egse_interface()
        state["logger"].info("Typecasting text script: %s", file_path)
        ui.notify("Sending text script to CmdTool...", type="info")
        ok = await run.io_bound(lambda: interface.typecast(file_path, verbose=True))
        if ok:
            state["logger"].info("Text script completed: %s", file_path)
            ui.notify("Text script sent to CmdTool")
        else:
            state["logger"].warning("Text script failed: %s", file_path)
            ui.notify("Text script failed", color="negative")
    except Exception as exc:
        state["logger"].error("Text script error: %s", exc)
        ui.notify("Text script error", color="negative")
    finally:
        state["txt_script_busy"] = False


async def _run_selected_script(state: dict[str, Any], script_key: str, buttons_row: Any = None) -> None:
    """Run the selected EB script from the menu."""
    key = (script_key or "").strip().lower()

    if key == "txt_script":
        await _run_txt_script(state, buttons_row)
        return

    script = _discover_eb_scripts().get(key)
    if script is None:
        ui.notify("Unsupported script selected", color="negative")
        return
    module_stem, _ = script

    if key == "tec_test" and not str(getattr(app.state, "scope_setup_file", "")).strip():
        ui.notify("Select a scope setup file in Scope Settings before running TEC_test", color="warning")
        return
    script_control = ui_runtime_controller.get_script_control()
    if bool(script_control.get("running")):
        if ui_runtime_controller.is_aborted():
            ui.notify("Waiting for the previous script to stop", color="warning")
            for _ in range(100):
                await asyncio.sleep(0.1)
                if not ui_runtime_controller.is_script_running():
                    break
            if ui_runtime_controller.is_script_running():
                ui.notify("Previous script is still stopping; try again when it has finished", color="warning")
                return
        else:
            ui.notify("A script is already running", color="warning")
            return

    if _script_uses_ebtc(script[1]):
        if state.get("ob_replay") is not None:
            ui.notify("EB scripts cannot run in offline OB replay", type="negative")
            return
        interface = eb_interface.get_egse_interface()
        connect_cmdtool = getattr(interface, "_connect_cmdtool_window", None)
        if not callable(connect_cmdtool) or connect_cmdtool(wait_for_window=0.5) is None:
            ui.notify("Cannot start script: CmdTool window is not available", color="negative")
            return

    ui_runtime_controller.start_script(script_name=key)

    def _runner() -> None:
        try:
            module = reload(import_module(f"scripts_modules.{module_stem}"))
            entry_point_names = sorted(
                name for name in dir(module) if name.startswith("run_") and callable(getattr(module, name))
            )
            if len(entry_point_names) != 1:
                raise RuntimeError(f"Script module '{key}' must define exactly one callable run_* function")
            script_runner = getattr(module, entry_point_names[0])
            state["logger"].info(f"Starting {key} script from menu")
            ebtcs.configure_send_flow_control(
                should_pause=lambda: ui_runtime_controller.is_paused() or ui_runtime_controller.is_force_paused(),
                should_abort=lambda: ui_runtime_controller.is_aborted(),
            )

            if key == "tec_test":
                script_runner(scope_setup_file=str(app.state.scope_setup_file))
            elif key == "in_rush":
                script_runner(
                    scope_setup_file=None,
                    psu_port=state.get("psu_port"),
                    psu_lock=state.get("psu_lock"),
                )
            else:
                _invoke_script_entry_point(script_runner, state)

            replay = state.get("ob_replay")
            if replay is not None and replay.last_error is not None:
                raise RuntimeError(f"Offline replay could not verify the script: {replay.last_error}")
            if ui_runtime_controller.is_aborted():
                state["logger"].warning(f"{key} script aborted")
            else:
                state["logger"].info(f"{key} script completed")
        except ui_runtime_controller.ScriptAbortRequested:
            state["logger"].warning("%s script aborted", key)
        except Exception as exc:
            state["logger"].error("%s script failed: %s", key, exc)
        finally:
            try:
                ebtcs.clear_send_flow_control()
            except Exception:
                pass
            ui_runtime_controller.finish_script()
            # ensure UI-visible pause state cleared
            ui_runtime_controller.clear_pause()

    ui.notify(f"{key} script started")
    threading.Thread(target=_runner, name=f"script-{key}", daemon=True).start()


async def _select_rs422_log_async(state: dict[str, Any]) -> None:
    """Select an RS422 log without blocking the NiceGUI event loop."""
    if state.get("rs422_select_busy", False):
        ui.notify("RS422 log selection is already open", type="warning")
        return

    state["rs422_select_busy"] = True
    try:
        selected = await run.io_bound(app.state.eb_interface.select_rs422_log, state["logger"])
        if selected:
            state.setdefault("log_search", {})["enabled"] = True
            # Force one bounded refresh of the newly selected file.  Parsing itself
            # remains in the I/O worker used by poll_tm.
            try:
                app.state.eb_interface.reset_rs422_log_changed_state()
            except Exception:
                pass
            ui.notify("RS422 log selected", type="positive")
    except Exception as exc:
        state["logger"].error("Failed to select RS422 log: %s", exc)
        ui.notify("Failed to select RS422 log", color="negative")
    finally:
        state["rs422_select_busy"] = False


async def _start_egse_tools(state: dict[str, Any], sync_visibility_fn: Any) -> None:
    """Start EB EGSE tools without blocking or allowing overlapping launches."""
    if state.get("egse_tools_busy", False):
        ui.notify("EGSE tools are already starting/stopping", type="warning")
        return
    if bool(getattr(app.state.eb_interface, "egse_started", False)):
        state["egse_tools_started"] = True
        state.setdefault("log_search", {})["enabled"] = True
        ui.notify("EB EGSE tools are already started", type="info")
        return

    state["egse_tools_busy"] = True
    ui.notify("Starting EB EGSE tools...", type="info")
    try:
        # start_egse_tools ultimately launches Start_tools.bat and deliberately waits
        # while the external tools initialise. Keep it off the NiceGUI event loop so
        # PSU/TM timers continue to refresh.
        await run.io_bound(app.state.eb_interface.start_egse_tools, state["logger"])

        started = bool(getattr(app.state.eb_interface, "egse_started", False))
        state["egse_tools_started"] = started
        state.setdefault("log_search", {})["enabled"] = started
        if callable(sync_visibility_fn):
            sync_visibility_fn(state.get("mode", "EB"))

        if started:
            ui.notify("EB EGSE tools started", type="positive")
        else:
            ui.notify("Failed to start EB EGSE tools", color="negative")
    finally:
        state["egse_tools_busy"] = False


async def _stop_egse_tools(state: dict[str, Any], sync_visibility_fn: Any) -> None:
    """Stop EB EGSE tools without blocking or overlapping a start/stop action."""
    if state.get("egse_tools_busy", False):
        ui.notify("EGSE tools are already starting/stopping", type="warning")
        return

    state["egse_tools_busy"] = True
    ui.notify("Stopping EB EGSE tools...", type="info")
    try:
        # stop_egse_tools also contains sleeps and file-system log synchronisation.
        await run.io_bound(app.state.eb_interface.stop_egse_tools, state["logger"])

        state["egse_tools_started"] = False
        state.setdefault("log_search", {})["enabled"] = False
        if callable(sync_visibility_fn):
            sync_visibility_fn(state.get("mode", "EB"))

        ui.notify("EB EGSE tools stopped", type="info")
    finally:
        state["egse_tools_busy"] = False


def _pause_selected_script(state: dict[str, Any], script_key: str) -> None:
    """Toggle pause/resume for a running script."""

    # Allow pause for all scripts

    # log entry for debugging which callback was invoked
    try:
        logger = state.get("logger")
        if logger:
            logger.info("_pause_selected_script invoked for key=%s", script_key)
    except Exception:
        pass

    if not ui_runtime_controller.is_script_running():
        ui.notify("No running script", color="warning")
        return

    if ui_runtime_controller.is_force_paused() or ui_runtime_controller.is_paused():
        ui.notify("Script already paused", color="warning")
        return

    ui_runtime_controller.request_pause()
    ui.notify("Script paused")


def _resume_selected_script(state: dict[str, Any], script_key: str) -> None:
    """Resume a paused running script."""
    if not ui_runtime_controller.is_script_running():
        ui.notify("No running script", color="warning")
        return

    if not ui_runtime_controller.is_paused() and not ui_runtime_controller.is_force_paused():
        ui.notify("Script is not paused", color="warning")
        return

    ui_runtime_controller.clear_force_pause()
    ui_runtime_controller.clear_pause()
    ui.notify("Script resumed")


def _abort_selected_script(state: dict[str, Any], script_key: str) -> None:
    """Abort a running script."""

    # Allow abort for all scripts

    if not ui_runtime_controller.is_script_running():
        ui.notify("No running script", color="warning")
        return

    ui_runtime_controller.request_abort()
    ui_runtime_controller.clear_pause()
    ui.notify("Stop requested; wait for the script to finish before starting another", color="warning")


async def stop_and_shutdown(state: dict[str, Any], stop_event: Any) -> None:
    """Stops any running processes and shuts down the application."""
    from utility_modules import psu

    shutdown_errors: list[str] = []

    # Only run EB-specific stop tools in EB mode
    if str(state.get("mode", "EB")).upper() == "EB":
        try:
            await _stop_egse_tools(state, sync_visibility_fn=None)
        except Exception as exc:
            # Shutdown must continue even if the external EGSE tools fail to stop.
            state["logger"].warning("Could not stop EB EGSE tools during shutdown: %s", exc)
            shutdown_errors.append(f"EB EGSE tools: {exc}")

    psu_port = state.get("psu_port")
    if psu_port is None:
        shutdown_errors.append("PSU port is unavailable")
    elif not bool(getattr(psu_port, "is_open", False)):
        shutdown_errors.append("PSU port is already closed")
    else:
        lock = state.get("psu_lock")
        lock_acquired = False
        try:
            if lock is not None:
                lock_acquired = bool(lock.acquire(timeout=5.0))
                if not lock_acquired:
                    raise TimeoutError("PSU lock was busy for 5 seconds")

            psu.emergencyShutDown(psu_port, stop_event=stop_event, psu_thread=state.get("psu_thread"))
            if bool(getattr(psu_port, "is_open", False)):
                raise RuntimeError("PSU emergency shutdown returned but the serial port is still open")
        except Exception as exc:
            shutdown_errors.append(f"PSU shutdown: {exc}")
            state["logger"].exception("PSU shutdown failed: %s", exc)
        finally:
            if lock is not None and lock_acquired:
                lock.release()

    if stop_event is not None:
        stop_event.set()

    def _close_and_open_logs() -> None:
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(const.LOG_PATH))
        except Exception as exc:
            state["logger"].warning("Could not open session log folder: %s", exc)
        ui.run_javascript(
            "window.open('', '_self');window.close();if (!window.closed) { window.location.href = 'about:blank'; }"
        )
        # Close the desktop window first, then stop NiceGUI after the callback
        # returns so the launcher can finish its main loop cleanly.
        destroy_desktop_window()
        shutdown_timer = threading.Timer(0.25, app.shutdown)
        shutdown_timer.daemon = True
        shutdown_timer.start()

    with ui.dialog() as shutdown_dialog, ui.card().classes("w-[34rem] max-w-full"):
        if shutdown_errors:
            ui.label("Shutdown completed with warnings.").classes("text-base text-negative")
            ui.label("The following operations did not complete:").classes("text-sm")
            ui.label("\n".join(f"• {error}" for error in shutdown_errors)).classes("whitespace-pre-wrap text-negative")
            state["logger"].error("Shutdown completed with warnings: %s", "; ".join(shutdown_errors))
        else:
            ui.label("EGSE tools shut down.").classes("text-base")
        ui.label("Close the window to open the session log folder.").classes("text-sm")
        with ui.row().classes("justify-end w-full"):
            ui.button("Close the window", color="negative", on_click=_close_and_open_logs)

    shutdown_dialog.open()
