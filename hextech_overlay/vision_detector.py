"""Pure-vision fallback detector: Win32 GDI capture, 3/4-card layout geometry, Windows OCR, and selection state machine."""

from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import threading
import time
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import numpy as np
from PIL import Image, ImageOps

from .catalog import AugmentCatalog, normalize_title
from .models import AugmentInfo, AugmentMetric, PickedAugment

BASE_W = 1920
BASE_H = 1080


@dataclass(frozen=True)
class AugmentLayout:
    name: str
    slot_count: int
    card_width_ratio: float
    gap_ratio: float
    modern: bool


FOUR_CARD_LAYOUT = AugmentLayout("four_card", 4, 0.174, 0.026, True)
THREE_CARD_LAYOUT = AugmentLayout("three_card", 3, 0.174, 0.026, True)
COMPACT_FOUR_CARD_LAYOUT = AugmentLayout("compact_four", 4, 0.168, 0.0175, True)
LEGACY_THREE_CARD_LAYOUT = AugmentLayout("legacy_three", 3, 0.168, 0.0175, False)

ALL_LAYOUTS = (
    FOUR_CARD_LAYOUT,
    THREE_CARD_LAYOUT,
    LEGACY_THREE_CARD_LAYOUT,
    COMPACT_FOUR_CARD_LAYOUT,
)


@dataclass(frozen=True)
class GameGeometry:
    """Maps 1920x1080 UI coordinates into any frame resolution (height-scaled, centered)."""

    scale: float
    off_x: int
    width: int
    height: int

    @classmethod
    def from_frame(cls, width: int, height: int) -> GameGeometry:
        s = float(height) / float(BASE_H)
        off_x = int(round(width / 2.0 - (BASE_W / 2.0) * s))
        return cls(scale=s, off_x=off_x, width=width, height=height)

    def x(self, xb: float) -> int:
        return int(xb * self.scale) + self.off_x

    def y(self, yb: float) -> int:
        return int(yb * self.scale)

    @property
    def content_width(self) -> float:
        return min(float(self.width), float(self.height) * (16.0 / 9.0))

    @property
    def content_left(self) -> float:
        return (float(self.width) - self.content_width) / 2.0


def get_card_geometry(width: int, height: int, layout: AugmentLayout) -> tuple[float, float, float, float]:
    """Return (card_width, card_gap, group_width, group_left) in pixel space."""
    geom = GameGeometry.from_frame(width, height)
    cw = geom.content_width * layout.card_width_ratio
    cg = geom.content_width * layout.gap_ratio
    gw = cw * layout.slot_count + cg * (layout.slot_count - 1)
    gl = geom.content_left + (geom.content_width - gw) / 2.0
    return cw, cg, gw, gl


