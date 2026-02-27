#!/usr/bin/env python3
"""Autostart — lightweight macOS service manager.

Reads config.toml, git-pulls each configured package, launches their
start_command, and shows a status window with per-package indicators.
"""

from __future__ import annotations

import base64
import enum
import dataclasses
import json
import pathlib
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox
import tomllib
import urllib.error
import urllib.request
import webbrowser
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class AppState(enum.Enum):
    OFF      = "off"
    STARTING = "starting"
    RUNNING  = "running"
    ERROR    = "error"


class _DialogState(enum.Enum):
    IDLE      = "idle"
    DETECTING = "detecting"
    REVIEW    = "review"
    SAVING    = "saving"


# ---------------------------------------------------------------------------
# UI constants
# ---------------------------------------------------------------------------

WINDOW_BG = "#F5F5F5"
PKG_BG    = "#FFFFFF"

HEADER_COLORS: dict[AppState, str] = {
    AppState.OFF:      "#616161",
    AppState.STARTING: "#E65100",
    AppState.RUNNING:  "#2E7D32",
    AppState.ERROR:    "#C62828",
}

STATUS_LABELS: dict[AppState, str] = {
    AppState.OFF:      "● Off",
    AppState.STARTING: "● Starting…",
    AppState.RUNNING:  "● Running",
    AppState.ERROR:    "● Error",
}

BUTTON_LABELS: dict[AppState, str] = {
    AppState.OFF:      "Start",
    AppState.STARTING: "Starting…",
    AppState.RUNNING:  "Stop",
    AppState.ERROR:    "Reset",
}

# Per-package dot colours
DOT_OFF      = "#BDBDBD"
DOT_STARTING = "#FB8C00"
DOT_RUNNING  = "#43A047"
DOT_ERROR    = "#E53935"



# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AutostartError(Exception):
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PackageConfig:
    name: str
    repo: str
    branch: str
    start_command: str
    url: Optional[str] = None

    @property
    def github_url(self) -> str:
        if self.repo.startswith(("https://", "http://", "git@")):
            return self.repo
        return f"https://github.com/{self.repo}.git"


@dataclasses.dataclass
class Config:
    packages_dir: str
    packages: list[PackageConfig]

    @classmethod
    def load(cls, path: pathlib.Path) -> "Config":
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        settings = data.get("settings", {})
        packages_dir = settings.get("packages_dir", "packages")
        pkgs = []
        for entry in data.get("package", []):
            pkgs.append(PackageConfig(
                name=entry["name"],
                repo=entry["repo"],
                branch=entry.get("branch", "main"),
                start_command=entry["start_command"],
                url=entry.get("url"),
            ))
        return cls(packages_dir=packages_dir, packages=pkgs)


# ---------------------------------------------------------------------------
# GitHub detection helpers
# ---------------------------------------------------------------------------

def parse_github_input(raw: str) -> tuple[str, str]:
    """Normalize a GitHub URL or owner/repo shorthand into (owner, repo)."""
    s = raw.strip()
    # SSH: git@github.com:owner/repo[.git]
    m = re.match(r'^git@github\.com:([^/]+)/([^/\s]+?)(?:\.git)?$', s)
    if m:
        return m.group(1), m.group(2)
    # HTTPS: https://github.com/owner/repo[.git][/...]
    m = re.match(r'^https?://github\.com/([^/]+)/([^/\s]+?)(?:\.git)?(?:/.*)?$', s)
    if m:
        return m.group(1), m.group(2)
    # owner/repo shorthand
    m = re.match(r'^([^/\s]+)/([^/\s]+?)(?:\.git)?$', s)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(
        f"Cannot parse GitHub input: {raw!r}\n"
        "Expected: https://github.com/owner/repo, owner/repo, or git@github.com:owner/repo.git"
    )


