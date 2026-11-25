from __future__ import annotations

import time
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist

from . import core
from .errors import AcmcpError

COLUMNS = ["App", "Latest", "Installed", "Mode", "Bridgeable", "Binary"]


class SettingsScreen(ModalScreen[None]):
    """Set the MCP client target, the install directory, and the mcp.json path."""

    CSS = """
    SettingsScreen { align: center middle; }
    #settings-box {
        width: 90;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #settings-box Input, #settings-box Select { width: 1fr; }
    .settings-group {
        width: 1fr;
        height: auto;
        border: hkey $primary;
        padding: 1 1 0 1;
        margin-bottom: 1;
    }
    .group-hint { color: $text-muted; }
    #settings-buttons { width: 1fr; margin-top: 1; align-horizontal: right; }
    """

    def __init__(self, mgr: core.Manager) -> None:
        super().__init__()
        self.mgr = mgr
        current = mgr.cfg.get("mcp_client") or "pi"
        self._initial_client = current if current in core.CLIENTS else next(iter(core.CLIENTS))

    def compose(self) -> ComposeResult:
        current = self._initial_client
        options = [(c["label"], name) for name, c in core.CLIENTS.items()]
        client_default = core.CLIENTS[current]["mcp_json"]
        override = self.mgr.cfg.get("mcp_json", "")
        with Vertical(id="settings-box"):
            yield Static("Settings", id="settings-title")
            with Vertical(classes="settings-group"):
                yield Label("MCP target")
                yield Label("MCP client")
                yield Select(options, value=current, allow_blank=False, id="client")
                yield Label("mcp.json path override (blank = client default, shown in the field)")
                yield Input(
                    value=override,
                    placeholder=client_default,
                    id="mcp_json",
                )
            with Vertical(classes="settings-group"):
                yield Label("Binaries")
                yield Label("Install directory")
                yield Input(value=str(self.mgr.install_dir), id="install_dir")
            with Horizontal(id="settings-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_select_changed(self, event: Select.Changed) -> None:
        # Select posts Changed on mount with the initial value; ignore it.
        # A real client switch resets the override, matching Manager.set_config.
        if event.select.id != "client" or event.value == self._initial_client:
            return
        if event.value in core.CLIENTS:
            path_input = self.query_one("#mcp_json", Input)
            path_input.value = ""
            path_input.placeholder = core.CLIENTS[event.value]["mcp_json"]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.mgr.set_config("mcp_client", str(self.query_one("#client", Select).value))
            install_dir = self.query_one("#install_dir", Input).value.strip()
            if install_dir:
                self.mgr.set_config("install_dir", install_dir)
            path = self.query_one("#mcp_json", Input).value.strip()
            if path:
                self.mgr.set_config("mcp_json", path)
            else:
                self.mgr.clear_config("mcp_json")
        self.dismiss()


class AcmcpApp(App):
    """Manage ArtCraft MCP companion binaries from the terminal."""

    CSS = """
    #status {
        height: auto;
        padding: 0 1;
        color: $text;
    }
    #table { height: 1fr; }
    """

    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("r", "refresh_table", "Refresh table"),
        ("c", "refresh_catalog", "Rebuild catalog"),
        ("i", "install", "Install"),
        ("u", "update", "Update"),
        ("x", "remove", "Remove"),
        ("e", "register", "Re-register"),
        ("m", "cycle_mode", "Registration mode"),
        ("g", "toggle_bridge", "Toggle bridge"),
        ("s", "settings", "Settings"),
    ]

    def __init__(self, **overrides):
        super().__init__()
        self.mgr = core.Manager(**overrides)
        self._base_status = "loading..."
        self._active: dict[str, str] = {}  # app -> progress line, touched on the UI thread only

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("loading...", id="status")
            yield DataTable(id="table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*COLUMNS)
        table.cursor_type = "row"
        self.refresh_table()

    # ------------------------------------------------------------ UI helpers

    def _set_status(self, message: str) -> None:
        self._base_status = message
        self._refresh_status()

    def _refresh_status(self) -> None:
        lines = [self._base_status] + [f"  {line}" for line in self._active.values()]
        self.query_one("#status", Static).update("\n".join(lines))

    def _show_rows(self, rows: list) -> None:
        table = self.query_one(DataTable)
        cursor_value = None
        if table.row_count:
            try:
                cursor_value = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except CellDoesNotExist:
                pass
        table.clear()
        for r in rows:
            mark = "!" if r["outdated"] else " "
            installed = (r["installed"] or "-") + (" <- newer" if r["outdated"] else "")
            binary = "present" if r["binary"] else "missing"
            table.add_row(
                f"{mark} {r['app']}",
                r["latest"],
                installed,
                r["mode"],
                "yes" if r["bridgeable"] else "no",
                binary,
                key=r["app"],
            )
        if cursor_value is not None:
            try:
                table.move_cursor(row=table.get_row_index(cursor_value))
            except RowDoesNotExist:
                pass

    def _render(self, rows: list, note: str) -> None:
        self._show_rows(rows)
        counts = sum(1 for r in rows if r["installed"])
        self._set_status(
            f"{len(rows)} companions, {counts} installed"
            f"  |  client: {self.mgr.cfg['mcp_client']}"
            f"  |  mcp.json: {self.mgr.mcp_json}"
            f"  |  s settings\n{note}"
        )

    def _selected(self):
        table = self.query_one(DataTable)
        if table.row_count == 0:
            self._set_status("catalog is empty; press c to build it")
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    # ------------------------------------------------------------ workers

    @work(thread=True, exclusive=True)
    def refresh_table(self) -> None:
        try:
            rows = self.mgr.status_rows()
            note = "press i/u/x on a row, c rebuilds the catalog, q quits"
            self.call_from_thread(self._render, rows, note)
        except AcmcpError as e:
            self.call_from_thread(self._set_status, f"error: {e}")

    @work(thread=True, exclusive=True)
    def refresh_catalog(self) -> None:
        self.call_from_thread(self._set_status, "rebuilding catalog (GitHub API calls)...")
        try:
            catalog = self.mgr.build_catalog()
            rows = self.mgr.status_rows_from(catalog)
            self.call_from_thread(
                self._render, rows, f"catalog rebuilt: {len(catalog['apps'])} companions"
            )
        except AcmcpError as e:
            self.call_from_thread(self._set_status, f"error: {e}")

    def _make_progress(self, app: str, label: str):
        last = [0.0]

        def on_progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] < 0.25 and done < total:
                return
            last[0] = now
            pct = done * 100 // total if total else 0
            mb = done / (1 << 20)

            def update() -> None:
                self._active[app] = f"{label} {app}: {mb:.0f} MB ({pct}%)"
                self._refresh_status()

            self.call_from_thread(update)

        return on_progress

    @work(thread=True)
    def _do(self, action: str, app: str) -> None:
        try:
            if action == "install":
                note = self.mgr.install(
                    app, on_progress=self._make_progress(app, "downloading")
                )
            elif action == "update":
                note = self.mgr.update(
                    app, on_progress=self._make_progress(app, "updating")
                )
            else:
                note = self.mgr.remove(app)
            done_text = f"{action} {app}: {note.replace(chr(10), '  |  ')}"
        except AcmcpError as e:
            done_text = f"error: {app}: {e}"

        def finish() -> None:
            self._active.pop(app, None)
            self._set_status(done_text)
            self.refresh_table()

        self.call_from_thread(finish)

    # ------------------------------------------------------------ actions

    def action_refresh_table(self) -> None:
        self.refresh_table()

    def action_refresh_catalog(self) -> None:
        self.refresh_catalog()

    def _start(self, action: str) -> None:
        app = self._selected()
        if not app:
            return
        if app in self._active:
            self._set_status(f"{app}: already in progress")
            return
        self._active[app] = f"{action} {app}: starting..."
        self._do(action, app)

    def action_install(self) -> None:
        self._start("install")

    def action_update(self) -> None:
        self._start("update")

    def action_remove(self) -> None:
        self._start("remove")

    def action_register(self) -> None:
        app = self._selected()
        if not app:
            return
        mode = self.mgr.registered_mode(app)
        if mode == "none":
            mode = "headless"
        try:
            note = self.mgr.set_mode(app, mode)
        except AcmcpError as e:
            note = str(e)
        self._set_status(f"re-register {app}: {note}")
        self.refresh_table()

    def _set_mode(self, mode: str) -> None:
        app = self._selected()
        if not app:
            return
        try:
            note = self.mgr.set_mode(app, mode)
        except AcmcpError as e:
            note = str(e)
        self._set_status(note)
        self.refresh_table()

    def action_cycle_mode(self) -> None:
        app = self._selected()
        if not app:
            return
        current = self.mgr.registered_mode(app)
        supported = ["headless"] + (["bridge", "both"] if app in core.BRIDGES else [])
        try:
            nxt = supported[supported.index(current) + 1] if current in supported else "headless"
        except IndexError:
            nxt = supported[0]
        self._set_mode(nxt)

    def action_toggle_bridge(self) -> None:
        app = self._selected()
        if not app:
            return
        current = self.mgr.registered_mode(app)
        self._set_mode("headless" if current in ("bridge", "both") else "both")

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.mgr), lambda _: self.refresh_table())


def main() -> None:
    AcmcpApp().run()


if __name__ == "__main__":
    main()