def has_visible_card_frames(rgb: np.ndarray, layout: AugmentLayout) -> bool:
    """Detect whether an augment selection modal is open via gold/silver card border geometry."""
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        return False
    h, w, _ = rgb.shape
    if w < 64 or h < 48:
        return False

    card_w, card_gap, _, group_left = get_card_geometry(w, h, layout)
    min_r, min_g, min_b = (110, 95, 75) if layout.modern else (130, 110, 80)

    def is_border_patch(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        sub = rgb[np.ix_(ys, xs)]
        r = sub[..., 0].astype(np.int16)
        g = sub[..., 1].astype(np.int16)
        b = sub[..., 2].astype(np.int16)
        spread = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
        return (r >= min_r) & (g >= min_g) & (b >= min_b) & (spread <= 125)

    def has_card_at(left: float) -> bool:
        ys_top = np.clip(np.round(h * np.linspace(0.14, 0.23, 24)).astype(int), 0, h - 1)
        xs_top = np.clip(np.round(left + card_w * np.linspace(0.15, 0.85, 36)).astype(int), 0, w - 1)
        top_cov = float(is_border_patch(xs_top, ys_top).mean(axis=1).max())
        if top_cov < 0.78:
            return False

        ys_edge = np.clip(np.round(h * np.linspace(0.24, 0.35, 24)).astype(int), 0, h - 1)
        for edge in (0.0, 0.91):
            xs_edge = np.clip(np.round(left + card_w * np.linspace(edge, edge + 0.09, 12)).astype(int), 0, w - 1)
            edge_cov = float(is_border_patch(xs_edge, ys_edge).mean(axis=0).max())
            if edge_cov < 0.78:
                return False

        ys_in = np.clip(np.round(h * np.linspace(0.50, 0.60, 8)).astype(int), 0, h - 1)
        xs_in = np.clip(np.round(left + card_w * np.linspace(0.20, 0.80, 8)).astype(int), 0, w - 1)
        interior = rgb[np.ix_(ys_in, xs_in)][..., :3].max(axis=2)
        return float((interior < 90).mean()) >= 0.48

    visible = 0
    for slot in range(layout.slot_count):
        if has_card_at(group_left + slot * (card_w + card_gap)):
            visible += 1
        if visible >= 3:
            return True
        if visible + (layout.slot_count - slot - 1) < 3:
            return False
    return False


def detect_card_layout(rgb: np.ndarray) -> AugmentLayout | None:
    """Identify whether a 3-card or 4-card augment selection overlay is currently visible."""
    for layout in ALL_LAYOUTS:
        if has_visible_card_frames(rgb, layout):
            return layout
    return None


def title_crop_boxes(width: int, height: int, layout: AugmentLayout) -> list[tuple[int, int, int, int]]:
    """Compute tight pixel bounding boxes for each card title region."""
    card_w, card_gap, _, group_left = get_card_geometry(width, height, layout)
    y0 = max(0, int(round(height * 0.365)))
    y1 = min(height, int(round(height * 0.448)))
    boxes: list[tuple[int, int, int, int]] = []
    for slot in range(layout.slot_count):
        left = group_left + slot * (card_w + card_gap)
        x0 = max(0, int(round(left + card_w * 0.06)))
        x1 = min(width, int(round(left + card_w * 0.94)))
        boxes.append((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))
    return boxes


def card_boxes(width: int, height: int, layout: AugmentLayout) -> list[tuple[int, int, int, int]]:
    """Compute full card body boxes for flare brightness measurement."""
    card_w, card_gap, _, group_left = get_card_geometry(width, height, layout)
    y0 = max(0, int(round(height * 0.17)))
    y1 = min(height, int(round(height * 0.67)))
    boxes: list[tuple[int, int, int, int]] = []
    for slot in range(layout.slot_count):
        left = group_left + slot * (card_w + card_gap)
        x0 = max(0, int(round(left + card_w * 0.05)))
        x1 = min(width, int(round(left + card_w * 0.95)))
        boxes.append((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))
    return boxes


def compute_card_stats(gray: np.ndarray, layout: AugmentLayout) -> list[dict[str, float]]:
    """Compute per-card brightness mean and bright pixel percentage (>= 200) for flare detection."""
    h, w = gray.shape[:2]
    stats: list[dict[str, float]] = []
    for x0, y0, x1, y1 in card_boxes(w, h, layout):
        patch = gray[y0:y1, x0:x1]
        if patch.size == 0:
            stats.append({"mean": 0.0, "bright_pct": 0.0})
            continue
        stats.append(
            {
                "mean": float(patch.mean()),
                "bright_pct": float((patch >= 200).mean() * 100.0),
            }
        )
    return stats


def classify_click_zone(
    rel_x: float,
    rel_y: float,
    layout: AugmentLayout,
    frame_w: int = BASE_W,
    frame_h: int = BASE_H,
) -> tuple[Literal["card", "reroll", "hide", "outside"], int | None]:
    """Classify a normalized cursor click within the game window."""
    if not (0.0 <= rel_x <= 1.0 and 0.0 <= rel_y <= 1.0):
        return "outside", None

    px = rel_x * frame_w
    card_w, card_gap, _, group_left = get_card_geometry(frame_w, frame_h, layout)

    for slot in range(layout.slot_count):
        left = group_left + slot * (card_w + card_gap)
        right = left + card_w
        if left <= px <= right:
            if 0.15 <= rel_y <= 0.675:
                return "card", slot
            if 0.675 < rel_y <= 0.775:
                return "reroll", slot

    geom = GameGeometry.from_frame(frame_w, frame_h)
    hide_left = geom.content_left + geom.content_width * 0.39
    hide_right = geom.content_left + geom.content_width * 0.61
    if hide_left <= px <= hide_right and 0.78 <= rel_y <= 0.94:
        return "hide", None

    return "outside", None


# ------------------------------------------------------------------ Win32 capture & passive click polling

_WIN32_READY = False
_user32 = None
_gdi32 = None


class _BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _ensure_win32() -> bool:
    global _WIN32_READY, _user32, _gdi32
    if os.name != "nt":
        return False
    if _WIN32_READY:
        return True
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        try:
            _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        _user32.FindWindowW.restype = wintypes.HWND
        _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        _user32.IsIconic.restype = wintypes.BOOL
        _user32.IsIconic.argtypes = [wintypes.HWND]
        _user32.IsWindowVisible.restype = wintypes.BOOL
        _user32.IsWindowVisible.argtypes = [wintypes.HWND]
        _user32.GetClientRect.restype = wintypes.BOOL
        _user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        _user32.ClientToScreen.restype = wintypes.BOOL
        _user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        _user32.GetForegroundWindow.restype = wintypes.HWND
        _user32.GetAsyncKeyState.restype = ctypes.c_short
        _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        _user32.GetCursorPos.restype = wintypes.BOOL
        _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        _user32.GetDC.restype = ctypes.c_void_p
        _user32.GetDC.argtypes = [ctypes.c_void_p]
        _user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        _gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        _gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
        _gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        _gdi32.SelectObject.restype = ctypes.c_void_p
        _gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _gdi32.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _gdi32.StretchBlt.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_void_p] + [ctypes.c_int] * 5
        _gdi32.GetDIBits.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        _gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        _WIN32_READY = True
        return True
    except Exception:
        return False


