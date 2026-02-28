#!/usr/bin/env python3
"""Autostart — lightweight macOS service manager with per-service controls."""

from __future__ import annotations

import base64
import enum
import dataclasses
import json
import os
import pathlib
import queue
import re
import shlex
import shutil
import signal
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

class PkgState(enum.Enum):
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
# UI constants — dark theme
# ---------------------------------------------------------------------------

WINDOW_BG         = "#1E1E24"
CARD_BG           = "#2A2A32"
CARD_BORDER       = "#3A3A44"
CARD_BORDER_HOVER = "#52525B"
HEADER_BG         = "#1E1E24"

TEXT_PRIMARY   = "#F5F5F7"
TEXT_SECONDARY = "#A0A0AB"
TEXT_TERTIARY  = "#6B6B76"

BLUE          = "#5B8DEF"
BLUE_HOVER    = "#7BA4F7"
RED           = "#F87171"
RED_HOVER     = "#EF4444"
STOP_BG       = "#DC2626"
DISABLED_BG   = "#3A3A44"
DISABLED_TEXT = "#6B6B76"

GREEN      = "#4ADE80"
GREEN_GLOW = "#1A5C32"
AMBER      = "#FBBF24"

# Indent for row-2 to align content under the service name (dot canvas + gap)
DOT_CANVAS_INDENT = 34

PILL_COLORS: dict[PkgState, tuple[str, str]] = {
    PkgState.OFF:      ("#3A3A44", "#A0A0AB"),
    PkgState.STARTING: ("#422006", "#FCD34D"),
    PkgState.RUNNING:  ("#052E16", "#86EFAC"),
    PkgState.ERROR:    ("#450A0A", "#FCA5A5"),
}

PILL_LABELS: dict[PkgState, str] = {
    PkgState.OFF:      "Off",
    PkgState.STARTING: "Starting…",
    PkgState.RUNNING:  "Running",
    PkgState.ERROR:    "Error",
}

DOT_COLORS: dict[PkgState, str] = {
    PkgState.OFF:      "#52525B",
    PkgState.STARTING: "#FBBF24",
    PkgState.RUNNING:  "#4ADE80",
    PkgState.ERROR:    "#F87171",
}


# ---------------------------------------------------------------------------
# Label-based button (macOS Aqua ignores bg/fg on tk.Button)
# ---------------------------------------------------------------------------

class LabelButton:
    """A tk.Label masquerading as a button — because macOS Aqua tk.Button
    ignores custom bg/fg colors entirely."""

    def __init__(
        self,
        parent: tk.Widget,
        text: str = "",
        font: tuple = (),
        bg: str = BLUE,
        fg: str = "#FFFFFF",
        hover_bg: str = BLUE_HOVER,
        hover_fg: str = "#FFFFFF",
        disabled_bg: str = DISABLED_BG,
        disabled_fg: str = DISABLED_TEXT,
        padx: int = 16,
        pady: int = 7,
        command: Optional[Callable] = None,
        cursor: str = "pointinghand",
    ) -> None:
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg
        self._hover_fg = hover_fg
        self._disabled_bg = disabled_bg
        self._disabled_fg = disabled_fg
        self._command = command
        self._enabled = True

        self._label = tk.Label(
            parent,
            text=text,
            font=font,
            bg=bg,
            fg=fg,
            padx=padx,
            pady=pady,
            cursor=cursor,
        )
        self._label.bind("<Button-1>", self._on_click)
        self._label.bind("<Enter>", self._on_enter)
        self._label.bind("<Leave>", self._on_leave)

    @property
    def widget(self) -> tk.Label:
        return self._label

    def pack(self, **kwargs) -> None:
        self._label.pack(**kwargs)

    def pack_forget(self) -> None:
        self._label.pack_forget()

    def configure(self, **kwargs) -> None:
        if "state" in kwargs:
            state = kwargs.pop("state")
            self._enabled = (state != tk.DISABLED)
            if not self._enabled:
                self._label.configure(bg=self._disabled_bg, fg=self._disabled_fg, cursor="")
            else:
                self._label.configure(bg=self._bg, fg=self._fg, cursor="pointinghand")
        if "bg" in kwargs:
            self._bg = kwargs["bg"]
            if self._enabled:
                self._label.configure(bg=self._bg)
        if "fg" in kwargs:
            self._fg = kwargs["fg"]
            if self._enabled:
                self._label.configure(fg=self._fg)
        if "hover_bg" in kwargs:
            self._hover_bg = kwargs.pop("hover_bg")
        if "hover_fg" in kwargs:
            self._hover_fg = kwargs.pop("hover_fg")
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if "cursor" in kwargs:
            if self._enabled:
                self._label.configure(cursor=kwargs["cursor"])
            kwargs.pop("cursor")
        passthrough = {k: v for k, v in kwargs.items() if k not in ("bg", "fg")}
        if passthrough:
            self._label.configure(**passthrough)

    def _on_click(self, _event) -> None:
        if self._enabled and self._command:
            self._command()

    def _on_enter(self, _event) -> None:
        if self._enabled:
            self._label.configure(bg=self._hover_bg, fg=self._hover_fg)

    def _on_leave(self, _event) -> None:
        if self._enabled:
            self._label.configure(bg=self._bg, fg=self._fg)


# ---------------------------------------------------------------------------
# Canvas-based button — rounded corners, works on macOS Aqua
# ---------------------------------------------------------------------------

