from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import mcpjson
from .errors import AcmcpError

API = "https://api.github.com"
CATALOG_TTL = 24 * 3600
NON_APP_SUFFIXES = ("-corpus", "-assets", "-libs")

# Supported MCP client targets. mcp_json is the client's default config location;
# a user-set mcp_json overrides it. format is the future hook for non-JSON schemas.
CLIENTS = {
    "pi": {
        "label": "pi",
        "mcp_json": "~/.pi/agent/mcp.json",
        "format": "mcpServers",
        # keys merged into every entry written for this client
        "entry_defaults": {"exposure": "deferred"},
    },
}

# Bridge (control-the-running-GUI) support, verified from each app's docs
# (docs/mcp.md, docs/control-protocol.md) or README. Args extend `<app>-cli mcp`.
# Not listed here: photocraft, whose plain entry already bridges automatically when
# the desktop runs (the CLI finds it via agent-connection.json, no flag involved).
BRIDGES = {
    "filmcraft": {"args": ["--bridge", "127.0.0.1:9876"], "env": "FILMCRAFT_CONTROL_TOKEN"},
    "cadcraft": {"args": ["--connect", "127.0.0.1:7979"]},
    "lightcraft": {"args": ["--connect", "127.0.0.1:7980"]},
    "designcraft": {"args": ["--connect", "7979"]},
    "gridcraft": {"args": ["--connect", "7979"]},
    "deckcraft": {"args": ["--connect", "7990"]},
    "figcraft": {"args": ["--attach", "7979"]},
    "soundcraft": {"args": ["--connect", "7801"]},
    "effectcraft": {"args": ["--bridge", "9877"]},
    "anicraft": {"args": ["--bridge", "9878"]},
    "materialcraft": {"args": ["--bridge", "7878"]},
    "texturecraft": {"args": ["--bridge", "7878"]},
}

# wordcraft is deliberately absent: wordcraft-cli mcp --connect is currently refused
# upstream (wordcraft #7124), and its headless mode needs two automation roots.

MODES = ("headless", "bridge", "both")

DEFAULT_CONFIG = {
    "org": "storytold",
    "install_dir": "~/bin/artcraft-mcp",
    "mcp_client": "pi",
}
CONFIG_KEYS = set(DEFAULT_CONFIG) | {"mcp_json"}


def version_tuple(v: str):
    return tuple(int(p) for p in re.findall(r"\d+", v)[:4])


