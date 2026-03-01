#!/usr/bin/env python3
"""Fairy Start — lightweight macOS service manager with per-service controls."""

from __future__ import annotations

import base64
import enum
import dataclasses
import json
import math
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


def _macos_set_app_name() -> None:
    """Override the macOS menu-bar app name to 'Fairy Start' before NSApplication init.

    Must be called before tk.Tk() — Tk reads CFBundleName from NSBundle.mainBundle()
    when it creates the application menu.  Uses the ObjC runtime via ctypes;
    no external packages required.
    """
    import sys
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        _lib = ctypes.cdll.LoadLibrary("libobjc.dylib")
        _lib.objc_getClass.restype    = ctypes.c_void_p
        _lib.objc_getClass.argtypes   = [ctypes.c_char_p]
        _lib.sel_registerName.restype  = ctypes.c_void_p
        _lib.sel_registerName.argtypes = [ctypes.c_char_p]

        def _msg(restype, obj, sel_bytes, *args):
            fn = _lib.objc_msgSend
            fn.restype  = restype
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [type(a) for a in args]
            return fn(obj, _lib.sel_registerName(sel_bytes), *args)

        NSBundle = _lib.objc_getClass(b"NSBundle")
        NSString = _lib.objc_getClass(b"NSString")

        bundle = _msg(ctypes.c_void_p, NSBundle, b"mainBundle")
        info   = _msg(ctypes.c_void_p, bundle,   b"infoDictionary")
        key    = _msg(ctypes.c_void_p, NSString,  b"stringWithUTF8String:",
                      ctypes.c_char_p(b"CFBundleName"))
        val    = _msg(ctypes.c_void_p, NSString,  b"stringWithUTF8String:",
                      ctypes.c_char_p(b"Fairy Start"))
        _msg(None, info, b"setObject:forKey:",
             ctypes.c_void_p(val), ctypes.c_void_p(key))
    except Exception:
        pass


def _macos_configure_titlebar(root: "tk.Tk") -> None:
    """Make the titlebar transparent with visible traffic-light buttons.

    Sets NSWindow properties so the titlebar chrome blends into the app
    background while keeping close/minimize/maximize controls visible.
    Uses the same ctypes/ObjC runtime pattern as _macos_set_app_name().
    """
    import sys
    if sys.platform != "darwin":
        return
    try:
        import ctypes

        objc = ctypes.cdll.LoadLibrary("libobjc.dylib")
        objc.objc_getClass.restype    = ctypes.c_void_p
        objc.objc_getClass.argtypes   = [ctypes.c_char_p]
        objc.sel_registerName.restype  = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def _msg(restype, obj, sel_bytes, *args):
            fn = objc.objc_msgSend
            fn.restype  = restype
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [type(a) for a in args]
            return fn(obj, objc.sel_registerName(sel_bytes), *args)

        # --- get the NSWindow for the Tk root ---
        # Tk's winfo_id() is NOT a valid NSView on modern macOS Tk, so we
        # go through NSApplication → windows array and match by title.
        root.update()  # force full event processing so NSWindow exists

        NSApp = objc.objc_getClass(b"NSApplication")
        app   = _msg(ctypes.c_void_p, NSApp, b"sharedApplication")
        wins  = _msg(ctypes.c_void_p, app,   b"windows")
        count = _msg(ctypes.c_uint64, wins,  b"count")
        if count == 0:
            return

        # Find the window whose title matches our Tk root title.
        win = None
        title = root.title()
        for i in range(count):
            w = _msg(ctypes.c_void_p, wins, b"objectAtIndex:",
                     ctypes.c_uint64(i))
            if not w:
                continue
            ns_title = _msg(ctypes.c_void_p, w, b"title")
            if not ns_title:
                continue
            utf8 = _msg(ctypes.c_char_p, ns_title, b"UTF8String")
            if utf8 and utf8.decode("utf-8", errors="replace") == title:
                win = w
                break

        if not win:
            # Fallback: just use the first window.
            win = _msg(ctypes.c_void_p, wins, b"objectAtIndex:",
                       ctypes.c_uint64(0))
        if not win:
            return

        # titlebarAppearsTransparent = YES
        _msg(None, win, b"setTitlebarAppearsTransparent:", ctypes.c_bool(True))

        # titleVisibility = NSWindowTitleHidden (1)
        _msg(None, win, b"setTitleVisibility:", ctypes.c_int64(1))

        # styleMask |= NSWindowStyleMaskFullSizeContentView (1 << 15)
        current_mask = _msg(ctypes.c_uint64, win, b"styleMask")
        new_mask = current_mask | (1 << 15)
        _msg(None, win, b"setStyleMask:", ctypes.c_uint64(new_mask))

        # backgroundColor — parse WINDOW_BG into NSColor
        bg = WINDOW_BG.lstrip("#")
        r = int(bg[0:2], 16) / 255.0
        g = int(bg[2:4], 16) / 255.0
        b_val = int(bg[4:6], 16) / 255.0
        NSColor = objc.objc_getClass(b"NSColor")
        color = _msg(
            ctypes.c_void_p, NSColor,
            b"colorWithRed:green:blue:alpha:",
            ctypes.c_double(r), ctypes.c_double(g),
            ctypes.c_double(b_val), ctypes.c_double(1.0),
        )
        _msg(None, win, b"setBackgroundColor:", ctypes.c_void_p(color))

        # movableByWindowBackground = YES
        _msg(None, win, b"setMovableByWindowBackground:", ctypes.c_bool(True))

    except Exception:
        pass


def _macos_set_titlebar_bg(root: "tk.Tk", hex_color: str) -> None:
    """Set the NSWindow background color to match the given hex color."""
    import sys
    if sys.platform != "darwin":
        return
    try:
        import ctypes

        objc = ctypes.cdll.LoadLibrary("libobjc.dylib")
        objc.objc_getClass.restype    = ctypes.c_void_p
        objc.objc_getClass.argtypes   = [ctypes.c_char_p]
        objc.sel_registerName.restype  = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def _msg(restype, obj, sel_bytes, *args):
            fn = objc.objc_msgSend
            fn.restype  = restype
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [type(a) for a in args]
            return fn(obj, objc.sel_registerName(sel_bytes), *args)

        NSApp = objc.objc_getClass(b"NSApplication")
        app   = _msg(ctypes.c_void_p, NSApp, b"sharedApplication")
        wins  = _msg(ctypes.c_void_p, app,   b"windows")
        count = _msg(ctypes.c_uint64, wins,  b"count")
        if count == 0:
            return

        win = None
        title = root.title()
        for i in range(count):
            w = _msg(ctypes.c_void_p, wins, b"objectAtIndex:",
                     ctypes.c_uint64(i))
            if not w:
                continue
            ns_title = _msg(ctypes.c_void_p, w, b"title")
            if not ns_title:
                continue
            utf8 = _msg(ctypes.c_char_p, ns_title, b"UTF8String")
            if utf8 and utf8.decode("utf-8", errors="replace") == title:
                win = w
                break

        if not win:
            win = _msg(ctypes.c_void_p, wins, b"objectAtIndex:",
                       ctypes.c_uint64(0))
        if not win:
            return

        bg = hex_color.lstrip("#")
        r = int(bg[0:2], 16) / 255.0
        g = int(bg[2:4], 16) / 255.0
        b_val = int(bg[4:6], 16) / 255.0
        NSColor = objc.objc_getClass(b"NSColor")
        color = _msg(
            ctypes.c_void_p, NSColor,
            b"colorWithRed:green:blue:alpha:",
            ctypes.c_double(r), ctypes.c_double(g),
            ctypes.c_double(b_val), ctypes.c_double(1.0),
        )
        _msg(None, win, b"setBackgroundColor:", ctypes.c_void_p(color))
    except Exception:
        pass


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
# UI constants — palette system (light + dark)
# ---------------------------------------------------------------------------