class CanvasButton:
    """Button drawn on a tk.Canvas — rounded corners and full color control
    on macOS Aqua (tk.Button ignores bg/fg entirely on this platform)."""

    RADIUS = 8

    def __init__(
        self,
        parent: tk.Widget,
        text: str = "",
        font: tuple = (),
        bg: str = BLUE,
        fg: str = "#FFFFFF",
        hover_bg: str = BLUE_HOVER,
        hover_fg: str = "#FFFFFF",
        disabled_bg: str = DISABLED_BG,
        disabled_fg: str = DISABLED_TEXT,
        padx: int = 16,
        pady: int = 6,
        command: Optional[Callable] = None,
        parent_bg: str = CARD_BG,
        min_width: int = 0,
    ) -> None:
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg
        self._hover_fg = hover_fg
        self._disabled_bg = disabled_bg
        self._disabled_fg = disabled_fg
        self._command = command
        self._enabled = True
        self._text = text
        self._font_spec = font
        self._padx = padx
        self._pady = pady
        self._min_w = min_width

        tmp = self._make_font()
        text_w = tmp.measure(text)
        text_h = tmp.metrics("linespace")
        self._w = max(text_w + padx * 2, min_width)
        self._h = text_h + pady * 2

        self._canvas = tk.Canvas(
            parent,
            width=self._w, height=self._h,
            bg=parent_bg,
            highlightthickness=0,
            cursor="pointinghand",
        )
        self._rect = self._draw_rounded_rect(bg)
        self._text_id = self._canvas.create_text(
            self._w // 2, self._h // 2,
            text=text, fill=fg, font=font,
        )
        self._canvas.bind("<Button-1>", self._on_click)
        self._canvas.bind("<Enter>", self._on_enter)
        self._canvas.bind("<Leave>", self._on_leave)

    def _make_font(self) -> tkfont.Font:
        f = self._font_spec
        return tkfont.Font(
            family=f[0] if f else "TkDefaultFont",
            size=f[1] if len(f) > 1 else 12,
            weight=f[2] if len(f) > 2 else "normal",
        )

    def _draw_rounded_rect(self, fill: str) -> int:
        r = self.RADIUS
        x1, y1, x2, y2 = 1, 1, self._w - 1, self._h - 1
        points = [
            x1+r, y1,   x2-r, y1,
            x2,   y1,   x2,   y1+r,
            x2,   y2-r, x2,   y2,
            x2-r, y2,   x1+r, y2,
            x1,   y2,   x1,   y2-r,
            x1,   y1+r, x1,   y1,
        ]
        return self._canvas.create_polygon(points, smooth=True, fill=fill, outline="")

    @property
    def widget(self) -> tk.Canvas:
        return self._canvas

    def pack(self, **kw) -> None:
        self._canvas.pack(**kw)

    def pack_forget(self) -> None:
        self._canvas.pack_forget()

    def configure(self, **kw) -> None:
        if "state" in kw:
            state = kw.pop("state")
            self._enabled = (state != tk.DISABLED)
            fill = self._bg if self._enabled else self._disabled_bg
            tfill = self._fg if self._enabled else self._disabled_fg
            cur = "pointinghand" if self._enabled else ""
            self._canvas.itemconfigure(self._rect, fill=fill)
            self._canvas.itemconfigure(self._text_id, fill=tfill)
            self._canvas.configure(cursor=cur)
        if "text" in kw:
            self._text = kw.pop("text")
            tmp = self._make_font()
            desired_w = max(tmp.measure(self._text) + self._padx * 2, self._min_w)
            if desired_w != self._w:
                self._w = desired_w
                self._canvas.configure(width=self._w)
                self._canvas.delete(self._rect)
                self._rect = self._draw_rounded_rect(
                    self._bg if self._enabled else self._disabled_bg
                )
                self._canvas.coords(self._text_id, self._w // 2, self._h // 2)
            self._canvas.itemconfigure(self._text_id, text=self._text)
        if "bg" in kw:
            self._bg = kw.pop("bg")
            if self._enabled:
                self._canvas.itemconfigure(self._rect, fill=self._bg)
        if "fg" in kw:
            self._fg = kw.pop("fg")
            if self._enabled:
                self._canvas.itemconfigure(self._text_id, fill=self._fg)
        if "hover_bg" in kw:
            self._hover_bg = kw.pop("hover_bg")
        if "hover_fg" in kw:
            self._hover_fg = kw.pop("hover_fg")
        if "command" in kw:
            self._command = kw.pop("command")

    def _on_click(self, _e) -> None:
        if self._enabled and self._command:
            self._command()

    def _on_enter(self, _e) -> None:
        if self._enabled:
            self._canvas.itemconfigure(self._rect, fill=self._hover_bg)
            self._canvas.itemconfigure(self._text_id, fill=self._hover_fg)

    def _on_leave(self, _e) -> None:
        if self._enabled:
            self._canvas.itemconfigure(self._rect, fill=self._bg)
            self._canvas.itemconfigure(self._text_id, fill=self._fg)


# ---------------------------------------------------------------------------
# Animated status dot
# ---------------------------------------------------------------------------

class DotAnimator:
    """Animated status dot drawn on a tk.Canvas.

    States:
      OFF      — static grey dot
      STARTING — amber core with pulsing outer ring
      RUNNING  — green core with static glow ring
      ERROR    — red core with double-blink pattern
    """

    _DOT  = 12   # core dot diameter (px)
    _GLOW = 18   # outer ring diameter (px)
    _PAD  = 3    # padding around glow so canvas isn't clipped
    _W    = _GLOW + _PAD * 2  # canvas width/height (24px)

    # Amber pulse: simulate breathing by cycling glow-ring colour
    _PULSE_STEPS = [
        "#FBBF24", "#C99B1E", "#977318", "#5A4830",
        "#2A2A32",  # matches CARD_BG — "off"
        "#5A4830", "#977318", "#C99B1E",
    ]

    # Error blink: on-off-on-off-on, then steady for several frames
    _BLINK_PATTERN = [True, False, True, False, True,
                      True, True, True, True, True, True, True]
    _BLINK_MS      = [200,  200,   200,  200,   200,
                      500,  500,   500,  500,   500,  500,  500]

    def __init__(self, parent: tk.Widget, bg: str = CARD_BG) -> None:
        w = self._W
        self._bg = bg
        self._canvas = tk.Canvas(
            parent, width=w, height=w,
            bg=bg, highlightthickness=0,
        )
        cx = cy = w // 2

        # Glow ring (behind core dot)
        self._glow_id = self._canvas.create_oval(
            cx - self._GLOW // 2, cy - self._GLOW // 2,
            cx + self._GLOW // 2, cy + self._GLOW // 2,
            fill=bg, outline="",
        )
        # Core dot
        self._dot_id = self._canvas.create_oval(
            cx - self._DOT // 2, cy - self._DOT // 2,
            cx + self._DOT // 2, cy + self._DOT // 2,
            fill=DOT_COLORS[PkgState.OFF], outline="",
        )

        self._state: PkgState = PkgState.OFF
        self._anim_id: Optional[str] = None
        self._root: Optional[tk.Tk] = None

    @property
    def canvas(self) -> tk.Canvas:
        return self._canvas

    def set_state(self, state: PkgState, root: tk.Tk) -> None:
        """Cancel any running animation and apply new state."""
        self._root = root
        self._cancel()
        self._state = state

        self._canvas.itemconfigure(self._glow_id, fill=self._bg)
        self._canvas.itemconfigure(self._dot_id,  fill=DOT_COLORS[state])

        if state == PkgState.STARTING:
            self._pulse(root, 0)
        elif state == PkgState.RUNNING:
            self._canvas.itemconfigure(self._glow_id, fill=GREEN_GLOW)
        elif state == PkgState.ERROR:
            self._blink(root, 0)

    def cancel(self) -> None:
        self._cancel()

    def _cancel(self) -> None:
        if self._anim_id is not None and self._root is not None:
            try:
                self._root.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None

    def _pulse(self, root: tk.Tk, step: int) -> None:
        if self._state != PkgState.STARTING:
            return
        if not self._canvas.winfo_exists():
            return
        color = self._PULSE_STEPS[step % len(self._PULSE_STEPS)]
        self._canvas.itemconfigure(self._glow_id, fill=color)
        self._anim_id = root.after(150, self._pulse, root, step + 1)

    def _blink(self, root: tk.Tk, step: int) -> None:
        if self._state != PkgState.ERROR:
            return
        if not self._canvas.winfo_exists():
            return
        idx = step % len(self._BLINK_PATTERN)
        visible = self._BLINK_PATTERN[idx]
        color = DOT_COLORS[PkgState.ERROR] if visible else self._bg
        self._canvas.itemconfigure(self._dot_id, fill=color)
        interval = self._BLINK_MS[idx]
        self._anim_id = root.after(interval, self._blink, root, step + 1)


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
                start_command=entry.get("start_command", ""),
                url=entry.get("url"),
            ))
        return cls(packages_dir=packages_dir, packages=pkgs)


# ---------------------------------------------------------------------------
# GitHub detection helpers
# ---------------------------------------------------------------------------

def parse_github_input(raw: str) -> tuple[str, str]:
    s = raw.strip()
    m = re.match(r'^git@github\.com:([^/]+)/([^/\s]+?)(?:\.git)?$', s)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r'^https?://github\.com/([^/]+)/([^/\s]+?)(?:\.git)?(?:/.*)?$', s)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r'^([^/\s]+)/([^/\s]+?)(?:\.git)?$', s)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(
        f"Cannot parse GitHub input: {raw!r}\n"
        "Expected: https://github.com/owner/repo, owner/repo, or git@github.com:owner/repo.git"
    )


def gh_api(endpoint: str, timeout: int = 15) -> dict:
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
    name: str
    repo_slug: str
    branch: str
    start_command: str
    url: str
    confidence: str
    notes: str


def _detect_from_package_json(content: str) -> tuple[str, str, str]:
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
    m = re.search(r'--port[= ](\d+)', cmd)
    if m:
        return m.group(1)
    m = re.search(r'--bind[= ]\S*:(\d+)', cmd)
    if m:
        return m.group(1)
    m = re.search(r'\s-p[= ](\d+)', cmd)
    if m:
        return m.group(1)
    # Shell scripts: prefer a localhost URL on a line that mentions the frontend/client
    for line in cmd.splitlines():
        if re.search(r'frontend|client', line, re.IGNORECASE):
            m = re.search(r'localhost:(\d+)', line)
            if m:
                return m.group(1)
    # Fall back to first localhost:PORT mention anywhere in the script
    m = re.search(r'localhost:(\d+)', cmd)
    if m:
        return m.group(1)
    return None


def _port_from_python_source(content: str) -> Optional[str]:
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
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("web:"):
            cmd = stripped[4:].strip()
            port = _port_from_command(cmd)
            url = f"http://localhost:{port}" if port else ""
            return cmd, url
    return "", ""


def _makefile_has_start(content: str) -> bool:
    return any(re.match(r'^start\s*:', line) for line in content.splitlines())


def detect_service(owner: str, repo: str) -> DetectionResult:
    info = gh_api(f"repos/{owner}/{repo}")
    name = info.get("name", repo)
    branch = info.get("default_branch", "main")

    # Shell script entry points take priority — they're an explicit developer choice
    for script_name in ("init.sh", "start.sh", "run.sh", "dev.sh"):
        script = gh_file_content(owner, repo, script_name)
        if script is not None:
            port = _port_from_command(script)
            url = f"http://localhost:{port}" if port else ""
            return DetectionResult(name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                                   start_command=f"bash {script_name}", url=url,
                                   confidence="full",
                                   notes=f"Detected shell entry point: {script_name}.")

    pkg_json = gh_file_content(owner, repo, "package.json")
    if pkg_json is not None:
        cmd, url, notes = _detect_from_package_json(pkg_json)
        confidence = "full" if cmd else "partial"
        return DetectionResult(name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                               start_command=cmd, url=url, confidence=confidence, notes=notes)

    procfile = gh_file_content(owner, repo, "Procfile")
    if procfile is not None:
        cmd, url = _detect_from_procfile(procfile)
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
        return DetectionResult(name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                               start_command=cmd, url=url, confidence=confidence, notes=notes)

    makefile = gh_file_content(owner, repo, "Makefile")
    if makefile is not None:
        if _makefile_has_start(makefile):
            return DetectionResult(name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                                   start_command="make start", url="", confidence="full",
                                   notes="Detected from Makefile.")
        return DetectionResult(name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                               start_command="", url="", confidence="partial",
                               notes="Found Makefile but no start: target.")

    if (gh_file_content(owner, repo, "pyproject.toml") is not None
            or gh_file_content(owner, repo, "requirements.txt") is not None):
        cmd, url, notes = _detect_from_python(owner, repo)
        confidence = "full" if cmd else "partial"
        return DetectionResult(name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                               start_command=cmd, url=url, confidence=confidence, notes=notes)

    if gh_file_content(owner, repo, "go.mod") is not None:
        return DetectionResult(name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                               start_command="", url="", confidence="partial",
                               notes="Detected Go project. Enter start command manually (e.g. go run .).")

    # Monorepo: no root package.json but server/ and client/ each have one
    server_pkg = gh_file_content(owner, repo, "server/package.json")
    client_pkg = gh_file_content(owner, repo, "client/package.json")
    if server_pkg is not None and client_pkg is not None:
        _, client_url, _ = _detect_from_package_json(client_pkg)
        return DetectionResult(name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                               start_command="",
                               url=client_url,
                               confidence="partial",
                               notes="Monorepo with server/ and client/. Enter a start command (e.g. a shell script that starts both).")

    return DetectionResult(name=name, repo_slug=f"{owner}/{repo}", branch=branch,
                           start_command="", url="", confidence="none",
                           notes="Could not detect project type. Fill in start command manually.")