class Manager:
    """All acmcp behavior: config, catalog discovery, install/remove, registration."""

    def __init__(self, **overrides):
        self.home = Path(os.environ.get("ACMCP_HOME", "~/.config/acmcp")).expanduser()
        self._state_lock = threading.Lock()
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(self._read_json(self.home / "config.json", {}) or {})
        for key in CONFIG_KEYS:
            if overrides.get(key):
                cfg[key] = overrides[key]
        self.cfg = cfg

    # ------------------------------------------------------------ config

    def _read_json(self, path: Path, default=None):
        if not path.exists():
            return default
        return json.loads(path.read_text())

    def _write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)

    def set_config(self, key: str, value: str) -> None:
        if key not in CONFIG_KEYS:
            raise AcmcpError(f"unknown key {key!r} (choose from: {', '.join(sorted(CONFIG_KEYS))})")
        if key == "mcp_client" and value not in CLIENTS:
            raise AcmcpError(f"unknown MCP client {value!r} (choose from: {', '.join(CLIENTS)})")
        cfg = self._read_json(self.home / "config.json", {}) or {}
        cfg[key] = value
        if key == "mcp_client":
            # switching client means switching to its default config file
            cfg.pop("mcp_json", None)
        self._write_json(self.home / "config.json", cfg)
        self.cfg[key] = value
        if key == "mcp_client":
            self.cfg.pop("mcp_json", None)

    def clear_config(self, key: str) -> None:
        if key not in CONFIG_KEYS:
            raise AcmcpError(f"unknown key {key!r}")
        cfg = self._read_json(self.home / "config.json", {}) or {}
        cfg.pop(key, None)
        self._write_json(self.home / "config.json", cfg)
        self.cfg.pop(key, None)

    @property
    def install_dir(self) -> Path:
        return Path(self.cfg["install_dir"]).expanduser()

    def _entry_defaults(self) -> dict:
        client = CLIENTS.get(self.cfg.get("mcp_client") or "pi", CLIENTS["pi"])
        return client.get("entry_defaults", {})

    @property
    def mcp_json(self) -> str:
        client = CLIENTS.get(self.cfg.get("mcp_client") or "pi", CLIENTS["pi"])
        return self.cfg.get("mcp_json") or client["mcp_json"]

    def bin_path(self, app: str) -> Path:
        return self.install_dir / f"{app}-cli"

    def bridge_args(self, app: str) -> list | None:
        bridge = BRIDGES.get(app)
        return ["mcp", *bridge["args"]] if bridge else None

    def entries_for(self, app: str, mode: str) -> dict:
        """entry-name -> mcp.json entry for the given registration mode."""
        if mode not in MODES:
            raise AcmcpError(f"unknown mode {mode!r}")
        bridge = BRIDGES.get(app)
        if mode in ("bridge", "both") and not bridge:
            raise AcmcpError(f"{app}: no bridge mode documented for this app")
        bin_path = self.bin_path(app)
        entries = {}
        if mode in ("headless", "both"):
            entries[app] = mcpjson.entry_for(app, bin_path, ["mcp"], self._entry_defaults())
        if mode in ("bridge", "both"):
            entry = mcpjson.entry_for(app, bin_path, self.bridge_args(app), self._entry_defaults())
            if bridge.get("env"):
                var = bridge["env"]
                entry["description"] += f" Needs {var} set in this server's environment."
            entries[f"{app}-bridge"] = entry
        return entries

    def registered_mode(self, app: str) -> str:
        servers = mcpjson.registered(self.mcp_json)
        head = app in servers
        bridged = f"{app}-bridge" in servers
        if head and bridged:
            return "both"
        if bridged:
            return "bridge"
        if head:
            return "headless"
        return "none"

    def set_mode(self, app: str, mode: str) -> str:
        """Rewrite this companion's registrations to exactly match mode."""
        entries = self.entries_for(app, mode)
        wanted = set(entries)
        messages = []
        for name in (app, f"{app}-bridge"):
            if name not in wanted:
                messages.append(mcpjson.unregister(self.mcp_json, name))
        for name, entry in entries.items():
            messages.append(mcpjson.register(self.mcp_json, name, entry, force=True))
        kept = [m for m in messages if not m.endswith("is not registered")]
        with self._state_lock:
            state = self._load_state()
            if app in state:
                state[app]["mode"] = mode
                self._save_state(state)
        return f"mode {app} -> {mode}" + (f" ({'; '.join(kept)})" if kept else "")

    # ------------------------------------------------------------ GitHub

    def _github_token(self):
        for var in ("ACMCP_GITHUB_TOKEN", "GITHUB_TOKEN"):
            tok = os.environ.get(var)
            if tok:
                return tok
        try:
            out = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=5, check=False
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    def _api_get(self, path: str):
        headers = {"User-Agent": "acmcp", "Accept": "application/vnd.github+json"}
        tok = self._github_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(f"{API}{path}", headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429):
                raise AcmcpError(
                    "GitHub API rate limit hit (anonymous access is 60 calls/hour). "
                    "Set ACMCP_GITHUB_TOKEN or GITHUB_TOKEN, or log in with the gh CLI."
                )
            raise AcmcpError(f"GitHub API {e.code} for {path}")

    # ------------------------------------------------------------ catalog

    @staticmethod
    def _repo_candidate(name: str) -> bool:
        return "craft" in name and not name.endswith(NON_APP_SUFFIXES)

    @staticmethod
    def _platform_patterns(app: str):
        system = platform.system()
        if system == "Darwin":
            return [re.compile(rf"^{re.escape(app)}-cli-.*-macos-universal\.zip$")]
        if system == "Linux":
            machine = platform.machine()
            arch = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
            if not arch:
                raise AcmcpError(f"unsupported Linux architecture: {machine}")
            # standalone CLI tarball first, then the app tarball which may ship the CLI inside
            return [
                re.compile(rf"^{re.escape(app)}-cli-.*-linux-{arch}\.tar\.gz$"),
                re.compile(rf"^{re.escape(app)}-\d.*-linux-{arch}\.tar\.gz$"),
            ]
        raise AcmcpError(f"{system} publishes no standalone CLI asset; request one upstream")

    def _pick_cli_asset(self, assets, app: str):
        for pattern in self._platform_patterns(app):
            for asset in assets:
                if pattern.match(asset["name"]):
                    return asset
        return None

    def build_catalog(self) -> dict:
        org = self.cfg["org"]
        apps = {}
        for page in range(1, 5):
            repos = self._api_get(f"/orgs/{org}/repos?per_page=100&page={page}")
            if not repos:
                break
            for repo in repos:
                name = repo["name"]
                if not self._repo_candidate(name):
                    continue
                release = self._api_get(f"/repos/{org}/{name}/releases/latest")
                if not release:
                    continue
                asset = self._pick_cli_asset(release["assets"], name)
                if asset:
                    apps[name] = {
                        "version": release["tag_name"].lstrip("vV"),
                        "asset": asset["name"],
                        "url": asset["browser_download_url"],
                        "size": asset["size"],
                    }
            if len(repos) < 100:
                break
        catalog = {"updated": time.time(), "org": org, "apps": apps}
        self._write_json(self.home / "catalog.json", catalog)
        return catalog

    def read_catalog(self, refresh: bool = False) -> dict:
        catalog = self._read_json(self.home / "catalog.json")
        if (
            not refresh
            and catalog
            and catalog.get("org") == self.cfg["org"]
            and time.time() - catalog.get("updated", 0) < CATALOG_TTL
        ):
            return catalog
        return self.build_catalog()

    # ------------------------------------------------------------ state

    def _load_state(self) -> dict:
        return self._read_json(self.home / "state.json", {}) or {}

    def _save_state(self, state: dict) -> None:
        self._write_json(self.home / "state.json", state)

    # ------------------------------------------------------------ views

    def status_rows(self, refresh: bool = False) -> list:
        return self.status_rows_from(self.read_catalog(refresh))

    def status_rows_from(self, catalog: dict) -> list:
        state = self._load_state()
        servers = mcpjson.registered(self.mcp_json)
        rows = []
        for app, info in sorted(catalog["apps"].items()):
            installed = state.get(app, {}).get("version")
            mode = (
                "both" if f"{app}-bridge" in servers and app in servers
                else "bridge" if f"{app}-bridge" in servers
                else "headless" if app in servers
                else "none"
            )
            rows.append(
                {
                    "app": app,
                    "latest": info["version"],
                    "installed": installed,
                    "registered": mode != "none",
                    "mode": mode,
                    "bridgeable": app in BRIDGES,
                    "binary": self.bin_path(app).exists(),
                    "outdated": bool(installed) and version_tuple(installed) < version_tuple(info["version"]),
                }
            )
        return rows

    # ------------------------------------------------------------ actions

    def _extract_binary(self, archive: Path, bin_name: str, dest: Path) -> None:
        found = False
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    if Path(info.filename).name == bin_name:
                        with zf.open(info) as src, open(dest, "wb") as out:
                            while chunk := src.read(1 << 20):
                                out.write(chunk)
                        found = True
                        break
        else:
            with tarfile.open(archive, "r:gz") as tf:
                for member in tf.getmembers():
                    if Path(member.name).name == bin_name and member.isfile():
                        src = tf.extractfile(member)
                        if src is None:
                            continue
                        with open(dest, "wb") as out:
                            while chunk := src.read(1 << 20):
                                out.write(chunk)
                        found = True
                        break
        if not found:
            raise AcmcpError(f"{bin_name} not found inside {archive.name}")
        dest.chmod(0o755)

    def install(
        self, app: str, refresh: bool = False, dry_run: bool = False, on_progress=None
    ) -> str:
        catalog = self.read_catalog(refresh)
        info = catalog["apps"].get(app)
        if not info:
            raise AcmcpError(f"{app} has no published MCP companion (see the catalog)")
        bin_path = self.bin_path(app)
        if dry_run:
            lines = [
                f"would install {app} v{info['version']} ({info['size'] // (1 << 20)} MB)",
                f"  from: {info['url']}",
                f"  to:   {bin_path}",
            ]
            for name, entry in self.entries_for(app, "headless").items():
                lines.append(
                    f"would register {name} in {Path(self.mcp_json).expanduser()}: "
                    + json.dumps(entry)
                )
            return "\n".join(lines)
        headers = {"User-Agent": "acmcp"}
        tok = self._github_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        self.install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / info["asset"]
            done = 0
            with urllib.request.urlopen(
                urllib.request.Request(info["url"], headers=headers)
            ) as resp, open(archive, "wb") as out:
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, info["size"])
            tmp_bin = self.install_dir / f"{app}-cli.new"
            self._extract_binary(archive, f"{app}-cli", tmp_bin)
            tmp_bin.replace(bin_path)

        with self._state_lock:
            state = self._load_state()
            state[app] = {
                "version": info["version"],
                "asset": info["asset"],
                "mode": "headless",
                "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._save_state(state)
        notes = [
            mcpjson.register(self.mcp_json, name, entry, force=True)
            for name, entry in self.entries_for(app, "headless").items()
        ]
        return f"installed {app} v{info['version']} -> {bin_path}\n" + "\n".join(notes)

    def update(self, app: str, dry_run: bool = False, on_progress=None) -> str:
        catalog = self.build_catalog()
        info = catalog["apps"].get(app)
        if not info:
            raise AcmcpError(f"{app} has no published MCP companion (see the catalog)")
        current = self._load_state().get(app, {}).get("version")
        if current and version_tuple(current) >= version_tuple(info["version"]):
            return f"{app}: up to date (v{current})"
        return self.install(app, False, dry_run, on_progress)

    def remove(self, app: str, keep_binary: bool = False, keep_entry: bool = False, dry_run: bool = False) -> str:
        out = []
        if not keep_entry:
            for name in (app, f"{app}-bridge"):
                if dry_run:
                    out.append(f"would unregister {name} from {Path(self.mcp_json).expanduser()}")
                else:
                    out.append(mcpjson.unregister(self.mcp_json, name))
        if not keep_binary:
            bin_path = self.bin_path(app)
            if bin_path.exists():
                if dry_run:
                    out.append(f"would delete {bin_path}")
                else:
                    bin_path.unlink()
                    with self._state_lock:
                        state = self._load_state()
                        state.pop(app, None)
                        self._save_state(state)
                    out.append(f"deleted {bin_path}")
            else:
                out.append(f"no binary installed for {app}")
        return "\n".join(out)


