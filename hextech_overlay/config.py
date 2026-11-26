"""Persistent configuration and mid-game session state storage."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .models import OverlayConfig, PickedAugment


class ConfigStore:
    """Thread-safe JSON persistence for OverlayConfig."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> OverlayConfig:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "display_mode" not in raw and "show_description" in raw:
                    raw["display_mode"] = "detailed" if raw.get("show_description") else "name_only"
                return OverlayConfig.model_validate(raw)
            except Exception:
                pass
        cfg = OverlayConfig()
        self._save_unlocked(cfg)
        return cfg

    def _save_unlocked(self, cfg: OverlayConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self) -> OverlayConfig:
        with self._lock:
            return self._config.model_copy()

    def update(self, patch: dict[str, Any]) -> OverlayConfig:
        with self._lock:
            merged = self._config.model_dump()
            cleaned = {k: v for k, v in patch.items() if v is not None}
            merged.update(cleaned)
            if "display_mode" in cleaned and "show_description" not in cleaned:
                merged["show_description"] = cleaned["display_mode"] != "name_only"
            elif "show_description" in cleaned and "display_mode" not in cleaned:
                if not cleaned["show_description"]:
                    merged["display_mode"] = "name_only"
                elif merged.get("display_mode") == "name_only":
                    merged["display_mode"] = "detailed"
            cfg = OverlayConfig.model_validate(merged)
            self._save_unlocked(cfg)
            self._config = cfg
            return cfg.model_copy()


class SessionStore:
    """Persists current match picks so mid-game restarts keep the overlay list."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def save(
        self,
        *,
        champion_alias: str,
        game_mode: str,
        game_time: float,
        picks: list[PickedAugment],
        local_hero_net_id: int | None = None,
    ) -> None:
        payload = {
            "saved_at": time.time(),
            "champion_alias": champion_alias,
            "game_mode": game_mode,
            "game_time": game_time,
            "local_hero_net_id": local_hero_net_id,
            "picks": [p.model_dump() for p in picks],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def try_restore(
        self,
        *,
        champion_alias: str,
        game_mode: str,
        game_time: float,
        max_age_seconds: float = 3600.0,
    ) -> tuple[list[PickedAugment], int | None] | None:
        with self._lock:
            if not self.path.exists():
                return None
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return None

        if time.time() - float(raw.get("saved_at") or 0) > max_age_seconds:
            return None
        if raw.get("game_mode") != game_mode:
            return None
        saved_champ = raw.get("champion_alias") or ""
        if saved_champ and champion_alias and saved_champ != champion_alias:
            return None
        saved_time = float(raw.get("game_time") or 0.0)
        if game_time + 15.0 < saved_time:
            return None

        picks: list[PickedAugment] = []
        for item in raw.get("picks") or []:
            try:
                picks.append(PickedAugment.model_validate(item))
            except Exception:
                continue
        net_id = raw.get("local_hero_net_id")
        return picks, (int(net_id) if isinstance(net_id, int) else None)

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