def _toml_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def append_package_to_config(config_path: pathlib.Path, pkg: PackageConfig) -> None:
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
# Fairy backup — silent background commits to a fairy-backup branch
# Uses git plumbing so the working branch is never touched.
# ---------------------------------------------------------------------------

def _fairy_backup_pkg(pkg_dir: pathlib.Path, push: bool = False) -> None:
    """Commit any working-tree changes to the fairy-backup branch without
    switching branches or stashing, then optionally push to origin.
    All errors are logged to fairy-backup.log; nothing is ever raised."""
    log_path = pkg_dir / "fairy-backup.log"

    def _log(msg: str) -> None:
        try:
            with log_path.open("a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except OSError:
            pass

    try:
        # Only proceed if there are changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(pkg_dir), capture_output=True, timeout=10,
        )
        if not status.stdout.strip():
            return

        # Stage everything
        subprocess.run(
            ["git", "add", "."],
            cwd=str(pkg_dir), capture_output=True, timeout=30, check=True,
        )

        # Write a tree object from the index
        tree_r = subprocess.run(
            ["git", "write-tree"],
            cwd=str(pkg_dir), capture_output=True, timeout=10, check=True,
        )
        tree_sha = tree_r.stdout.decode().strip()

        # Resolve parent: use existing fairy-backup tip if present, else HEAD
        parent_r = subprocess.run(
            ["git", "rev-parse", "--verify", "fairy-backup"],
            cwd=str(pkg_dir), capture_output=True, timeout=5,
        )
        if parent_r.returncode == 0:
            parent_sha = parent_r.stdout.decode().strip()
        else:
            head_r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(pkg_dir), capture_output=True, timeout=5, check=True,
            )
            parent_sha = head_r.stdout.decode().strip()

        # Create the commit object
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        commit_r = subprocess.run(
            ["git", "commit-tree", tree_sha, "-p", parent_sha, "-m", f"fairy-backup: {ts}"],
            cwd=str(pkg_dir), capture_output=True, timeout=10, check=True,
        )
        commit_sha = commit_r.stdout.decode().strip()

        # Point fairy-backup branch at the new commit (creates it if absent)
        subprocess.run(
            ["git", "update-ref", "refs/heads/fairy-backup", commit_sha],
            cwd=str(pkg_dir), capture_output=True, timeout=5, check=True,
        )

        # Reset the index back to HEAD so the working branch stays clean
        subprocess.run(
            ["git", "reset", "HEAD"],
            cwd=str(pkg_dir), capture_output=True, timeout=10,
        )

        # Push to origin — git creates the remote branch automatically if absent
        if push:
            push_r = subprocess.run(
                ["git", "push", "origin", "fairy-backup"],
                cwd=str(pkg_dir), capture_output=True, timeout=30,
            )
            if push_r.returncode != 0:
                err = push_r.stderr.decode(errors="replace").strip()
                _log(f"push failed: {err}")

    except Exception as exc:
        _log(f"backup error: {exc}")


# ---------------------------------------------------------------------------
# Process manager — per-package, kills whole process group on stop
# ---------------------------------------------------------------------------

class ProcessManager:
    def __init__(self, packages_dir: pathlib.Path) -> None:
        self._packages_dir = packages_dir
        self._procs: dict[str, subprocess.Popen] = {}
        self._log_fhs: dict[str, object] = {}

    def start_one(self, pkg: PackageConfig) -> None:
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

    def stop_one(self, pkg_name: str) -> None:
        proc = self._procs.pop(pkg_name, None)
        if proc is None:
            return
        if proc.poll() is None:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            if proc.poll() is None:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    try:
                        proc.kill()
                    except OSError:
                        pass
        fh = self._log_fhs.pop(pkg_name, None)
        if fh:
            try:
                fh.close()
            except OSError:
                pass

    def stop_all(self) -> None:
        for name in list(self._procs.keys()):
            self.stop_one(name)

    def poll_one(self, pkg_name: str) -> Optional[int]:
        proc = self._procs.get(pkg_name)
        if proc is None:
            return None
        return proc.poll()

    def is_running(self, pkg_name: str) -> bool:
        return pkg_name in self._procs


# ---------------------------------------------------------------------------
# Per-package worker thread
# ---------------------------------------------------------------------------

def _pkg_worker(
    pkg: PackageConfig,
    packages_dir: pathlib.Path,
    pm: ProcessManager,
    ui_queue: queue.Queue,
) -> None:
    try:
        pkg_dir = ensure_repo(pkg, packages_dir)
        _maybe_npm_install(pkg_dir)
        pm.start_one(pkg)
        _deadline = time.monotonic() + 1.5
        while time.monotonic() < _deadline:
            if pm.poll_one(pkg.name) is not None:
                log_path = pkg_dir / "autostart.log"
                try:
                    log_text = log_path.read_text(errors="replace")
                except OSError:
                    log_text = ""
                msg = _make_advisory(log_text) or "Service stopped immediately after starting."
                raise AutostartError(msg)
            time.sleep(0.1)
        ui_queue.put(("pkg_state", pkg.name, PkgState.RUNNING, ""))
    except AutostartError as exc:
        ui_queue.put(("pkg_state", pkg.name, PkgState.ERROR, str(exc)))
    except Exception as exc:
        ui_queue.put(("pkg_state", pkg.name, PkgState.ERROR, f"Unexpected error: {exc}"))


# ---------------------------------------------------------------------------
# Advisory layer
# ---------------------------------------------------------------------------

_ADVISORIES: list[tuple[str, str]] = [
    (r'localStorage\.getItem is not a function',
     "This app accesses browser storage before the page loads. "
     "Wrap the affected code in  if (typeof window !== 'undefined') { … }"),
    (r'EADDRINUSE|address already in use',
     "Something else is already using this port. "
     "Edit the start command to free the port first, or switch to a different one."),
    (r'command not found|not found|No such file',
     "A command or file wasn't found. "
     "Check the start command is correct and all required tools are installed."),
    (r'MODULE_NOT_FOUND|Cannot find module|ModuleNotFoundError',
     "A required package is missing. "
     "Run  npm install  or  pip install -r requirements.txt  in the project folder."),
    (r'EACCES|permission denied',
     "Permission denied. "
     "Try a port number above 1024, or check the folder's permissions."),
    (r'JavaScript heap out of memory|out of memory',
     "The service ran out of memory. "
     "Add  NODE_OPTIONS=--max-old-space-size=4096  before your start command."),
]


def _make_advisory(log_text: str) -> str:
    for pattern, message in _ADVISORIES:
        if re.search(pattern, log_text, re.IGNORECASE):
            return message
    return ""


