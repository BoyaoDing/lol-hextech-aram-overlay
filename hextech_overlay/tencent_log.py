"""Read-only Tencent CN iCreate encrypted game log decoder and tail state machine."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import psutil
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

TENCENT_BIZ_ID = "10014"
MAX_LINE_BYTES = 256 * 1024
MAX_READ_BYTES = 256 * 1024
MAX_PARTIAL_BYTES = 256 * 1024
TENCENT_SOURCE_STALE_SECONDS = 15.0
TENCENT_CANDIDATE_MAX_AGE_SECONDS = 60.0
PENDING_PICK_DIFF_WINDOW_SECONDS = 12.0

_TRANSMUTE_ONE_BASES = {
    "transmuteprismatic",
    "transmutegold",
    "transmutesilver",
    "unstabletransmutation",
}


def classify_augment_transform(key: str) -> Literal["normal", "chaos", "transmute", "pandora"]:
    """Classify augments that transform themselves or existing augments in 696 owned lists."""
    base = key.strip()
    if base.lower().startswith("aram_"):
        base = base[5:]
    low = base.lower()
    if low == "transmutechaos":
        return "chaos"
    if low in _TRANSMUTE_ONE_BASES:
        return "transmute"
    if low == "pandorasbox":
        return "pandora"
    return "normal"


def is_stat_anvil_key(key: str) -> bool:
    """Identify Mayhem Stat Anvil choices logged via 693/694 that are not augments."""
    return "statanvil" in (key or "").strip().lower()

_REGION_RE = re.compile(r"^(?:TENCENT|CN|CHINA)$", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(r"\b(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\b")
_CIPHER_RE = re.compile(r"(?:^|\s)([a-fA-F0-9]{32,})\s*$")
_AUGMENT_KEY_RE = re.compile(r"^[A-Za-z0-9_]{0,128}$")
_LOGDIR_ARG_RE = re.compile(r'(?:^|\s)--logDir(?:=|\s+)(?:"([^"]+)"|([^\s]+))', re.IGNORECASE)
_ABS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class TencentOwnedPlayer:
    net_id: int
    keys: list[str]


@dataclass(frozen=True)
class TencentAugmentEvent:
    kind: Literal["candidates", "selected", "owned"]
    at: float
    keys: list[str] = ()  # type: ignore[assignment]
    index: int = -1
    players: list[TencentOwnedPlayer] = ()  # type: ignore[assignment]


@dataclass(frozen=True)
class TencentLogRecord:
    at: float
    sub_cmd: int
    event: TencentAugmentEvent | None
    gdp_enabled: bool | None = None


@dataclass(frozen=True)
class StateAction:
    kind: Literal["candidates", "picked", "reconcile_owned"]
    at: float
    keys: list[str] = ()  # type: ignore[assignment]
    picked_index: int = -1
    picked_key: str | None = None


def is_tencent_region(region: str | None) -> bool:
    return bool(isinstance(region, str) and _REGION_RE.fullmatch(region.strip()))


def derive_tencent_log_key(puuid: str) -> bytes:
    """Derive 16-byte AES-128-CBC key/IV in memory from player PUUID."""
    digest = hashlib.sha256(f"{TENCENT_BIZ_ID}{puuid}".encode("utf-8")).hexdigest()
    return digest[:16].encode("utf-8")


def parse_icreate_log_dir(cmdline: str) -> Path | None:
    """Safely extract local absolute iCreateLogs/main.log path from --logDir."""
    match = _LOGDIR_ARG_RE.search(cmdline or "")
    if not match:
        return None
    directory = (match.group(1) or match.group(2) or "").strip()
    if not directory or len(directory) >= 4096 or "\0" in directory:
        return None
    if not _ABS_DRIVE_RE.match(directory):
        return None
    return Path(directory) / "iCreateLogs" / "main.log"


def locate_icreate_log() -> Path | None:
    """Locate Tencent icreate-sdk main.log without invoking or modifying it."""
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidate = Path(local_app_data) / "icreate-sdk" / "lol" / "iCreateLogs" / "main.log"
        if candidate.is_file():
            return candidate

    try:
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = (proc.info.get("name") or "").lower()
            if name == "icreatelol.exe":
                cmdline = " ".join(proc.info.get("cmdline") or [])
                parsed = parse_icreate_log_dir(cmdline)
                if parsed and parsed.is_file():
                    return parsed
    except Exception:
        pass
    return None


def _is_valid_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def decode_tencent_log_line(line: str, key: bytes) -> TencentLogRecord | None:
    """Decode one encrypted icreate game-log line; return None on non-game or invalid lines."""
    if not line or len(line) > MAX_LINE_BYTES or len(key) != 16:
        return None

    ts_match = _TIMESTAMP_RE.search(line)
    if not ts_match:
        return None
    try:
        at_ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except ValueError:
        return None

    cipher_match = _CIPHER_RE.search(line)
    if not cipher_match:
        return None
    hex_str = cipher_match.group(1)
    if len(hex_str) % 32 != 0:
        return None

    try:
        ciphertext = bytes.fromhex(hex_str)
        cipher = Cipher(algorithms.AES(key), modes.CBC(key))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = (unpadder.update(padded) + unpadder.finalize()).decode("utf-8", errors="strict")
    except Exception:
        return None

    marker = plaintext.find("game-log-> ")
    if marker < 0:
        if "settingEnableChange gdp false" in plaintext:
            return TencentLogRecord(at=at_ts, sub_cmd=-1, event=None, gdp_enabled=False)
        if "settingEnableChange gdp true" in plaintext:
            return TencentLogRecord(at=at_ts, sub_cmd=-1, event=None, gdp_enabled=True)
        return None

    try:
        data = json.loads(plaintext[marker + 11 :])
    except ValueError:
        return None

    if not isinstance(data, dict) or data.get("form") != "game":
        return None
    sub_cmd = data.get("dwSubCmd")
    params = data.get("params")
    if not _is_valid_int(sub_cmd) or not isinstance(params, dict):
        return None

    event: TencentAugmentEvent | None = None
    if sub_cmd == 693:
        raw_list = params.get("astAugments")
        if not isinstance(raw_list, list) or not (3 <= len(raw_list) <= 5):
            return None
        keys: list[str] = []
        for item in raw_list:
            if not isinstance(item, dict):
                return None
            k = item.get("szAugmentName")
            if not isinstance(k, str) or not _AUGMENT_KEY_RE.fullmatch(k):
                return None
            keys.append(k)
        event = TencentAugmentEvent(kind="candidates", at=at_ts, keys=keys)

    elif sub_cmd == 694:
        idx = params.get("dwAugmentIndex")
        if not _is_valid_int(idx) or not (0 <= idx <= 4):
            return None
        event = TencentAugmentEvent(kind="selected", at=at_ts, index=idx)

    elif sub_cmd == 696:
        raw_players = params.get("astPlayerHextechAugmentSelectionList")
        if not isinstance(raw_players, list) or len(raw_players) > 10:
            return None
        players: list[TencentOwnedPlayer] = []
        for entry in raw_players:
            if not isinstance(entry, dict):
                return None
            net_id = entry.get("dwHeroNetID")
            aug_list = entry.get("astAugments")
            if not _is_valid_int(net_id) or not isinstance(aug_list, list) or len(aug_list) > 6:
                return None
            p_keys: list[str] = []
            for aug_item in aug_list:
                if not isinstance(aug_item, dict):
                    return None
                k = aug_item.get("szAugmentName")
                if not isinstance(k, str) or not _AUGMENT_KEY_RE.fullmatch(k):
                    return None
                p_keys.append(k)
            players.append(TencentOwnedPlayer(net_id=net_id, keys=p_keys))
        event = TencentAugmentEvent(kind="owned", at=at_ts, players=players)

    return TencentLogRecord(at=at_ts, sub_cmd=sub_cmd, event=event, gdp_enabled=True)


def probe_tencent_gdp_enabled(file_path: Path, key: bytes, max_bytes: int = MAX_READ_BYTES) -> bool | None:
    """Inspect recent icreate main.log tail to see if highlight event capture (gdp) is enabled."""
    if len(key) != 16:
        return None
    try:
        path = Path(file_path)
        if not path.is_file():
            return None
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            offset = max(0, size - max_bytes)
            handle.seek(offset, os.SEEK_SET)
            raw = handle.read(max_bytes)
    except OSError:
        return None

    lines = raw.decode("utf-8", errors="replace").splitlines()
    if offset > 0 and lines:
        lines = lines[1:]
    for ln in reversed(lines):
        if not ln.strip():
            continue
        rec = decode_tencent_log_line(ln, key)
        if rec is None:
            continue
        if rec.gdp_enabled is not None:
            return rec.gdp_enabled
        if rec.sub_cmd >= 0:
            return True
    return None


class TencentLogTail:
    """Bounded read-only log tail with rotation and truncation detection."""

    def __init__(self, file_path: Path, start_at_end: bool = True) -> None:
        self.file_path = Path(file_path)
        self.start_at_end = start_at_end
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._anchor = b""
        self._partial = ""
        self._discard_partial = False

    def reset_position(self) -> None:
        self._identity = None
        self._offset = 0
        self._anchor = b""
        self._partial = ""
        self._discard_partial = False

    def read_new_lines(self) -> tuple[list[str], bool]:
        if not self.file_path.is_file():
            raise FileNotFoundError(str(self.file_path))

        with open(self.file_path, "rb") as handle:
            st = os.fstat(handle.fileno())
            ident = (st.st_dev, st.st_ino)
            size = st.st_size

            # On initial open with start_at_end, jump cleanly to EOF so old matches aren't replayed.
            if self._identity is None and self.start_at_end:
                self._identity = ident
                self._offset = size
                if size > 0:
                    anchor_len = min(64, size)
                    handle.seek(size - anchor_len, os.SEEK_SET)
                    self._anchor = handle.read(anchor_len)
                return [], False

            reset = self._identity is not None and (ident != self._identity or size < self._offset)
            if not reset and self._anchor:
                handle.seek(self._offset - len(self._anchor), os.SEEK_SET)
                if handle.read(len(self._anchor)) != self._anchor:
                    reset = True

            if self._identity is None or reset or size - self._offset > MAX_READ_BYTES:
                if self._identity is not None:
                    reset = True
                self._offset = max(0, size - MAX_READ_BYTES)
                self._partial = ""
                self._discard_partial = self._offset > 0
                self._anchor = b""

            self._identity = ident
            to_read = min(MAX_READ_BYTES, max(0, size - self._offset))
            if to_read == 0:
                return [], reset

            handle.seek(self._offset, os.SEEK_SET)
            buf = handle.read(to_read)
            self._offset += len(buf)
            if buf:
                self._anchor = buf[-64:]

        text = self._partial + buf.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._partial = lines.pop() if lines else ""
        if self._discard_partial and lines:
            lines.pop(0)
            self._discard_partial = False
        if len(self._partial) > MAX_PARTIAL_BYTES:
            self._partial = ""
            self._discard_partial = True
            reset = True

        return [ln.rstrip("\r") for ln in lines if ln.strip()], reset


@dataclass(frozen=True)
class _PendingPick:
    at: float
    key: str
    kind: Literal["normal", "chaos", "transmute", "pandora"]
    prev_snapshot: dict[int, list[str]]


class TencentAugmentStateMachine:
    """Correlates 693 candidates, 694 local selections, and 696 safe player reconciliation."""

    def __init__(self) -> None:
        self._candidates: tuple[float, list[str]] | None = None
        self._last_nonempty: tuple[float, list[str]] | None = None
        self._last_record_at: float = 0.0
        self._last_event_at: float = 0.0
        self._last_selection_at: float = 0.0
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._confirmed_stable_keys: list[str] = []
        self._local_hero_net_id: int | None = None
        self._last_owned_keys: list[str] | None = None
        self._last_all_players: dict[int, list[str]] = {}
        self._candidate_net_ids: set[int] | None = None
        self._pending_pick: _PendingPick | None = None
        self._ignoring_anvil_offer: bool = False

    @property
    def local_hero_net_id(self) -> int | None:
        return self._local_hero_net_id

    @local_hero_net_id.setter
    def local_hero_net_id(self, value: int | None) -> None:
        self._local_hero_net_id = value

    def reset(self, *, keep_picks: bool = False) -> None:
        self._candidates = None
        self._last_nonempty = None
        self._last_record_at = 0.0
        self._last_event_at = 0.0
        self._last_selection_at = 0.0
        self._seen.clear()
        self._pending_pick = None
        self._ignoring_anvil_offer = False
        if not keep_picks:
            self._confirmed_stable_keys.clear()
            self._local_hero_net_id = None
            self._last_owned_keys = None
            self._last_all_players.clear()
            self._candidate_net_ids = None

    def seed_confirmed_keys(self, keys: list[str], local_hero_net_id: int | None = None) -> None:
        self._confirmed_stable_keys = [
            k for k in keys if k and classify_augment_transform(k) == "normal" and not is_stat_anvil_key(k)
        ]
        self._local_hero_net_id = local_hero_net_id
        self._candidate_net_ids = {local_hero_net_id} if local_hero_net_id is not None else None

    def is_stale(self, now: float) -> bool:
        return self._last_record_at <= 0 or (now - self._last_record_at) > TENCENT_SOURCE_STALE_SECONDS

    def observe(self, record: TencentLogRecord) -> list[StateAction]:
        if record.sub_cmd < 0:
            return []

        if self._last_record_at and record.at - self._last_record_at > TENCENT_SOURCE_STALE_SECONDS:
            self._candidates = None
            self._last_nonempty = None
        self._last_record_at = max(self._last_record_at, record.at)

        event = record.event
        if event is None:
            return []

        if event.kind == "owned":
            players_sig = tuple((p.net_id, tuple(p.keys)) for p in event.players)
            sig = f"{event.at:.3f}:owned:{hash(players_sig)}:{len(players_sig)}:{players_sig}"
        else:
            sig = f"{event.at:.3f}:{event.kind}:{event.index}:{tuple(event.keys)}"

        if sig in self._seen or event.at + 1e-3 < self._last_event_at:
            return []
        self._seen[sig] = None
        if len(self._seen) > 128:
            self._seen.popitem(last=False)
        self._last_event_at = max(self._last_event_at, event.at)

        actions: list[StateAction] = []
        if event.kind == "candidates":
            if event.at <= self._last_selection_at:
                return []
            keys = self._trim_trailing_empty(event.keys)
            non_empty = [k for k in keys if k]
            if non_empty and all(is_stat_anvil_key(k) for k in non_empty):
                self._ignoring_anvil_offer = True
                self._candidates = None
                self._last_nonempty = None
                return []
            if non_empty:
                self._ignoring_anvil_offer = False
                self._candidates = (event.at, keys)
                self._last_nonempty = (event.at, keys)
                actions.append(StateAction(kind="candidates", at=event.at, keys=keys))
            else:
                if self._ignoring_anvil_offer:
                    self._candidates = None
                    return []
                # Empty 693 can arrive right before 694; clear UI candidates while retaining _last_nonempty.
                self._candidates = None
                actions.append(StateAction(kind="candidates", at=event.at, keys=[]))

        elif event.kind == "selected":
            if self._ignoring_anvil_offer:
                self._ignoring_anvil_offer = False
                self._last_selection_at = event.at
                self._candidates = None
                self._last_nonempty = None
                return []

            picked_key: str | None = None
            if self._last_nonempty is not None:
                cand_at, cand_keys = self._last_nonempty
                if event.at - cand_at <= TENCENT_CANDIDATE_MAX_AGE_SECONDS and 0 <= event.index < len(cand_keys):
                    picked_key = cand_keys[event.index] or None
            self._last_selection_at = event.at
            self._candidates = None
            self._last_nonempty = None
            if picked_key and is_stat_anvil_key(picked_key):
                return []

            transform_kind = classify_augment_transform(picked_key or "")
            if picked_key:
                if transform_kind == "normal":
                    self._confirmed_stable_keys.append(picked_key)
                elif transform_kind == "pandora":
                    self._confirmed_stable_keys.clear()
            self._pending_pick = _PendingPick(
                at=event.at,
                key=picked_key or "",
                kind=transform_kind,
                prev_snapshot={nid: list(ks) for nid, ks in self._last_all_players.items()},
            )

            actions.append(
                StateAction(
                    kind="picked",
                    at=event.at,
                    picked_index=event.index,
                    picked_key=picked_key,
                )
            )

        elif event.kind == "owned":
            reconciled = self._handle_owned_event(event.at, event.players)
            if reconciled is not None:
                actions.append(StateAction(kind="reconcile_owned", at=event.at, keys=reconciled))

        return actions

    def current_candidates(self, now: float) -> list[str]:
        if self.is_stale(now) or self._candidates is None:
            return []
        cand_at, keys = self._candidates
        if now - cand_at > TENCENT_CANDIDATE_MAX_AGE_SECONDS:
            return []
        return list(keys)

    @staticmethod
    def _trim_trailing_empty(keys: list[str]) -> list[str]:
        end = len(keys)
        while end > 0 and not keys[end - 1]:
            end -= 1
        return list(keys[:end])

    @staticmethod
    def _matches_diff(prev: list[str], curr: list[str], pending: _PendingPick) -> bool:
        if pending.kind == "chaos":
            return (
                (len(curr) >= len(prev) + 2 and curr[: len(prev)] == prev)
                or (bool(pending.key) and pending.key in curr and pending.key not in prev)
            )
        if pending.kind == "transmute":
            return len(curr) >= len(prev) + 1 and curr[: len(prev)] == prev and curr != prev
        if pending.kind == "pandora":
            return (
                len(curr) >= max(1, len(prev))
                and curr != prev
                and (not prev or curr[: len(prev)] != prev or len(curr) > len(prev))
            )
        # normal pick
        if pending.key:
            return len(curr) >= len(prev) + 1 and curr.count(pending.key) > prev.count(pending.key)
        return len(curr) >= len(prev) + 1

    def _handle_owned_event(self, event_at: float, players: list[TencentOwnedPlayer]) -> list[str] | None:
        """Bind local_hero_net_id via stable history or 694-to-696 delta narrowing."""
        curr_map: dict[int, list[str]] = {
            p.net_id: [k for k in p.keys if k]
            for p in players
        }

        if self._local_hero_net_id is None:
            stable_matches: set[int] = set()
            if self._confirmed_stable_keys:
                stable_matches = {
                    nid
                    for nid, owned in curr_map.items()
                    if len(owned) >= len(self._confirmed_stable_keys)
                    and self._matches_prefix(owned, self._confirmed_stable_keys)
                }

            pending = self._pending_pick
            if pending is not None and (event_at - pending.at) > PENDING_PICK_DIFF_WINDOW_SECONDS:
                self._pending_pick = None
                pending = None

            diff_matches: set[int] = set()
            if pending is not None and 0.0 <= (event_at - pending.at) <= PENDING_PICK_DIFF_WINDOW_SECONDS:
                diff_matches = {
                    nid
                    for nid, owned in curr_map.items()
                    if self._matches_diff(pending.prev_snapshot.get(nid, []), owned, pending)
                }

            if diff_matches:
                # Consume pending pick only once 696 reflects a matching transition.
                self._pending_pick = None
                if stable_matches and (diff_matches & stable_matches):
                    round_set = diff_matches & stable_matches
                else:
                    round_set = diff_matches

                if self._candidate_net_ids is None:
                    self._candidate_net_ids = round_set
                else:
                    narrowed = self._candidate_net_ids & round_set
                    self._candidate_net_ids = narrowed if narrowed else round_set
            elif (pending is None or pending.kind == "normal") and stable_matches:
                if self._candidate_net_ids is None:
                    self._candidate_net_ids = stable_matches
                else:
                    narrowed = self._candidate_net_ids & stable_matches
                    self._candidate_net_ids = narrowed if narrowed else stable_matches

            if self._candidate_net_ids is not None and len(self._candidate_net_ids) == 1:
                self._local_hero_net_id = next(iter(self._candidate_net_ids))

        self._last_all_players = curr_map

        if self._local_hero_net_id is None:
            return None

        owned = curr_map.get(self._local_hero_net_id)
        if not owned or owned == self._last_owned_keys:
            return None
        self._last_owned_keys = list(owned)
        return owned

    @staticmethod
    def _matches_prefix(owned: list[str], confirmed: list[str]) -> bool:
        if owned[: len(confirmed)] == confirmed:
            return True
        it = iter(owned)
        return all(item in it for item in confirmed)