_DARK: dict[str, str] = {
    "WINDOW_BG":         "#1E1E24",
    "CARD_BG":           "#2A2A32",
    "CARD_BORDER":       "#3A3A44",
    "CARD_BORDER_HOVER": "#52525B",
    "HEADER_BG":         "#1E1E24",

    "TEXT_PRIMARY":   "#F5F5F7",
    "TEXT_SECONDARY": "#A0A0AB",
    "TEXT_TERTIARY":  "#9090A0",

    "BLUE":          "#4070CF",
    "BLUE_HOVER":    "#4575C5",
    "RED":           "#F87171",
    "RED_HOVER":     "#EF4444",
    "STOP_BG":       "#DC2626",
    "DISABLED_BG":   "#3A3A44",
    "DISABLED_TEXT":  "#8E8E99",

    "GREEN":      "#4ADE80",
    "GREEN_GLOW": "#1A5C32",
    "AMBER":      "#FBBF24",

    "INPUT_BG":      "#1A1A22",
    "LOG_BG":        "#16161C",
    "BTN_TEXT":      "#FFFFFF",

    "ERROR_BG":      "#450A0A",
    "ERROR_TEXT":     "#FCA5A5",
    "WARNING_BG":    "#422006",
    "WARNING_TEXT":   "#FCD34D",

    "AUTH_BANNER_BG":   "#2A1A00",
    "UPDATE_BANNER_BG": "#0D2017",

    "REMOVE_LINK":       "#F87171",
    "REMOVE_LINK_HOVER": "#F87171",
    "GREEN_HOVER":       "#22C55E",
    "AMBER_HOVER":       "#F59E0B",
    "PULSE_BRIGHT":      "#9CA3AF",

    # Pill colors: (bg, fg) per state
    "PILL_OFF_BG":      "#3A3A44",
    "PILL_OFF_FG":      "#ABABBA",
    "PILL_STARTING_BG": "#422006",
    "PILL_STARTING_FG": "#FCD34D",
    "PILL_RUNNING_BG":  "#052E16",
    "PILL_RUNNING_FG":  "#86EFAC",
    "PILL_ERROR_BG":    "#450A0A",
    "PILL_ERROR_FG":    "#FCA5A5",

    # Dot colors per state
    "DOT_OFF":      "#78788A",
    "DOT_STARTING": "#FBBF24",
    "DOT_RUNNING":  "#4ADE80",
    "DOT_ERROR":    "#F87171",

    # Banner / keyline text
    "AUTH_BANNER_TEXT":   "#FBBF24",
    "UPDATE_BANNER_TEXT": "#4ADE80",
    "AMBER_BTN_FG":      "#FFFFFF",
    "GREEN_BTN_FG":      "#FFFFFF",
    "BLUE_TEXT":         "#5B8DEF",
    "RED_TEXT":          "#F87171",
}

_LIGHT: dict[str, str] = {
    "WINDOW_BG":         "#F5F5F7",
    "CARD_BG":           "#FFFFFF",
    "CARD_BORDER":       "#E5E7EB",
    "CARD_BORDER_HOVER": "#D1D5DB",
    "HEADER_BG":         "#F5F5F7",

    "TEXT_PRIMARY":   "#1F2937",
    "TEXT_SECONDARY": "#636B78",
    "TEXT_TERTIARY":  "#6A7276",

    "BLUE":          "#2D6BD9",
    "BLUE_HOVER":    "#2563EB",
    "RED":           "#EF4444",
    "RED_HOVER":     "#DC2626",
    "STOP_BG":       "#DC2626",
    "DISABLED_BG":   "#E5E7EB",
    "DISABLED_TEXT":  "#7A828E",

    "GREEN":      "#22C55E",
    "GREEN_GLOW": "#BBF7D0",
    "AMBER":      "#F59E0B",

    "INPUT_BG":      "#FFFFFF",
    "LOG_BG":        "#F3F4F6",
    "BTN_TEXT":      "#FFFFFF",

    "ERROR_BG":      "#FEE2E2",
    "ERROR_TEXT":     "#B91C1C",
    "WARNING_BG":    "#FEF3C7",
    "WARNING_TEXT":   "#92400E",

    "AUTH_BANNER_BG":   "#FEF3C7",
    "UPDATE_BANNER_BG": "#DCFCE7",

    "REMOVE_LINK":       "#DC2626",
    "REMOVE_LINK_HOVER": "#EF4444",
    "GREEN_HOVER":       "#16A34A",
    "AMBER_HOVER":       "#D97706",
    "PULSE_BRIGHT":      "#6B7280",

    # Pill colors: (bg, fg) per state
    "PILL_OFF_BG":      "#E5E7EB",
    "PILL_OFF_FG":      "#585F6C",
    "PILL_STARTING_BG": "#FEF3C7",
    "PILL_STARTING_FG": "#92400E",
    "PILL_RUNNING_BG":  "#DCFCE7",
    "PILL_RUNNING_FG":  "#166534",
    "PILL_ERROR_BG":    "#FEE2E2",
    "PILL_ERROR_FG":    "#B91C1C",

    # Dot colors per state
    "DOT_OFF":      "#848B97",
    "DOT_STARTING": "#F59E0B",
    "DOT_RUNNING":  "#22C55E",
    "DOT_ERROR":    "#EF4444",

    # Banner / keyline text
    "AUTH_BANNER_TEXT":   "#854D0E",
    "UPDATE_BANNER_TEXT": "#166534",
    "AMBER_BTN_FG":      "#422006",
    "GREEN_BTN_FG":      "#052E16",
    "BLUE_TEXT":         "#2D6BD9",
    "RED_TEXT":          "#B91C1C",
}

# Module-level color globals — updated by _apply_palette()
WINDOW_BG = CARD_BG = CARD_BORDER = CARD_BORDER_HOVER = HEADER_BG = ""
TEXT_PRIMARY = TEXT_SECONDARY = TEXT_TERTIARY = ""
BLUE = BLUE_HOVER = RED = RED_HOVER = STOP_BG = DISABLED_BG = DISABLED_TEXT = ""
GREEN = GREEN_GLOW = AMBER = ""
INPUT_BG = LOG_BG = BTN_TEXT = ""
ERROR_BG = ERROR_TEXT = WARNING_BG = WARNING_TEXT = ""
AUTH_BANNER_BG = UPDATE_BANNER_BG = ""
REMOVE_LINK = REMOVE_LINK_HOVER = GREEN_HOVER = AMBER_HOVER = PULSE_BRIGHT = ""
AUTH_BANNER_TEXT = UPDATE_BANNER_TEXT = ""
AMBER_BTN_FG = GREEN_BTN_FG = ""
BLUE_TEXT = RED_TEXT = ""

PILL_COLORS: dict[PkgState, tuple[str, str]] = {}
DOT_COLORS: dict[PkgState, str] = {}


def _apply_palette(palette: dict[str, str]) -> None:
    """Update all module-level color globals from a palette dict."""
    g = globals()
    for key, val in palette.items():
        g[key] = val
    g["PILL_COLORS"] = {
        PkgState.OFF:      (palette["PILL_OFF_BG"],      palette["PILL_OFF_FG"]),
        PkgState.STARTING: (palette["PILL_STARTING_BG"], palette["PILL_STARTING_FG"]),
        PkgState.RUNNING:  (palette["PILL_RUNNING_BG"],  palette["PILL_RUNNING_FG"]),
        PkgState.ERROR:    (palette["PILL_ERROR_BG"],     palette["PILL_ERROR_FG"]),
    }
    g["DOT_COLORS"] = {
        PkgState.OFF:      palette["DOT_OFF"],
        PkgState.STARTING: palette["DOT_STARTING"],
        PkgState.RUNNING:  palette["DOT_RUNNING"],
        PkgState.ERROR:    palette["DOT_ERROR"],
    }


def _detect_system_theme() -> str:
    """Return 'dark' or 'light' based on macOS system appearance."""
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, timeout=3,
        )
        if b"Dark" in result.stdout:
            return "dark"
    except Exception:
        pass
    return "light"


def _blend(c1: str, c2: str, t: float) -> str:
    """Linear interpolation between two hex colors. t=0 → c1, t=1 → c2."""
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02X}{g:02X}{b:02X}"


# Apply initial palette based on system theme
_apply_palette(_DARK if _detect_system_theme() == "dark" else _LIGHT)

# Standard macOS titlebar height (28pt) — used as a spacer so content
# doesn't overlap the traffic-light buttons.
_TITLEBAR_HEIGHT = 28

# Indent for row-2 to align content under the service name (dot canvas + gap)
DOT_CANVAS_INDENT = 34

# Max card width for resizable window
CARD_MAX_WIDTH = 520

