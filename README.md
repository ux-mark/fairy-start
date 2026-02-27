# Autostart

A macOS menubar-style launcher for local dev services. Autostart manages a set of git-backed projects — cloning them on first run, pulling on subsequent starts, and launching each service's start command in a managed subprocess. A live health-check loop monitors each service's URL and surfaces plain-English advisory hints when something goes wrong.

![Status indicators: off, starting, running, error]

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Requires `tomllib` (stdlib since 3.11) |
| `python-tk` | Not bundled with Homebrew Python — install separately |
| `gh` CLI | Required only for the **Add via URL** feature |

```bash
brew install python-tk@3.14
brew install gh
gh auth login   # for private repos or the Add via URL feature
```

## Run

```bash
python3 autostart.py
```

No dependencies to install — stdlib only (`tkinter`, `tomllib`, `subprocess`, `threading`).

## Configuration

Services are defined in `config.toml` at the project root:

```toml
[settings]
packages_dir = "packages"   # where repos are cloned (gitignored)

[[package]]
name          = "my-api"                              # folder name under packages/
repo          = "https://github.com/user/repo.git"   # full URL or "user/repo" shorthand
branch        = "main"
start_command = "npm install && npm start"
url           = "http://localhost:3000"               # optional — enables health checks + Open button

[[package]]
name          = "another-service"
repo          = "user/other-repo"
branch        = "main"
start_command = "bash -c 'lsof -ti:8080 | xargs kill -9 2>/dev/null; python3 server.py'"
```

**Notes:**
- `repo` accepts either a full `https://` URL or a `user/repo` shorthand (resolved via `gh` CLI)
- `url` is optional; if provided, Autostart polls it every 5 seconds to determine running/healthy state
- `start_command` is run as a shell command inside the cloned repo directory
- Port-killing can be included directly in `start_command`

## UI Overview

Each service row shows a coloured status indicator:

| Indicator | Meaning |
|---|---|
| Grey ● off | Service is stopped |
| Amber ● starting… | Process launching or URL not yet reachable |
| Green ● running | Process healthy (no URL configured, or URL responding 2xx) |
| Amber ● errors | URL returning 5xx responses |
| Red ● error | Process exited unexpectedly |

When a URL is configured and the service is running, an **Open ↗** button appears to launch it in the browser.

### Advisory hints

When a service is in an error state, Autostart scans the log output for known patterns and shows a plain-English fix hint inline — for example:

- `localStorage is not a function` → SSR guard needed
- `EADDRINUSE` → port already in use (suggests a kill command)
- `command not found` → missing dependency
- `Cannot find module` → missing `npm install`
- `Permission denied` → file permission issue
- `JavaScript heap out of memory` → Node memory limit

## Adding a Service

Click the **+** button in the header (only available when all services are stopped) to add a new service via GitHub URL.

1. Paste a GitHub repo URL or `user/repo` shorthand
2. Click **Detect** — Autostart probes the repo via `gh api` and infers the start command from `package.json`, `Procfile`, `Makefile`, or language files
3. Review and edit the detected fields (name, branch, start command, URL)
4. Click **Add Service** to append the entry to `config.toml`

The new service appears immediately without restarting the app.
