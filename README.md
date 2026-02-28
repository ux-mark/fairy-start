# Fairy Start

**A macOS desktop app for starting and monitoring your local dev services.**

Define a list of GitHub repos in a single TOML file. Fairy Start clones them, starts each one, and shows you live status — all from a compact window that stays out of your way.

```
┌─ Fairy Start ──────────────────────────────────────┐
│                                              [ + ]  │
│  ● running   editable-web    http://localhost:5050  │
│  ● running   jobs            http://localhost:5001  │
│  ● starting… my-api                                 │
│  ● off       another-service                        │
└────────────────────────────────────────────────────┘
```

- Clones repos on first run, pulls latest on subsequent starts
- Live health-check polling with plain-English error hints
- **Open ↗** button appears when a service is running at its URL
- Add new services from a GitHub URL without editing config by hand
- No pip installs — stdlib only (`tkinter`, `tomllib`, `subprocess`, `threading`)

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Requires `tomllib` (stdlib since 3.11) |
| `python-tk` | Not bundled with Homebrew Python — install separately |
| `gh` CLI | Required only for the **Add via URL** feature |

```bash
brew install python-tk@3.14
brew install gh
gh auth login   # needed for private repos or the Add via URL feature
```

---

## Quick Start

```bash
git clone https://github.com/ux-mark/fairy-start.git
cd fairy-start
python3 fairy_start.py
```

Edit `config.toml` to point at your own repos, then restart the app.

---

## Configuration

Services are defined in `config.toml` at the project root:

```toml
[settings]
packages_dir = "packages"   # where repos are cloned (gitignored)

[[package]]
name          = "my-api"
repo          = "user/repo"                          # GitHub shorthand or full https:// URL
branch        = "main"
start_command = "npm install && npm start"
url           = "http://localhost:3000"              # optional — enables health checks + Open button

[[package]]
name          = "another-service"
repo          = "user/other-repo"
branch        = "main"
start_command = "bash -c 'lsof -ti:8080 | xargs kill -9 2>/dev/null; python3 server.py'"
```

**Notes:**
- `repo` accepts either a full `https://` URL or a `user/repo` shorthand (resolved via `gh` CLI)
- `url` is optional; if provided, Fairy Start polls it every 5 seconds
- `start_command` runs as a shell command inside the cloned repo directory
- Port-killing can be included directly in `start_command`

---

## Status Indicators

| Indicator | Meaning |
|---|---|
| Grey ● off | Service is stopped |
| Amber ● starting… | Process launching or URL not yet reachable |
| Green ● running | Process healthy (no URL configured, or URL responding 2xx) |
| Amber ● errors | URL returning 5xx responses |
| Red ● error | Process exited unexpectedly |

When a service is running and has a URL configured, an **Open ↗** button appears to launch it in the browser.

---

## Error Hints

When a service fails, Fairy Start scans the log output and shows a plain-English fix inline:

| Log pattern | Hint shown |
|---|---|
| `localStorage is not a function` | SSR guard needed |
| `EADDRINUSE` | Port already in use — suggests a kill command |
| `command not found` | Missing dependency |
| `Cannot find module` | Run `npm install` |
| `Permission denied` | File permission issue |
| `JavaScript heap out of memory` | Node memory limit needs increasing |

---

## Adding a Service via URL

Click **+** in the header (available when all services are stopped):

1. Paste a GitHub repo URL or `user/repo` shorthand
2. Click **Detect** — Fairy Start probes the repo via `gh api` and infers the start command from `package.json`, `Procfile`, `Makefile`, or language files
3. Review and edit the detected fields
4. Click **Add Service** — the entry is appended to `config.toml` and appears immediately

---

## License

MIT — see [LICENSE](LICENSE).
