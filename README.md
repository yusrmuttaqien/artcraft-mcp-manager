# acmcp

Install and manage the MCP companion binaries of the ArtCraft Craft App suite.

Every app in the suite (PhotoCraft, FilmCraft, PdfCraft, and 14 more) publishes a
standalone CLI whose `mcp` subcommand serves Model Context Protocol over stdio:

```
photocraft-cli mcp
```

acmcp discovers those companions from the GitHub org, keeps plain binary copies in a
directory you choose, and registers the matching stdio entry in your MCP client config.
The entry point is a terminal UI.

## Setup

```sh
git clone https://github.com/yusrmuttaqien/artcraft-mcp-manager
cd artcraft-mcp-manager
uv sync                                # builds .venv from uv.lock, installs acmcp editable
source .venv/bin/activate
acmcp
```

`uv.lock` pins the exact tested dependency versions. Without it, `uv pip install -e .`
resolves current releases instead.

First run: press `c` to build the catalog (GitHub API calls, about 15 seconds), then
`i` on any row installs that companion. You can press `i` on several rows one after
another: downloads run in parallel and the status area grows one progress line each.
The GitHub API allows 60 anonymous calls per hour; a `GITHUB_TOKEN`, an
`ACMCP_GITHUB_TOKEN`, or a `gh` login removes the limit.

## Keys

| Key | Action |
|---|---|
| arrows | move row cursor |
| `i` | install the selected companion |
| `u` | update it when a newer release exists |
| `x` | remove it (unregister and delete) |
| `e` | rewrite its mcp.json entry (recreate after edits or deletions) |
| `m` | cycle its registration mode: headless -> bridge -> both -> headless |
| `g` | toggle the bridge entry alongside headless (both / headless) |
| `r` | refresh the table |
| `c` | rebuild the catalog |
| `s` | settings |
| `q` | quit |

The Mode column shows what is registered now: `headless`, `bridge`, `both`, or
`none`. The Bridgeable column shows which apps document a bridge mode.

## Settings

`!` marks an installed companion whose latest release is newer.

The settings screen (`s`) configures:

- **MCP client**: the config target. Each client in `CLIENTS` (`acmcp/core.py`) is
  an id, a display label, a default `mcp.json` location, and a schema tag. Only
  `pi` (`~/.pi/agent/mcp.json`) exists today; a new client is one dict entry.
- **Install directory**: where the `<app>-cli` binaries are copied.
- **mcp.json path override** (grouped under the client): the field stays blank when
  no override exists, and gray placeholder text shows the client default that is in
  effect. Typing a path and saving creates the override; emptying the field and
  saving removes it. Switching client clears any override. Resolution order: your
  override, otherwise the client default.

## Registration modes

- **headless**: one entry, `<app>`, running `<app>-cli mcp` with no GUI.
- **bridge**: one entry, `<app>-bridge`, running `<app>-cli mcp` plus the app's
  documented connect flag, driving the live desktop app over its control channel.
- **both**: the two entries side by side.

Bridge flags and default ports are curated in `BRIDGES` in `acmcp/core.py`, verified
from each app's docs or README: `--connect` on cadcraft, designcraft, deckcraft
(7990), gridcraft, lightcraft, soundcraft; `--attach` on figcraft; `--bridge` on
anicraft, effectcraft, filmcraft, materialcraft and texturecraft (7878). The
running desktop app must be started with the matching `--control <port>`.
WordCraft is excluded until upstream un-refuses `mcp --connect` (wordcraft #7124);
PdfCraft, craft-anything and similar have no documented bridge, and the app
refuses the mode.

Two special cases:

- **PhotoCraft** is deliberately absent from `BRIDGES`: its plain headless entry
  already bridges automatically when the desktop app runs (the CLI finds it via
  `agent-connection.json`), so a separate bridge entry would be identical.
- **FilmCraft** bridge entries need `FILMCRAFT_CONTROL_TOKEN` in the server
  environment; the entry's description says so. Set it via pi's `env` key on the
  entry after registering, or manage that one entry yourself.

## Notes

- Entries written for pi include `"exposure": "deferred"` by default, so its tools
  load through `tool_search` instead of being declared up front. Client defaults
  live in `CLIENTS[<id>]["entry_defaults"]`. Re-registering an already-registered
  companion rewrites its entry with the current defaults.
- Companion discovery is by release asset, not a hardcoded list: a future suite app
  that publishes `<app>-cli-*` appears after the next catalog rebuild.
- macOS uses the `*-macos-universal.zip` asset. Linux prefers `*-cli-*-linux-*.tar.gz`
  (only PdfCraft publishes one today) and otherwise falls back to the app tarball.
- Windows has no standalone CLI asset upstream; acmcp reports this honestly.
- State lives in `~/.config/acmcp/` (override with `ACMCP_HOME`): `config.json`,
  `catalog.json`, `state.json`. Concurrent installs share one file and are guarded
  by a lock.
- `acmcp/core.py` and `acmcp/mcpjson.py` stay importable as a library for scripts.

## Development

The headless test harness drives the real app with simulated keys:

```sh
.venv/bin/python - <<'EOF'
import asyncio
from textual.widgets import DataTable
from acmcp.tui import AcmcpApp
async def main():
    async with AcmcpApp().run_test() as pilot:
        await pilot.pause()
asyncio.run(main())
EOF
```
