"""Read-only official Riot Live Client Data API (127.0.0.1:2999) poller."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LIVE_ALL_GAME_DATA_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"


@dataclass(frozen=True)
class LiveSnapshot:
    game_mode: str
    game_time: float
    level: int
    champion_alias: str
    champion_name: str
    summoner_name: str
    is_mayhem: bool


class LiveClientApi:
    """Polls 127.0.0.1:2999/liveclientdata/allgamedata for game mode, level, and champion."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.trust_env = False  # Bypass Clash/system proxy for localhost
        self._session.verify = False

    def poll(self, allow_non_kiwi_mode: bool = False) -> LiveSnapshot | None:
        try:
            resp = self._session.get(LIVE_ALL_GAME_DATA_URL, timeout=1.2)
            if resp.status_code != 200:
                return None
            return self.parse_payload(resp.json(), allow_non_kiwi_mode=allow_non_kiwi_mode)
        except (requests.RequestException, ValueError):
            return None

    @staticmethod
    def parse_payload(payload: dict[str, Any], allow_non_kiwi_mode: bool = False) -> LiveSnapshot | None:
        if not isinstance(payload, dict):
            return None
        game_data = payload.get("gameData") or {}
        active = payload.get("activePlayer") or {}
        players = payload.get("allPlayers") or []

        game_mode = str(game_data.get("gameMode") or "")
        game_time = float(game_data.get("gameTime") or 0.0)
        level = int(active.get("level") or 0)

        active_ids = {
            str(active.get(k)).strip()
            for k in ("riotId", "summonerName", "riotIdGameName")
            if active.get(k)
        }
        mine: dict[str, Any] | None = None
        for p in players:
            if not isinstance(p, dict):
                continue
            p_ids = {
                str(p.get(k)).strip()
                for k in ("riotId", "summonerName", "riotIdGameName")
                if p.get(k)
            }
            if active_ids & p_ids:
                mine = p
                break

        raw_champ = str((mine or {}).get("rawChampionName") or "")
        alias = re.sub(r"^game_character_displayname_", "", raw_champ).strip()
        champ_name = str((mine or {}).get("championName") or alias or "")
        summoner_name = str(
            active.get("riotIdGameName")
            or active.get("summonerName")
            or (mine or {}).get("summonerName")
            or ""
        )

        mode_upper = game_mode.upper()
        is_mayhem = mode_upper == "KIWI" or (allow_non_kiwi_mode and mode_upper in ("KIWI", "ARAM", "CHERRY", "PRACTICETOOL"))
        return LiveSnapshot(
            game_mode=game_mode,
            game_time=game_time,
            level=level,
            champion_alias=alias,
            champion_name=champ_name,
            summoner_name=summoner_name,
            is_mayhem=is_mayhem,
        )