# ---------------------------------------------------------------------------
# Add Service dialog
# ---------------------------------------------------------------------------

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
        top.configure(bg=CARD_BG)
        top.grab_set()
        top.transient(parent)
        self._top = top

        self._build_ui()
        top.after(self._POLL_MS, self._poll)

    def _build_ui(self) -> None:
        fn = self._font_name

        tk.Label(
            self._top, text="Add a Service",
            bg=CARD_BG, fg=TEXT_PRIMARY, font=(fn, 14, "bold"),
        ).pack(anchor="w", padx=20, pady=(20, 4))

        tk.Label(
            self._top, text="Paste a GitHub URL or owner/repo shorthand.",
            bg=CARD_BG, fg=TEXT_SECONDARY, font=(fn, 11),
        ).pack(anchor="w", padx=20, pady=(0, 12))

        input_row = tk.Frame(self._top, bg=CARD_BG)
        input_row.pack(fill=tk.X, padx=20, pady=(0, 4))

        entry_var = tk.StringVar()
        entry = tk.Entry(
            input_row, textvariable=entry_var,
            font=(fn, 12), width=34,
            relief=tk.FLAT, highlightthickness=1,
            highlightbackground=CARD_BORDER, highlightcolor=BLUE,
            bg="#1A1A22", fg=TEXT_PRIMARY,
            insertbackground=TEXT_PRIMARY,
        )
        entry.pack(side=tk.LEFT, ipady=7)
        entry.focus_set()
        self._entry_var = entry_var
        self._entry = entry

        _dfont = tkfont.Font(family=fn, size=12, weight="bold")
        _detect_min_w = max(_dfont.measure(t) for t in ("Detect", "Detecting...")) + 32
        detect_btn = CanvasButton(
            input_row, text="Detect", font=(fn, 12, "bold"),
            command=self._on_detect,
            bg=BLUE, fg="#FFFFFF",
            hover_bg=BLUE_HOVER, hover_fg="#FFFFFF",
            padx=16, pady=7,
            parent_bg=CARD_BG,
            min_width=_detect_min_w,
        )
        detect_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._detect_btn = detect_btn

        entry.bind("<Return>", lambda _e: self._on_detect())

        status_lbl = tk.Label(
            self._top, text="",
            bg=CARD_BG, fg=TEXT_SECONDARY, font=(fn, 10),
            wraplength=420, justify="left", anchor="w",
        )
        status_lbl.pack(fill=tk.X, padx=20, pady=(4, 0))
        self._status_lbl = status_lbl

        self._review_frame = tk.Frame(self._top, bg=CARD_BG)

        self._sep = tk.Frame(self._top, bg=CARD_BORDER, height=1)
        self._sep.pack(fill=tk.X, pady=12)

        btn_row = tk.Frame(self._top, bg=CARD_BG)
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 20))

        cancel_btn = CanvasButton(
            btn_row, text="Cancel", font=(fn, 12),
            command=self._top.destroy,
            bg=CARD_BORDER, fg=TEXT_PRIMARY,
            hover_bg=CARD_BORDER_HOVER, hover_fg=TEXT_PRIMARY,
            padx=16, pady=7,
            parent_bg=CARD_BG,
        )
        cancel_btn.pack(side=tk.LEFT, padx=(0, 8))

        confirm_btn = CanvasButton(
            btn_row, text="Add Service", font=(fn, 12, "bold"),
            command=self._on_confirm_clicked,
            bg=BLUE, fg="#FFFFFF",
            hover_bg=BLUE_HOVER, hover_fg="#FFFFFF",
            padx=16, pady=7,
            parent_bg=CARD_BG,
        )
        confirm_btn.configure(state=tk.DISABLED)
        confirm_btn.pack(side=tk.LEFT)
        self._confirm_btn = confirm_btn

    def _show_review(self, result: DetectionResult) -> None:
        fn = self._font_name
        frame = self._review_frame
        for w in frame.winfo_children():
            w.destroy()

        def _field(label: str, var: tk.StringVar, highlight_empty: bool = False) -> tk.Entry:
            row = tk.Frame(frame, bg=CARD_BG)
            row.pack(fill=tk.X, pady=3)
            tk.Label(
                row, text=label, bg=CARD_BG, fg=TEXT_SECONDARY,
                font=(fn, 10), width=14, anchor="e",
            ).pack(side=tk.LEFT)
            e = tk.Entry(
                row, textvariable=var,
                font=(fn, 12), width=28,
                relief=tk.FLAT, highlightthickness=1,
                highlightbackground=CARD_BORDER, highlightcolor=BLUE,
                bg="#1A1A22", fg=TEXT_PRIMARY,
                insertbackground=TEXT_PRIMARY,
            )
            e.pack(side=tk.LEFT, padx=(8, 0), ipady=5)
            if highlight_empty:
                def _update(*_):
                    e.configure(highlightbackground=AMBER if not var.get().strip() else CARD_BORDER)
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

        detected_port_m = re.search(r':(\d+)', result.url) if result.url else None
        detected_port = detected_port_m.group(1) if detected_port_m else None

        url_warn_lbl = tk.Label(
            frame, text="", bg=CARD_BG, fg="#FCD34D",
            font=(fn, 10), wraplength=340, justify="left", anchor="w",
        )
        url_warn_lbl.pack(fill=tk.X, padx=(16 + 8 + 2, 0), pady=(0, 2))

        def _check_url_port(*_):
            if detected_port is None:
                return
            entered_m = re.search(r':(\d+)', url_var.get())
            entered_port = entered_m.group(1) if entered_m else None
            if entered_port and entered_port != detected_port:
                url_warn_lbl.configure(
                    text=f"Detected port is :{detected_port}. The start command likely "
                         f"ignores this — update the port in the repo instead."
                )
            else:
                url_warn_lbl.configure(text="")

        url_var.trace_add("write", _check_url_port)

        self._name_var   = name_var
        self._branch_var = branch_var
        self._cmd_var    = cmd_var
        self._url_var    = url_var

        dup_lbl = tk.Label(frame, text="", bg=CARD_BG, fg="#FCA5A5", font=(fn, 10))
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

        frame.pack(fill=tk.X, padx=20, pady=(8, 0), before=self._sep)
        self._top.geometry("")

    def _on_detect(self) -> None:
        raw = self._entry_var.get().strip()
        if not raw:
            self._status_lbl.configure(text="Please enter a GitHub URL or owner/repo.", fg="#FCA5A5")
            return
        try:
            owner, repo = parse_github_input(raw)
        except ValueError as exc:
            self._status_lbl.configure(text=str(exc), fg="#FCA5A5")
            return

        self._state = _DialogState.DETECTING
        self._detect_btn.configure(state=tk.DISABLED, text="Detecting...")
        self._status_lbl.configure(text=f"Fetching info for {owner}/{repo}…", fg=TEXT_SECONDARY)
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
                    self._status_lbl.configure(text="", fg=TEXT_SECONDARY)
                    self._show_review(result)
                elif msg[0] == "error":
                    self._state = _DialogState.IDLE
                    self._detect_btn.configure(state=tk.NORMAL, text="Detect")
                    self._status_lbl.configure(text=msg[1], fg="#FCA5A5")
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

        pkg = PackageConfig(name=name, repo=self._detection_result.repo_slug,
                            branch=branch, start_command=cmd, url=url)
        self._state = _DialogState.SAVING
        self._confirm_btn.configure(state=tk.DISABLED)
        try:
            append_package_to_config(self._config_path, pkg)
        except OSError as exc:
            self._status_lbl.configure(text=f"Failed to save config: {exc}", fg="#FCA5A5")
            self._confirm_btn.configure(state=tk.NORMAL)
            self._state = _DialogState.REVIEW
            return
        self._top.destroy()
        self._on_confirm(pkg)


# ---------------------------------------------------------------------------
# Edit Service dialog
# ---------------------------------------------------------------------------