def find_game_window_rect() -> tuple[int, int, int, int] | None:
    """Return (left, top, width, height) of League of Legends client area if foreground/visible."""
    if not _ensure_win32():
        return None
    for title in ("League of Legends (TM) Client", "League of Legends"):
        hwnd = _user32.FindWindowW(None, title)
        if not hwnd or not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
            continue
        rect = wintypes.RECT()
        pt = wintypes.POINT(0, 0)
        if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
            continue
        if not _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            continue
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w >= 640 and h >= 360:
            return (pt.x, pt.y, w, h)
    return None


def capture_rect_rgb(rect: tuple[int, int, int, int], out_w: int | None = None, out_h: int | None = None) -> np.ndarray | None:
    """Non-intrusive screen pixel capture via GDI StretchBlt(SRCCOPY) without CAPTUREBLT."""
    if not _ensure_win32():
        return None
    l, t, w, h = rect
    ow = out_w or w
    oh = out_h or h
    if w <= 0 or h <= 0 or ow <= 0 or oh <= 0:
        return None

    sdc = _user32.GetDC(None)
    if not sdc:
        return None
    mdc = _gdi32.CreateCompatibleDC(sdc)
    bmp = _gdi32.CreateCompatibleBitmap(sdc, ow, oh)
    old = _gdi32.SelectObject(mdc, bmp)
    try:
        _gdi32.SetStretchBltMode(mdc, 4)  # HALFTONE
        _gdi32.StretchBlt(mdc, 0, 0, ow, oh, sdc, l, t, w, h, 0x00CC0020)
        bmi = _BMIH(ctypes.sizeof(_BMIH), ow, -oh, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(ow * oh * 4)
        _gdi32.GetDIBits(mdc, bmp, 0, oh, buf, ctypes.byref(bmi), 0)
        bgra = np.frombuffer(buf, np.uint8).reshape(oh, ow, 4)
        return bgra[:, :, [2, 1, 0]].copy()
    except Exception:
        return None
    finally:
        _gdi32.SelectObject(mdc, old)
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mdc)
        _user32.ReleaseDC(None, sdc)


