from __future__ import annotations

import json
from pathlib import Path

from .errors import AcmcpError


def entry_for(app: str, bin_path: Path, args: list[str], extra: dict | None = None) -> dict:
    mode = "bridged to the running app" if any(a.startswith(("--connect", "--attach", "--bridge")) for a in args) else "headless"
    return {
        "command": str(bin_path),
        "args": args,
        "description": f"{app.title()}: drive the {app.title()} engine over MCP ({mode}).",
        **(extra or {}),
    }


def load(mcp_json: str) -> dict:
    path = Path(mcp_json).expanduser()
    if not path.exists():
        return {"mcpServers": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise AcmcpError(f"cannot read {path}: {e}")
    if not isinstance(data, dict):
        raise AcmcpError(f"{path} is not a JSON object")
    data.setdefault("mcpServers", {})
    return data


def save(mcp_json: str, data: dict) -> None:
    path = Path(mcp_json).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def registered(mcp_json: str) -> dict:
    return load(mcp_json)["mcpServers"]


def register(mcp_json: str, name: str, entry: dict, force: bool = False) -> str:
    data = load(mcp_json)
    servers = data["mcpServers"]
    existing = servers.get(name)
    if existing and existing.get("command") != entry.get("command") and not force:
        raise AcmcpError(
            f"mcpServers.{name} already points at {existing.get('command')}; "
            "pass force=True to replace it"
        )
    servers[name] = entry
    save(mcp_json, data)
    return f"registered {name} in {Path(mcp_json).expanduser()}"


def unregister(mcp_json: str, name: str) -> str:
    data = load(mcp_json)
    if name not in data["mcpServers"]:
        return f"{name} is not registered"
    data["mcpServers"].pop(name)
    save(mcp_json, data)
    return f"unregistered {name} from {Path(mcp_json).expanduser()}"
