#!/usr/bin/env python3
"""
Screen Translate for macOS — GUI version
----------------------------------------------------------------
A small always-on-top app window with buttons to:
  - Select a region and translate it once
  - Capture the full screen and translate it
  - Start/stop "Live" mode, which watches a chosen region and
    re-translates automatically whenever the text on screen changes

Any source/target language pair supported by both Google Translate and
Tesseract can be selected from the header dropdowns.

Run:
    python3 screen_translate_gui.py

Dependencies (install once):
    brew install tesseract tesseract-lang python-tk@3.14
    pip3 install pytesseract deep-translator pillow
"""

import subprocess
import sys
import tempfile
import time
import os
import re
import hashlib
import json
import threading
import traceback
from collections import OrderedDict, deque

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    print("tkinter is not available. Run: brew install python-tk@3.14")
    sys.exit(1)

try:
    import pytesseract
    from PIL import Image
    from deep_translator import GoogleTranslator
except ImportError:
    print("Missing dependencies. Run:")
    print("  brew install tesseract tesseract-lang")
    print("  pip3 install pytesseract deep-translator pillow")
    sys.exit(1)


# ---------- Core capture / OCR / translate helpers ----------

def _safe_unlink(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def capture_screenshot(full: bool) -> str:
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    cmd = ["screencapture", "-x", path] if full else ["screencapture", "-i", "-x", path]
    try:
        subprocess.run(cmd, check=True)
    except Exception:
        _safe_unlink(path)
        raise
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        _safe_unlink(path)
        return None
    return path


def capture_region(rect) -> str:
    x, y, w, h = rect
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        subprocess.run(["screencapture", "-x", f"-R{x},{y},{w},{h}", path], check=True)
    except Exception:
        _safe_unlink(path)
        raise
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        _safe_unlink(path)
        return None
    return path


# Common Tesseract junk / non-textual runes that appear on mid-render frames.
_OCR_JUNK_RE = re.compile(r"[\x0b\x0c|~^`_\\<>{}\[\]]+")


def clean_ocr_text(raw: str) -> str:
    """Strip common Tesseract artifacts and collapse whitespace."""
    if not raw:
        return ""
    cleaned = _OCR_JUNK_RE.sub(" ", raw)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()


def looks_like_real_text(text: str, min_len: int = 4, min_alpha_ratio: float = 0.55) -> bool:
    """Reject fragments that are too short or dominated by non-letters —
    a good filter against mid-render OCR garbage."""
    if not text or len(text) < min_len:
        return False
    letters = sum(1 for c in text if c.isalpha())
    non_space = sum(1 for c in text if not c.isspace())
    if non_space == 0:
        return False
    return (letters / non_space) >= min_alpha_ratio


_TESS_CONFIG = "--oem 1 --psm 6"


# ---------- Supported languages ----------
#
# (display name, Google Translate code, Tesseract language pack code).
# All Tesseract codes ship with Homebrew's `tesseract-lang` formula.
LANGUAGES = [
    ("Spanish",              "es",    "spa"),
    ("English",              "en",    "eng"),
    ("French",               "fr",    "fra"),
    ("German",               "de",    "deu"),
    ("Portuguese",           "pt",    "por"),
    ("Italian",              "it",    "ita"),
    ("Dutch",                "nl",    "nld"),
    ("Russian",              "ru",    "rus"),
    ("Chinese (Simplified)", "zh-CN", "chi_sim"),
    ("Japanese",             "ja",    "jpn"),
    ("Korean",               "ko",    "kor"),
    ("Arabic",               "ar",    "ara"),
]
LANG_DISPLAY_TO_GOOGLE = {name: g for name, g, _t in LANGUAGES}
LANG_DISPLAY_TO_TESS   = {name: t for name, _g, t in LANGUAGES}
LANG_GOOGLE_TO_DISPLAY = {g: name for name, g, _t in LANGUAGES}


def ocr_image_path(image_path: str, lang: str = "spa") -> str:
    # psm 6 = "Assume a single uniform block of text". Faster than the default
    # auto page-segmentation pass and a good fit for a small watched region.
    return pytesseract.image_to_string(
        Image.open(image_path), lang=lang, config=_TESS_CONFIG
    ).strip()


def ocr_image(img, lang: str = "spa") -> str:
    """OCR an already-decoded PIL Image. Skips a second open()/decode when
    the caller has already loaded the image (as the live-mode worker does
    to compute the change-detection hash). Pass a compound tag like
    "spa+eng" to let Tesseract score both language models per word — useful
    when the watched region contains mixed languages you want to filter down."""
    return pytesseract.image_to_string(img, lang=lang, config=_TESS_CONFIG).strip()


# ---------- Language detection ----------
#
# Two-tier: use `langdetect` when installed (accurate on 15+ char lines), and
# fall back to a stopword/diacritic heuristic otherwise so the feature works
# out of the box. Install for best accuracy:  pip3 install langdetect
try:
    from langdetect import detect_langs, DetectorFactory  # type: ignore
    DetectorFactory.seed = 0  # deterministic classification
    _HAS_LANGDETECT = True
except Exception:
    _HAS_LANGDETECT = False


# ---------- Optional global-hotkey backend ----------
#
# Uses `pynput` when installed to register a system-wide hotkey (default
# ⌘⇧T) that triggers "Select region" without focusing the window. macOS
# will prompt for Accessibility permission the first time.
try:
    from pynput import keyboard as _pynput_keyboard  # type: ignore
    _HAS_PYNPUT = True
except Exception:
    _HAS_PYNPUT = False

GLOBAL_HOTKEY = "<cmd>+<shift>+t"
GLOBAL_HOTKEY_DISPLAY = "⌘⇧T"


# ---------- History persistence ----------
_HISTORY_DIR = os.path.expanduser("~/Library/Application Support/ScreenTranslate")
_HISTORY_PATH = os.path.join(_HISTORY_DIR, "history.json")
_HISTORY_MAX = 20


_SPANISH_STOPWORDS = frozenset({
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al",
    "y", "o", "u", "que", "en", "por", "para", "con", "sin", "es", "son",
    "se", "no", "sí", "si", "su", "sus", "le", "les", "me", "te", "nos",
    "lo", "yo", "tú", "él", "ella", "nosotros", "vosotros", "ellos", "ellas",
    "esto", "esta", "este", "eso", "esa", "ese", "aquí", "allí", "más", "muy",
    "pero", "como", "cuando", "donde", "porque", "también", "aunque", "hasta",
    "desde", "sobre", "entre", "hacia", "hay", "está", "están", "ser", "estar",
    "tener", "hacer", "todo", "todos", "toda", "todas", "otro", "otra", "otros",
    "otras", "mismo", "misma", "cada", "algún", "alguna", "ningún", "ninguna",
    "cual", "cuál", "quién", "qué", "eso", "ya", "aun", "aún",
})
_ENGLISH_STOPWORDS = frozenset({
    "the", "and", "or", "but", "of", "to", "in", "for", "with", "on", "at",
    "by", "from", "as", "is", "was", "were", "are", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "this", "that", "these", "those",
    "there", "their", "they", "them", "then", "than", "when", "where",
    "which", "who", "what", "why", "how", "not", "yes", "if", "so",
    "about", "into", "over", "under", "after", "before", "some", "any",
    "all", "each", "very", "just", "you", "your", "we", "our", "my", "his",
    "her", "him", "its", "it",
})
_SPANISH_CHARS = frozenset("ñÑáéíóúüÁÉÍÓÚÜ¿¡")
_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+")


def _looks_spanish_heuristic(text: str) -> bool:
    """Cheap offline classifier. Returns True when `text` looks more Spanish
    than English. Used for very short lines (where langdetect is unreliable)
    and as the fallback when langdetect isn't installed."""
    if not text:
        return False
    has_es_char = any(c in _SPANISH_CHARS for c in text)
    words = _WORD_RE.findall(text.lower())
    if not words:
        return False
    es_hits = sum(1 for w in words if w in _SPANISH_STOPWORDS)
    en_hits = sum(1 for w in words if w in _ENGLISH_STOPWORDS)
    if en_hits > es_hits and not has_es_char:
        return False
    if es_hits > en_hits:
        return True
    if has_es_char:
        return True
    return False


def _line_is_language(line: str, lang_code: str,
                      min_langdetect_chars: int = 15,
                      min_prob: float = 0.6) -> bool:
    """Return True if `line` looks like `lang_code` (Google code, e.g. "es").

    Uses langdetect when available; falls back to the Spanish heuristic
    when the target is Spanish. For any other target without langdetect
    installed, we can't classify offline — return True so we don't drop
    everything."""
    stripped = line.strip()
    if not stripped:
        return False
    # langdetect returns 2-letter ISO codes ("es", "en", "zh-cn", ...).
    ld_code = lang_code.lower()
    if not _HAS_LANGDETECT or len(stripped) < min_langdetect_chars:
        if lang_code == "es":
            return _looks_spanish_heuristic(stripped)
        return True
    try:
        langs = detect_langs(stripped)
    except Exception:
        return _looks_spanish_heuristic(stripped) if lang_code == "es" else True
    if not langs:
        return _looks_spanish_heuristic(stripped) if lang_code == "es" else True
    best = langs[0]
    if best.lang == ld_code and best.prob >= min_prob:
        return True
    # Special case: langdetect often splits Spanish 50/50 with pt/it. Trust
    # the strong Spanish heuristic when it disagrees.
    if lang_code == "es" and best.lang != "en" and _looks_spanish_heuristic(stripped):
        return True
    return False


def filter_to_language(text: str, lang_code: str) -> str:
    """Return only the lines of `text` classified as `lang_code`."""
    if not text:
        return ""
    kept = [line for line in text.splitlines() if _line_is_language(line, lang_code)]
    return "\n".join(l.strip() for l in kept).strip()


def perceptual_image_hash(img) -> bytes:
    """Downscale to 16x16 grayscale and hash. Tolerates subpixel/anti-alias
    jitter that would trip a raw-bytes hash and force an unnecessary OCR."""
    thumb = img.convert("L").resize((16, 16))
    return hashlib.blake2b(thumb.tobytes(), digest_size=16).digest()


# Reuse translator instances across calls, keyed by (source, target). Each
# GoogleTranslator holds a `requests.Session` internally, so reusing avoids
# the per-call TCP/TLS handshake — a big win when polling in live mode.
_TRANSLATORS: dict = {}
_TRANSLATOR_LOCK = threading.Lock()


def _get_translator(source: str, target: str) -> GoogleTranslator:
    key = (source, target)
    t = _TRANSLATORS.get(key)
    if t is None:
        t = GoogleTranslator(source=source, target=target)
        _TRANSLATORS[key] = t
    return t

# Small LRU cache. Live mode very often re-sees the same phrase (subtitles,
# menus, dialog boxes), and a cache hit is essentially free.
class _LRU:
    def __init__(self, maxsize=512):
        self.maxsize = maxsize
        self._d = OrderedDict()
        self._lock = threading.Lock()
    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
            return None
    def set(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)


_TRANSLATION_CACHE = _LRU(maxsize=512)


def translate_text(text: str, source: str, target: str) -> str:
    if not text:
        return ""
    key = (source, target, text)
    cached = _TRANSLATION_CACHE.get(key)
    if cached is not None:
        return cached
    translator = _get_translator(source, target)
    with _TRANSLATOR_LOCK:
        result = translator.translate(text)
    if result:
        _TRANSLATION_CACHE.set(key, result)
    return result


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def copy_to_clipboard(text: str):
    subprocess.run("pbcopy", text=True, input=text)


def notify(title: str, message: str):
    short = message if len(message) < 200 else message[:197] + "..."
    safe = short.replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'])


def select_region_interactive(parent):
    """Transparent overlay for dragging a rectangle, sized to cover the
    screen manually (NOT via -fullscreen, which triggers macOS's native
    fullscreen mode and switches you to a new Space/desktop).
    Returns (x, y, w, h) in screen point coordinates, or None if cancelled."""
    coords = {}
    overlay = tk.Toplevel(parent)

    screen_w = overlay.winfo_screenwidth()
    screen_h = overlay.winfo_screenheight()
    overlay.geometry(f"{screen_w}x{screen_h}+0+0")
    overlay.overrideredirect(True)   # borderless, no native fullscreen transition
    overlay.attributes("-alpha", 0.32)
    overlay.attributes("-topmost", True)
    overlay.configure(bg=THEME["bg_deep"])
    overlay.config(cursor="crosshair")
    overlay.focus_force()
    overlay.grab_set()

    canvas = tk.Canvas(overlay, bg=THEME["bg_deep"], highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    start = {}
    rect_id = {"id": None}

    def on_press(event):
        start["x"], start["y"] = event.x_root, event.y_root
        rect_id["id"] = canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                  outline=THEME["accent_glow"], width=2)

    def on_drag(event):
        if rect_id["id"] is not None:
            x0 = start["x"] - overlay.winfo_rootx()
            y0 = start["y"] - overlay.winfo_rooty()
            canvas.coords(rect_id["id"], x0, y0, event.x, event.y)

    def on_release(event):
        x1, y1 = start["x"], start["y"]
        x2, y2 = event.x_root, event.y_root
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        if w > 5 and h > 5:
            coords["rect"] = (int(x), int(y), int(w), int(h))
        overlay.destroy()

    def on_escape(event):
        overlay.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    overlay.bind("<Escape>", on_escape)

    parent.wait_window(overlay)
    return coords.get("rect")


# ---------- Theme: Arctic Frost (light, open, welcoming) ----------

# ---------- Theme: Nightshift Dashboard (dark navy + cyan) ----------
# Palette inspired by a modern fintech-dashboard look — deep navy surface,
# elevated cards with a subtle rim, and a single bright cyan accent used
# sparingly for the primary CTA + focused state.
THEME = {
    "bg":              "#0a1424",  # Deep navy — window
    "bg_deep":         "#050b16",  # Slightly darker for overlays
    "surface":         "#111d33",  # Card body
    "surface_2":       "#1a2846",  # Elevated / hover
    "surface_press":   "#0d1626",  # Pressed depression
    "border":          "#1e2c4a",  # Subtle card rim
    "border_hi":       "#2b3d63",  # Focused border
    "highlight":       "#1c3a6d",  # Text selection

    "accent":          "#3ba0ff",  # Bright cyan-blue CTA
    "accent_glow":     "#66c0ff",  # Hover
    "accent_dark":     "#2a7bd0",  # Pressed
    "accent_soft":     "#5f83b8",  # Muted steel for secondary

    "text":            "#ffffff",  # Bright white — headings, values
    "text_body":       "#c9d3e2",  # Body text
    "text_muted":      "#7a8598",  # Labels
    "text_dim":        "#4d5670",  # Section eyebrows / very dim

    "success":         "#4ade80",
    "warning":         "#f5b567",
    "error":           "#ff6b6b",

    # Font stack tried in order — prefers macOS system fonts.
    "font_stack": ("SF Pro Text", "Helvetica Neue", "Helvetica",
                   "DejaVu Sans", "Arial"),
    "size_body":       12,
    "size_body_lg":    13,
    "size_eyebrow":    10,
    "size_title":      24,
    "size_subtitle":   12,
    "size_status":     11,
    "size_value":      15,

    "radius_card":     18,
    "radius_button":   12,
    "radius_pill":     999,
    "radius_check":     6,
}


def _resolve_font_family(root) -> str:
    """Return the first font family in THEME['font_stack'] that Tk knows about."""
    try:
        from tkinter import font as tkfont
        families = set(tkfont.families(root))
        for name in THEME["font_stack"]:
            if name in families:
                return name
    except Exception:
        pass
    return "Helvetica"


# ---------- Custom rounded widgets (canvas-based) ----------
#
# Tkinter has no border-radius, so pill buttons and glass cards are drawn on
# a tk.Canvas: a smooth-polygon rounded rectangle for the shape, and either a
# text item (buttons) or an embedded widget window (cards) placed on top.


def _rounded_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    """Draw a rounded rectangle on `canvas`. Uses a smooth polygon: each
    corner has a single control point so the Bézier smoothing produces a
    quarter-circle-ish curve; the edges have duplicated points to stay
    straight."""
    r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [
        x1 + r, y1,  x2 - r, y1,  x2 - r, y1,
        x2, y1,      x2, y1 + r,  x2, y1 + r,
        x2, y2 - r,  x2, y2 - r,  x2, y2,
        x2 - r, y2,  x1 + r, y2,  x1 + r, y2,
        x1, y2,      x1, y2 - r,  x1, y2 - r,
        x1, y1 + r,  x1, y1 + r,  x1, y1,
    ]
    return canvas.create_polygon(*points, smooth=True, **kwargs)


class RoundedButton(tk.Canvas):
    """Flat pill button with true rounded corners and hover/press states."""

    def __init__(self, parent, text, command=None, *,
                 bg, fg, hover_bg=None, active_bg=None,
                 border=None, focus_border=None,
                 font=None, radius=12, padx=18, pady=10,
                 min_width=0, **kw):
        parent_bg = self._resolve_parent_bg(parent)
        super().__init__(parent, bg=parent_bg,
                         highlightthickness=0, bd=0, **kw)
        self._bg = bg
        self._hover_bg = hover_bg or bg
        self._active_bg = active_bg or hover_bg or bg
        self._border = border
        self._focus_border = focus_border
        self._fg = fg
        self._font = font
        self._radius = radius
        self._command = command
        self._pressed = False
        self._enabled = True

        tmp = self.create_text(0, 0, text=text, font=font, anchor="nw")
        bx = self.bbox(tmp) or (0, 0, 0, 0)
        self.delete(tmp)
        tw, th = bx[2] - bx[0], bx[3] - bx[1]
        w = max(int(tw) + 2 * padx, int(min_width))
        h = int(th) + 2 * pady
        # NOTE: use dedicated attribute names — `self._w` is Tkinter's internal
        # Tcl widget pathname; clobbering it wedges every subsequent tk.call.
        self._btn_w, self._btn_h = w, h
        super().configure(width=w, height=h)

        self._shape = _rounded_rect(
            self, 1, 1, w - 1, h - 1, radius,
            fill=bg, outline=(border or bg), width=1,
        )
        self._label = self.create_text(
            w // 2, h // 2, text=text, fill=fg, font=font,
        )

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.config(cursor="hand2")

    @staticmethod
    def _resolve_parent_bg(parent):
        try:
            return parent.cget("bg")
        except tk.TclError:
            return THEME["bg"]

    def _paint(self, fill):
        self.itemconfig(self._shape, fill=fill)

    def _on_enter(self, _e):
        if not self._enabled:
            return
        self._paint(self._active_bg if self._pressed else self._hover_bg)

    def _on_leave(self, _e):
        self._pressed = False
        if self._enabled:
            self._paint(self._bg)

    def _on_press(self, _e):
        if not self._enabled:
            return
        self._pressed = True
        self._paint(self._active_bg)

    def _on_release(self, e):
        if not self._enabled:
            return
        was = self._pressed
        self._pressed = False
        inside = 0 <= e.x <= self._btn_w and 0 <= e.y <= self._btn_h
        self._paint(self._hover_bg if inside else self._bg)
        if was and inside and self._command is not None:
            self._command()

    def config(self, **kw):
        # Accept .config(text=...) so callers written for tk.Button still work.
        if "text" in kw:
            self.itemconfig(self._label, text=kw.pop("text"))
        if "state" in kw:
            state = kw.pop("state")
            self._enabled = (state != "disabled")
            if not self._enabled:
                self._paint(self._bg)
        if kw:
            super().configure(**kw)

    configure = config


class RoundedDropdown(tk.Canvas):
    """Pill-shaped dropdown. Renders the current value of a StringVar plus a
    chevron; clicking pops up a tk.Menu with `options`. Themed to match
    RoundedButton so it slots into the same rows."""

    def __init__(self, parent, variable, options, *,
                 bg, fg, hover_bg=None, active_bg=None,
                 border=None, menu_bg=None, menu_fg=None,
                 menu_active_bg=None, menu_active_fg=None,
                 font=None, radius=12, padx=16, pady=9,
                 min_width=0, on_change=None, **kw):
        parent_bg = RoundedButton._resolve_parent_bg(parent)
        super().__init__(parent, bg=parent_bg,
                         highlightthickness=0, bd=0, **kw)
        self._var = variable
        self._option_values = list(options)
        self._on_change = on_change
        self._bg = bg
        self._hover_bg = hover_bg or bg
        self._active_bg = active_bg or hover_bg or bg
        self._menu_bg = menu_bg or bg
        self._menu_fg = menu_fg or fg
        self._menu_active_bg = menu_active_bg or (hover_bg or bg)
        self._menu_active_fg = menu_active_fg or fg
        self._font = font
        self._enabled = True

        # Size for the widest option so the pill doesn't jump when the value
        # changes. Chevron gets a fixed 14px slot.
        widest = max((self._measure(o) for o in self._option_values), default=(0, 0))
        tw, th = widest
        chevron_w = 14
        gap = 8
        w = max(int(tw) + chevron_w + gap + 2 * padx, int(min_width))
        h = int(th) + 2 * pady
        self._btn_w, self._btn_h = w, h
        super().configure(width=w, height=h)

        self._shape = _rounded_rect(
            self, 1, 1, w - 1, h - 1, radius,
            fill=bg, outline=(border or bg), width=1,
        )
        label_x = padx
        self._label = self.create_text(
            label_x, h // 2, text=str(self._var.get()),
            fill=fg, font=font, anchor="w",
        )
        # Down chevron drawn as a small V.
        cx = w - padx - chevron_w // 2
        cy = h // 2
        self._chevron = self.create_line(
            cx - 4, cy - 2,  cx, cy + 3,  cx + 4, cy - 2,
            fill=fg, width=2, capstyle="round", joinstyle="round",
        )

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._open_menu)
        self.config(cursor="hand2")

        # Keep label in sync when the variable is written externally.
        self._var.trace_add("write", lambda *_a: self._render())

    def _measure(self, text: str):
        tmp = self.create_text(0, 0, text=text, font=self._font, anchor="nw")
        bx = self.bbox(tmp) or (0, 0, 0, 0)
        self.delete(tmp)
        return bx[2] - bx[0], bx[3] - bx[1]

    def _paint(self, fill):
        self.itemconfig(self._shape, fill=fill)

    def _on_enter(self, _e):
        if self._enabled:
            self._paint(self._hover_bg)

    def _on_leave(self, _e):
        if self._enabled:
            self._paint(self._bg)

    def _on_press(self, _e):
        if self._enabled:
            self._paint(self._active_bg)

    def _open_menu(self, _e):
        if not self._enabled:
            return
        self._paint(self._hover_bg)
        menu = tk.Menu(
            self, tearoff=0,
            bg=self._menu_bg, fg=self._menu_fg,
            activebackground=self._menu_active_bg,
            activeforeground=self._menu_active_fg,
            bd=0, relief="flat",
            font=self._font,
        )
        for opt in self._option_values:
            menu.add_command(label=opt, command=lambda o=opt: self._select(o))
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self._btn_h
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _select(self, value):
        if value == self._var.get():
            return
        self._var.set(value)
        if self._on_change is not None:
            self._on_change(value)

    def _render(self):
        self.itemconfig(self._label, text=str(self._var.get()))

    def config(self, **kw):
        if "state" in kw:
            state = kw.pop("state")
            self._enabled = (state != "disabled")
            self._paint(self._bg)
        if kw:
            super().configure(**kw)

    configure = config