class PassiveMouseTracker:
    """Read-only mouse left-click edge detector (never hooks or modifies input)."""

    def __init__(self) -> None:
        self._was_down = False

    def poll_click(self) -> tuple[int, int] | None:
        if not _ensure_win32():
            return None
        down = bool(_user32.GetAsyncKeyState(0x01) & 0x8000)
        pressed_now = down and not self._was_down
        self._was_down = down
        if not pressed_now:
            return None
        pt = wintypes.POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            return (pt.x, pt.y)
        return None


def get_cursor_pos() -> tuple[int, int] | None:
    """Return current screen cursor position (x, y) without modifying input state."""
    if not _ensure_win32():
        return None
    pt = wintypes.POINT()
    if _user32.GetCursorPos(ctypes.byref(pt)):
        return (int(pt.x), int(pt.y))
    return None


def is_tab_key_down() -> bool:
    """Read-only check for whether the Tab scoreboard key is currently held."""
    if not _ensure_win32():
        return False
    return bool(_user32.GetAsyncKeyState(0x09) & 0x8000)


def tooltip_crop_rect(
    game_rect: tuple[int, int, int, int],
    cursor_xy: tuple[int, int],
    *,
    tab_held: bool = False,
) -> tuple[int, int, int, int] | None:
    """Return a clamped screen capture box near the cursor for bottom-HUD or Tab tooltips."""
    gl, gt, gw, gh = game_rect
    cx, cy = cursor_xy
    if gw <= 0 or gh <= 0 or not (gl <= cx < gl + gw and gt <= cy < gt + gh):
        return None

    rel_x = (cx - gl) / float(gw)
    rel_y = (cy - gt) / float(gh)
    if rel_y < 0.70 and not (tab_held and 0.12 <= rel_x <= 0.88 and 0.12 <= rel_y <= 0.86):
        return None

    s = float(gh) / float(BASE_H)
    box_w = max(80, int(round(520 * s)))
    box_h = max(60, int(round(380 * s)))
    if rel_y >= 0.70:
        x0 = cx - int(round(180 * s))
        y0 = cy - int(round(360 * s))
    else:
        x0 = cx - int(round(200 * s))
        y0 = cy - int(round(220 * s))

    x1 = min(gl + gw, max(gl + box_w, x0 + box_w))
    y1 = min(gt + gh, max(gt + box_h, y0 + box_h))
    x0 = max(gl, x1 - box_w)
    y0 = max(gt, y1 - box_h)
    w = x1 - x0
    h = y1 - y0
    if w < 80 or h < 60:
        return None
    return (x0, y0, w, h)


_CJK_SPACE_LEFT_RE = re.compile(r"(?<=[\u4e00-\u9fff：，。！？（）【】、])\s+(?=[\u4e00-\u9fff：，。！？（）【】、:：0-9])")
_CJK_SPACE_RIGHT_RE = re.compile(r"(?<=[0-9:：])\s+(?=[\u4e00-\u9fff])")
_COMMA_NUM_SPACE_RE = re.compile(r",\s+(?=[0-9])")

_DAMAGE_METRIC_RE = re.compile(
    r"(?P<prefix>累计|总共|共|已|额外)?"
    r"(?:造成(?:的)?(?:总|额外|魔法|物理|真实)?伤害|总伤害|伤害统计|额外伤害)"
    r"[:：]?\s*([0-9][0-9,\.]*(?:\s*[kKwW万])?)"
)
_HEAL_METRIC_RE = re.compile(
    r"(?:累计|总共|共|已)?(?:治疗(?:量|生命值)?|回复生命值)[:：]?\s*([0-9][0-9,\.]*(?:\s*[kKwW万])?)"
)
_SHIELD_METRIC_RE = re.compile(
    r"(?:累计|总共|共|已)?(?:提供|格挡|吸收)?护盾(?:值|量)?[:：]?\s*([0-9][0-9,\.]*(?:\s*[kKwW万])?)"
)
_GOLD_METRIC_RE = re.compile(
    r"(?:累计|总共|共|已)?(?:获得|赚取|窃取)(?:的)?金币[:：]?\s*([0-9][0-9,\.]*(?:\s*[kKwW万])?)"
)
_STACK_METRIC_RE_A = re.compile(r"(?:当前|已叠加|累计|总)?层数[:：]?\s*([0-9][0-9,\.]*)")
_STACK_METRIC_RE_B = re.compile(r"(?:已叠加|叠加了|已获得|当前)[:：]?\s*([0-9][0-9,\.]*)\s*层")
_TRIGGER_METRIC_RE = re.compile(r"(?:已触发|触发次数)[:：]?\s*([0-9][0-9,\.]*)\s*次?")
_CDR_METRIC_RE = re.compile(r"(?:已减少|减少冷却(?:时间)?|冷却缩减)[:：]?\s*([0-9][0-9,\.]*)\s*[秒s]?")


