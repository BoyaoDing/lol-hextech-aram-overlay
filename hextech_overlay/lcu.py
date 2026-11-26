"""Read-only League Client Update (LCU) local API connector."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any

import psutil
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

COMMON_LOCKFILE_PATHS = (
    Path(r"C:\WeGameApps\英雄联盟\LeagueClient\lockfile"),
    Path(r"D:\WeGameApps\英雄联盟\LeagueClient\lockfile"),
    Path(r"E:\WeGameApps\英雄联盟\LeagueClient\lockfile"),
    Path(r"C:\Program Files\Tencent\英雄联盟\LeagueClient\lockfile"),
    Path(r"D:\Program Files\Tencent\英雄联盟\LeagueClient\lockfile"),
    Path(r"C:\Riot Games\League of Legends\lockfile"),
    Path(r"D:\Riot Games\League of Legends\lockfile"),
)


class LcuClient:
    """Strictly read-only LCU connector (discovers port/token in memory only)."""

    def __init__(self) -> None:
        self._port: int | None = None
        self._token: str = ""
        self._session = requests.Session()
        self._session.trust_env = False  # Never route 127.0.0.1 through system proxies
        self._session.verify = False

    def is_connected(self) -> bool:
        return self._port is not None and bool(self._token)

    def disconnect(self) -> None:
        self._port = None
        self._token = ""
        self._session.headers.pop("Authorization", None)

    def _set_credentials(self, port: int, token: str) -> None:
        self._port = port
        self._token = token
        auth = base64.b64encode(f"riot:{token}".encode("utf-8")).decode("ascii")
        self._session.headers["Authorization"] = f"Basic {auth}"

    def discover(self) -> bool:
        """Discover active LeagueClientUx process credentials or lockfile."""
        if self.is_connected() and self.get_gameflow_phase() is not None:
            return True

        # 1. Process command line discovery (preferred; works without knowing install folder)
        candidate_lockfiles: list[Path] = []
        try:
            for proc in psutil.process_iter(["name", "cmdline", "exe"]):
                name = (proc.info.get("name") or "").lower()
                if name in ("leagueclientux.exe", "leagueclientuxrender.exe"):
                    cmdline = " ".join(proc.info.get("cmdline") or [])
                    port_m = re.search(r"--app-port=(\d+)", cmdline)
                    tok_m = re.search(r"--remoting-auth-token=([^\s\"']+)", cmdline)
                    if port_m and tok_m:
                        self._set_credentials(int(port_m.group(1)), tok_m.group(1))
                        if self.get_gameflow_phase() is not None:
                            return True
                    exe = proc.info.get("exe")
                    if exe:
                        candidate_lockfiles.append(Path(exe).resolve().parent / "lockfile")
                elif name == "leagueclient.exe":
                    exe = proc.info.get("exe")
                    if exe:
                        candidate_lockfiles.append(Path(exe).resolve().parent / "lockfile")
        except Exception:
            pass

        # 2. Lockfile fallback
        for lf in [*candidate_lockfiles, *COMMON_LOCKFILE_PATHS]:
            if self._try_lockfile(lf):
                return True

        self.disconnect()
        return False

    def _try_lockfile(self, path: Path) -> bool:
        try:
            if not path.exists():
                return False
            parts = path.read_text(encoding="utf-8", errors="ignore").strip().split(":")
            if len(parts) < 5:
                return False
            port = int(parts[2])
            token = parts[3].strip()
            if port <= 0 or not token:
                return False
            self._set_credentials(port, token)
            return self.get_gameflow_phase() is not None
        except Exception:
            return False

    def get_json(self, endpoint: str, timeout: float = 1.5) -> Any | None:
        if not self.is_connected():
            return None
        path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        try:
            resp = self._session.get(f"https://127.0.0.1:{self._port}{path}", timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (401, 403):
                self.disconnect()
        except requests.RequestException:
            self.disconnect()
        except ValueError:
            pass
        return None

    def get_bytes(self, endpoint: str, timeout: float = 2.0) -> bytes | None:
        if not self.is_connected():
            return None
        path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        try:
            resp = self._session.get(f"https://127.0.0.1:{self._port}{path}", timeout=timeout)
            if resp.status_code == 200:
                return resp.content
        except requests.RequestException:
            pass
        return None

    def get_gameflow_phase(self) -> str | None:
        data = self.get_json("/lol-gameflow/v1/gameflow-phase", timeout=1.0)
        return data if isinstance(data, str) else None

    def get_region_locale(self) -> tuple[str, str]:
        data = self.get_json("/riotclient/region-locale", timeout=1.2)
        if not isinstance(data, dict):
            return "", ""
        return str(data.get("region") or ""), str(data.get("locale") or "")

    def get_current_summoner(self) -> dict[str, str] | None:
        data = self.get_json("/lol-summoner/v1/current-summoner", timeout=1.2)
        if not isinstance(data, dict):
            return None
        puuid = str(data.get("puuid") or "").strip()
        display = str(data.get("gameName") or data.get("displayName") or "").strip()
        return {"puuid": puuid, "display_name": display} if puuid else None