class RoundedFrame(tk.Frame):
    """A frame with a rounded-corner filled background. Children pack into
    `.body`. The body drives the frame's requested size; the rounded fill is
    drawn on a tk.Canvas that is placed as a background layer behind it."""

    def __init__(self, parent, *, fill, border=None,
                 radius=16, padx=16, pady=14, **kw):
        parent_bg = RoundedButton._resolve_parent_bg(parent)
        super().__init__(parent, bg=parent_bg, **kw)
        self._fill = fill
        self._border = border
        self._radius = radius
        pad_x = max(padx, radius // 2 + 4)
        pad_y = max(pady, radius // 2 + 4)

        # Canvas is a background layer — it MUST be placed (not packed), so it
        # doesn't add to the frame's requested size. It only paints the shape.
        self._canvas = tk.Canvas(self, bg=parent_bg,
                                 highlightthickness=0, bd=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)

        # Body drives the frame's requested size via pack. Children go here.
        # It renders on top of the placed canvas because it's created after.
        self.body = tk.Frame(self, bg=fill)
        self.body.pack(fill="both", expand=True, padx=pad_x, pady=pad_y)

        # Bind on the canvas, not the frame — the canvas is what actually
        # resizes (via place with relwidth/relheight), and by the time its
        # <Configure> fires its winfo_width/height are up to date. Binding on
        # the frame fires earlier and reads stale canvas dimensions, so the
        # rounded background lags behind resizes.
        self._canvas.bind("<Configure>", lambda _e: self._redraw())

    def _redraw(self):
        c = self._canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 4 or h < 4:
            return
        _rounded_rect(c, 1, 1, w - 1, h - 1, self._radius,
                      fill=self._fill,
                      outline=self._border or self._fill, width=1)


class RoundedCheckbox(tk.Canvas):
    """Modern rounded-square checkbox with label. Toggles a BooleanVar and
    stays in sync with it (via a variable trace) so external `.set()` calls
    also update the visual state."""

    def __init__(self, parent, text, variable, *,
                 fg, muted_fg=None, accent, box_bg, box_border, check_fg,
                 font=None, radius=6, box_size=18, gap=12, **kw):
        parent_bg = RoundedButton._resolve_parent_bg(parent)
        super().__init__(parent, bg=parent_bg,
                         highlightthickness=0, bd=0, **kw)
        self._var = variable
        self._accent = accent
        self._box_bg = box_bg
        self._box_border = box_border
        self._check_fg = check_fg

        tmp = self.create_text(0, 0, text=text, font=font, anchor="nw")
        bx = self.bbox(tmp) or (0, 0, 0, 0)
        self.delete(tmp)
        tw, th = bx[2] - bx[0], bx[3] - bx[1]
        pad = 3
        h = max(box_size, int(th)) + 2 * pad
        w = box_size + gap + int(tw) + 2 * pad
        super().configure(width=w, height=h)

        by = (h - box_size) // 2
        self._box = _rounded_rect(
            self, pad, by, pad + box_size, by + box_size, radius,
            fill=box_bg, outline=box_border, width=1,
        )
        # Checkmark polyline: three points, thick rounded, initially hidden.
        cx = pad
        self._check = self.create_line(
            cx + 4,           by + box_size // 2 + 1,
            cx + box_size // 2 - 1, by + box_size - 5,
            cx + box_size - 3, by + 4,
            fill=check_fg, width=2, state="hidden",
            capstyle="round", joinstyle="round",
        )
        self._label_item = self.create_text(
            pad + box_size + gap, h // 2,
            text=text, font=font, fill=fg, anchor="w",
        )

        self.bind("<Button-1>", self._toggle)
        self.bind("<Key-space>", self._toggle)
        self.config(cursor="hand2")
        self._trace_id = self._var.trace_add("write", lambda *_a: self._render())
        self._render()

    def _toggle(self, _e=None):
        try:
            self._var.set(not bool(self._var.get()))
        except tk.TclError:
            pass

    def _render(self):
        checked = False
        try:
            checked = bool(self._var.get())
        except tk.TclError:
            pass
        if checked:
            self.itemconfig(self._box, fill=self._accent, outline=self._accent)
            self.itemconfig(self._check, state="normal")
        else:
            self.itemconfig(self._box, fill=self._box_bg, outline=self._box_border)
            self.itemconfig(self._check, state="hidden")

    def set_text(self, text: str):
        self.itemconfig(self._label_item, text=text)


class RoundedStepper(tk.Frame):
    """[-] value [+] number input. Modern replacement for ttk.Spinbox on
    dark themes. Reads / writes a DoubleVar so existing bindings still work."""

    def __init__(self, parent, variable, *, from_, to, increment,
                 fmt="{:.1f}", unit="",
                 bg, border, accent, text, muted,
                 font=None, font_value=None, **kw):
        parent_bg = RoundedButton._resolve_parent_bg(parent)
        super().__init__(parent, bg=parent_bg, **kw)
        self._var = variable
        self._from, self._to, self._step = from_, to, increment
        self._fmt, self._unit = fmt, unit
        self._text = text

        def _step(delta):
            def go():
                try:
                    v = float(self._var.get())
                except tk.TclError:
                    v = self._from
                v = max(self._from, min(self._to, v + delta * self._step))
                self._var.set(round(v, 4))
            return go

        btn_kw = dict(
            bg=bg, fg=text, hover_bg=RoundedButton._resolve_parent_bg(parent),
            active_bg=bg, border=border,
            font=font, radius=10, padx=10, pady=6,
        )
        # Override hover for the steppers so they lift toward accent-tinted
        # elevated surface, not to the parent bg.
        btn_kw["hover_bg"] = accent
        btn_kw["active_bg"] = bg

        minus = RoundedButton(self, "−", command=_step(-1), **btn_kw)
        minus.pack(side="left")

        self._label_var = tk.StringVar(value=self._formatted())
        tk.Label(self, textvariable=self._label_var,
                 bg=parent_bg, fg=text,
                 font=font_value or font,
                 width=6, anchor="center").pack(side="left", padx=10)

        plus = RoundedButton(self, "+", command=_step(+1), **btn_kw)
        plus.pack(side="left")

        # Keep the label in sync when the variable changes externally.
        self._var.trace_add("write", lambda *_a: self._label_var.set(self._formatted()))

    def _formatted(self):
        try:
            v = float(self._var.get())
        except tk.TclError:
            v = self._from
        return self._fmt.format(v) + self._unit


# ---------- Main app ----------

class ScreenTranslateApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen Translate")
        self.root.geometry("640x900")
        self.root.minsize(500, 640)
        self._font_family = _resolve_font_family(root)
        self._apply_theme_globals()

        # Language pair (display names — see LANGUAGES for supported set).
        self.source_lang_var = tk.StringVar(value="Spanish")
        self.target_lang_var = tk.StringVar(value="English")
        self.source_lang_var.trace_add("write", lambda *_a: self._on_language_change())
        self.target_lang_var.trace_add("write", lambda *_a: self._on_language_change())

        self.live_running = False
        self.live_rect = None
        self.last_live_text = None      # last text we actually translated
        self._pending_text = None       # candidate awaiting stability
        self._pending_since = 0.0       # monotonic timestamp pending text first appeared
        self._last_image_hash = None    # cheap short-circuit: skip OCR if pixels unchanged
        self._backoff_until = 0.0       # unix time until which translation is paused
        self._backoff_seconds = 0.0     # current backoff window (grows exponentially)
        self._BACKOFF_MIN = 2.0
        self._BACKOFF_MAX = 60.0
        # Minimum time a candidate string must persist unchanged before we
        # translate it. This is what stops us from translating half-typed
        # sentences: any keystroke changes the OCR text and restarts the clock.
        self._STABILITY_SECS = 0.6

        # Background worker for live mode. Tk itself is not thread-safe, so the
        # worker never touches widgets directly — it marshals updates back via
        # root.after(0, ...).
        self._live_thread = None
        self._live_stop = threading.Event()

        # History (most-recent first).
        self.history: deque = deque(maxlen=_HISTORY_MAX)
        self._history_window = None
        self._history_list_frame = None
        self._load_history()

        # Global hotkey listener (optional).
        self._hotkey_listener = None

        self._build_ui()
        self._start_hotkey_listener()

    # ---- theme plumbing ----

    def _apply_theme_globals(self):
        """Set global option defaults for the dashboard-style dark palette."""
        r = self.root
        r.configure(bg=THEME["bg"])

        fam = self._font_family
        self._font_body     = (fam, THEME["size_body"])
        self._font_body_lg  = (fam, THEME["size_body_lg"])
        self._font_button   = (fam, THEME["size_body"], "bold")
        self._font_eyebrow  = (fam, THEME["size_eyebrow"], "bold")
        self._font_title    = (fam, THEME["size_title"], "bold")
        self._font_subtitle = (fam, THEME["size_subtitle"])
        self._font_status   = (fam, THEME["size_status"])
        self._font_text_box = (fam, THEME["size_body_lg"])
        self._font_value    = (fam, THEME["size_value"], "bold")

        opts = {
            "*Frame.background":              THEME["bg"],
            "*Label.background":              THEME["bg"],
            "*Label.foreground":              THEME["text_body"],
            "*Label.font":                    self._font_body,
            "*Text.background":               THEME["surface"],
            "*Text.foreground":               THEME["text"],
            "*Text.insertBackground":         THEME["accent"],
            "*Text.selectBackground":         THEME["highlight"],
            "*Text.selectForeground":         THEME["text"],
            "*Text.font":                     self._font_text_box,
            "*Text.relief":                   "flat",
            "*Text.borderWidth":              0,
            "*Text.highlightThickness":       0,
            "*Text.padX":                     16,
            "*Text.padY":                     14,
            "*Text.spacing1":                 4,
            "*Text.spacing3":                 6,
        }
        for k, v in opts.items():
            r.option_add(k, v)

        # No ttk styling needed — the checkbox and stepper are now custom
        # canvas widgets, so we no longer rely on ttk theming.

    def _make_button(self, parent, text, command, primary=False, size="md"):
        """Rounded pill button (canvas-drawn). primary=True is the cyan CTA."""
        if primary:
            bg = THEME["accent"]
            hover_bg = THEME["accent_glow"]
            active_bg = THEME["accent_dark"]
            fg = THEME["bg_deep"]      # dark text on bright pill
            border = THEME["accent"]
        else:
            bg = THEME["surface"]
            hover_bg = THEME["surface_2"]
            active_bg = THEME["surface_press"]
            fg = THEME["text"]
            border = THEME["border"]
        if size == "lg":
            padx, pady = 24, 13
        else:
            padx, pady = 18, 11
        return RoundedButton(
            parent, text=text, command=command,
            bg=bg, fg=fg,
            hover_bg=hover_bg, active_bg=active_bg,
            border=border,
            font=self._font_button,
            radius=THEME["radius_button"],
            padx=padx, pady=pady,
        )

    def _make_eyebrow(self, parent, text):
        """Small uppercase muted section eyebrow (dashboard-style label)."""
        parent_bg = RoundedButton._resolve_parent_bg(parent)
        return tk.Label(
            parent, text=text.upper(),
            font=self._font_eyebrow,
            fg=THEME["text_muted"], bg=parent_bg,
            anchor="w",
        )

    def _make_divider(self, parent):
        return tk.Frame(parent, height=1, bg=THEME["border"])

    def _make_card(self, parent, radius=None, padx=18, pady=16, border=True):
        return RoundedFrame(
            parent,
            fill=THEME["surface"],
            border=THEME["border"] if border else None,
            radius=radius or THEME["radius_card"],
            padx=padx, pady=pady,
        )

    def _make_text_card(self, parent, height=5):
        """Rounded card wrapping a tk.Text widget. Returns (card, text)."""
        card = self._make_card(parent, padx=6, pady=6)
        # width=1 stops Text from demanding its default 80-char natural width,
        # which would otherwise force the whole window to that width.
        # fill="both", expand=True is what actually sizes it inside the card.
        text = tk.Text(
            card.body, width=1, height=height, wrap="word",
            bg=THEME["surface"], fg=THEME["text"],
            insertbackground=THEME["accent"],
            selectbackground=THEME["highlight"], selectforeground=THEME["text"],
            font=self._font_text_box,
            relief="flat", bd=0, highlightthickness=0,
            padx=16, pady=14,
            spacing1=4, spacing3=6,
        )
        text.pack(fill="both", expand=True)
        return card, text

    # ---- layout ----

    def _build_ui(self):
        SIDE = 22  # horizontal window inset
        SECTION_GAP = 14  # vertical gap between logical sections

        # --- App header (title + language selectors) ---
        header = tk.Frame(self.root, bg=THEME["bg"])
        header.pack(fill="x", padx=SIDE, pady=(22, 4))
        tk.Label(header, text="Screen Translate",
                 font=self._font_title, fg=THEME["text"],
                 bg=THEME["bg"], anchor="w").pack(anchor="w")

        lang_row = tk.Frame(header, bg=THEME["bg"])
        lang_row.pack(anchor="w", pady=(6, 0))
        options = [name for name, _g, _t in LANGUAGES]
        self.source_dropdown = RoundedDropdown(
            lang_row, self.source_lang_var, options,
            bg=THEME["surface"], fg=THEME["text"],
            hover_bg=THEME["surface_2"], active_bg=THEME["surface_press"],
            border=THEME["border"],
            menu_bg=THEME["surface"], menu_fg=THEME["text"],
            menu_active_bg=THEME["surface_2"], menu_active_fg=THEME["text"],
            font=self._font_body_lg,
            radius=THEME["radius_button"],
        )
        self.source_dropdown.pack(side="left")
        tk.Label(lang_row, text="  \u2192  ",
                 font=self._font_body_lg, fg=THEME["text_muted"],
                 bg=THEME["bg"]).pack(side="left")
        self.target_dropdown = RoundedDropdown(
            lang_row, self.target_lang_var, options,
            bg=THEME["surface"], fg=THEME["text"],
            hover_bg=THEME["surface_2"], active_bg=THEME["surface_press"],
            border=THEME["border"],
            menu_bg=THEME["surface"], menu_fg=THEME["text"],
            menu_active_bg=THEME["surface_2"], menu_active_fg=THEME["text"],
            font=self._font_body_lg,
            radius=THEME["radius_button"],
        )
        self.target_dropdown.pack(side="left")

        # --- Quick capture section ---
        quick_eyebrow_text = "Quick capture"
        if _HAS_PYNPUT:
            quick_eyebrow_text += f"  \u00b7  {GLOBAL_HOTKEY_DISPLAY}"
        self._make_eyebrow(self.root, quick_eyebrow_text).pack(
            fill="x", padx=SIDE, pady=(SECTION_GAP, 8))
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill="x", padx=SIDE)

        self._make_button(btn_frame, "\u2325  Select region",
                          self.on_select_region).pack(side="left", padx=(0, 10))
        self._make_button(btn_frame, "\u2610  Full screen",
                          self.on_full_screen).pack(side="left")
        if not _HAS_PYNPUT:
            tk.Label(
                self.root,
                text="pip3 install pynput to enable a global \u2318\u21e7T hotkey",
                fg=THEME["text_dim"], bg=THEME["bg"],
                font=self._font_status, anchor="w",
            ).pack(fill="x", padx=SIDE, pady=(6, 0))

        # --- Live mode section (in a glass card) ---
        self._make_eyebrow(self.root, "Live mode").pack(
            fill="x", padx=SIDE, pady=(SECTION_GAP, 8))

        live_card = self._make_card(self.root, padx=18, pady=16)
        live_card.pack(fill="x", padx=SIDE, ipady=0)

        row1 = tk.Frame(live_card.body, bg=THEME["surface"])
        row1.pack(fill="x")

        # Left cluster: eyebrow label above the [-] value [+] stepper.
        left_cluster = tk.Frame(row1, bg=THEME["surface"])
        left_cluster.pack(side="left", anchor="w")
        tk.Label(left_cluster, text="CHECK INTERVAL",
                 fg=THEME["text_muted"], bg=THEME["surface"],
                 font=self._font_eyebrow).pack(anchor="w")

        self.interval_var = tk.DoubleVar(value=0.3)
        RoundedStepper(
            left_cluster, self.interval_var,
            from_=0.1, to=30, increment=0.1,
            fmt="{:.1f}", unit=" s",
            bg=THEME["surface_2"], border=THEME["border_hi"],
            accent=THEME["accent"], text=THEME["text"],
            muted=THEME["text_muted"],
            font=self._font_button, font_value=self._font_value,
        ).pack(anchor="w", pady=(8, 0))

        self.live_btn = self._make_button(
            row1, "▶  Start Live Mode",
            self.on_toggle_live, primary=True, size="lg")
        self.live_btn.pack(side="right", anchor="e")

        # Divider inside the card.
        tk.Frame(live_card.body, height=1, bg=THEME["border"]).pack(
            fill="x", pady=(20, 16))

        # Custom rounded checkbox — replaces the default tk.Checkbutton.
        self.source_only_var = tk.BooleanVar(value=False)
        self.source_only_checkbox = RoundedCheckbox(
            live_card.body,
            self._source_only_label(),
            self.source_only_var,
            fg=THEME["text_body"], accent=THEME["accent"],
            box_bg=THEME["surface_2"], box_border=THEME["border_hi"],
            check_fg=THEME["bg_deep"],
            font=self._font_body_lg,
        )
        self.source_only_checkbox.pack(anchor="w", pady=(0, 2))
        if not _HAS_LANGDETECT:
            # Show the install hint as a small muted note underneath — keeps
            # the checkbox label a normal length so it doesn't blow out width.
            tk.Label(
                live_card.body,
                text="pip3 install langdetect for best accuracy",
                fg=THEME["text_dim"], bg=THEME["surface"],
                font=self._font_status, anchor="w",
            ).pack(anchor="w", padx=(30, 0), pady=(0, 2))

        self.full_screen_live_btn = self._make_button(
            live_card.body, "◱  Watch full screen (live)",
            self.on_toggle_live_full_screen, primary=False)
        # anchor='w' (not fill='x'): the pill keeps its intrinsic width instead
        # of floating inside a wider empty allocation.
        self.full_screen_live_btn.pack(anchor="w", pady=(14, 0))

        # --- Original text (glass card) ---
        orig_head = tk.Frame(self.root, bg=THEME["bg"])
        orig_head.pack(fill="x", padx=SIDE, pady=(SECTION_GAP, 8))
        self.orig_eyebrow = self._make_eyebrow(
            orig_head, f"Original — {self.source_lang_var.get()}")
        self.orig_eyebrow.pack(side="left")

        orig_card, self.orig_box = self._make_text_card(self.root, height=1)
        orig_card.pack(fill="both", expand=True, padx=SIDE, pady=(0, 8))

        # --- Translated text (glass card) ---
        trans_head = tk.Frame(self.root, bg=THEME["bg"])
        trans_head.pack(fill="x", padx=SIDE, pady=(6, 8))
        self.trans_eyebrow = self._make_eyebrow(
            trans_head, f"Translation — {self.target_lang_var.get()}")
        self.trans_eyebrow.pack(side="left")

        trans_card, self.trans_box = self._make_text_card(self.root, height=1)
        trans_card.pack(fill="both", expand=True, padx=SIDE, pady=(0, 8))

        copy_row = tk.Frame(self.root, bg=THEME["bg"])
        copy_row.pack(fill="x", padx=SIDE, pady=(4, 12))
        self._make_button(copy_row, "⧉  Copy translation",
                          self.on_copy, primary=True).pack(side="right")
        self.history_btn = self._make_button(
            copy_row, f"⧗  History ({len(self.history)})", self.on_open_history)
        self.history_btn.pack(side="left")

        # --- Status bar (with dot) ---
        self._make_divider(self.root).pack(fill="x", padx=SIDE)
        status_bar = tk.Frame(self.root, bg=THEME["bg"])
        status_bar.pack(fill="x", padx=SIDE, pady=(10, 14))
        self._status_dot = tk.Label(
            status_bar, text="●", fg=THEME["accent_soft"],
            bg=THEME["bg"], font=self._font_status)
        self._status_dot.pack(side="left")
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(status_bar, textvariable=self.status_var,
                 fg=THEME["text_muted"], bg=THEME["bg"],
                 font=self._font_status,
                 anchor="w").pack(side="left", padx=(8, 0))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def set_status(self, text):
        self.status_var.set(text)
        # Tint the status dot by state so glanceable info reads without reading.
        dot = getattr(self, "_status_dot", None)
        if dot is None:
            return
        low = text.lower()
        if any(k in low for k in ("error", "failed", "rate-limit", "backing off")):
            color = THEME["error"]
        elif any(k in low for k in ("updated", "ready", "copied", "done")):
            color = THEME["success"]
        elif any(k in low for k in ("watching", "waiting", "confirming",
                                     "detect", "settle", "capturing", "no spanish")):
            color = THEME["accent"]
        else:
            color = THEME["accent_soft"]
        dot.config(fg=color)

    def _source_only_label(self) -> str:
        return f"{self.source_lang_var.get()} only — filter out other languages"

    def _on_language_change(self):
        """Refresh dynamic labels + reset live-mode state so the new pair takes
        effect on the next tick. Also clears cached-image detection so we
        re-OCR immediately."""
        src = self.source_lang_var.get()
        tgt = self.target_lang_var.get()
        for attr, label in (("orig_eyebrow", f"Original — {src}"),
                            ("trans_eyebrow", f"Translation — {tgt}")):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.config(text=label.upper())
        checkbox = getattr(self, "source_only_checkbox", None)
        if checkbox is not None:
            checkbox.set_text(self._source_only_label())
        # Force the next live-mode tick to re-OCR and re-translate under the
        # new pair rather than short-circuiting on the cached hash.
        self._last_image_hash = None
        self.last_live_text = None
        self._pending_text = None
        self._pending_since = 0.0

    def _post(self, fn, *args, **kwargs):
        """Schedule `fn(*args, **kwargs)` to run on the Tk main thread.
        Safe to call from the live-mode worker thread."""
        try:
            self.root.after(0, lambda: fn(*args, **kwargs))
        except Exception:
            # root may already be destroyed during shutdown
            pass

    def set_texts(self, original, translated):
        self.orig_box.delete("1.0", "end")
        self.orig_box.insert("1.0", original)
        self.trans_box.delete("1.0", "end")
        self.trans_box.insert("1.0", translated)

    def on_copy(self):
        text = self.trans_box.get("1.0", "end").strip()
        if text:
            copy_to_clipboard(text)
            self.set_status("Translation copied to clipboard.")

    # --- Single-shot actions ---

    def on_select_region(self):
        if self.live_running:
            self.set_status("Stop live mode first.")
            return
        self.set_status("Drag to select a region...")
        self.root.update()
        path = capture_screenshot(full=False)
        self._process_single(path)

    def on_full_screen(self):
        if self.live_running:
            self.set_status("Stop live mode first.")
            return
        self.set_status("Capturing full screen...")
        self.root.update()
        path = capture_screenshot(full=True)
        self._process_single(path)

    def _process_single(self, path):
        if not path:
            self.set_status("No selection made.")
            return
        source_tess = LANG_DISPLAY_TO_TESS[self.source_lang_var.get()]
        source_g = LANG_DISPLAY_TO_GOOGLE[self.source_lang_var.get()]
        target_g = LANG_DISPLAY_TO_GOOGLE[self.target_lang_var.get()]
        try:
            try:
                raw = ocr_image_path(path, lang=source_tess)
            finally:
                _safe_unlink(path)

            text = clean_ocr_text(raw)
            if not text:
                self.set_status("No text detected.")
                self.set_texts("", "")
                return

            self.set_status("Translating...")
            self.root.update()
            translated = translate_text(text, source_g, target_g)
            self.set_texts(text, translated)
            copy_to_clipboard(translated)
            notify("Screen Translate", "Translation copied to clipboard.")
            self._record_history(
                self.source_lang_var.get(), self.target_lang_var.get(),
                text, translated)
            self.set_status(f"Done at {time.strftime('%H:%M:%S')}")
        except Exception as e:
            traceback.print_exc()
            self.set_status(f"Error: {type(e).__name__}: {e}")

    # --- Translation with exponential backoff ---

    def _translate_with_backoff(self, text: str, source: str, target: str):
        """Translate `text` from `source` to `target` (Google codes), or return
        None if we're inside a backoff window or the call fails. On failure,
        grows the backoff window.

        Safe to call from either the Tk main thread or the live-mode worker
        thread — all status-bar writes go through _post."""
        now = time.time()
        if now < self._backoff_until:
            remaining = int(self._backoff_until - now)
            self._post(self.set_status, f"Rate-limited; retrying in {remaining}s...")
            return None
        try:
            translated = translate_text(text, source, target)
        except Exception as e:
            traceback.print_exc()
            self._backoff_seconds = (
                self._BACKOFF_MIN if self._backoff_seconds <= 0
                else min(self._backoff_seconds * 2, self._BACKOFF_MAX)
            )
            self._backoff_until = time.time() + self._backoff_seconds
            self._post(
                self.set_status,
                f"Translate failed ({type(e).__name__}): backing off "
                f"{int(self._backoff_seconds)}s",
            )
            return None
        # Success — reset backoff.
        self._backoff_seconds = 0.0
        self._backoff_until = 0.0
        return translated

    # --- Live mode ---

    def on_toggle_live(self):
        if self.live_running:
            self._stop_live_thread()
            self.live_btn.config(text="▶  Start Live Mode")
            self.full_screen_live_btn.config(text="◱  Watch full screen (live)")
            self.set_status("Live mode stopped.")
            return

        self.set_status("Drag to select the region to watch...")
        self.root.update()
        rect = select_region_interactive(self.root)
        if rect is None:
            self.set_status("No region selected.")
            return
        self._start_live(rect)
        self.live_btn.config(text="■  Stop Live Mode")

    def on_toggle_live_full_screen(self):
        if self.live_running:
            self._stop_live_thread()
            self.live_btn.config(text="▶  Start Live Mode")
            self.full_screen_live_btn.config(text="◱  Watch full screen (live)")
            self.set_status("Live mode stopped.")
            return
        w = self.root.winfo_screenwidth()
        h = self.root.winfo_screenheight()
        self._start_live((0, 0, w, h))
        self.full_screen_live_btn.config(text="■  Stop Live Mode")
        if not self.spanish_only_var.get():
            self.set_status(
                "Watching full screen — enable 'Spanish only' to filter UI/English text."
            )

    def _start_live(self, rect):
        self.live_rect = rect
        self.last_live_text = None
        self._pending_text = None
        self._pending_since = 0.0
        self._last_image_hash = None
        self._backoff_until = 0.0
        self._backoff_seconds = 0.0
        self.live_running = True
        self._live_stop.clear()
        self._live_thread = threading.Thread(
            target=self._live_worker, name="live-translate", daemon=True
        )
        self._live_thread.start()
        self.set_status("Watching for text...")

    def _stop_live_thread(self):
        self.live_running = False
        self._live_stop.set()
        t = self._live_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.5)
        self._live_thread = None

    def _get_interval_secs(self) -> float:
        try:
            return max(0.1, float(self.interval_var.get()))
        except Exception:
            return 0.3

    def _live_worker(self):
        """Background loop. Runs capture / OCR / translate off the Tk main
        thread so the GUI stays responsive and the polling cadence isn't
        stretched by slow translate calls."""
        # Small initial delay so the overlay window has time to disappear.
        if self._live_stop.wait(0.15):
            return
        while not self._live_stop.is_set():
            tick_start = time.time()
            try:
                self._live_worker_tick()
            except Exception as e:
                traceback.print_exc()
                self._post(self.set_status, f"Live error: {type(e).__name__}: {e}")
            # Sleep the remainder of the interval; wake early on stop.
            interval = self._get_interval_secs()
            remaining = interval - (time.time() - tick_start)
            if remaining > 0 and self._live_stop.wait(remaining):
                return

    def _live_worker_tick(self):
        # 1. Capture.
        path = None
        try:
            path = capture_region(self.live_rect)
        except Exception:
            traceback.print_exc()
            self._post(self.set_status, "Capture failed; retrying...")
            return
        if not path:
            self._post(self.set_status, "Capture returned empty; retrying...")
            return

        # 2. Open + perceptual-hash. Delete the file as soon as we've decoded
        # the image; everything downstream works on the in-memory PIL image.
        img = None
        image_hash = None
        try:
            try:
                img = Image.open(path)
                img.load()
                image_hash = perceptual_image_hash(img)
            except Exception:
                traceback.print_exc()
                self._post(self.set_status, "Decode failed; retrying...")
                return
        finally:
            _safe_unlink(path)

        now = time.monotonic()

        source_display = self.source_lang_var.get()
        target_display = self.target_lang_var.get()
        source_tess = LANG_DISPLAY_TO_TESS[source_display]
        source_g = LANG_DISPLAY_TO_GOOGLE[source_display]
        target_g = LANG_DISPLAY_TO_GOOGLE[target_display]

        # 3. If the frame hasn't changed AND we have a pending candidate that's
        # been sitting there for at least _STABILITY_SECS, the text has stopped
        # moving — translate now. Otherwise, skip OCR entirely.
        if image_hash is not None and image_hash == self._last_image_hash:
            pending = self._pending_text
            if (pending
                    and pending != self.last_live_text
                    and (now - self._pending_since) >= self._STABILITY_SECS):
                translated = self._translate_with_backoff(pending, source_g, target_g)
                if translated is not None:
                    self.last_live_text = pending
                    self._pending_text = None
                    self._pending_since = 0.0
                    self._post(self._apply_live_result, pending, translated)
            return
        self._last_image_hash = image_hash

        # 4. OCR the already-decoded image. When the source-only filter is on
        # we run Tesseract with both the source and English models so that
        # English (or other Latin-script) UI chrome is recognized cleanly
        # rather than butchered by the source-only model — giving the
        # language classifier a clean input to reject.
        source_only = bool(self.source_only_var.get())
        ocr_lang = f"{source_tess}+eng" if (source_only and source_tess != "eng") else source_tess
        try:
            raw = ocr_image(img, lang=ocr_lang)
        except Exception:
            traceback.print_exc()
            self._post(self.set_status, "OCR failed; retrying...")
            return

        text = clean_ocr_text(raw)

        if not text:
            self._pending_text = None
            self._pending_since = 0.0
            self._post(self.set_status, "Watching for text...")
            return

        if source_only:
            filtered = filter_to_language(text, source_g)
            if not filtered:
                # There may be lots of English/UI text on screen — don't spam
                # "noisy frame"; just say we're waiting for the source lang.
                self._pending_text = None
                self._pending_since = 0.0
                self._post(self.set_status, f"No {source_display} text detected...")
                return
            text = filtered

        if not looks_like_real_text(text):
            # Don't clear pending on a noisy frame — the previously-detected
            # good text may still be on screen.
            self._post(self.set_status, "Ignoring noisy OCR frame...")
            return

        if text == self.last_live_text:
            self._pending_text = None
            self._pending_since = 0.0
            return

        # 5. Stability gate. If the OCR text changed vs. the pending candidate,
        # the user is still typing / the frame is still rendering — restart the
        # timer. Only translate once the same text has persisted for
        # _STABILITY_SECS. This is the fix for translating mid-word / mid-render
        # captures: any keystroke resets pending_since.
        if text != self._pending_text:
            self._pending_text = text
            self._pending_since = now
            self._post(self.set_status, "Detected new text; waiting for it to settle...")
            return

        if (now - self._pending_since) < self._STABILITY_SECS:
            # Same text as last tick but not stable long enough yet.
            return

        translated = self._translate_with_backoff(text, source_g, target_g)
        if translated is not None:
            self.last_live_text = text
            self._pending_text = None
            self._pending_since = 0.0
            self._post(self._apply_live_result, text, translated)

    def _apply_live_result(self, original, translated):
        self.set_texts(original, translated)
        copy_to_clipboard(translated)
        self._record_history(
            self.source_lang_var.get(), self.target_lang_var.get(),
            original, translated)
        self.set_status(f"Updated {time.strftime('%H:%M:%S')}")

    # --- History ---

    def _load_history(self):
        try:
            with open(_HISTORY_PATH) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, list):
            return
        # File is stored newest-first; deque uses appendleft on insert, so
        # replay in reverse to preserve order after loading.
        for entry in reversed(data[:_HISTORY_MAX]):
            if isinstance(entry, dict) and "source_text" in entry:
                self.history.appendleft(entry)

    def _save_history(self):
        try:
            os.makedirs(_HISTORY_DIR, exist_ok=True)
            with open(_HISTORY_PATH, "w") as f:
                json.dump(list(self.history), f, ensure_ascii=False)
        except OSError:
            pass

    def _record_history(self, source_display: str, target_display: str,
                        source_text: str, translated: str):
        source_text = source_text.strip()
        translated = translated.strip()
        if not source_text or not translated:
            return
        if self.history and self.history[0].get("source_text") == source_text \
                and self.history[0].get("source_lang") == source_display \
                and self.history[0].get("target_lang") == target_display:
            # Same input, same pair — don't clutter the list.
            return
        self.history.appendleft({
            "source_lang": source_display,
            "target_lang": target_display,
            "source_text": source_text,
            "translated": translated,
            "timestamp": time.time(),
        })
        self._refresh_history_button()
        # If the history window is open, refresh it too.
        if self._history_window is not None and self._history_window.winfo_exists():
            self._rebuild_history_list()

    def _refresh_history_button(self):
        btn = getattr(self, "history_btn", None)
        if btn is not None:
            btn.config(text=f"⧗  History ({len(self.history)})")

    def on_open_history(self):
        if self._history_window is not None and self._history_window.winfo_exists():
            self._history_window.lift()
            self._history_window.focus_force()
            return
        win = tk.Toplevel(self.root)
        self._history_window = win
        win.title("Translation history")
        win.configure(bg=THEME["bg"])
        win.geometry("560x520")
        win.minsize(420, 360)
        win.protocol("WM_DELETE_WINDOW", self._on_close_history_window)

        header = tk.Frame(win, bg=THEME["bg"])
        header.pack(fill="x", padx=20, pady=(18, 6))
        tk.Label(header, text="History",
                 font=self._font_title, fg=THEME["text"],
                 bg=THEME["bg"], anchor="w").pack(side="left")
        self._make_button(header, "Clear all", self._clear_history).pack(side="right")

        # Scrollable list area.
        list_wrap = tk.Frame(win, bg=THEME["bg"])
        list_wrap.pack(fill="both", expand=True, padx=20, pady=(6, 18))
        canvas = tk.Canvas(list_wrap, bg=THEME["bg"],
                           highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(list_wrap, orient="vertical",
                                 command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        inner = tk.Frame(canvas, bg=THEME["bg"])
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        # Keep the inner frame width in sync with the canvas so rows fill.
        def _resize_inner(event):
            canvas.itemconfig(inner_id, width=event.width)
        canvas.bind("<Configure>", _resize_inner)
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))

        # Two-finger scroll on macOS.
        def _on_wheel(event):
            canvas.yview_scroll(int(-event.delta), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)

        self._history_list_frame = inner
        self._rebuild_history_list()

    def _on_close_history_window(self):
        try:
            # Unbind the global mousewheel binding this window installed.
            self.root.unbind_all("<MouseWheel>")
        except Exception:
            pass
        if self._history_window is not None:
            self._history_window.destroy()
        self._history_window = None
        self._history_list_frame = None

    def _rebuild_history_list(self):
        parent = self._history_list_frame
        if parent is None:
            return
        for child in parent.winfo_children():
            child.destroy()
        if not self.history:
            tk.Label(parent, text="No translations yet.",
                     fg=THEME["text_muted"], bg=THEME["bg"],
                     font=self._font_body).pack(anchor="w", pady=8)
            return
        for entry in self.history:
            self._make_history_row(parent, entry)

    def _make_history_row(self, parent, entry):
        row = RoundedFrame(parent, fill=THEME["surface"],
                            border=THEME["border"],
                            radius=THEME["radius_card"] - 4,
                            padx=14, pady=10)
        row.pack(fill="x", pady=(0, 8))

        meta = tk.Frame(row.body, bg=THEME["surface"])
        meta.pack(fill="x")
        pair = f"{entry.get('source_lang', '?')} → {entry.get('target_lang', '?')}"
        tk.Label(meta, text=pair.upper(),
                 fg=THEME["text_muted"], bg=THEME["surface"],
                 font=self._font_eyebrow).pack(side="left")
        ts = entry.get("timestamp")
        if isinstance(ts, (int, float)):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            tk.Label(meta, text=when,
                     fg=THEME["text_dim"], bg=THEME["surface"],
                     font=self._font_status).pack(side="right")

        src = _truncate(entry.get("source_text", ""), 140)
        trans = _truncate(entry.get("translated", ""), 140)
        tk.Label(row.body, text=src, fg=THEME["text"], bg=THEME["surface"],
                 font=self._font_body_lg, anchor="w", justify="left",
                 wraplength=460).pack(fill="x", pady=(8, 2))
        tk.Label(row.body, text=trans, fg=THEME["text_body"],
                 bg=THEME["surface"],
                 font=self._font_body, anchor="w", justify="left",
                 wraplength=460).pack(fill="x")

        def _recall(_e=None):
            self._recall_entry(entry)
        # Bind on the row, body, and every child so clicks anywhere recall.
        for w in (row, row.body, *row.body.winfo_children()):
            w.bind("<Button-1>", _recall)
            try:
                w.config(cursor="hand2")
            except tk.TclError:
                pass

    def _recall_entry(self, entry):
        src_lang = entry.get("source_lang")
        tgt_lang = entry.get("target_lang")
        if src_lang in LANG_DISPLAY_TO_GOOGLE:
            self.source_lang_var.set(src_lang)
        if tgt_lang in LANG_DISPLAY_TO_GOOGLE:
            self.target_lang_var.set(tgt_lang)
        self.set_texts(entry.get("source_text", ""), entry.get("translated", ""))
        self.set_status("Recalled from history.")

    def _clear_history(self):
        self.history.clear()
        self._refresh_history_button()
        self._rebuild_history_list()
        self._save_history()

    # --- Global hotkey ---

    def _start_hotkey_listener(self):
        if not _HAS_PYNPUT:
            return
        try:
            self._hotkey_listener = _pynput_keyboard.GlobalHotKeys({
                GLOBAL_HOTKEY: self._on_hotkey,
            })
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
        except Exception:
            # On macOS this most often means the process lacks Accessibility
            # permission. Log and carry on — hotkey is a nice-to-have.
            traceback.print_exc()
            self._hotkey_listener = None

    def _stop_hotkey_listener(self):
        listener = self._hotkey_listener
        self._hotkey_listener = None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:
            pass

    def _on_hotkey(self):
        """Runs on pynput's listener thread. Marshal to the Tk main thread."""
        self._post(self._trigger_capture_from_hotkey)

    def _trigger_capture_from_hotkey(self):
        # Bring the window forward briefly so users get feedback that the
        # hotkey fired, but don't require it to stay focused for the actual
        # capture (`screencapture -i` overlays the whole screen anyway).
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass
        self.on_select_region()

    def on_close(self):
        self._stop_live_thread()
        self._stop_hotkey_listener()
        self._save_history()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = ScreenTranslateApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