class EditServiceDialog:
    """Modal for editing branch, start command, and URL of an existing service."""

    def __init__(
        self,
        parent: tk.Tk,
        pkg: PackageConfig,
        on_confirm: Callable[[str, str, Optional[str]], None],
        font_name: str,
    ) -> None:
        self._pkg = pkg
        self._on_confirm = on_confirm
        self._font_name = font_name

        top = tk.Toplevel(parent)
        top.title(f"Edit — {pkg.name}")
        top.resizable(False, False)
        top.configure(bg=CARD_BG)
        top.grab_set()
        top.transient(parent)
        self._top = top
        self._build_ui()

    def _build_ui(self) -> None:
        fn = self._font_name
        pkg = self._pkg

        tk.Label(
            self._top, text=f"Edit \"{pkg.name}\"",
            bg=CARD_BG, fg=TEXT_PRIMARY, font=(fn, 14, "bold"),
        ).pack(anchor="w", padx=20, pady=(20, 12))

        frame = tk.Frame(self._top, bg=CARD_BG)
        frame.pack(fill=tk.X, padx=20)

        def _ro_field(label: str, value: str) -> None:
            row = tk.Frame(frame, bg=CARD_BG)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, bg=CARD_BG, fg=TEXT_SECONDARY,
                     font=(fn, 10), width=14, anchor="e").pack(side=tk.LEFT)
            tk.Label(row, text=value, bg=CARD_BG, fg=TEXT_TERTIARY,
                     font=(fn, 12), anchor="w").pack(side=tk.LEFT, padx=(8, 0))

        def _field(label: str, var: tk.StringVar, highlight_empty: bool = False) -> tk.Entry:
            row = tk.Frame(frame, bg=CARD_BG)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, bg=CARD_BG, fg=TEXT_SECONDARY,
                     font=(fn, 10), width=14, anchor="e").pack(side=tk.LEFT)
            e = tk.Entry(
                row, textvariable=var, font=(fn, 12), width=28,
                relief=tk.FLAT, highlightthickness=1,
                highlightbackground=CARD_BORDER, highlightcolor=BLUE,
                bg="#1A1A22", fg=TEXT_PRIMARY,
                insertbackground=TEXT_PRIMARY,
            )
            e.pack(side=tk.LEFT, padx=(8, 0), ipady=5)
            if highlight_empty:
                def _upd(*_):
                    e.configure(highlightbackground=AMBER if not var.get().strip() else CARD_BORDER)
                var.trace_add("write", _upd)
                _upd()
            return e

        _ro_field("Repo", pkg.repo)
        _ro_field("Name", pkg.name)

        branch_var = tk.StringVar(value=pkg.branch)
        cmd_var    = tk.StringVar(value=pkg.start_command)
        url_var    = tk.StringVar(value=pkg.url or "")

        _field("Branch", branch_var)
        _field("Start command", cmd_var, highlight_empty=True)
        _field("URL", url_var)

        tk.Label(
            self._top, text="Changes take effect on the next start.",
            bg=CARD_BG, fg=TEXT_TERTIARY, font=(fn, 10),
        ).pack(anchor="w", padx=20, pady=(10, 0))

        tk.Frame(self._top, bg=CARD_BORDER, height=1).pack(fill=tk.X, pady=12)

        btn_row = tk.Frame(self._top, bg=CARD_BG)
        btn_row.pack(fill=tk.X, padx=20, pady=(0, 20))

        CanvasButton(
            btn_row, text="Cancel", font=(fn, 12),
            command=self._top.destroy,
            bg=CARD_BORDER, fg=TEXT_PRIMARY,
            hover_bg=CARD_BORDER_HOVER, hover_fg=TEXT_PRIMARY,
            padx=16, pady=7, parent_bg=CARD_BG,
        ).pack(side=tk.LEFT, padx=(0, 8))

        save_btn = CanvasButton(
            btn_row, text="Save Changes", font=(fn, 12, "bold"),
            command=lambda: self._save(branch_var, cmd_var, url_var),
            bg=BLUE, fg="#FFFFFF",
            hover_bg=BLUE_HOVER, hover_fg="#FFFFFF",
            padx=16, pady=7, parent_bg=CARD_BG,
        )
        save_btn.pack(side=tk.LEFT)
        self._save_btn = save_btn

        def _check(*_):
            save_btn.configure(state=tk.NORMAL if cmd_var.get().strip() else tk.DISABLED)
        cmd_var.trace_add("write", _check)

    def _save(self, branch_var: tk.StringVar, cmd_var: tk.StringVar,
              url_var: tk.StringVar) -> None:
        branch = branch_var.get().strip() or "main"
        cmd    = cmd_var.get().strip()
        url    = url_var.get().strip() or None
        if not cmd:
            return
        self._top.destroy()
        self._on_confirm(branch, cmd, url)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class AutostartApp:
    _POLL_MS              = 100
    _HEALTH_INTERVAL      = 5.0
    _MONITOR_POLL         = 2.0
    _FAIRY_BACKUP_INTERVAL = 300.0

    def __init__(self, config: Config, config_path: pathlib.Path) -> None:
        self._config = config
        self._config_path = config_path
        self._packages_dir = config_path.parent / config.packages_dir
        self._packages_dir.mkdir(parents=True, exist_ok=True)

        self._pm = ProcessManager(self._packages_dir)
        self._pkg_states: dict[str, PkgState] = {p.name: PkgState.OFF for p in config.packages}
        self._pkg_stop_events: dict[str, threading.Event] = {}
        self._stopping: set[str] = set()
        self._ui_queue: queue.Queue = queue.Queue()
        self._fairy_backup_stop = threading.Event()

        self._build_ui()
        self._start_fairy_backup()

    # ---- Font resolution -----------------------------------------------

    def _resolve_font(self) -> str:
        available = tkfont.families()
        for candidate in (".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue"):
            if candidate in available:
                return candidate
        return "TkDefaultFont"

    def _resolve_mono_font(self) -> str:
        available = tkfont.families()
        for candidate in ("SF Mono", "Menlo", "Monaco"):
            if candidate in available:
                return candidate
        return "TkFixedFont"

    # ---- UI construction -----------------------------------------------

    def _build_ui(self) -> None:
        root = tk.Tk()
        root.title("Autostart")
        root.resizable(False, True)
        root.configure(bg=WINDOW_BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root = root

        # App icon — titlebar
        try:
            _icon_path = pathlib.Path(__file__).parent / "AppIcon.iconset" / "icon_32x32@2x.png"
            if _icon_path.exists():
                self._app_icon = tk.PhotoImage(file=str(_icon_path))
                root.iconphoto(True, self._app_icon)
        except Exception:
            pass

        # App icon — macOS Dock (no forced size — let macOS scale correctly)
        try:
            from AppKit import NSApplication, NSImage  # type: ignore[import]
            _icns = (pathlib.Path(__file__).parent
                     / "Autostart.app" / "Contents" / "Resources" / "AppIcon.icns")
            if _icns.exists():
                _nsimg = NSImage.alloc().initWithContentsOfFile_(str(_icns))
                if _nsimg:
                    NSApplication.sharedApplication().setApplicationIconImage_(_nsimg)
        except Exception:
            pass

        self._font_name = self._resolve_font()
        self._mono_font = self._resolve_mono_font()
        fn = self._font_name

        # ── Header bar ─────────────────────────────────────────────────
        header = tk.Frame(root, bg=HEADER_BG, height=52)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        # App icon inline in header
        try:
            _hicon_path = pathlib.Path(__file__).parent / "AppIcon.iconset" / "icon_32x32@2x.png"
            if _hicon_path.exists():
                _raw_icon = tk.PhotoImage(file=str(_hicon_path))
                self._header_icon = _raw_icon.subsample(2)   # 64×64 → 32×32 logical px
                tk.Label(
                    header, image=self._header_icon, bg=HEADER_BG,
                ).pack(side=tk.LEFT, padx=(16, 0))
        except Exception:
            pass

        tk.Label(
            header, text="Autostart",
            bg=HEADER_BG, fg=TEXT_PRIMARY,
            font=(fn, 16, "bold"),
        ).pack(side=tk.LEFT, padx=(8, 0))

        # ── Circular "+" add button (rightmost) ──────────────────────
        _ADD = 28
        add_canvas = tk.Canvas(
            header, width=_ADD, height=_ADD,
            bg=HEADER_BG, highlightthickness=0, cursor="pointinghand",
        )
        add_canvas.create_oval(1, 1, _ADD - 1, _ADD - 1,
                               fill=CARD_BG, outline=CARD_BORDER, width=1, tags="bg")
        _mid = _ADD // 2
        add_canvas.create_line(_mid, 7, _mid, _ADD - 7, fill=TEXT_SECONDARY, width=2, tags="plus")
        add_canvas.create_line(7, _mid, _ADD - 7, _mid, fill=TEXT_SECONDARY, width=2, tags="plus")
        add_canvas.pack(side=tk.RIGHT, padx=(0, 16))
        add_canvas.bind("<Button-1>", lambda e: self._on_add_service())
        add_canvas.bind("<Enter>", lambda e: (
            add_canvas.itemconfigure("bg",   fill=BLUE, outline=BLUE),
            add_canvas.itemconfigure("plus", fill="#FFFFFF"),
        ))
        add_canvas.bind("<Leave>", lambda e: (
            add_canvas.itemconfigure("bg",   fill=CARD_BG, outline=CARD_BORDER),
            add_canvas.itemconfigure("plus", fill=TEXT_SECONDARY),
        ))
        self._add_btn = add_canvas

        # ── Start All / Stop All button ──────────────────────────────
        global_btn = CanvasButton(
            header, text="Start All",
            font=(fn, 11),
            bg=CARD_BG, fg=TEXT_SECONDARY,
            hover_bg=CARD_BORDER_HOVER, hover_fg=TEXT_PRIMARY,
            disabled_bg=CARD_BG, disabled_fg=TEXT_TERTIARY,
            padx=12, pady=4,
            command=self._on_global_action,
            parent_bg=HEADER_BG,
        )
        global_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._global_btn = global_btn

        if not self._config.packages:
            global_btn.configure(state=tk.DISABLED)

        # Header bottom border
        tk.Frame(root, bg=CARD_BORDER, height=1).pack(fill=tk.X)

        # ── Card list area ─────────────────────────────────────────────
        cards_outer = tk.Frame(root, bg=WINDOW_BG)
        cards_outer.pack(fill=tk.BOTH, expand=True, pady=(0, 12))
        self._cards_outer = cards_outer

        self._pkg_widgets: dict[str, dict] = {}
        self._empty_frame: Optional[tk.Frame] = None

        if not self._config.packages:
            self._show_empty_state()
        else:
            for pkg in self._config.packages:
                self._add_pkg_card(pkg)

        root.after(self._POLL_MS, self._poll_queue)

    def _show_empty_state(self) -> None:
        frame = tk.Frame(self._cards_outer, bg=WINDOW_BG)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=24)

        # Three dots arranged diagonally (mirrors app icon)
        dots_canvas = tk.Canvas(frame, width=84, height=62,
                                bg=WINDOW_BG, highlightthickness=0)
        dots_canvas.pack(pady=(36, 16))

        _DOT = 12
        _dot_ids = [
            dots_canvas.create_oval(4, 42, 4 + _DOT, 42 + _DOT,
                                    fill=DOT_COLORS[PkgState.OFF], outline=""),
            dots_canvas.create_oval(34, 24, 34 + _DOT, 24 + _DOT,
                                    fill=DOT_COLORS[PkgState.OFF], outline=""),
            dots_canvas.create_oval(64, 6, 64 + _DOT, 6 + _DOT,
                                    fill=DOT_COLORS[PkgState.OFF], outline=""),
        ]
        _PULSE_BRIGHT = "#9CA3AF"

        def _pulse_empty(step: int = 0) -> None:
            if not frame.winfo_exists():
                return
            active = step % 3
            for i, dot_id in enumerate(_dot_ids):
                color = _PULSE_BRIGHT if i == active else DOT_COLORS[PkgState.OFF]
                dots_canvas.itemconfigure(dot_id, fill=color)
            frame.after(600, _pulse_empty, step + 1)

        _pulse_empty()

        tk.Label(frame, text="Nothing running yet.",
                 bg=WINDOW_BG, fg=TEXT_SECONDARY,
                 font=(self._font_name, 14)).pack()
        tk.Label(frame, text="Add your first service to get started.",
                 bg=WINDOW_BG, fg=TEXT_TERTIARY,
                 font=(self._font_name, 12)).pack(pady=(4, 20))

        CanvasButton(
            frame, text="+ Add a service",
            font=(self._font_name, 12, "bold"),
            bg=BLUE, fg="#FFFFFF",
            hover_bg=BLUE_HOVER, hover_fg="#FFFFFF",
            padx=20, pady=8,
            command=self._on_add_service,
            parent_bg=WINDOW_BG,
        ).pack()

        self._empty_frame = frame

    def _hide_empty_state(self) -> None:
        if self._empty_frame:
            self._empty_frame.destroy()
            self._empty_frame = None

    # ---- Card construction -------------------------------------------

    def _add_pkg_card(self, pkg: PackageConfig) -> None:
        fn = self._font_name

        outer = tk.Frame(self._cards_outer, bg=WINDOW_BG)
        outer.pack(fill=tk.X, padx=16, pady=(6, 0))

        card = tk.Frame(
            outer, bg=CARD_BG,
            highlightthickness=1,
            highlightbackground=CARD_BORDER,
            highlightcolor=CARD_BORDER,
        )
        card.pack(fill=tk.X)

        # ── Row 1: animated dot · service name · action button ────────
        row1 = tk.Frame(card, bg=CARD_BG)
        row1.pack(fill=tk.X, padx=16, pady=(14, 0))

        dot_animator = DotAnimator(row1)
        dot_animator.canvas.pack(side=tk.LEFT, padx=(0, 10))

        name_lbl = tk.Label(
            row1, text=pkg.name,
            bg=CARD_BG, fg=TEXT_PRIMARY,
            font=(fn, 14, "bold"), anchor="w",
        )
        name_lbl.pack(side=tk.LEFT)

        _abfont = tkfont.Font(family=fn, size=10, weight="bold")
        _action_min_w = max(
            _abfont.measure(t)
            for t in ("Start", "Stop", "Restart", "Starting...", "Stopping...")
        ) + 28
        action_btn = CanvasButton(
            row1, text="Start",
            font=(fn, 10, "bold"),
            bg=BLUE, fg="#FFFFFF",
            hover_bg=BLUE_HOVER, hover_fg="#FFFFFF",
            padx=14, pady=4,
            command=lambda n=pkg.name: self._on_pkg_action(n),
            parent_bg=CARD_BG,
            min_width=_action_min_w,
        )
        action_btn.pack(side=tk.RIGHT)

        # ── Row 2: URL metadata (plain until healthy, then a link) ────
        row2 = tk.Frame(card, bg=CARD_BG)
        row2.pack(fill=tk.X, padx=16, pady=(4, 0))
        # Spacer to align text under the service name
        tk.Frame(row2, width=DOT_CANVAS_INDENT, bg=CARD_BG).pack(side=tk.LEFT)

        url_lbl: Optional[tk.Label] = None
        if pkg.url:
            _pm = re.search(r':(\d+)', pkg.url)
            url_display = f"localhost:{_pm.group(1)}" if _pm else pkg.url
            url_lbl = tk.Label(
                row2, text=url_display,
                bg=CARD_BG, fg=TEXT_TERTIARY,
                font=(fn, 11), anchor="w",
            )
            url_lbl.pack(side=tk.LEFT)

        # ── Context menu (right-click power-user shortcut) ────────────
        ctx_menu = tk.Menu(card, tearoff=False,
                           bg=CARD_BG, fg=TEXT_PRIMARY,
                           activebackground=CARD_BORDER,
                           activeforeground=TEXT_PRIMARY)
        ctx_menu.add_command(
            label="Edit service…",
            command=lambda n=pkg.name: self._on_edit_service(n),
        )
        ctx_menu.add_separator()
        ctx_menu.add_command(
            label=f"Remove \"{pkg.name}\"…",
            command=lambda n=pkg.name: self._on_remove_service(n),
            foreground=RED,
        )

        def _show_ctx(e: tk.Event) -> None:
            ctx_menu.post(e.x_root, e.y_root)

        # ── Advisory / error panel ────────────────────────────────────
        advisory_outer = tk.Frame(card, bg=CARD_BG)
        tk.Frame(advisory_outer, bg=CARD_BORDER, height=1).pack(fill=tk.X)

        advisory_inner = tk.Frame(advisory_outer, bg="#450A0A")
        advisory_inner.pack(fill=tk.X)

        left_bar = tk.Frame(advisory_inner, bg=RED, width=4)
        left_bar.pack(side=tk.LEFT, fill=tk.Y)

        advisory_lbl = tk.Label(
            advisory_inner, text="",
            bg="#450A0A", fg="#FCA5A5",
            font=(fn, 10),
            wraplength=340, justify="left", anchor="w",
            padx=12, pady=8,
        )
        advisory_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        adv_action_row = tk.Frame(advisory_outer, bg=CARD_BG)
        adv_action_row.pack(fill=tk.X, padx=16, pady=(0, 6))

        _eaf  = tkfont.Font(family=fn, size=10)
        _eafu = tkfont.Font(family=fn, size=10, underline=True)
        edit_adv_btn = tk.Label(
            adv_action_row, text="Edit service",
            bg=CARD_BG, fg=BLUE,
            font=_eaf, cursor="pointinghand",
        )
        edit_adv_btn.pack(side=tk.LEFT, padx=(0, 16))
        edit_adv_btn.bind("<Button-1>", lambda e, n=pkg.name: self._on_edit_service(n))
        edit_adv_btn.bind("<Enter>", lambda e, b=edit_adv_btn, f=_eafu: b.configure(font=f))
        edit_adv_btn.bind("<Leave>", lambda e, b=edit_adv_btn, f=_eaf:  b.configure(font=f))

        log_toggle = tk.Label(
            adv_action_row, text="Show log",
            bg=CARD_BG, fg=TEXT_SECONDARY,
            font=(fn, 10), cursor="pointinghand",
        )
        log_toggle.pack(side=tk.LEFT)

        log_frame = tk.Frame(advisory_outer, bg=CARD_BG)
        log_lbl = tk.Label(
            log_frame, text="",
            bg="#16161C", fg=TEXT_SECONDARY,
            font=(self._mono_font, 9),
            wraplength=356, justify="left", anchor="w",
            padx=12, pady=8,
        )
        log_lbl.pack(fill=tk.X, padx=16, pady=(0, 8))

        # ── Hover action strip: Edit + Remove ─────────────────────────
        hover_row = tk.Frame(card, bg=CARD_BG)

        _hlf  = tkfont.Font(family=fn, size=10)
        _hlfu = tkfont.Font(family=fn, size=10, underline=True)

        remove_link = tk.Label(
            hover_row, text="Remove",
            bg=CARD_BG, fg="#7F3535",
            font=_hlf, cursor="pointinghand",
        )
        remove_link.pack(side=tk.RIGHT)
        remove_link.bind("<Button-1>", lambda e, n=pkg.name: self._on_remove_service(n))
        remove_link.bind("<Enter>", lambda e, b=remove_link: b.configure(fg=RED))
        remove_link.bind("<Leave>", lambda e, b=remove_link: b.configure(fg="#7F3535"))

        edit_link = tk.Label(
            hover_row, text="Edit",
            bg=CARD_BG, fg=TEXT_TERTIARY,
            font=_hlf, cursor="pointinghand",
        )
        edit_link.pack(side=tk.RIGHT, padx=(0, 12))
        edit_link.bind("<Button-1>", lambda e, n=pkg.name: self._on_edit_service(n))
        edit_link.bind("<Enter>", lambda e, b=edit_link: b.configure(fg=TEXT_SECONDARY, font=_hlfu))
        edit_link.bind("<Leave>", lambda e, b=edit_link: b.configure(fg=TEXT_TERTIARY, font=_hlf))

        # ── Bottom padding ────────────────────────────────────────────
        bottom_pad = tk.Frame(card, bg=CARD_BG, height=14)
        bottom_pad.pack(fill=tk.X)

        # ── Card hover: brighten border + reveal action strip ─────────
        hover_row_shown = [False]
        _hover_cancel   = [None]
        accordion_open  = [False]

        def _toggle_log(n: str = pkg.name) -> None:
            ww = self._pkg_widgets[n]
            if accordion_open[0]:
                ww["log_frame"].pack_forget()
                ww["log_toggle"].configure(text="Show log")
                accordion_open[0] = False
            else:
                ww["log_lbl"].configure(text=self._read_log_tail(n))
                ww["log_frame"].pack(fill=tk.X)
                ww["log_toggle"].configure(text="Hide log")
                accordion_open[0] = True
            self._root.geometry("")

        log_toggle.bind("<Button-1>", lambda e: _toggle_log())

        def _on_hover_enter(e: tk.Event) -> None:
            if _hover_cancel[0] is not None:
                self._root.after_cancel(_hover_cancel[0])
                _hover_cancel[0] = None
            card.configure(highlightbackground=CARD_BORDER_HOVER)
            if not hover_row_shown[0]:
                hover_row.pack(fill=tk.X, padx=16, pady=(0, 0), before=bottom_pad)
                hover_row_shown[0] = True
                self._root.geometry("")

        def _on_hover_leave(e: tk.Event) -> None:
            def _check() -> None:
                _hover_cancel[0] = None
                if not card.winfo_exists():
                    return
                x, y = self._root.winfo_pointerxy()
                cx = card.winfo_rootx()
                cy = card.winfo_rooty()
                cw = card.winfo_width()
                ch = card.winfo_height()
                if cx <= x < cx + cw and cy <= y < cy + ch:
                    return  # pointer still inside card
                card.configure(highlightbackground=CARD_BORDER)
                if hover_row_shown[0]:
                    hover_row.pack_forget()
                    hover_row_shown[0] = False
                    self._root.geometry("")
            _hover_cancel[0] = self._root.after(80, _check)

        def _bind_events(w: tk.Widget) -> None:
            w.bind("<Enter>", _on_hover_enter, add="+")
            w.bind("<Leave>", _on_hover_leave, add="+")
            w.bind("<Button-2>", _show_ctx, add="+")
            w.bind("<Control-Button-1>", _show_ctx, add="+")
            for child in w.winfo_children():
                _bind_events(child)

        _bind_events(card)

        self._pkg_widgets[pkg.name] = {
            "outer":          outer,
            "card":           card,
            "dot_animator":   dot_animator,
            "action_btn":     action_btn,
            "url_lbl":        url_lbl,
            "advisory_outer": advisory_outer,
            "advisory_lbl":   advisory_lbl,
            "advisory_inner": advisory_inner,
            "left_bar":       left_bar,
            "log_toggle":     log_toggle,
            "log_frame":      log_frame,
            "log_lbl":        log_lbl,
            "accordion_open": accordion_open,
            "_row1":          row1,
            "_row2":          row2,
            "hover_row":      hover_row,
            "hover_row_shown": hover_row_shown,
            "_hover_cancel":  _hover_cancel,
            "bottom_pad":     bottom_pad,
        }
        self._pkg_states[pkg.name] = PkgState.OFF
        self._root.geometry("")

    # ---- Per-package state update (main thread only) -------------------

    def _set_pkg_state(
        self,
        pkg_name: str,
        state: PkgState,
        error_msg: str = "",
    ) -> None:
        self._pkg_states[pkg_name] = state
        w = self._pkg_widgets.get(pkg_name)
        if w is None:
            self._update_global_btn()
            return

        fn = self._font_name

        # Dot animation
        w["dot_animator"].set_state(state, self._root)

        # Action button
        if state == PkgState.OFF:
            w["action_btn"].configure(
                text="Start", state=tk.NORMAL,
                bg=BLUE, fg="#FFFFFF", hover_bg=BLUE_HOVER,
            )
        elif state == PkgState.STARTING:
            w["action_btn"].configure(text="Starting...", state=tk.DISABLED)
        elif state == PkgState.RUNNING:
            w["action_btn"].configure(
                text="Stop", state=tk.NORMAL,
                bg=STOP_BG, fg="#FFFFFF", hover_bg=RED_HOVER,
            )
        elif state == PkgState.ERROR:
            w["action_btn"].configure(
                text="Restart", state=tk.NORMAL,
                bg=BLUE, fg="#FFFFFF", hover_bg=BLUE_HOVER,
            )

        # URL label — reset to plain text whenever not RUNNING
        if state != PkgState.RUNNING and w["url_lbl"] is not None:
            pkg = next((p for p in self._config.packages if p.name == pkg_name), None)
            if pkg and pkg.url:
                _pm = re.search(r':(\d+)', pkg.url)
                url_display = f"localhost:{_pm.group(1)}" if _pm else pkg.url
                w["url_lbl"].configure(text=url_display, fg=TEXT_TERTIARY,
                                       cursor="", font=(fn, 11))
                w["url_lbl"].unbind("<Button-1>")

        # Advisory section
        if state == PkgState.ERROR:
            log_text = self._read_log_tail(pkg_name)
            advisory = (_make_advisory(log_text)
                        or "The service stopped unexpectedly. Check the log for details.")
            w["advisory_lbl"].configure(text=advisory)
            # Error colours (red tint)
            w["advisory_inner"].configure(bg="#450A0A")
            w["advisory_lbl"].configure(bg="#450A0A", fg="#FCA5A5")
            w["left_bar"].configure(bg=RED)
            if w["accordion_open"][0]:
                w["log_lbl"].configure(text=log_text)
            w["advisory_outer"].pack(fill=tk.X, before=w["bottom_pad"])
        else:
            w["advisory_outer"].pack_forget()
            if w["accordion_open"][0]:
                w["log_frame"].pack_forget()
                w["log_toggle"].configure(text="Show log")
                w["accordion_open"][0] = False

        self._update_global_btn()
        self._root.geometry("")

    # ---- Health check sub-state updates --------------------------------

    def _apply_pkg_health(self, pkg_name: str, status: int) -> None:
        """Updates dot animation + URL link based on HTTP health. Main thread only."""
        if self._pkg_states.get(pkg_name) != PkgState.RUNNING:
            return
        w = self._pkg_widgets.get(pkg_name)
        if w is None:
            return

        fn = self._font_name
        pkg = next((p for p in self._config.packages if p.name == pkg_name), None)

        if status == 0:
            # Not yet responding
            w["dot_animator"].set_state(PkgState.STARTING, self._root)
            if w["url_lbl"] is not None and pkg and pkg.url:
                _pm = re.search(r':(\d+)', pkg.url)
                url_display = f"localhost:{_pm.group(1)}" if _pm else pkg.url
                w["url_lbl"].configure(text=url_display, fg=TEXT_TERTIARY,
                                       cursor="", font=(fn, 11))
                w["url_lbl"].unbind("<Button-1>")
            # If the log reveals the service is on a different port, say so
            if pkg and pkg.url:
                log_text = self._read_log_tail(pkg_name)
                cfg_port_m = re.search(r':(\d+)', pkg.url)
                log_port_m = re.search(r'localhost:(\d+)', log_text)
                if cfg_port_m and log_port_m and log_port_m.group(1) != cfg_port_m.group(1):
                    advisory = (f"Service is on :{log_port_m.group(1)}, not :{cfg_port_m.group(1)}. "
                                f"Update the URL here, or change the port in the repo.")
                    w["advisory_lbl"].configure(text=advisory)
                    w["advisory_inner"].configure(bg="#422006")
                    w["advisory_lbl"].configure(bg="#422006", fg="#FCD34D")
                    w["left_bar"].configure(bg=AMBER)
                    w["advisory_outer"].pack(fill=tk.X, before=w["bottom_pad"])
                    return
            w["advisory_outer"].pack_forget()

        elif status >= 500:
            # Process alive but returning errors — amber warning
            w["dot_animator"].set_state(PkgState.STARTING, self._root)
            if w["url_lbl"] is not None and pkg and pkg.url:
                _pm = re.search(r':(\d+)', pkg.url)
                link_text = f"Open localhost:{_pm.group(1)} →" if _pm else "Open service →"
                w["url_lbl"].configure(text=link_text, fg=BLUE,
                                       cursor="pointinghand", font=(fn, 11))
                w["url_lbl"].bind("<Button-1>", lambda e, u=pkg.url: webbrowser.open(u))
            # Warning colours (amber tint)
            log_text = self._read_log_tail(pkg_name)
            advisory = (_make_advisory(log_text)
                        or "The service is responding with errors. Check the log for details.")
            w["advisory_lbl"].configure(text=advisory)
            w["advisory_inner"].configure(bg="#422006")
            w["advisory_lbl"].configure(bg="#422006", fg="#FCD34D")
            w["left_bar"].configure(bg=AMBER)
            if w["accordion_open"][0]:
                w["log_lbl"].configure(text=log_text)
            w["advisory_outer"].pack(fill=tk.X, before=w["bottom_pad"])

        else:
            # Healthy — but verify we don't have a port-conflict false positive
            log_text = self._read_log_tail(pkg_name)
            if re.search(r'EADDRINUSE|address already in use', log_text, re.IGNORECASE):
                self._set_pkg_state(pkg_name, PkgState.ERROR)
                self._signal_stop_event(pkg_name)
                return
            w["dot_animator"].set_state(PkgState.RUNNING, self._root)
            if w["url_lbl"] is not None and pkg and pkg.url:
                _pm = re.search(r':(\d+)', pkg.url)
                link_text = f"Open localhost:{_pm.group(1)} →" if _pm else "Open service →"
                w["url_lbl"].configure(text=link_text, fg=BLUE,
                                       cursor="pointinghand", font=(fn, 11))
                w["url_lbl"].bind("<Button-1>", lambda e, u=pkg.url: webbrowser.open(u))
            w["advisory_outer"].pack_forget()
            if w["accordion_open"][0]:
                w["log_frame"].pack_forget()
                w["log_toggle"].configure(text="Show log")
                w["accordion_open"][0] = False

        self._root.geometry("")

    # ---- Global Start All / Stop All -----------------------------------

    def _update_global_btn(self) -> None:
        btn = getattr(self, "_global_btn", None)
        if btn is None:
            return
        states = list(self._pkg_states.values())
        if not states:
            btn.configure(state=tk.DISABLED, text="Start All")
            return
        if all(s == PkgState.RUNNING for s in states):
            btn.configure(state=tk.NORMAL, text="Stop All")
        else:
            btn.configure(state=tk.NORMAL, text="Start All")

    def _on_global_action(self) -> None:
        states = list(self._pkg_states.values())
        if states and all(s == PkgState.RUNNING for s in states):
            for pkg in self._config.packages:
                if self._pkg_states.get(pkg.name) == PkgState.RUNNING:
                    self._do_stop_pkg(pkg.name)
        else:
            for pkg in self._config.packages:
                if self._pkg_states.get(pkg.name) in (PkgState.OFF, PkgState.ERROR):
                    self._do_start_pkg(pkg.name)

    # ---- Queue polling ------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._ui_queue.get_nowait()
                try:
                    if msg[0] == "pkg_state":
                        _, pkg_name, state, error_msg = msg
                        self._set_pkg_state(pkg_name, state, error_msg)
                        if state == PkgState.RUNNING:
                            self._start_health_check(pkg_name)
                        elif state in (PkgState.ERROR, PkgState.OFF):
                            self._signal_stop_event(pkg_name)

                    elif msg[0] == "pkg_health":
                        _, pkg_name, status = msg
                        self._apply_pkg_health(pkg_name, status)

                    elif msg[0] == "pkg_exited":
                        _, pkg_name, log_tail = msg
                        if pkg_name not in self._stopping:
                            self._set_pkg_state(pkg_name, PkgState.ERROR, log_tail)

                except Exception as exc:
                    print(f"[Autostart] error handling {msg[0]!r} for {msg[1]!r}: {exc}",
                          file=sys.stderr)

        except queue.Empty:
            pass
        self._root.after(self._POLL_MS, self._poll_queue)

    # ---- User actions ------------------------------------------------

    def _on_pkg_action(self, pkg_name: str) -> None:
        state = self._pkg_states.get(pkg_name, PkgState.OFF)
        if state in (PkgState.OFF, PkgState.ERROR):
            self._do_start_pkg(pkg_name)
        elif state == PkgState.RUNNING:
            self._do_stop_pkg(pkg_name)

    def _do_start_pkg(self, pkg_name: str) -> None:
        pkg = next((p for p in self._config.packages if p.name == pkg_name), None)
        if pkg is None:
            return

        stop_event = threading.Event()
        self._pkg_stop_events[pkg_name] = stop_event
        self._stopping.discard(pkg_name)

        self._set_pkg_state(pkg_name, PkgState.STARTING)

        threading.Thread(
            target=_pkg_worker,
            args=(pkg, self._packages_dir, self._pm, self._ui_queue),
            daemon=True,
        ).start()

        threading.Thread(
            target=self._pkg_monitor_loop,
            args=(pkg_name, stop_event),
            daemon=True,
        ).start()

    def _do_stop_pkg(self, pkg_name: str) -> None:
        self._stopping.add(pkg_name)
        self._signal_stop_event(pkg_name)

        w = self._pkg_widgets.get(pkg_name)
        if w:
            w["action_btn"].configure(text="Stopping...", state=tk.DISABLED)

        def _stop() -> None:
            self._pm.stop_one(pkg_name)
            self._stopping.discard(pkg_name)
            self._ui_queue.put(("pkg_state", pkg_name, PkgState.OFF, ""))

        threading.Thread(target=_stop, daemon=True).start()

    # ---- Health checks -----------------------------------------------

    def _start_health_check(self, pkg_name: str) -> None:
        pkg = next((p for p in self._config.packages if p.name == pkg_name), None)
        if pkg is None or not pkg.url:
            return
        stop_event = self._pkg_stop_events.get(pkg_name)
        if stop_event is None or stop_event.is_set():
            return
        threading.Thread(
            target=self._health_check_loop,
            args=(pkg_name, pkg.url, stop_event),
            daemon=True,
        ).start()

    def _health_check_loop(self, pkg_name: str, url: str, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                resp = urllib.request.urlopen(url, timeout=4)
                status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except (urllib.error.URLError, OSError):
                status = 0
            self._ui_queue.put(("pkg_health", pkg_name, status))
            stop_event.wait(self._HEALTH_INTERVAL)

    # ---- Per-package process monitor --------------------------------

    def _pkg_monitor_loop(self, pkg_name: str, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            if self._pm.is_running(pkg_name):
                break
            stop_event.wait(0.2)

        if stop_event.is_set():
            return

        while not stop_event.wait(self._MONITOR_POLL):
            ret = self._pm.poll_one(pkg_name)
            if ret is not None:
                if stop_event.is_set():
                    return
                log_tail = self._read_log_tail(pkg_name)
                self._ui_queue.put(("pkg_exited", pkg_name, log_tail))
                return

    def _signal_stop_event(self, pkg_name: str) -> None:
        ev = self._pkg_stop_events.get(pkg_name)
        if ev:
            ev.set()

    # ---- Log reading ------------------------------------------------

    def _read_log_tail(self, pkg_name: str, n: int = 8) -> str:
        log_path = self._packages_dir / pkg_name / "autostart.log"
        try:
            text = log_path.read_text(errors="replace")
            lines = []
            for raw in text.splitlines():
                line = re.sub(r'\x1b\[[0-9;]*m', '', raw)
                line = re.sub(r'^\[[^\]]+\]\s*', '', line).strip()
                if line:
                    lines.append(line)
            tail = lines[-n:] if len(lines) >= n else lines
            return "\n".join(tail) if tail else f"{pkg_name} exited (no log output)"
        except OSError:
            return f"{pkg_name} exited unexpectedly"

    # ---- Add service ------------------------------------------------

    def _on_add_service(self) -> None:
        AddServiceDialog(
            parent=self._root,
            config_path=self._config_path,
            existing_names=[p.name for p in self._config.packages],
            on_confirm=self._on_service_added,
            font_name=self._font_name,
        )

    def _on_service_added(self, pkg: PackageConfig) -> None:
        self._hide_empty_state()
        self._config.packages.append(pkg)
        self._pkg_states[pkg.name] = PkgState.OFF
        self._add_pkg_card(pkg)
        self._update_global_btn()
        self._root.geometry("")

    # ---- Edit service -----------------------------------------------

    def _on_edit_service(self, pkg_name: str) -> None:
        pkg = next((p for p in self._config.packages if p.name == pkg_name), None)
        if pkg is None:
            return

        def _on_confirm(branch: str, start_command: str, url: Optional[str]) -> None:
            pkg.branch = branch
            pkg.start_command = start_command
            pkg.url = url
            rewrite_config(self._config_path, self._config.packages_dir, self._config.packages)
            # Update url_lbl text if it exists
            w = self._pkg_widgets.get(pkg_name)
            if w and w.get("url_lbl") and url:
                _pm = re.search(r':(\d+)', url)
                new_text = f"localhost:{_pm.group(1)}" if _pm else url
                w["url_lbl"].configure(text=new_text)

        EditServiceDialog(self._root, pkg, _on_confirm, self._font_name)

    # ---- Remove service ---------------------------------------------

    def _on_remove_service(self, pkg_name: str) -> None:
        pkg_dir = self._packages_dir / pkg_name
        msg = (
            f"Remove '{pkg_name}' from Autostart?"
            + (f"\n\nThis will also delete {pkg_dir}." if pkg_dir.exists() else "")
            + "\n\nThis cannot be undone."
        )
        if not tkinter.messagebox.askyesno("Remove Service", msg, icon="warning"):
            return

        state = self._pkg_states.get(pkg_name, PkgState.OFF)
        if state in (PkgState.RUNNING, PkgState.STARTING):
            self._stopping.add(pkg_name)
            self._signal_stop_event(pkg_name)
            self._pm.stop_one(pkg_name)
            self._stopping.discard(pkg_name)

        # Cancel any dot animations
        w = self._pkg_widgets.get(pkg_name)
        if w and w.get("dot_animator"):
            w["dot_animator"].cancel()

        self._config.packages = [p for p in self._config.packages if p.name != pkg_name]
        self._pkg_states.pop(pkg_name, None)
        self._pkg_stop_events.pop(pkg_name, None)
        self._stopping.discard(pkg_name)

        w = self._pkg_widgets.pop(pkg_name, None)
        if w:
            w["outer"].destroy()

        rewrite_config(self._config_path, self._config.packages_dir, self._config.packages)

        if pkg_dir.exists():
            shutil.rmtree(pkg_dir, ignore_errors=True)

        if not self._config.packages:
            self._show_empty_state()

        self._update_global_btn()
        self._root.geometry("")

    # ---- Fairy backup -----------------------------------------------

    def _start_fairy_backup(self) -> None:
        threading.Thread(
            target=self._fairy_backup_loop,
            daemon=True,
        ).start()

    def _fairy_backup_loop(self) -> None:
        while not self._fairy_backup_stop.wait(self._FAIRY_BACKUP_INTERVAL):
            for pkg in list(self._config.packages):
                pkg_dir = self._packages_dir / pkg.name
                if pkg_dir.exists():
                    push = "ux-mark/" in pkg.repo
                    _fairy_backup_pkg(pkg_dir, push=push)

    # ---- Window close -----------------------------------------------

    def _on_close(self) -> None:
        self._fairy_backup_stop.set()
        for ev in self._pkg_stop_events.values():
            ev.set()
        self._pm.stop_all()
        self._root.destroy()

    # ---- Run --------------------------------------------------------

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