def _compact_ocr_line(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "").strip()
    s = _COMMA_NUM_SPACE_RE.sub(",", s)
    s = _CJK_SPACE_LEFT_RE.sub("", s)
    s = _CJK_SPACE_RIGHT_RE.sub("", s)
    return s


def parse_tooltip_metrics(
    lines: list[str],
    picks: list[PickedAugment],
    catalog: AugmentCatalog,
) -> tuple[str, list[AugmentMetric]] | None:
    """Identify an owned augment from tooltip OCR lines and extract tracked counter metrics."""
    compacted = [_compact_ocr_line(ln) for ln in lines if ln and ln.strip()]
    if not compacted or not picks:
        return None

    owned_by_id = {p.augment_id: p for p in picks if p.augment_id > 0}
    owned_by_key = {p.key.lower(): p for p in picks if p.key}
    picks_by_name = sorted(
        [(normalize_title(p.name), p) for p in picks if len(normalize_title(p.name)) >= 2],
        key=lambda item: len(item[0]),
        reverse=True,
    )

    matched_pick: PickedAugment | None = None
    for idx, line in enumerate(compacted[:5]):
        norm_line = normalize_title(line)
        if not norm_line:
            continue
        for norm_name, p in picks_by_name:
            if norm_name in norm_line:
                matched_pick = p
                break
        if matched_pick is not None:
            break
        aug, score = catalog.match_ocr_text(line)
        if aug is not None and score >= 0.78:
            candidate = owned_by_id.get(aug.id) or owned_by_key.get(aug.key.lower())
            if candidate is not None:
                matched_pick = candidate
                break

    if matched_pick is None:
        return None

    metrics: list[AugmentMetric] = []
    seen_labels: set[tuple[str, str]] = set()
    now = time.time()

    def _add(kind: Literal["stack", "damage", "heal", "shield", "gold", "stat"], label: str, raw_val: str) -> None:
        val = raw_val.replace(" ", "").strip()
        if not val or (kind, label) in seen_labels:
            return
        seen_labels.add((kind, label))
        metrics.append(AugmentMetric(label=label, value=val, kind=kind, source="tooltip_ocr", updated_at=now))

    for line in compacted:
        if m := _DAMAGE_METRIC_RE.search(line):
            lbl = "额外伤害" if (m.group("prefix") == "额外" or "额外伤害" in m.group(0)) else "已造成伤害"
            _add("damage", lbl, m.group(2))
        if m := _HEAL_METRIC_RE.search(line):
            _add("heal", "已治疗", m.group(1))
        if m := _SHIELD_METRIC_RE.search(line):
            _add("shield", "已提供护盾", m.group(1))
        if m := _GOLD_METRIC_RE.search(line):
            _add("gold", "已获金币", m.group(1))
        if m := _STACK_METRIC_RE_A.search(line):
            val = m.group(1).strip()
            _add("stack", "叠层", val if val.endswith("层") else f"{val}层")
        elif m := _STACK_METRIC_RE_B.search(line):
            val = m.group(1).strip()
            _add("stack", "叠层", val if val.endswith("层") else f"{val}层")
        if m := _TRIGGER_METRIC_RE.search(line):
            val = m.group(1).strip()
            _add("stat", "触发", val if val.endswith("次") else f"{val}次")
        if m := _CDR_METRIC_RE.search(line):
            val = m.group(1).strip()
            _add("stat", "减少冷却", val if val.endswith("秒") else f"{val}秒")

    if not metrics:
        return None
    return matched_pick.uid, metrics