def gh_api(endpoint: str, timeout: int = 15) -> dict:
    """Run `gh api {endpoint}` and return parsed JSON."""
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise AutostartError("gh CLI not found — install from https://cli.github.com")
    except subprocess.TimeoutExpired:
        raise AutostartError(f"gh api timed out for: {endpoint}")
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise AutostartError(f"gh api failed: {stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AutostartError(f"gh api returned invalid JSON: {exc}")


def gh_file_content(owner: str, repo: str, path: str) -> Optional[str]:
    """Fetch a file from GitHub. Returns decoded text or None if not found."""
    try:
        data = gh_api(f"repos/{owner}/{repo}/contents/{path}")
    except AutostartError as exc:
        msg = str(exc)
        if "404" in msg or "Not Found" in msg:
            return None
        raise
    content = data.get("content", "")
    try:
        return base64.b64decode(content).decode(errors="replace")
    except Exception:
        return None


@dataclasses.dataclass
class DetectionResult:
    name: str          # repo name
    repo_slug: str     # "owner/repo"
    branch: str        # from API default_branch
    start_command: str # inferred or ""
    url: str           # inferred or ""
    confidence: str    # "full" | "partial" | "none"
    notes: str         # human-readable summary


def _detect_from_package_json(content: str) -> tuple[str, str, str]:
    """Returns (start_command, url, notes) from package.json content."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return "", "", "Could not parse package.json."

    scripts = data.get("scripts", {})
    all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}

    script_key = None
    for key in ("dev", "start", "serve"):
        if key in scripts:
            script_key = key
            break

    if script_key is None:
        return "", "", "Found package.json but no dev/start/serve script."

    start_command = f"npm run {script_key}"
    script_value = scripts[script_key]

    port = None
    m = re.search(r'--port[= ](\d+)', script_value)
    if m:
        port = m.group(1)
    else:
        if "next" in all_deps:
            port = "3000"
        elif "vite" in all_deps:
            port = "5173"
        elif "react-scripts" in all_deps:
            port = "3000"
        elif "nuxt" in all_deps:
            port = "3000"
        elif "svelte" in all_deps or "@sveltejs/kit" in all_deps:
            port = "5173"

    url = f"http://localhost:{port}" if port else ""
    return start_command, url, f"Detected npm script: {script_key}."


def _port_from_command(cmd: str) -> Optional[str]:
    """Extract a port number from a shell command string."""
    m = re.search(r'--port[= ](\d+)', cmd)
    if m:
        return m.group(1)
    m = re.search(r'--bind[= ]\S*:(\d+)', cmd)
    if m:
        return m.group(1)
    m = re.search(r'\s-p[= ](\d+)', cmd)
    if m:
        return m.group(1)
    return None


def _port_from_python_source(content: str) -> Optional[str]:
    """Extract a listening port from Python source code."""
    for pattern in (
        r'\.run\s*\([^)]*port\s*=\s*(\d+)',
        r'\bport\s*=\s*(\d+)',
        r'os\.environ\.get\(["\']PORT["\'],\s*["\']?(\d+)["\']?\)',
    ):
        m = re.search(pattern, content)
        if m:
            return m.group(1)
    return None


def _detect_from_python(owner: str, repo: str) -> tuple[str, str, str]:
    """Detect start command and URL for a Python repo. Returns (cmd, url, notes)."""
    # Django
    if gh_file_content(owner, repo, "manage.py") is not None:
        return "python manage.py runserver", "http://localhost:8000", "Detected Django project."

    for filename in ("server.py", "app.py", "main.py", "run.py"):
        content = gh_file_content(owner, repo, filename)
        if content is None:
            continue

        port = _port_from_python_source(content)
        is_fastapi = bool(re.search(r'from fastapi import|import fastapi', content, re.IGNORECASE))
        is_uvicorn_run = bool(re.search(r'uvicorn\.run\s*\(', content))

        if is_fastapi and not is_uvicorn_run:
            cmd = f"uvicorn {filename[:-3]}:app --reload"
        else:
            cmd = f"python {filename}"

        url = f"http://localhost:{port}" if port else ""
        notes = f"Detected Python entry point: {filename}."
        if port:
            notes += f" Port {port} inferred from source."
        else:
            notes += " Could not detect port; enter URL manually."
        return cmd, url, notes

    return "", "", "Detected Python project. Enter start command manually (e.g. python server.py)."


def _detect_from_procfile(content: str) -> tuple[str, str]:
    """Return (web_command, url) from a Procfile, or ("", "") if not found."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("web:"):
            cmd = stripped[4:].strip()
            port = _port_from_command(cmd)
            url = f"http://localhost:{port}" if port else ""
            return cmd, url
    return "", ""


def _makefile_has_start(content: str) -> bool:
    """Return True if the Makefile has a `start:` target."""
    return any(re.match(r'^start\s*:', line) for line in content.splitlines())


def detect_service(owner: str, repo: str) -> DetectionResult:
    """Probe a GitHub repo and infer name, branch, start_command, url."""
    info = gh_api(f"repos/{owner}/{repo}")
    name = info.get("name", repo)
    branch = info.get("default_branch", "main")

    # package.json
    pkg_json = gh_file_content(owner, repo, "package.json")
    if pkg_json is not None:
        cmd, url, notes = _detect_from_package_json(pkg_json)
        confidence = "full" if cmd else "partial"
        return DetectionResult(
            name=name, repo_slug=f"{owner}/{repo}", branch=branch,
            start_command=cmd, url=url, confidence=confidence, notes=notes,
        )

    # Procfile
    procfile = gh_file_content(owner, repo, "Procfile")
    if procfile is not None:
        cmd, url = _detect_from_procfile(procfile)
        # If the command is a Python script and no port was found in the command
        # itself, scan the script source for a port.
        if cmd and not url:
            py_match = re.match(r'python\d*\s+(\S+\.py)', cmd)
            if py_match:
                py_content = gh_file_content(owner, repo, py_match.group(1))
                if py_content:
                    port = _port_from_python_source(py_content)
                    if port:
                        url = f"http://localhost:{port}"
        confidence = "full" if cmd else "partial"
        notes = "Detected from Procfile." if cmd else "Found Procfile but no web: process type."
        return DetectionResult(
            name=name, repo_slug=f"{owner}/{repo}", branch=branch,
            start_command=cmd, url=url, confidence=confidence, notes=notes,
        )

    # Makefile
    makefile = gh_file_content(owner, repo, "Makefile")
    if makefile is not None:
        if _makefile_has_start(makefile):
            return DetectionResult(
                name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                start_command="make start", url="", confidence="full",
                notes="Detected from Makefile.",
            )
        return DetectionResult(
            name=name, repo_slug=f"{owner}/{repo}", branch=branch,
            start_command="", url="", confidence="partial",
            notes="Found Makefile but no start: target.",
        )

    # Python
    if (gh_file_content(owner, repo, "pyproject.toml") is not None
            or gh_file_content(owner, repo, "requirements.txt") is not None):
        cmd, url, notes = _detect_from_python(owner, repo)
        confidence = "full" if cmd else "partial"
        return DetectionResult(
            name=name, repo_slug=f"{owner}/{repo}", branch=branch,
            start_command=cmd, url=url, confidence=confidence, notes=notes,
        )

    # Go
    if gh_file_content(owner, repo, "go.mod") is not None:
        return DetectionResult(
            name=name, repo_slug=f"{owner}/{repo}", branch=branch,
            start_command="", url="", confidence="partial",
            notes="Detected Go project. Enter start command manually (e.g. go run .).",
        )

    return DetectionResult(
        name=name, repo_slug=f"{owner}/{repo}", branch=branch,
        start_command="", url="", confidence="none",
        notes="Could not detect project type. Fill in start command manually.",
    )


def _toml_str(value: str) -> str:
    """Escape a string for use as a TOML basic string value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def append_package_to_config(config_path: pathlib.Path, pkg: PackageConfig) -> None:
    """Append a [[package]] block to config.toml, preserving existing content."""
    lines = [
        "",
        "[[package]]",
        f'name          = "{_toml_str(pkg.name)}"',
        f'repo          = "{_toml_str(pkg.repo)}"',
        f'branch        = "{_toml_str(pkg.branch)}"',
        f'start_command = "{_toml_str(pkg.start_command)}"',
    ]
    if pkg.url:
        lines.append(f'url           = "{_toml_str(pkg.url)}"')
    with config_path.open("a") as fh:
        fh.write("\n".join(lines) + "\n")


def rewrite_config(
    config_path: pathlib.Path,
    packages_dir: str,
    packages: list[PackageConfig],
) -> None:
    """Rewrite config.toml from scratch with the given package list."""
    lines = [
        "[settings]",
        f'packages_dir = "{_toml_str(packages_dir)}"',
    ]
    for pkg in packages:
        lines += [
            "",
            "[[package]]",
            f'name          = "{_toml_str(pkg.name)}"',
            f'repo          = "{_toml_str(pkg.repo)}"',
            f'branch        = "{_toml_str(pkg.branch)}"',
            f'start_command = "{_toml_str(pkg.start_command)}"',
        ]
        if pkg.url:
            lines.append(f'url           = "{_toml_str(pkg.url)}"')
    config_path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _run_git(
    args: list[str],
    cwd: Optional[pathlib.Path] = None,
    timeout: int = 60,
) -> None:
    """Run a git command; raise AutostartError on any failure."""
    try:
        subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise AutostartError("git not found — install Xcode Command Line Tools")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise AutostartError(f"git failed: {stderr}")
    except subprocess.TimeoutExpired:
        raise AutostartError("git timed out")


def _maybe_npm_install(pkg_dir: pathlib.Path) -> None:
    """Run `npm install` if package.json is present. Raises AutostartError on failure."""
    if not (pkg_dir / "package.json").exists():
        return
    try:
        subprocess.run(
            ["npm", "install"],
            cwd=str(pkg_dir),
            check=True,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise AutostartError("npm not found — install Node.js from https://nodejs.org")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise AutostartError(f"npm install failed: {stderr}")
    except subprocess.TimeoutExpired:
        raise AutostartError("npm install timed out")


def ensure_repo(pkg: PackageConfig, packages_dir: pathlib.Path) -> pathlib.Path:
    """Clone or update the repo for *pkg*. Returns the repo directory."""
    pkg_dir = packages_dir / pkg.name
    if not pkg_dir.exists():
        _run_git(
            ["clone", "--depth", "1", "--branch", pkg.branch, pkg.github_url, str(pkg_dir)],
            timeout=120,
        )
    else:
        _run_git(["fetch", "--depth", "1", "origin", pkg.branch], cwd=pkg_dir, timeout=60)
        _run_git(["reset", "--hard", f"origin/{pkg.branch}"], cwd=pkg_dir, timeout=60)
    return pkg_dir


# ---------------------------------------------------------------------------
# Process manager
# ---------------------------------------------------------------------------

class ProcessManager:
    def __init__(self, packages_dir: pathlib.Path) -> None:
        self._packages_dir = packages_dir
        self._procs: dict[str, subprocess.Popen] = {}
        self._log_fhs: dict[str, object] = {}

    def start_all(self, packages: list[PackageConfig]) -> None:
        for pkg in packages:
            pkg_dir = self._packages_dir / pkg.name
            log_path = pkg_dir / "autostart.log"
            log_fh = log_path.open("a")
            self._log_fhs[pkg.name] = log_fh
            proc = subprocess.Popen(
                shlex.split(pkg.start_command),
                cwd=str(pkg_dir),
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
            )
            self._procs[pkg.name] = proc

    def stop_all(self) -> None:
        for proc in self._procs.values():
            try:
                proc.terminate()
            except OSError:
                pass

        deadline = time.monotonic() + 5.0
        for proc in self._procs.values():
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    pass

        for proc in self._procs.values():
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass

        for fh in self._log_fhs.values():
            try:
                fh.close()
            except OSError:
                pass

        self._procs.clear()
        self._log_fhs.clear()

    def poll_all(self) -> Optional[str]:
        """Return name of first exited process, or None if all still running."""
        for name, proc in self._procs.items():
            if proc.poll() is not None:
                return name
        return None

    @property
    def has_processes(self) -> bool:
        return bool(self._procs)


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

def _worker(
    config: Config,
    packages_dir: pathlib.Path,
    pm: ProcessManager,
    ui_queue: queue.Queue,
) -> None:
    """Runs in a daemon thread. Performs git ops then launches processes."""
    current_pkg = None
    try:
        for pkg in config.packages:
            current_pkg = pkg.name
            pkg_dir = ensure_repo(pkg, packages_dir)
            _maybe_npm_install(pkg_dir)
        pm.start_all(config.packages)
        ui_queue.put(("state", AppState.RUNNING, "", ""))
    except AutostartError as exc:
        pm.stop_all()
        ui_queue.put(("state", AppState.ERROR, str(exc), current_pkg or ""))
    except Exception as exc:
        pm.stop_all()
        ui_queue.put(("state", AppState.ERROR, f"Unexpected error: {exc}", current_pkg or ""))


# ---------------------------------------------------------------------------
# Add Service dialog
# ---------------------------------------------------------------------------

class Tooltip:
    """Minimal hover tooltip for tkinter widgets."""

    def __init__(self, widget: tk.Widget, text: str, font_name: str = "TkDefaultFont", delay: int = 400) -> None:
        self._widget = widget
        self._text = text
        self._font_name = font_name
        self._delay = delay
        self._tip: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._cancel)
        widget.bind("<ButtonPress>", self._cancel)

    def _schedule(self, event=None) -> None:
        self._cancel()
        self._after_id = self._widget.after(self._delay, self._show)

    def _cancel(self, event=None) -> None:
        if self._after_id:
            self._widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None

    def _show(self) -> None:
        x = self._widget.winfo_rootx() + self._widget.winfo_width() // 2
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=self._text,
            font=(self._font_name, 10),
            fg="#424242",
            bg="#F5F5F5",
            relief=tk.SOLID,
            bd=1,
            padx=6,
            pady=3,
        ).pack()


class AddServiceDialog:
    _POLL_MS = 100

    def __init__(
        self,
        parent: tk.Tk,
        config_path: pathlib.Path,
        existing_names: list[str],
        on_confirm: Callable[[PackageConfig], None],
        font_name: str,
    ) -> None:
        self._config_path = config_path
        self._existing_names = existing_names
        self._on_confirm = on_confirm
        self._font_name = font_name
        self._state = _DialogState.IDLE
        self._queue: queue.Queue = queue.Queue()
        self._detection_result: Optional[DetectionResult] = None

        top = tk.Toplevel(parent)
        top.title("Add Service")
        top.resizable(False, False)
        top.configure(bg=WINDOW_BG)
        top.grab_set()
        top.transient(parent)
        self._top = top

        self._build_ui()
        top.after(self._POLL_MS, self._poll)

    def _build_ui(self) -> None:
        fn = self._font_name

        # Title
        tk.Label(
            self._top,
            text="Add a Service",
            bg=WINDOW_BG,
            fg="#212121",
            font=(fn, 14, "bold"),
        ).pack(anchor="w", padx=16, pady=(16, 4))

        # Subtitle
        tk.Label(
            self._top,
            text="Paste a GitHub URL or owner/repo shorthand.",
            bg=WINDOW_BG,
            fg="#757575",
            font=(fn, 11),
        ).pack(anchor="w", padx=16, pady=(0, 8))

        # ── Input row ─────────────────────────────────────────────────────
        input_row = tk.Frame(self._top, bg=WINDOW_BG)
        input_row.pack(fill=tk.X, padx=16, pady=(0, 4))

        entry_var = tk.StringVar()
        entry = tk.Entry(
            input_row,
            textvariable=entry_var,
            font=(fn, 12),
            width=36,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#BDBDBD",
            highlightcolor="#1565C0",
        )
        entry.pack(side=tk.LEFT, ipady=6)
        entry.focus_set()
        self._entry_var = entry_var
        self._entry = entry

        detect_btn = tk.Button(
            input_row,
            text="Detect",
            font=(fn, 12),
            command=self._on_detect,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            cursor="hand2",
            padx=10,
        )
        detect_btn.pack(side=tk.LEFT, padx=(8, 0), ipady=6)
        self._detect_btn = detect_btn

        entry.bind("<Return>", lambda _e: self._on_detect())

        # ── Status label ───────────────────────────────────────────────────
        status_lbl = tk.Label(
            self._top,
            text="",
            bg=WINDOW_BG,
            fg="#757575",
            font=(fn, 10),
            wraplength=460,
            justify="left",
            anchor="w",
        )
        status_lbl.pack(fill=tk.X, padx=16, pady=(2, 0))
        self._status_lbl = status_lbl

        # ── Review frame (hidden initially, inserted before sep on detection) ──
        self._review_frame = tk.Frame(self._top, bg=WINDOW_BG)

        # ── Separator ─────────────────────────────────────────────────────
        self._sep = tk.Frame(self._top, bg="#BDBDBD", height=1)
        self._sep.pack(fill=tk.X, pady=8)

        # ── Bottom buttons ─────────────────────────────────────────────────
        btn_row = tk.Frame(self._top, bg=WINDOW_BG)
        btn_row.pack(fill=tk.X, padx=16, pady=(0, 16))
        self._btn_row = btn_row

        cancel_btn = tk.Button(
            btn_row,
            text="Cancel",
            font=(fn, 12),
            command=self._top.destroy,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            cursor="hand2",
            padx=10,
        )
        cancel_btn.pack(side=tk.LEFT, padx=(0, 8), ipady=6)

        confirm_btn = tk.Button(
            btn_row,
            text="Add Service",
            font=(fn, 12),
            command=self._on_confirm_clicked,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            cursor="hand2",
            padx=10,
            state=tk.DISABLED,
        )
        confirm_btn.pack(side=tk.LEFT, ipady=6)
        self._confirm_btn = confirm_btn

    def _show_review(self, result: DetectionResult) -> None:
        """Populate and show the editable review form."""
        fn = self._font_name
        frame = self._review_frame

        # Clear any previous review widgets
        for w in frame.winfo_children():
            w.destroy()

        def _field(label: str, var: tk.StringVar, highlight_empty: bool = False) -> tk.Entry:
            row = tk.Frame(frame, bg=WINDOW_BG)
            row.pack(fill=tk.X, pady=3)
            tk.Label(
                row, text=label, bg=WINDOW_BG, fg="#616161",
                font=(fn, 10), width=16, anchor="e",
            ).pack(side=tk.LEFT)
            e = tk.Entry(
                row,
                textvariable=var,
                font=(fn, 12),
                width=30,
                relief=tk.FLAT,
                highlightthickness=1,
                highlightbackground="#BDBDBD",
                highlightcolor="#1565C0",
            )
            e.pack(side=tk.LEFT, padx=(8, 0), ipady=5)
            if highlight_empty:
                def _update(*_):
                    e.configure(
                        highlightbackground="#FFA726" if not var.get().strip() else "#BDBDBD"
                    )
                var.trace_add("write", _update)
                _update()
            return e

        name_var   = tk.StringVar(value=result.name)
        branch_var = tk.StringVar(value=result.branch)
        cmd_var    = tk.StringVar(value=result.start_command)
        url_var    = tk.StringVar(value=result.url)

        _field("Name", name_var)
        _field("Branch", branch_var)
        _field("Start command", cmd_var, highlight_empty=True)
        _field("URL", url_var)

        self._name_var   = name_var
        self._branch_var = branch_var
        self._cmd_var    = cmd_var
        self._url_var    = url_var

        # Duplicate name warning label
        dup_lbl = tk.Label(
            frame, text="", bg=WINDOW_BG, fg="#C62828", font=(fn, 10),
        )
        dup_lbl.pack(anchor="w", pady=(2, 0))
        self._dup_lbl = dup_lbl

        def _validate(*_):
            name = name_var.get().strip()
            cmd  = cmd_var.get().strip()
            if not name or not cmd:
                self._confirm_btn.configure(state=tk.DISABLED)
                dup_lbl.configure(text="")
                return
            if name in self._existing_names:
                self._confirm_btn.configure(state=tk.DISABLED)
                dup_lbl.configure(text=f'A service named "{name}" already exists.')
                return
            self._confirm_btn.configure(state=tk.NORMAL)
            dup_lbl.configure(text="")

        name_var.trace_add("write", _validate)
        cmd_var.trace_add("write", _validate)
        _validate()

        # Insert review frame just above the separator
        frame.pack(fill=tk.X, padx=16, pady=(8, 0), before=self._sep)
        self._top.geometry("")

    def _on_detect(self) -> None:
        raw = self._entry_var.get().strip()
        if not raw:
            self._status_lbl.configure(
                text="Please enter a GitHub URL or owner/repo.", fg="#C62828"
            )
            return
        try:
            owner, repo = parse_github_input(raw)
        except ValueError as exc:
            self._status_lbl.configure(text=str(exc), fg="#C62828")
            return

        self._state = _DialogState.DETECTING
        self._detect_btn.configure(state=tk.DISABLED, text="Detecting…")
        self._status_lbl.configure(
            text=f"Fetching info for {owner}/{repo}…", fg="#757575"
        )

        # Hide any previous review
        self._review_frame.pack_forget()
        self._confirm_btn.configure(state=tk.DISABLED)
        self._top.geometry("")

        def _worker_fn() -> None:
            try:
                result = detect_service(owner, repo)
                self._queue.put(("ok", result))
            except AutostartError as exc:
                self._queue.put(("error", str(exc)))
            except Exception as exc:
                self._queue.put(("error", f"Unexpected error: {exc}"))

        threading.Thread(target=_worker_fn, daemon=True).start()

    def _poll(self) -> None:
        if not self._top.winfo_exists():
            return
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg[0] == "ok":
                    result: DetectionResult = msg[1]
                    self._detection_result = result
                    self._state = _DialogState.REVIEW
                    self._detect_btn.configure(state=tk.NORMAL, text="Detect")
                    self._status_lbl.configure(text="", fg="#757575")
                    self._show_review(result)
                elif msg[0] == "error":
                    self._state = _DialogState.IDLE
                    self._detect_btn.configure(state=tk.NORMAL, text="Detect")
                    self._status_lbl.configure(text=msg[1], fg="#C62828")
        except queue.Empty:
            pass
        self._top.after(self._POLL_MS, self._poll)

    def _on_confirm_clicked(self) -> None:
        if self._detection_result is None:
            return
        name    = self._name_var.get().strip()
        branch  = self._branch_var.get().strip() or "main"
        cmd     = self._cmd_var.get().strip()
        raw_url = self._url_var.get().strip()
        url     = raw_url if raw_url else None

        pkg = PackageConfig(
            name=name,
            repo=self._detection_result.repo_slug,
            branch=branch,
            start_command=cmd,
            url=url,
        )

        self._state = _DialogState.SAVING
        self._confirm_btn.configure(state=tk.DISABLED)
        try:
            append_package_to_config(self._config_path, pkg)
        except OSError as exc:
            self._status_lbl.configure(
                text=f"Failed to save config: {exc}", fg="#C62828"
            )
            self._confirm_btn.configure(state=tk.NORMAL)
            self._state = _DialogState.REVIEW
            return

        self._top.destroy()
        self._on_confirm(pkg)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class AutostartApp:
    _POLL_MS = 100
    _MONITOR_INTERVAL = 2.0

    def __init__(self, config: Config, config_path: pathlib.Path) -> None:
        self._config = config
        self._config_path = config_path
        self._packages_dir = config_path.parent / config.packages_dir
        self._packages_dir.mkdir(parents=True, exist_ok=True)

        self._state = AppState.OFF
        self._ui_queue: queue.Queue = queue.Queue()
        self._pm = ProcessManager(self._packages_dir)
        self._worker_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._build_ui()

    # ---- UI construction -----------------------------------------------

    def _build_ui(self) -> None:
        root = tk.Tk()
        root.title("Autostart")
        root.resizable(False, True)
        root.configure(bg=WINDOW_BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root = root

        # Font resolution
        available = tkfont.families()
        for candidate in (".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue"):
            if candidate in available:
                font_name = candidate
                break
        else:
            font_name = "TkDefaultFont"

        self._font_name = font_name

        header_font = (font_name, 13, "bold")
        btn_font    = (font_name, 12)

        # ── Header: status label (left) + buttons (right) ──────────────────
        header = tk.Frame(root, bg=HEADER_COLORS[AppState.OFF])
        header.pack(fill=tk.X)

        status_label = tk.Label(
            header,
            text=STATUS_LABELS[AppState.OFF],
            bg=HEADER_COLORS[AppState.OFF],
            fg="#FFFFFF",
            font=header_font,
        )
        status_label.pack(side=tk.LEFT, padx=16, pady=14)

        toggle_btn = tk.Button(
            header,
            text=BUTTON_LABELS[AppState.OFF],
            font=btn_font,
            command=self._on_toggle,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            cursor="hand2",
        )
        toggle_btn.pack(side=tk.RIGHT, padx=12, pady=10)

        add_btn = tk.Button(
            header,
            text="+",
            font=btn_font,
            command=self._on_add_service,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            cursor="hand2",
            padx=8,
        )
        add_btn.pack(side=tk.RIGHT, padx=(0, 4), pady=10)

        self._header       = header
        self._status_label = status_label
        self._toggle_btn   = toggle_btn
        self._add_btn      = add_btn

        # ── Divider ────────────────────────────────────────────────────────
        tk.Frame(root, bg="#BDBDBD", height=1).pack(fill=tk.X)

        # ── Per-package rows ───────────────────────────────────────────────
        pkg_frame = tk.Frame(root, bg=PKG_BG)
        pkg_frame.pack(fill=tk.X)
        self._pkg_frame = pkg_frame

        self._pkg_widgets: dict[str, dict] = {}

        for i, pkg in enumerate(self._config.packages):
            self._add_pkg_row(pkg, pkg_frame, font_name, is_first=(i == 0))

        root.after(self._POLL_MS, self._poll_queue)

    def _add_pkg_row(
        self,
        pkg: PackageConfig,
        pkg_frame: tk.Frame,
        font_name: str,
        is_first: bool = False,
    ) -> None:
        """Build and pack a package row into pkg_frame."""
        pkg_font   = (font_name, 12)
        state_font = (font_name, 10)

        divider = None
        if not is_first:
            divider = tk.Frame(pkg_frame, bg="#E0E0E0", height=1)
            divider.pack(fill=tk.X)

        container = tk.Frame(pkg_frame, bg=PKG_BG)
        container.pack(fill=tk.X)

        row = tk.Frame(container, bg=PKG_BG)
        row.pack(fill=tk.X, padx=16, pady=10)

        dot = tk.Label(row, text="●", fg=DOT_OFF, bg=PKG_BG, font=(font_name, 11))
        dot.pack(side=tk.LEFT)

        name_lbl = tk.Label(
            row, text=pkg.name, bg=PKG_BG, fg="#212121", font=pkg_font,
        )
        name_lbl.pack(side=tk.LEFT, padx=(8, 0))

        state_lbl = tk.Label(
            row, text="off", bg=PKG_BG, fg="#9E9E9E", font=state_font,
        )
        state_lbl.pack(side=tk.RIGHT)

        # Open button — only for packages with a url; hidden until running
        open_btn = None
        if pkg.url:
            _port_m = re.search(r':(\d+)', pkg.url)
            _open_label = f"localhost:{_port_m.group(1)} ↗" if _port_m else "Open ↗"
            open_btn = tk.Button(
                row,
                text=_open_label,
                font=(font_name, 11),
                fg="#1565C0",
                bg=PKG_BG,
                activeforeground="#0D47A1",
                activebackground="#E3F2FD",
                relief=tk.FLAT,
                bd=0,
                highlightthickness=0,
                padx=6,
                pady=2,
                cursor="hand2",
                command=lambda u=pkg.url: webbrowser.open(u),
            )
            Tooltip(open_btn, f"Open {pkg.url} in browser", font_name=font_name)
            # Packed when running, hidden otherwise

        # Remove button — shown only in OFF state
        remove_btn = tk.Label(
            row,
            text="×",
            bg=PKG_BG,
            fg="#9E9E9E",
            font=(font_name, 14),
            cursor="hand2",
            padx=8,
            pady=0,
        )
        remove_btn.bind("<Button-1>", lambda e, n=pkg.name: self._on_remove_service(n))
        remove_btn.pack(side=tk.RIGHT, padx=(0, 4))  # initial state is OFF

        # ── Summary row (yellow bar with toggle) ──────────────────────────
        diag_row = tk.Frame(container, bg="#FFF8E1")

        diag_lbl = tk.Label(
            diag_row,
            text="",
            bg="#FFF8E1",
            fg="#E65100",
            font=(font_name, 10),
            anchor="w",
            padx=16,
            pady=6,
        )
        diag_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        accordion_btn = tk.Label(
            diag_row,
            text="▶ View full error",
            bg="#FFF8E1",
            fg="#1565C0",
            font=(font_name, 10),
            cursor="hand2",
            padx=16,
            pady=6,
        )
        accordion_btn.pack(side=tk.RIGHT)
        accordion_btn.bind("<Button-1>", lambda e, n=pkg.name: self._toggle_accordion(n))

        # ── Collapsible log tail (starts hidden) ──────────────────────────
        err_lbl = tk.Label(
            container,
            text="",
            bg="#FFEBEE",
            fg="#B71C1C",
            font=("Menlo", 9),
            wraplength=316,
            justify="left",
            anchor="w",
            padx=16,
            pady=8,
        )
        # NOT packed — starts collapsed

        self._pkg_widgets[pkg.name] = {
            "container":      container,
            "divider":        divider,
            "dot":            dot,
            "state_lbl":      state_lbl,
            "open_btn":       open_btn,
            "remove_btn":     remove_btn,
            "diag_row":       diag_row,
            "diag_lbl":       diag_lbl,
            "accordion_btn":  accordion_btn,
            "accordion_open": False,
            "err_lbl":        err_lbl,
        }

    def _toggle_accordion(self, pkg_name: str) -> None:
        w = self._pkg_widgets[pkg_name]
        if w["accordion_open"]:
            w["err_lbl"].pack_forget()
            w["accordion_btn"].configure(text="▶ View full error")
            w["accordion_open"] = False
        else:
            w["err_lbl"].pack(fill=tk.X, after=w["diag_row"])
            w["accordion_btn"].configure(text="▼ Hide full error")
            w["accordion_open"] = True
        self._root.geometry("")

    # ---- State management -----------------------------------------------

    def _set_state(
        self, new_state: AppState, error_msg: str = "", failed_pkg: str = ""
    ) -> None:
        """Must only be called from the main (tkinter) thread."""
        self._state = new_state
        color = HEADER_COLORS[new_state]

        # Header
        self._header.configure(bg=color)
        self._status_label.configure(text=STATUS_LABELS[new_state], bg=color)
        self._toggle_btn.configure(
            text=BUTTON_LABELS[new_state],
            state=tk.DISABLED if new_state == AppState.STARTING else tk.NORMAL,
        )
        self._add_btn.configure(
            state=tk.NORMAL if new_state == AppState.OFF else tk.DISABLED,
        )

        # Per-package rows
        for pkg_name, w in self._pkg_widgets.items():
            w["diag_row"].pack_forget()
            w["err_lbl"].pack_forget()
            w["accordion_open"] = False
            w["accordion_btn"].configure(text="▶ View full error")

            has_url = w["open_btn"] is not None

            if new_state == AppState.OFF:
                dot_color, state_text, state_fg = DOT_OFF, "off", "#9E9E9E"
            elif new_state == AppState.STARTING:
                dot_color, state_text, state_fg = DOT_STARTING, "starting…", "#E65100"
            elif new_state == AppState.RUNNING:
                if has_url:
                    # Stay amber until health check confirms it's up
                    dot_color, state_text, state_fg = DOT_STARTING, "starting…", "#E65100"
                else:
                    dot_color, state_text, state_fg = DOT_RUNNING, "running", "#2E7D32"
            elif new_state == AppState.ERROR:
                if pkg_name == failed_pkg:
                    dot_color, state_text, state_fg = DOT_ERROR, "error", "#C62828"
                elif failed_pkg:
                    dot_color, state_text, state_fg = DOT_RUNNING, "running", "#2E7D32"
                else:
                    dot_color, state_text, state_fg = DOT_OFF, "off", "#9E9E9E"
            else:
                dot_color, state_text, state_fg = DOT_OFF, "off", "#9E9E9E"

            w["dot"].configure(fg=dot_color)
            w["state_lbl"].configure(text=state_text, fg=state_fg)

            if w["open_btn"]:
                # Open button shown only once health check confirms server is up
                if new_state != AppState.RUNNING:
                    w["open_btn"].pack_forget()

            if new_state == AppState.OFF:
                w["remove_btn"].pack(side=tk.RIGHT, padx=(0, 4))
            else:
                w["remove_btn"].pack_forget()

        # Error detail under the relevant package (or last package for config errors)
        if new_state == AppState.ERROR and error_msg:
            target = failed_pkg if failed_pkg in self._pkg_widgets else (
                list(self._pkg_widgets.keys())[-1] if self._pkg_widgets else None
            )
            if target:
                w = self._pkg_widgets[target]
                w["diag_lbl"].configure(
                    text=f"Service error — check the logs in {target}/autostart.log"
                )
                w["err_lbl"].configure(text=error_msg)
                w["diag_row"].pack(fill=tk.X)
                # err_lbl stays collapsed; user clicks toggle to see it

        self._root.geometry("")  # let tkinter auto-size height

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._ui_queue.get_nowait()
                if msg[0] == "state":
                    new_state  = msg[1]
                    error_msg  = msg[2] if len(msg) > 2 else ""
                    failed_pkg = msg[3] if len(msg) > 3 else ""
                    self._set_state(new_state, error_msg, failed_pkg)
                    if new_state == AppState.RUNNING:
                        self._start_monitor()
                elif msg[0] == "pkg_health":
                    self._apply_pkg_health(msg[1], msg[2])
        except queue.Empty:
            pass
        self._root.after(self._POLL_MS, self._poll_queue)

    # ---- User actions ---------------------------------------------------

    def _on_toggle(self) -> None:
        if self._state == AppState.OFF:
            self._do_start()
        elif self._state == AppState.RUNNING:
            self._do_stop()
        elif self._state == AppState.ERROR:
            self._do_reset()

    def _do_start(self) -> None:
        self._set_state(AppState.STARTING)
        self._pm = ProcessManager(self._packages_dir)
        t = threading.Thread(
            target=_worker,
            args=(self._config, self._packages_dir, self._pm, self._ui_queue),
            daemon=True,
        )
        self._worker_thread = t
        t.start()

    def _do_stop(self) -> None:
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=self._MONITOR_INTERVAL + 1)
        self._pm.stop_all()
        self._stop_event.clear()
        self._set_state(AppState.OFF)

    def _do_reset(self) -> None:
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=self._MONITOR_INTERVAL + 1)
        self._pm.stop_all()
        self._stop_event.clear()
        self._set_state(AppState.OFF)

    def _on_add_service(self) -> None:
        existing_names = [p.name for p in self._config.packages]
        AddServiceDialog(
            parent=self._root,
            config_path=self._config_path,
            existing_names=existing_names,
            on_confirm=self._on_service_added,
            font_name=self._font_name,
        )

    def _on_service_added(self, pkg: PackageConfig) -> None:
        is_first = len(self._config.packages) == 0
        self._config.packages.append(pkg)
        self._add_pkg_row(pkg, self._pkg_frame, self._font_name, is_first=is_first)
        self._root.geometry("")

    def _on_remove_service(self, pkg_name: str) -> None:
        pkg_dir = self._packages_dir / pkg_name
        msg = (
            f"Remove '{pkg_name}' from the service list?"
            + (f"\n\nThis will also delete {pkg_dir}." if pkg_dir.exists() else "")
            + "\n\nThis cannot be undone."
        )
        if not tkinter.messagebox.askyesno("Remove Service", msg, icon="warning"):
            return

        pkg_names = [p.name for p in self._config.packages]
        idx = pkg_names.index(pkg_name)

        # If removing the first item and a second exists, destroy its divider
        if idx == 0 and len(self._config.packages) > 1:
            next_name = self._config.packages[1].name
            next_w = self._pkg_widgets[next_name]
            if next_w["divider"] is not None:
                next_w["divider"].destroy()
                next_w["divider"] = None

        # Remove from in-memory config
        self._config.packages.pop(idx)

        # Destroy all widgets for this package
        w = self._pkg_widgets.pop(pkg_name)
        if w["divider"] is not None:
            w["divider"].destroy()
        w["container"].destroy()

        # Rewrite config.toml
        rewrite_config(self._config_path, self._config.packages_dir, self._config.packages)

        # Delete local directory
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir, ignore_errors=True)

        self._root.geometry("")

    # ---- Health checks --------------------------------------------------

    def _start_health_checks(self) -> None:
        for pkg in self._config.packages:
            if pkg.url:
                t = threading.Thread(
                    target=self._health_check_loop,
                    args=(pkg.name, pkg.url),
                    daemon=True,
                )
                t.start()

    def _health_check_loop(self, pkg_name: str, url: str) -> None:
        while not self._stop_event.is_set():
            try:
                resp = urllib.request.urlopen(url, timeout=4)
                status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except (urllib.error.URLError, OSError):
                status = 0  # not yet listening

            self._ui_queue.put(("pkg_health", pkg_name, status))
            self._stop_event.wait(5)

    def _apply_pkg_health(self, pkg_name: str, status: int) -> None:
        """Update a package row based on its HTTP health status. Main thread only."""
        if self._state != AppState.RUNNING:
            return
        if pkg_name not in self._pkg_widgets:
            return

        w = self._pkg_widgets[pkg_name]

        if status == 0:
            # Not yet listening — keep showing "starting…"
            w["dot"].configure(fg=DOT_STARTING)
            w["state_lbl"].configure(text="starting…", fg="#E65100")
            w["open_btn"].pack_forget()
            w["diag_row"].pack_forget()
            w["err_lbl"].pack_forget()
            w["accordion_open"] = False
            w["accordion_btn"].configure(text="▶ View full error")
        elif status >= 500:
            # Listening but returning server errors — show log
            w["dot"].configure(fg=DOT_STARTING)
            w["state_lbl"].configure(text="errors", fg="#E65100")
            w["open_btn"].pack(side=tk.RIGHT, padx=(6, 0))
            log_text = self._read_log_tail(pkg_name)
            w["diag_lbl"].configure(
                text=f"Service error — check the logs in {pkg_name}/autostart.log"
            )
            w["err_lbl"].configure(text=log_text)
            w["diag_row"].pack(fill=tk.X)
            if w["accordion_open"]:
                w["err_lbl"].pack(fill=tk.X, after=w["diag_row"])
            # If closed, leave err_lbl hidden — don't snap it shut on every poll
        else:
            # Healthy
            w["dot"].configure(fg=DOT_RUNNING)
            w["state_lbl"].configure(text="running", fg="#2E7D32")
            w["open_btn"].pack(side=tk.RIGHT, padx=(6, 0))
            w["diag_row"].pack_forget()
            w["err_lbl"].pack_forget()
            w["accordion_open"] = False
            w["accordion_btn"].configure(text="▶ View full error")

        self._root.geometry("")

    # ---- Monitor thread -------------------------------------------------

    def _start_monitor(self) -> None:
        self._stop_event.clear()
        t = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread = t
        t.start()
        self._start_health_checks()

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self._MONITOR_INTERVAL):
            dead = self._pm.poll_all()
            if dead is not None:
                self._pm.stop_all()
                error_msg = self._read_log_tail(dead)
                self._root.after(
                    0,
                    lambda name=dead, msg=error_msg: self._set_state(
                        AppState.ERROR, msg, name
                    ),
                )
                return

    def _read_log_tail(self, pkg_name: str, n: int = 8) -> str:
        """Return the last n meaningful lines from a package's log."""
        log_path = self._packages_dir / pkg_name / "autostart.log"
        try:
            text = log_path.read_text(errors="replace")
            lines = []
            for raw in text.splitlines():
                # Strip concurrently's [PREFIX] markers and ANSI colour codes
                line = re.sub(r'\x1b\[[0-9;]*m', '', raw)
                line = re.sub(r'^\[[^\]]+\]\s*', '', line).strip()
                if line:
                    lines.append(line)
            tail = lines[-n:] if len(lines) >= n else lines
            return "\n".join(tail) if tail else f"{pkg_name} exited (no log output)"
        except OSError:
            return f"{pkg_name} exited unexpectedly"

    # ---- Window close ---------------------------------------------------

    def _on_close(self) -> None:
        self._stop_event.set()
        self._pm.stop_all()
        self._root.destroy()

    # ---- Run ------------------------------------------------------------

    def run(self) -> None:
        self._root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config_path = pathlib.Path(__file__).parent / "config.toml"
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        config = Config.load(config_path)
    except (KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"Error: invalid config.toml — {exc}", file=sys.stderr)
        sys.exit(1)

    app = AutostartApp(config, config_path)
    app.run()


if __name__ == "__main__":
    main()