PILL_LABELS: dict[PkgState, str] = {
    PkgState.OFF:      "Off",
    PkgState.STARTING: "Starting…",
    PkgState.RUNNING:  "Running",
    PkgState.ERROR:    "Error",
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
        bg: str = "",
        fg: str = "",
        hover_bg: str = "",
        hover_fg: str = "",
        disabled_bg: str = "",
        disabled_fg: str = "",
        padx: int = 16,
        pady: int = 7,
        command: Optional[Callable] = None,
        cursor: str = "pointinghand",
    ) -> None:
        self._bg = bg or BLUE
        self._fg = fg or BTN_TEXT
        self._hover_bg = hover_bg or BLUE_HOVER
        self._hover_fg = hover_fg or BTN_TEXT
        self._disabled_bg = disabled_bg or DISABLED_BG
        self._disabled_fg = disabled_fg or DISABLED_TEXT
        self._command = command
        self._enabled = True

        self._label = tk.Label(
            parent,
            text=text,
            font=font,
            bg=self._bg,
            fg=self._fg,
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
        if "disabled_bg" in kwargs:
            self._disabled_bg = kwargs.pop("disabled_bg")
        if "disabled_fg" in kwargs:
            self._disabled_fg = kwargs.pop("disabled_fg")
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
    on macOS Aqua (tk.Button ignores bg/fg entirely on this platform).

    Fill: overlapping ovals + rects (single color, no outline — clean).
    Border: create_arc corner strokes (anti-aliased by CoreGraphics) +
    create_line straight edges (axis-aligned, crisp).  Arcs extended 5°
    past each corner to overlap the lines — prevents junction gaps.
    """

    RADIUS = 12
    _TAG_FILL = "rrf"   # fill layer (ovals + rects)
    _TAG_BA = "rrba"    # border arcs  (recolour via outline=)
    _TAG_BL = "rrbl"    # border lines (recolour via fill=)
    _TAG_ICON = "rri"   # icon items
    _ICON_W = 10        # icon drawing area width
    _ICON_GAP = 4       # gap between icon and text

    def __init__(
        self,
        parent: tk.Widget,
        text: str = "",
        font: tuple = (),
        bg: str = "",
        fg: str = "",
        hover_bg: str = "",
        hover_fg: str = "",
        disabled_bg: str = "",
        disabled_fg: str = "",
        padx: int = 16,
        pady: int = 6,
        command: Optional[Callable] = None,
        parent_bg: str = "",
        min_width: int = 0,
        outline: str = "",
        outline_width: int = 0,
        hover_outline: str = "",
        icon: Optional[str] = None,
    ) -> None:
        self._bg = bg or BLUE
        self._fg = fg or BTN_TEXT
        self._hover_bg = hover_bg or BLUE_HOVER
        self._hover_fg = hover_fg or BTN_TEXT
        self._disabled_bg = disabled_bg or DISABLED_BG
        self._disabled_fg = disabled_fg or DISABLED_TEXT
        self._command = command
        self._enabled = True
        self._text = text
        self._font_spec = font
        self._padx = padx
        self._pady = pady
        self._min_w = min_width
        self._outline = outline
        self._outline_width = outline_width or (2 if outline else 0)
        self._hover_outline = hover_outline
        self._icon = icon

        tmp = self._make_font()
        text_w = tmp.measure(text)
        text_h = tmp.metrics("linespace")
        icon_extra = (self._ICON_W + self._ICON_GAP) if icon else 0
        self._w = max(text_w + icon_extra + padx * 2, min_width)
        self._h = text_h + pady * 2

        _parent_bg = parent_bg or CARD_BG
        self._canvas = tk.Canvas(
            parent,
            width=self._w, height=self._h,
            bg=_parent_bg,
            highlightthickness=0,
            cursor="pointinghand",
        )
        self._text_id = None
        self._redraw_bg()
        tx, ty = self._text_pos(text_w, icon_extra)
        self._text_id = self._canvas.create_text(
            tx, ty, text=text, fill=self._fg, font=font,
        )
        if icon:
            self._draw_icon(icon_extra, text_w)
            self._canvas.tag_raise(self._text_id)
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

    def _text_pos(self, text_w: float = 0, icon_extra: float = 0) -> tuple:
        """Return (x, y) center for the text item."""
        if not text_w:
            text_w = self._make_font().measure(self._text)
        if icon_extra == 0 and self._icon:
            icon_extra = self._ICON_W + self._ICON_GAP
        total = icon_extra + text_w
        start_x = (self._w - total) / 2
        tx = start_x + icon_extra + text_w / 2
        return tx, self._h / 2

    def _draw_icon(self, icon_extra: float = 0, text_w: float = 0) -> None:
        """Draw the current icon (play/stop) next to the text."""
        self._canvas.delete(self._TAG_ICON)
        if not self._icon:
            return
        if not text_w:
            text_w = self._make_font().measure(self._text)
        if not icon_extra:
            icon_extra = self._ICON_W + self._ICON_GAP
        total = icon_extra + text_w
        start_x = (self._w - total) / 2
        cx = start_x + self._ICON_W / 2
        cy = self._h / 2
        fg = self._disabled_fg if not self._enabled else self._fg
        if self._icon == "play":
            self._canvas.create_polygon(
                cx - 4, cy - 5, cx - 4, cy + 5, cx + 5, cy,
                fill=fg, outline="", tags=(self._TAG_ICON,),
            )
        elif self._icon == "stop":
            self._canvas.create_rectangle(
                cx - 4, cy - 4, cx + 4, cy + 4,
                fill=fg, outline="", tags=(self._TAG_ICON,),
            )

    # ---- Rounded-rect rendering ----------------------------------------

    def _draw_rr_fill(self, x1: float, y1: float, x2: float, y2: float,
                      r: float, color: str) -> None:
        """Draw fill: 4 overlapping ovals + 2 rects, single color."""
        c = self._canvas
        d = 2 * r
        tag = self._TAG_FILL
        c.create_oval(x1, y1, x1 + d, y1 + d, fill=color, outline="", tags=(tag,))
        c.create_oval(x2 - d, y1, x2, y1 + d, fill=color, outline="", tags=(tag,))
        c.create_oval(x1, y2 - d, x1 + d, y2, fill=color, outline="", tags=(tag,))
        c.create_oval(x2 - d, y2 - d, x2, y2, fill=color, outline="", tags=(tag,))
        c.create_rectangle(x1 + r, y1, x2 - r, y2, fill=color, outline="", tags=(tag,))
        c.create_rectangle(x1, y1 + r, x2, y2 - r, fill=color, outline="", tags=(tag,))

    def _draw_rr_border(self, ow: int, r: float, color: str) -> None:
        """Draw border: arc strokes (anti-aliased) + straight lines.

        Arcs are extended 5° past each 90° corner so they overlap the
        straight lines, preventing any visible gap at the junction.
        """
        c = self._canvas
        w, h = self._w, self._h
        inset = math.ceil(ow / 2)  # round up so stroke stays inside canvas
        bx1, by1 = inset, inset
        bx2, by2 = w - inset, h - inset
        rb = max(r - inset, 1)
        d = 2 * rb
        ta, tl = self._TAG_BA, self._TAG_BL
        _OV = 5  # overlap degrees
        ov_px = rb * math.sin(math.radians(_OV))  # ~1px at r=12
        # Corner arc strokes (anti-aliased by CoreGraphics)
        c.create_arc(bx1, by1, bx1 + d, by1 + d,
                     start=90 - _OV, extent=90 + 2 * _OV,
                     style="arc", outline=color, width=ow, tags=(ta,))
        c.create_arc(bx2 - d, by1, bx2, by1 + d,
                     start=0 - _OV, extent=90 + 2 * _OV,
                     style="arc", outline=color, width=ow, tags=(ta,))
        c.create_arc(bx1, by2 - d, bx1 + d, by2,
                     start=180 - _OV, extent=90 + 2 * _OV,
                     style="arc", outline=color, width=ow, tags=(ta,))
        c.create_arc(bx2 - d, by2 - d, bx2, by2,
                     start=270 - _OV, extent=90 + 2 * _OV,
                     style="arc", outline=color, width=ow, tags=(ta,))
        # Straight edges (extended into arc overlap region)
        c.create_line(bx1 + rb - ov_px, by1, bx2 - rb + ov_px, by1,
                      fill=color, width=ow, tags=(tl,))
        c.create_line(bx1 + rb - ov_px, by2, bx2 - rb + ov_px, by2,
                      fill=color, width=ow, tags=(tl,))
        c.create_line(bx1, by1 + rb - ov_px, bx1, by2 - rb + ov_px,
                      fill=color, width=ow, tags=(tl,))
        c.create_line(bx2, by1 + rb - ov_px, bx2, by2 - rb + ov_px,
                      fill=color, width=ow, tags=(tl,))

    def _redraw_bg(self) -> None:
        """Delete and redraw fill (+ border if keyline)."""
        self._canvas.delete(self._TAG_FILL)
        self._canvas.delete(self._TAG_BA)
        self._canvas.delete(self._TAG_BL)
        r = min(self.RADIUS, self._w / 2, self._h / 2)
        ow = self._outline_width
        fill = self._bg if self._enabled else self._disabled_bg
        # Fill layer — ovals + rects
        self._draw_rr_fill(0, 0, self._w, self._h, r, fill)
        # Border on top (keyline only)
        if ow > 0:
            border = self._outline if self._outline else self._bg
            if not self._enabled:
                border = self._disabled_bg
            self._draw_rr_border(ow, r, border)
        # Keep foreground items on top
        if self._canvas.find_withtag(self._TAG_ICON):
            self._canvas.tag_raise(self._TAG_ICON)
        if self._text_id is not None:
            self._canvas.tag_raise(self._text_id)

    def _set_colors(self, fill: str, border: str) -> None:
        """Update layer colors without redrawing geometry."""
        self._canvas.itemconfigure(self._TAG_FILL, fill=fill)
        if self._canvas.find_withtag(self._TAG_BA):
            self._canvas.itemconfigure(self._TAG_BA, outline=border)
        if self._canvas.find_withtag(self._TAG_BL):
            self._canvas.itemconfigure(self._TAG_BL, fill=border)

    # ---- Public API ----------------------------------------------------

    @property
    def widget(self) -> tk.Canvas:
        return self._canvas

    def pack(self, **kw) -> None:
        self._canvas.pack(**kw)

    def pack_forget(self) -> None:
        self._canvas.pack_forget()

    def configure(self, **kw) -> None:
        if "parent_bg" in kw:
            self._canvas.configure(bg=kw.pop("parent_bg"))
        if "disabled_bg" in kw:
            self._disabled_bg = kw.pop("disabled_bg")
        if "disabled_fg" in kw:
            self._disabled_fg = kw.pop("disabled_fg")
        if "outline" in kw:
            self._outline = kw.pop("outline")
            if self._enabled:
                oln = self._outline if self._outline else self._bg
                if self._canvas.find_withtag(self._TAG_BA):
                    self._canvas.itemconfigure(self._TAG_BA, outline=oln)
                if self._canvas.find_withtag(self._TAG_BL):
                    self._canvas.itemconfigure(self._TAG_BL, fill=oln)
        if "hover_outline" in kw:
            self._hover_outline = kw.pop("hover_outline")
        if "state" in kw:
            state = kw.pop("state")
            self._enabled = (state != tk.DISABLED)
            if self._enabled:
                fill = self._bg
                oln = self._outline if self._outline else self._bg
                tfill = self._fg
            else:
                fill = self._disabled_bg
                oln = self._disabled_bg
                tfill = self._disabled_fg
            cur = "pointinghand" if self._enabled else ""
            self._set_colors(fill, oln)
            self._canvas.itemconfigure(self._text_id, fill=tfill)
            if self._canvas.find_withtag(self._TAG_ICON):
                self._canvas.itemconfigure(self._TAG_ICON, fill=tfill)
            self._canvas.configure(cursor=cur)
        _need_layout = False
        if "outline_width" in kw:
            self._outline_width = kw.pop("outline_width")
            _need_layout = True
        if "icon" in kw:
            self._icon = kw.pop("icon")
            _need_layout = True
        if "text" in kw:
            self._text = kw.pop("text")
            _need_layout = True
        if _need_layout:
            tmp = self._make_font()
            text_w = tmp.measure(self._text)
            icon_extra = (self._ICON_W + self._ICON_GAP) if self._icon else 0
            desired_w = max(text_w + icon_extra + self._padx * 2, self._min_w)
            self._w = desired_w
            self._canvas.configure(width=self._w)
            self._redraw_bg()
            tx, ty = self._text_pos(text_w, icon_extra)
            self._canvas.coords(self._text_id, tx, ty)
            self._canvas.itemconfigure(self._text_id, text=self._text)
            self._draw_icon(icon_extra, text_w)
            self._canvas.tag_raise(self._text_id)
        if "bg" in kw:
            self._bg = kw.pop("bg")
            if self._enabled:
                self._canvas.itemconfigure(self._TAG_FILL, fill=self._bg)
                if not self._outline:
                    if self._canvas.find_withtag(self._TAG_BA):
                        self._canvas.itemconfigure(self._TAG_BA, outline=self._bg)
                    if self._canvas.find_withtag(self._TAG_BL):
                        self._canvas.itemconfigure(self._TAG_BL, fill=self._bg)
        if "fg" in kw:
            self._fg = kw.pop("fg")
            if self._enabled:
                self._canvas.itemconfigure(self._text_id, fill=self._fg)
                if self._canvas.find_withtag(self._TAG_ICON):
                    self._canvas.itemconfigure(self._TAG_ICON, fill=self._fg)
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
            h_oln = self._hover_outline if self._hover_outline else self._hover_bg
            self._set_colors(self._hover_bg, h_oln)
            self._canvas.itemconfigure(self._text_id, fill=self._hover_fg)
            if self._canvas.find_withtag(self._TAG_ICON):
                self._canvas.itemconfigure(self._TAG_ICON, fill=self._hover_fg)

    def _on_leave(self, _e) -> None:
        if self._enabled:
            oln = self._outline if self._outline else self._bg
            self._set_colors(self._bg, oln)
            self._canvas.itemconfigure(self._text_id, fill=self._fg)
            if self._canvas.find_withtag(self._TAG_ICON):
                self._canvas.itemconfigure(self._TAG_ICON, fill=self._fg)


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

    # Error blink: on-off-on-off-on, then steady for several frames
    _BLINK_PATTERN = [True, False, True, False, True,
                      True, True, True, True, True, True, True]
    _BLINK_MS      = [200,  200,   200,  200,   200,
                      500,  500,   500,  500,   500,  500,  500]

    def _pulse_steps(self) -> list[str]:
        """Compute pulse gradient dynamically from current theme colors."""
        # 8 steps: AMBER → CARD_BG → AMBER (breathing effect)
        fracs = [0.0, 0.25, 0.5, 0.75, 1.0, 0.75, 0.5, 0.25]
        return [_blend(AMBER, CARD_BG, t) for t in fracs]

    def __init__(self, parent: tk.Widget, bg: str = "") -> None:
        w = self._W
        self._bg = bg or CARD_BG
        self._canvas = tk.Canvas(
            parent, width=w, height=w,
            bg=self._bg, highlightthickness=0,
        )
        cx = cy = w // 2

        # Glow ring (behind core dot)
        self._glow_id = self._canvas.create_oval(
            cx - self._GLOW // 2, cy - self._GLOW // 2,
            cx + self._GLOW // 2, cy + self._GLOW // 2,
            fill=self._bg, outline="",
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

    def retheme(self, bg: str) -> None:
        """Update background color for theme change, re-apply current state."""
        self._bg = bg
        self._canvas.configure(bg=bg)
        if self._root is not None:
            self.set_state(self._state, self._root)

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
        steps = self._pulse_steps()
        color = steps[step % len(steps)]
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

class FairyStartError(Exception):
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
    fairy_backup: bool = True

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
                fairy_backup=entry.get("fairy_backup", True),
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
        raise FairyStartError("gh CLI not found — install from https://cli.github.com")
    except subprocess.TimeoutExpired:
        raise FairyStartError(f"gh api timed out for: {endpoint}")
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        _AUTH_HINTS = ("not logged", "auth token", "Please log in", "authentication required")
        if any(h.lower() in stderr.lower() for h in _AUTH_HINTS):
            raise FairyStartError(
                "GitHub authentication has expired — click 'Connect' in the banner to log in again."
            )
        raise FairyStartError(f"gh api failed: {stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FairyStartError(f"gh api returned invalid JSON: {exc}")


def gh_auth_status() -> str:
    """Return 'authenticated', 'unauthenticated', or 'gh_not_found'. Background thread only."""
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=10)
    except FileNotFoundError:
        return "gh_not_found"
    except subprocess.TimeoutExpired:
        return "unauthenticated"
    return "authenticated" if result.returncode == 0 else "unauthenticated"


def gh_file_content(owner: str, repo: str, path: str) -> Optional[str]:
    try:
        data = gh_api(f"repos/{owner}/{repo}/contents/{path}")
    except FairyStartError as exc:
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
        if not pkg.fairy_backup:
            lines.append(f'fairy_backup  = false')
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
        raise FairyStartError("git not found — install Xcode Command Line Tools")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise FairyStartError(f"git failed: {stderr}")
    except subprocess.TimeoutExpired:
        raise FairyStartError("git timed out")


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
        raise FairyStartError("npm not found — install Node.js from https://nodejs.org")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise FairyStartError(f"npm install failed: {stderr}")
    except subprocess.TimeoutExpired:
        raise FairyStartError("npm install timed out")


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
        log_path = pkg_dir / "fairy-start.log"
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
                log_path = pkg_dir / "fairy-start.log"
                try:
                    log_text = log_path.read_text(errors="replace")
                except OSError:
                    log_text = ""
                msg = _make_advisory(log_text) or "Service stopped immediately after starting."
                raise FairyStartError(msg)
            time.sleep(0.1)
        ui_queue.put(("pkg_state", pkg.name, PkgState.RUNNING, ""))
    except FairyStartError as exc:
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
            bg=INPUT_BG, fg=TEXT_PRIMARY,
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
            bg=BLUE, fg=BTN_TEXT,
            hover_bg=BLUE_HOVER, hover_fg=BTN_TEXT,
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
            bg=BLUE, fg=BTN_TEXT,
            hover_bg=BLUE_HOVER, hover_fg=BTN_TEXT,
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
                bg=INPUT_BG, fg=TEXT_PRIMARY,
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
            frame, text="", bg=CARD_BG, fg=WARNING_TEXT,
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

        dup_lbl = tk.Label(frame, text="", bg=CARD_BG, fg=ERROR_TEXT, font=(fn, 10))
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
            self._status_lbl.configure(text="Please enter a GitHub URL or owner/repo.", fg=ERROR_TEXT)
            return
        try:
            owner, repo = parse_github_input(raw)
        except ValueError as exc:
            self._status_lbl.configure(text=str(exc), fg=ERROR_TEXT)
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
            except FairyStartError as exc:
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
                    self._status_lbl.configure(text=msg[1], fg=ERROR_TEXT)
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
            self._status_lbl.configure(text=f"Failed to save config: {exc}", fg=ERROR_TEXT)
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
        on_confirm: Callable[[str, str, Optional[str], bool], None],
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
                bg=INPUT_BG, fg=TEXT_PRIMARY,
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
        fairy_backup_var = tk.BooleanVar(value=pkg.fairy_backup)

        _field("Branch", branch_var)
        _field("Start command", cmd_var, highlight_empty=True)
        _field("URL", url_var)

        # Auto-backup checkbox row
        cb_row = tk.Frame(frame, bg=CARD_BG)
        cb_row.pack(fill=tk.X, pady=3)
        tk.Label(cb_row, text="Auto-backup", bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=(fn, 10), width=14, anchor="e").pack(side=tk.LEFT)
        cb_inner = tk.Frame(cb_row, bg=CARD_BG)
        cb_inner.pack(side=tk.LEFT, padx=(8, 0))
        tk.Checkbutton(
            cb_inner, variable=fairy_backup_var,
            bg=CARD_BG, fg=TEXT_SECONDARY,
            activebackground=CARD_BG, activeforeground=TEXT_PRIMARY,
            selectcolor=CARD_BG,
        ).pack(side=tk.LEFT)
        tk.Label(
            cb_inner,
            text="Commit working-tree changes to fairy-backup branch every 5 min",
            bg=CARD_BG, fg=TEXT_TERTIARY, font=(fn, 9),
        ).pack(side=tk.LEFT, padx=(2, 0))

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
            command=lambda: self._save(branch_var, cmd_var, url_var, fairy_backup_var),
            bg=BLUE, fg=BTN_TEXT,
            hover_bg=BLUE_HOVER, hover_fg=BTN_TEXT,
            padx=16, pady=7, parent_bg=CARD_BG,
        )
        save_btn.pack(side=tk.LEFT)
        self._save_btn = save_btn

        def _check(*_):
            save_btn.configure(state=tk.NORMAL if cmd_var.get().strip() else tk.DISABLED)
        cmd_var.trace_add("write", _check)

    def _save(self, branch_var: tk.StringVar, cmd_var: tk.StringVar,
              url_var: tk.StringVar, fairy_backup_var: tk.BooleanVar) -> None:
        branch       = branch_var.get().strip() or "main"
        cmd          = cmd_var.get().strip()
        url          = url_var.get().strip() or None
        fairy_backup = fairy_backup_var.get()
        if not cmd:
            return
        self._top.destroy()
        self._on_confirm(branch, cmd, url, fairy_backup)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

_FAIRY_START_REPO = "ux-mark/fairy-start"


class FairyStartApp:
    _POLL_MS              = 100
    _HEALTH_INTERVAL      = 5.0
    _MONITOR_POLL         = 2.0
    _FAIRY_BACKUP_INTERVAL = 300.0
    _AUTH_RECHECK_MS      = 30_000
    _UPDATE_CHECK_DELAY_MS = 2_000

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

        self._auth_banner: Optional[tk.Frame] = None
        self._auth_banner_visible: bool = False
        self._auth_check_job: Optional[str] = None

        self._update_banner: Optional[tk.Frame] = None
        self._update_banner_visible: bool = False

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
        root.title("Fairy Start")
        root.resizable(True, True)
        root.minsize(460, 300)
        root.configure(bg=WINDOW_BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root = root

        # Transparent titlebar — the function calls root.update() internally
        # to ensure the NSWindow exists before configuring it.
        _macos_configure_titlebar(root)

        self._font_name = self._resolve_font()
        self._mono_font = self._resolve_mono_font()
        fn = self._font_name

        # ── Titlebar spacer (keeps content below traffic-light buttons) ─
        self._spacer = tk.Frame(root, bg=HEADER_BG, height=_TITLEBAR_HEIGHT)
        self._spacer.pack(fill=tk.X)
        self._spacer.pack_propagate(False)

        # ── Header bar ─────────────────────────────────────────────────
        self._header = tk.Frame(root, bg=HEADER_BG, height=52)
        self._header.pack(fill=tk.X)
        self._header.pack_propagate(False)
        header = self._header

        # App icon inline in header
        self._header_icon_lbl = None
        try:
            _hicon_path = pathlib.Path(__file__).parent / "AppIcon.iconset" / "icon_32x32@2x.png"
            if _hicon_path.exists():
                _raw_icon = tk.PhotoImage(file=str(_hicon_path))
                self._header_icon = _raw_icon.subsample(2)   # 64×64 → 32×32 logical px
                self._header_icon_lbl = tk.Label(
                    header, image=self._header_icon, bg=HEADER_BG,
                )
                self._header_icon_lbl.pack(side=tk.LEFT, padx=(16, 0))
        except Exception:
            pass

        self._header_title_lbl = tk.Label(
            header, text="Fairy Start",
            bg=HEADER_BG, fg=TEXT_PRIMARY,
            font=(fn, 16, "bold"),
        )
        self._header_title_lbl.pack(side=tk.LEFT, padx=(8, 0))

        # ── "+ Add Repo" keyline button (rightmost) ─────────────────
        add_btn = CanvasButton(
            header, text="+ Add Repo", font=(fn, 11),
            bg=HEADER_BG, fg=BLUE_TEXT,
            outline=BLUE, outline_width=1,
            hover_bg=BLUE, hover_fg=BTN_TEXT, hover_outline=BLUE,
            disabled_bg=HEADER_BG, disabled_fg=TEXT_TERTIARY,
            padx=12, pady=4,
            command=self._on_add_service, parent_bg=HEADER_BG,
        )
        add_btn.pack(side=tk.RIGHT, padx=(0, 16))
        self._add_btn = add_btn

        # ── Start All / Stop All button ──────────────────────────────
        global_btn = CanvasButton(
            header, text="Start All", icon="play",
            font=(fn, 11),
            bg=HEADER_BG, fg=BLUE_TEXT,
            outline=BLUE, outline_width=1,
            hover_bg=BLUE, hover_fg=BTN_TEXT, hover_outline=BLUE,
            disabled_bg=HEADER_BG, disabled_fg=TEXT_TERTIARY,
            padx=12, pady=4,
            command=self._on_global_action,
            parent_bg=HEADER_BG,
        )
        global_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._global_btn = global_btn

        if not self._config.packages:
            global_btn.configure(state=tk.DISABLED)

        # Header bottom border
        self._header_border = tk.Frame(root, bg=CARD_BORDER, height=1)
        self._header_border.pack(fill=tk.X)

        # ── Auth banner (hidden until needed) ──────────────────────────
        self._build_auth_banner()

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
        root.after(500, self._run_auth_check)   # 500ms lets the window render first
        self._build_update_banner()
        self._start_update_check()

        # Theme polling
        self._current_theme = _detect_system_theme()
        root.after(2000, self._check_theme)

        # Resizable card centering
        self._last_center_width = 0
        cards_outer.bind("<Configure>", self._on_cards_configure)

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
        def _pulse_empty(step: int = 0) -> None:
            if not frame.winfo_exists():
                return
            active = step % 3
            for i, dot_id in enumerate(_dot_ids):
                color = PULSE_BRIGHT if i == active else DOT_COLORS[PkgState.OFF]
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
            bg=BLUE, fg=BTN_TEXT,
            hover_bg=BLUE_HOVER, hover_fg=BTN_TEXT,
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
        _ICON_EXTRA = CanvasButton._ICON_W + CanvasButton._ICON_GAP
        _action_min_w = max(
            max(_abfont.measure(t) + _ICON_EXTRA
                for t in ("Start", "Stop", "Restart")),
            max(_abfont.measure(t)
                for t in ("Starting...", "Stopping...")),
        ) + 28
        action_btn = CanvasButton(
            row1, text="Start", icon="play",
            font=(fn, 10, "bold"),
            bg=BLUE, fg=BTN_TEXT,
            hover_bg=BLUE_HOVER, hover_fg=BTN_TEXT,
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

        backup_off_lbl = tk.Label(
            row2, text="backup off",
            bg=CARD_BG, fg=TEXT_TERTIARY,
            font=(fn, 9), anchor="w",
        )
        if not pkg.fairy_backup:
            backup_off_lbl.pack(side=tk.LEFT, padx=(6, 0))

        # ── Inline Edit / Remove links (always visible, right-aligned) ──
        _rlf  = tkfont.Font(family=fn, size=10)
        _rlfu = tkfont.Font(family=fn, size=10, underline=True)

        remove_link = tk.Label(
            row2, text="Remove",
            bg=CARD_BG, fg=REMOVE_LINK,
            font=_rlf, cursor="pointinghand",
        )
        remove_link.pack(side=tk.RIGHT)
        remove_link.bind("<Button-1>", lambda e, n=pkg.name: self._on_remove_service(n))
        remove_link.bind("<Enter>", lambda e, b=remove_link: b.configure(fg=REMOVE_LINK_HOVER, font=_rlfu))
        remove_link.bind("<Leave>", lambda e, b=remove_link: b.configure(fg=REMOVE_LINK, font=_rlf))

        edit_link = tk.Label(
            row2, text="Edit",
            bg=CARD_BG, fg=TEXT_TERTIARY,
            font=_rlf, cursor="pointinghand",
        )
        edit_link.pack(side=tk.RIGHT, padx=(0, 12))
        edit_link.bind("<Button-1>", lambda e, n=pkg.name: self._on_edit_service(n))
        edit_link.bind("<Enter>", lambda e, b=edit_link: b.configure(fg=TEXT_SECONDARY, font=_rlfu))
        edit_link.bind("<Leave>", lambda e, b=edit_link: b.configure(fg=TEXT_TERTIARY, font=_rlf))

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
        advisory_sep = tk.Frame(advisory_outer, bg=CARD_BORDER, height=1)
        advisory_sep.pack(fill=tk.X)

        advisory_inner = tk.Frame(advisory_outer, bg=ERROR_BG)
        advisory_inner.pack(fill=tk.X)

        left_bar = tk.Frame(advisory_inner, bg=RED, width=4)
        left_bar.pack(side=tk.LEFT, fill=tk.Y)

        advisory_lbl = tk.Label(
            advisory_inner, text="",
            bg=ERROR_BG, fg=ERROR_TEXT,
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
            bg=LOG_BG, fg=TEXT_SECONDARY,
            font=(self._mono_font, 9),
            wraplength=356, justify="left", anchor="w",
            padx=12, pady=8,
        )
        log_lbl.pack(fill=tk.X, padx=16, pady=(0, 8))

        # ── Bottom padding ────────────────────────────────────────────
        bottom_pad = tk.Frame(card, bg=CARD_BG, height=14)
        bottom_pad.pack(fill=tk.X)

        # ── Card hover: brighten border ─────────────────────────────────
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
            "outer":           outer,
            "card":            card,
            "dot_animator":    dot_animator,
            "action_btn":      action_btn,
            "name_lbl":        name_lbl,
            "url_lbl":         url_lbl,
            "backup_off_lbl":  backup_off_lbl,
            "advisory_outer": advisory_outer,
            "advisory_sep":  advisory_sep,
            "advisory_lbl":   advisory_lbl,
            "advisory_inner": advisory_inner,
            "left_bar":       left_bar,
            "log_toggle":     log_toggle,
            "log_frame":      log_frame,
            "log_lbl":        log_lbl,
            "accordion_open": accordion_open,
            "_row1":          row1,
            "_row2":          row2,
            "edit_link":      edit_link,
            "remove_link":    remove_link,
            "ctx_menu":       ctx_menu,
            "edit_adv_btn":   edit_adv_btn,
            "adv_action_row": adv_action_row,
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
                text="Start", icon="play", state=tk.NORMAL,
                bg=BLUE, fg=BTN_TEXT, hover_bg=BLUE_HOVER,
            )
        elif state == PkgState.STARTING:
            w["action_btn"].configure(
                text="Starting...", icon=None, state=tk.DISABLED,
            )
        elif state == PkgState.RUNNING:
            w["action_btn"].configure(
                text="Stop", icon="stop", state=tk.NORMAL,
                bg=STOP_BG, fg=BTN_TEXT, hover_bg=RED_HOVER,
            )
        elif state == PkgState.ERROR:
            w["action_btn"].configure(
                text="Restart", icon="play", state=tk.NORMAL,
                bg=BLUE, fg=BTN_TEXT, hover_bg=BLUE_HOVER,
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
            w["advisory_inner"].configure(bg=ERROR_BG)
            w["advisory_lbl"].configure(bg=ERROR_BG, fg=ERROR_TEXT)
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

    # ---- Theme switching -----------------------------------------------

    def _apply_theme(self, theme: str) -> None:
        """Switch all UI to the given theme ('dark' or 'light')."""
        self._current_theme = theme
        _apply_palette(_DARK if theme == "dark" else _LIGHT)

        # Root window + NSWindow titlebar
        self._root.configure(bg=WINDOW_BG)
        _macos_set_titlebar_bg(self._root, WINDOW_BG)

        # Header area
        self._spacer.configure(bg=HEADER_BG)
        self._header.configure(bg=HEADER_BG)
        self._header_title_lbl.configure(bg=HEADER_BG, fg=TEXT_PRIMARY)
        if self._header_icon_lbl:
            self._header_icon_lbl.configure(bg=HEADER_BG)

        # Add button
        self._add_btn.configure(
            outline_width=1, parent_bg=HEADER_BG,
            bg=HEADER_BG, fg=BLUE_TEXT,
            outline=BLUE, hover_bg=BLUE, hover_fg=BTN_TEXT, hover_outline=BLUE,
            disabled_bg=HEADER_BG, disabled_fg=TEXT_TERTIARY,
        )

        # Global button
        self._global_btn.configure(
            outline_width=1, parent_bg=HEADER_BG,
            disabled_bg=HEADER_BG, disabled_fg=TEXT_TERTIARY,
        )
        self._update_global_btn()

        # Header border
        self._header_border.configure(bg=CARD_BORDER)

        # Rebuild banners (simplest approach — few widgets)
        was_auth_visible = self._auth_banner_visible
        was_update_visible = self._update_banner_visible
        if self._auth_banner:
            self._auth_banner.pack_forget()
            self._auth_banner.destroy()
            self._auth_banner_visible = False
        if self._update_banner:
            self._update_banner.pack_forget()
            self._update_banner.destroy()
            self._update_banner_visible = False
        self._build_auth_banner()
        self._build_update_banner()
        if was_auth_visible:
            self._show_auth_banner()
        if was_update_visible:
            self._show_update_banner()

        # Cards outer
        self._cards_outer.configure(bg=WINDOW_BG)

        # Empty state
        if self._empty_frame and self._empty_frame.winfo_exists():
            self._empty_frame.destroy()
            self._empty_frame = None
            self._show_empty_state()

        # Per-card retheme
        for pkg_name, w in self._pkg_widgets.items():
            state = self._pkg_states.get(pkg_name, PkgState.OFF)
            self._retheme_card(pkg_name, w, state)

    def _retheme_card(self, pkg_name: str, w: dict, state: PkgState) -> None:
        """Reconfigure all widgets in a card with current palette colors."""
        fn = self._font_name

        w["outer"].configure(bg=WINDOW_BG)
        w["card"].configure(bg=CARD_BG, highlightbackground=CARD_BORDER,
                            highlightcolor=CARD_BORDER)
        w["_row1"].configure(bg=CARD_BG)
        w["_row2"].configure(bg=CARD_BG)
        w["bottom_pad"].configure(bg=CARD_BG)

        # Name label
        w["name_lbl"].configure(bg=CARD_BG, fg=TEXT_PRIMARY)

        # Dot animator
        w["dot_animator"].retheme(CARD_BG)

        # Action button — re-apply state-dependent colors + icons
        if state == PkgState.OFF:
            w["action_btn"].configure(
                icon="play",
                bg=BLUE, fg=BTN_TEXT, hover_bg=BLUE_HOVER,
                disabled_bg=DISABLED_BG, disabled_fg=DISABLED_TEXT,
                parent_bg=CARD_BG,
            )
        elif state == PkgState.STARTING:
            w["action_btn"].configure(
                icon=None,
                disabled_bg=DISABLED_BG, disabled_fg=DISABLED_TEXT,
                parent_bg=CARD_BG,
            )
        elif state == PkgState.RUNNING:
            w["action_btn"].configure(
                icon="stop",
                bg=STOP_BG, fg=BTN_TEXT, hover_bg=RED_HOVER,
                parent_bg=CARD_BG,
            )
        elif state == PkgState.ERROR:
            w["action_btn"].configure(
                icon="play",
                bg=BLUE, fg=BTN_TEXT, hover_bg=BLUE_HOVER,
                parent_bg=CARD_BG,
            )

        # URL + backup labels
        if w["url_lbl"]:
            w["url_lbl"].configure(bg=CARD_BG)
            # Only set fg if not currently a clickable link (blue)
            current_fg = str(w["url_lbl"].cget("fg"))
            if current_fg != BLUE and current_fg != str(BLUE):
                w["url_lbl"].configure(fg=TEXT_TERTIARY)
        w["backup_off_lbl"].configure(bg=CARD_BG, fg=TEXT_TERTIARY)

        # Indent spacer in row2
        for child in w["_row2"].winfo_children():
            if isinstance(child, tk.Frame):
                child.configure(bg=CARD_BG)
                break

        # Edit / Remove links
        w["edit_link"].configure(bg=CARD_BG, fg=TEXT_TERTIARY)
        w["remove_link"].configure(bg=CARD_BG, fg=REMOVE_LINK)

        # Context menu
        w["ctx_menu"].configure(bg=CARD_BG, fg=TEXT_PRIMARY,
                                activebackground=CARD_BORDER,
                                activeforeground=TEXT_PRIMARY)
        # Update "Remove" item foreground (index 2 = after edit + separator)
        try:
            w["ctx_menu"].entryconfigure(2, foreground=RED)
        except Exception:
            pass

        # Advisory panel
        w["advisory_outer"].configure(bg=CARD_BG)
        w["advisory_sep"].configure(bg=CARD_BORDER)
        w["adv_action_row"].configure(bg=CARD_BG)
        w["edit_adv_btn"].configure(bg=CARD_BG, fg=BLUE)
        w["log_toggle"].configure(bg=CARD_BG, fg=TEXT_SECONDARY)
        w["log_frame"].configure(bg=CARD_BG)
        w["log_lbl"].configure(bg=LOG_BG, fg=TEXT_SECONDARY)

        # Advisory inner: depends on current advisory state
        if state == PkgState.ERROR:
            w["advisory_inner"].configure(bg=ERROR_BG)
            w["advisory_lbl"].configure(bg=ERROR_BG, fg=ERROR_TEXT)
            w["left_bar"].configure(bg=RED)
        else:
            # Could be warning (amber) or hidden — check if visible
            w["advisory_inner"].configure(bg=WARNING_BG)
            w["advisory_lbl"].configure(bg=WARNING_BG, fg=WARNING_TEXT)
            w["left_bar"].configure(bg=AMBER)

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
                    w["advisory_inner"].configure(bg=WARNING_BG)
                    w["advisory_lbl"].configure(bg=WARNING_BG, fg=WARNING_TEXT)
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
            w["advisory_inner"].configure(bg=WARNING_BG)
            w["advisory_lbl"].configure(bg=WARNING_BG, fg=WARNING_TEXT)
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
            btn.configure(state=tk.DISABLED, text="Start All", icon="play")
            return
        if all(s == PkgState.RUNNING for s in states):
            btn.configure(state=tk.NORMAL, text="Stop All", icon="stop",
                          bg=HEADER_BG, fg=RED_TEXT,
                          outline=STOP_BG, hover_bg=STOP_BG,
                          hover_fg=BTN_TEXT, hover_outline=STOP_BG)
        else:
            btn.configure(state=tk.NORMAL, text="Start All", icon="play",
                          bg=HEADER_BG, fg=BLUE_TEXT,
                          outline=BLUE, hover_bg=BLUE,
                          hover_fg=BTN_TEXT, hover_outline=BLUE)

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
                    print(f"[Fairy Start] error handling {msg[0]!r} for {msg[1]!r}: {exc}",
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
        log_path = self._packages_dir / pkg_name / "fairy-start.log"
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

        def _on_confirm(branch: str, start_command: str, url: Optional[str], fairy_backup: bool) -> None:
            pkg.branch = branch
            pkg.start_command = start_command
            pkg.url = url
            pkg.fairy_backup = fairy_backup
            rewrite_config(self._config_path, self._config.packages_dir, self._config.packages)
            # Update url_lbl text if it exists
            w = self._pkg_widgets.get(pkg_name)
            if w and w.get("url_lbl") and url:
                _pm = re.search(r':(\d+)', url)
                new_text = f"localhost:{_pm.group(1)}" if _pm else url
                w["url_lbl"].configure(text=new_text)
            # Update backup_off_lbl visibility
            if w and w.get("backup_off_lbl"):
                if fairy_backup:
                    w["backup_off_lbl"].pack_forget()
                else:
                    w["backup_off_lbl"].pack(side=tk.LEFT, padx=(6, 0))

        EditServiceDialog(self._root, pkg, _on_confirm, self._font_name)

    # ---- Remove service ---------------------------------------------

    def _on_remove_service(self, pkg_name: str) -> None:
        pkg_dir = self._packages_dir / pkg_name
        msg = (
            f"Remove '{pkg_name}' from Fairy Start?"
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

    # ---- GitHub auth banner -----------------------------------------

    def _build_auth_banner(self) -> None:
        fn = self._font_name
        banner = tk.Frame(self._root, bg=AUTH_BANNER_BG, height=36)
        banner.pack_propagate(False)
        # Left amber accent bar
        tk.Frame(banner, bg=AMBER, width=3).pack(side=tk.LEFT, fill=tk.Y)
        # Content area
        content = tk.Frame(banner, bg=AUTH_BANNER_BG)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        # Warning icon + message
        tk.Label(
            content, text="⚠",
            bg=AUTH_BANNER_BG, fg=AUTH_BANNER_TEXT,
            font=(fn, 12),
        ).pack(side=tk.LEFT, pady=8)
        tk.Label(
            content,
            text=" GitHub not connected — some repos may be inaccessible.",
            bg=AUTH_BANNER_BG, fg=AUTH_BANNER_TEXT,
            font=(fn, 11),
        ).pack(side=tk.LEFT, pady=8)
        # Connect button
        connect_btn = CanvasButton(
            banner, text="Connect",
            font=(fn, 11),
            bg=AMBER, fg=AMBER_BTN_FG,
            hover_bg=AMBER_HOVER, hover_fg=AMBER_BTN_FG,
            padx=10, pady=3,
            command=self._on_connect_github,
            parent_bg=AUTH_BANNER_BG,
        )
        connect_btn.pack(side=tk.RIGHT, padx=(0, 8))
        # Dismiss button
        dismiss = tk.Label(
            banner, text="✕",
            bg=AUTH_BANNER_BG, fg=TEXT_SECONDARY,
            font=(fn, 12), cursor="pointinghand",
        )
        dismiss.pack(side=tk.RIGHT, padx=(0, 4))
        dismiss.bind("<Button-1>", lambda e: self._dismiss_auth_banner())
        dismiss.bind("<Enter>", lambda e: dismiss.configure(fg=TEXT_PRIMARY))
        dismiss.bind("<Leave>", lambda e: dismiss.configure(fg=TEXT_SECONDARY))

        self._auth_banner = banner

    def _show_auth_banner(self) -> None:
        if self._auth_banner_visible or self._auth_banner is None:
            return
        self._auth_banner.pack(fill=tk.X, before=self._cards_outer)
        self._auth_banner_visible = True

    def _hide_auth_banner(self) -> None:
        if not self._auth_banner_visible or self._auth_banner is None:
            return
        self._auth_banner.pack_forget()
        self._auth_banner_visible = False

    def _dismiss_auth_banner(self) -> None:
        self._hide_auth_banner()
        if self._auth_check_job is not None:
            self._root.after_cancel(self._auth_check_job)
            self._auth_check_job = None

    def _on_connect_github(self) -> None:
        script = ('tell application "Terminal"\n'
                  '    activate\n'
                  '    do script "gh auth login --web"\n'
                  'end tell')
        subprocess.Popen(["osascript", "-e", script])

    def _run_auth_check(self) -> None:
        self._auth_check_job = None
        def _check():
            status = gh_auth_status()
            if self._root.winfo_exists():
                self._root.after(0, lambda: self._apply_auth_status(status))
        threading.Thread(target=_check, daemon=True).start()

    def _apply_auth_status(self, status: str) -> None:
        if status == "gh_not_found":
            return   # gh not installed; different problem
        if status == "authenticated":
            self._hide_auth_banner()
        else:
            self._show_auth_banner()
        # Reschedule so banner reappears if token expires mid-session
        self._auth_check_job = self._root.after(self._AUTH_RECHECK_MS, self._run_auth_check)

    # ---- Update banner ----------------------------------------------

    def _build_update_banner(self) -> None:
        fn = self._font_name
        banner = tk.Frame(self._root, bg=UPDATE_BANNER_BG, height=36)
        banner.pack_propagate(False)
        # Left green accent bar
        tk.Frame(banner, bg=GREEN, width=3).pack(side=tk.LEFT, fill=tk.Y)
        # Content area
        content = tk.Frame(banner, bg=UPDATE_BANNER_BG)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        # Icon + message
        tk.Label(
            content, text="↑",
            bg=UPDATE_BANNER_BG, fg=UPDATE_BANNER_TEXT,
            font=(fn, 12),
        ).pack(side=tk.LEFT, pady=8)
        tk.Label(
            content,
            text=" An update is available for Fairy Start.",
            bg=UPDATE_BANNER_BG, fg=UPDATE_BANNER_TEXT,
            font=(fn, 11),
        ).pack(side=tk.LEFT, pady=8)
        # Update Now button
        update_btn = CanvasButton(
            banner, text="Update Now",
            font=(fn, 11),
            bg=GREEN, fg=GREEN_BTN_FG,
            hover_bg=GREEN_HOVER, hover_fg=GREEN_BTN_FG,
            padx=10, pady=3,
            command=self._on_update_now,
            parent_bg=UPDATE_BANNER_BG,
        )
        update_btn.pack(side=tk.RIGHT, padx=(0, 8))
        # Dismiss button
        dismiss = tk.Label(
            banner, text="✕",
            bg=UPDATE_BANNER_BG, fg=TEXT_SECONDARY,
            font=(fn, 12), cursor="pointinghand",
        )
        dismiss.pack(side=tk.RIGHT, padx=(0, 4))
        dismiss.bind("<Button-1>", lambda e: self._dismiss_update_banner())
        dismiss.bind("<Enter>", lambda e: dismiss.configure(fg=TEXT_PRIMARY))
        dismiss.bind("<Leave>", lambda e: dismiss.configure(fg=TEXT_SECONDARY))

        self._update_banner = banner

    def _show_update_banner(self) -> None:
        if self._update_banner_visible or self._update_banner is None:
            return
        self._update_banner.pack(fill=tk.X, before=self._cards_outer)
        self._update_banner_visible = True

    def _hide_update_banner(self) -> None:
        if not self._update_banner_visible or self._update_banner is None:
            return
        self._update_banner.pack_forget()
        self._update_banner_visible = False

    def _dismiss_update_banner(self) -> None:
        self._hide_update_banner()

    def _start_update_check(self) -> None:
        self._root.after(self._UPDATE_CHECK_DELAY_MS, self._run_update_check)

    def _run_update_check(self) -> None:
        def _check():
            try:
                script_dir = pathlib.Path(__file__).parent
                local = subprocess.run(
                    ["git", "-C", str(script_dir), "rev-parse", "HEAD"],
                    capture_output=True, timeout=5,
                )
                if local.returncode != 0:
                    return   # not a git repo (e.g. .app bundle)
                local_sha = local.stdout.decode().strip()

                req = urllib.request.Request(
                    f"https://api.github.com/repos/{_FAIRY_START_REPO}/commits/main",
                    headers={"Accept": "application/vnd.github.sha",
                             "User-Agent": "fairy-start"},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    remote_sha = resp.read().decode().strip()

                if remote_sha and remote_sha != local_sha:
                    if self._root.winfo_exists():
                        self._root.after(0, self._show_update_banner)
            except Exception:
                pass   # silently ignore: no internet, rate-limited, etc.
        threading.Thread(target=_check, daemon=True).start()

    def _on_update_now(self) -> None:
        if not tkinter.messagebox.askyesno(
            "Update Fairy Start",
            "Applying the update requires restarting Fairy Start.\n\nUpdate now?",
            icon="info",
        ):
            return
        self._hide_update_banner()

        def _pull():
            try:
                script_dir = pathlib.Path(__file__).parent
                result = subprocess.run(
                    ["git", "-C", str(script_dir), "pull", "--ff-only"],
                    capture_output=True, timeout=30,
                )
                if self._root.winfo_exists():
                    if result.returncode == 0:
                        self._root.after(0, self._on_update_success)
                    else:
                        err = result.stderr.decode(errors="replace").strip()
                        self._root.after(0, lambda: self._on_update_failed(err))
            except Exception as exc:
                if self._root.winfo_exists():
                    self._root.after(0, lambda: self._on_update_failed(str(exc)))
        threading.Thread(target=_pull, daemon=True).start()

    def _on_update_success(self) -> None:
        tkinter.messagebox.showinfo(
            "Update Applied",
            "Fairy Start has been updated.\n\nPlease restart the app to use the new version.",
        )

    def _on_update_failed(self, err: str) -> None:
        tkinter.messagebox.showerror(
            "Update Failed",
            f"git pull failed:\n\n{err}\n\nYou can update manually by running:\n  git pull",
        )

    # ---- Fairy backup -----------------------------------------------

    def _start_fairy_backup(self) -> None:
        threading.Thread(
            target=self._fairy_backup_loop,
            daemon=True,
        ).start()

    def _fairy_backup_loop(self) -> None:
        while not self._fairy_backup_stop.wait(self._FAIRY_BACKUP_INTERVAL):
            for pkg in list(self._config.packages):
                if not pkg.fairy_backup:
                    continue
                pkg_dir = self._packages_dir / pkg.name
                if pkg_dir.exists():
                    push = "ux-mark/" in pkg.repo
                    _fairy_backup_pkg(pkg_dir, push=push)

    # ---- Theme polling -----------------------------------------------

    def _check_theme(self) -> None:
        detected = _detect_system_theme()
        if detected != self._current_theme:
            self._apply_theme(detected)
        self._root.after(2000, self._check_theme)

    # ---- Resizable card centering ------------------------------------

    def _on_cards_configure(self, event: tk.Event) -> None:
        available = event.width
        if available == self._last_center_width:
            return
        self._last_center_width = available
        if available > CARD_MAX_WIDTH + 32:
            padx = (available - CARD_MAX_WIDTH) // 2
        else:
            padx = 16
        for w in self._pkg_widgets.values():
            w["outer"].pack_configure(padx=padx)
        if self._empty_frame and self._empty_frame.winfo_exists():
            self._empty_frame.pack_configure(padx=padx)

    # ---- Window close -----------------------------------------------

    def _on_close(self) -> None:
        if self._auth_check_job is not None:
            self._root.after_cancel(self._auth_check_job)
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
    _macos_set_app_name()   # must run before tk.Tk()
    config_path = pathlib.Path(__file__).parent / "config.toml"
    if not config_path.exists():
        config_path.write_text('[settings]\npackages_dir = "packages"\n')

    try:
        config = Config.load(config_path)
    except (KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"Error: invalid config.toml — {exc}", file=sys.stderr)
        sys.exit(1)

    app = FairyStartApp(config, config_path)
    app.run()


if __name__ == "__main__":
    main()