# ------------------------------------------------------------------ Windows built-in OCR worker

_PS_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
[Console]::InputEncoding = New-Object Text.UTF8Encoding $false
trap { [Console]::Out.WriteLine('INITERR ' + ($_.Exception.Message -replace "[\r\n]+", ' ')); exit 1 }
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
$wanted = '__LANG__'
$installed = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages
$language = $installed | Where-Object LanguageTag -eq $wanted | Select-Object -First 1
if (-not $language) {
  $language = $installed | Where-Object LanguageTag -match '^zh-(Hans|CN|SG)(-|$)' | Select-Object -First 1
}
if (-not $language) {
  $language = $installed | Where-Object LanguageTag -match '^en-' | Select-Object -First 1
}
if (-not $language) { [Console]::Out.WriteLine('NOENGINE'); exit 2 }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if (-not $engine) { throw "Cannot initialize OCR: $($language.LanguageTag)" }
[Console]::Out.WriteLine('READY ' + $engine.RecognizerLanguage.LanguageTag)
while (($line = [Console]::In.ReadLine()) -ne $null) {
  if (-not $line) { continue }
  try {
    $bytes = [System.IO.File]::ReadAllBytes($line)
    $ms = New-Object System.IO.MemoryStream(,$bytes)
    $stream = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($ms)
    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
    foreach ($l in $res.Lines) {
      [Console]::Out.WriteLine('LINE ' + $l.Text)
    }
    $stream.Dispose()
    $ms.Dispose()
    [Console]::Out.WriteLine('END')
  } catch {
    [Console]::Out.WriteLine('ERR ' + ($_.Exception.Message -replace "[\r\n]+", ' '))
    [Console]::Out.WriteLine('END')
  }
}
"""


class WindowsOcrWorker:
    """Persistent hidden PowerShell host wrapping Windows.Media.Ocr.OcrEngine."""

    def __init__(self, tmp_dir: Path, lang: str = "zh-Hans-CN") -> None:
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.lang = lang
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._q: queue.Queue[str] = queue.Queue()
        self._slot = 0
        self.active_language = ""
        self.last_error = ""

    def start(self, timeout: float = 8.0) -> bool:
        if os.name != "nt":
            self.last_error = "非 Windows 系统"
            return False
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self.active_language:
                return True
            self._close_locked()
            script = _PS_OCR_SCRIPT.replace("__LANG__", self.lang)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._q = queue.Queue()
            try:
                self._proc = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
            except OSError as exc:
                self.last_error = str(exc)
                return False

            threading.Thread(target=self._reader_loop, args=(self._proc,), daemon=True).start()
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    msg = self._q.get(timeout=max(0.05, deadline - time.time()))
                except queue.Empty:
                    break
                if msg.startswith("READY "):
                    self.active_language = msg[6:].strip()
                    self.last_error = ""
                    return True
                if msg.startswith(("INITERR ", "NOENGINE", "EOF")):
                    self.last_error = msg
                    self._close_locked()
                    return False
            self.last_error = "Windows OCR 初始化超时"
            self._close_locked()
            return False

    def _reader_loop(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                self._q.put(raw.rstrip("\r\n"))
        except Exception:
            pass
        self._q.put("EOF")

    def recognize_image(self, image: Image.Image, timeout: float = 3.0) -> list[str]:
        """Run Windows OCR on a PIL Image using a rotated temp BMP slot."""
        if not self.start():
            return []
        with self._lock:
            if self._proc is None or self._proc.stdin is None or self._proc.poll() is not None:
                return []
            self._slot = (self._slot + 1) % 4
            bmp_path = self.tmp_dir / f"ocr_{self._slot}.bmp"
            # Pad with white border and ensure tall enough for Windows OCR accuracy
            gray = ImageOps.grayscale(image)
            if gray.height < 84:
                scale = min(3.0, 96.0 / max(1, gray.height))
                gray = gray.resize((int(round(gray.width * scale)), int(round(gray.height * scale))), Image.LANCZOS)
            padded = ImageOps.expand(gray, border=24, fill=0)
            padded.save(bmp_path, format="BMP")

            while not self._q.empty():
                self._q.get_nowait()

            try:
                self._proc.stdin.write(str(bmp_path.resolve()) + "\n")
                self._proc.stdin.flush()
            except OSError as exc:
                self.last_error = str(exc)
                self._close_locked()
                return []

            lines: list[str] = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    msg = self._q.get(timeout=max(0.05, deadline - time.time()))
                except queue.Empty:
                    break
                if msg == "END":
                    break
                if msg == "EOF":
                    self._close_locked()
                    break
                if msg.startswith("LINE "):
                    txt = msg[5:].strip()
                    if txt:
                        lines.append(txt)
                elif msg.startswith("ERR "):
                    self.last_error = msg[4:]
            return lines

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        self.active_language = ""
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=1.5)
            except Exception:
                pass
            self._proc = None


# ------------------------------------------------------------------ Pure Visual Selection State Machine

@dataclass
class VisualPickResult:
    augment: AugmentInfo
    slot_index: int | None
    source: Literal["vision_click", "vision_flare"]


@dataclass
class VisualFrameOutcome:
    panel_open: bool
    candidates: list[tuple[int, AugmentInfo | None, float]]
    picked: VisualPickResult | None = None
    reverted_augment_id: int | None = None


class VisualSelectionStateMachine:
    """Tracks augment offer open/reroll/close/hide/pick transitions without OS dependencies."""

    def __init__(self, catalog: AugmentCatalog) -> None:
        self.catalog = catalog
        self.is_open = False
        self._open_hits = 0
        self._miss_hits = 0
        self.layout: AugmentLayout | None = None
        self.candidates: dict[int, tuple[AugmentInfo, float]] = {}
        self.offer_opened_at = 0.0
        self.last_reroll_at = 0.0
        self.last_click: tuple[float, Literal["card", "reroll", "hide", "outside"], int | None] | None = None
        self.last_card_click: tuple[float, int] | None = None
        self.recent_stats: list[tuple[float, list[dict[str, float]]]] = []
        self._last_closed: tuple[frozenset[int], float, int | None] | None = None

    def reset(self) -> None:
        self.is_open = False
        self._open_hits = 0
        self._miss_hits = 0
        self.layout = None
        self.candidates.clear()
        self.offer_opened_at = 0.0
        self.last_reroll_at = 0.0
        self.last_click = None
        self.last_card_click = None
        self.recent_stats.clear()
        self._last_closed = None

    def record_click(
        self,
        now: float,
        rel_x: float,
        rel_y: float,
        frame_w: int = BASE_W,
        frame_h: int = BASE_H,
    ) -> None:
        if not self.is_open or self.layout is None:
            return
        zone, slot = classify_click_zone(rel_x, rel_y, self.layout, frame_w, frame_h)
        self.last_click = (now, zone, slot)
        if zone == "card" and slot is not None:
            self.last_card_click = (now, slot)
        elif zone == "reroll":
            self.last_reroll_at = now
            self.recent_stats.clear()

    def process_frame(
        self,
        now: float,
        layout: AugmentLayout | None,
        stats: list[dict[str, float]] | None = None,
        ocr_reader: Callable[[], dict[int, tuple[AugmentInfo | None, float]]] | None = None,
    ) -> VisualFrameOutcome:
        if layout is None:
            self._open_hits = 0
            if not self.is_open:
                return VisualFrameOutcome(panel_open=False, candidates=[])
            self._miss_hits += 1
            if self._miss_hits < 3:
                return VisualFrameOutcome(
                    panel_open=True,
                    candidates=self._candidate_list(),
                )
            return self._close_offer(now)

        # Card borders detected
        self._miss_hits = 0
        self._open_hits += 1
        self.layout = layout
        if stats is not None:
            self.recent_stats.append((now, stats))
            if len(self.recent_stats) > 12:
                self.recent_stats.pop(0)

        reverted_id: int | None = None
        if not self.is_open and self._open_hits >= 1:
            self.is_open = True
            self.offer_opened_at = now
            self.candidates.clear()

        # Run OCR if needed (on open, after reroll, or while slots remain unknown)
        need_ocr = (
            len(self.candidates) < layout.slot_count
            or (now - self.last_reroll_at < 2.0)
        )
        if need_ocr and ocr_reader is not None:
            readings = ocr_reader()
            for slot_idx, (aug, score) in readings.items():
                if aug is None:
                    continue
                prev = self.candidates.get(slot_idx)
                if prev is not None and prev[0].id != aug.id:
                    self.last_reroll_at = now
                    self.recent_stats.clear()
                self.candidates[slot_idx] = (aug, score)

        # Check if an earlier "hide" was misread as a pick when the exact same offer returns within 180s
        if self._last_closed is not None and len(self.candidates) >= 2:
            closed_ids, closed_at, picked_id = self._last_closed
            current_ids = frozenset(aug.id for aug, _ in self.candidates.values())
            if current_ids == closed_ids and (now - closed_at) < 180.0 and picked_id is not None:
                reverted_id = picked_id
                self._last_closed = None
            elif not (current_ids & closed_ids):
                self._last_closed = None

        return VisualFrameOutcome(
            panel_open=self.is_open,
            candidates=self._candidate_list(),
            reverted_augment_id=reverted_id,
        )

    def _close_offer(self, now: float) -> VisualFrameOutcome:
        self.is_open = False
        self._open_hits = 0
        self._miss_hits = 0

        picked: VisualPickResult | None = None
        # 1. Prefer explicit passive card-click tracking if recent & not clicking hide/reroll
        if self.last_click is not None and (now - self.last_click[0]) <= 6.0:
            _, zone, slot = self.last_click
            if zone == "card" and slot is not None and slot in self.candidates:
                picked = VisualPickResult(
                    augment=self.candidates[slot][0],
                    slot_index=slot,
                    source="vision_click",
                )
            elif zone in ("reroll", "hide"):
                # Explicitly clicked hide/reroll before closing -> do not guess a pick
                picked = None
        elif self.last_card_click is not None and (now - self.last_card_click[0]) <= 6.0:
            _, slot = self.last_card_click
            if slot in self.candidates:
                picked = VisualPickResult(
                    augment=self.candidates[slot][0],
                    slot_index=slot,
                    source="vision_click",
                )

        # 2. Fall back to single-card selection flare shortly before closing
        if picked is None and self.candidates:
            picked = self._detect_flare_pick(now)

        closed_ids = frozenset(aug.id for aug, _ in self.candidates.values())
        self._last_closed = (closed_ids, now, picked.augment.id if picked else None)
        self.candidates.clear()
        self.last_click = None
        self.last_card_click = None
        self.recent_stats.clear()
        return VisualFrameOutcome(panel_open=False, candidates=[], picked=picked)

    def _detect_flare_pick(self, now: float) -> VisualPickResult | None:
        if (now - self.last_reroll_at) < 0.7:
            return None
        recent = [(ts, st) for ts, st in self.recent_stats if (now - ts) <= 0.8]
        if not recent:
            return None

        best_margin = 0.0
        best_slot: int | None = None
        for _, stats in recent:
            if len(stats) < 2:
                continue
            ranked = sorted(enumerate(stats), key=lambda item: item[1]["bright_pct"], reverse=True)
            top_slot, top_val = ranked[0]
            second_val = ranked[1][1]["bright_pct"]
            margin = top_val["bright_pct"] - second_val
            if top_val["bright_pct"] >= 18.0 and margin >= 12.0 and margin > best_margin:
                best_margin = margin
                best_slot = top_slot

        if best_slot is not None and best_slot in self.candidates:
            return VisualPickResult(
                augment=self.candidates[best_slot][0],
                slot_index=best_slot,
                source="vision_flare",
            )
        return None

    def _candidate_list(self) -> list[tuple[int, AugmentInfo | None, float]]:
        total = self.layout.slot_count if self.layout else 3
        out: list[tuple[int, AugmentInfo | None, float]] = []
        for idx in range(total):
            item = self.candidates.get(idx)
            if item:
                out.append((idx, item[0], item[1]))
            else:
                out.append((idx, None, 0.0))
        return out
