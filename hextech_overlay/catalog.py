"""Augment catalog, offline cache, fuzzy OCR title matching, and icon proxy cache."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import threading
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any

from .models import AugmentInfo, Rarity

BUNDLED_CATALOG_PATH = Path(__file__).resolve().parent / "assets" / "bundled_augments.zh_cn.json"

OCR_PUNCTUATION_RE = re.compile(
    r"""[\s"'“”‘’`.,，。:：;；!！?？、|｜/\\()[\]{}<>《》【】「」『』\-_=+~·•]+"""
)

OCR_BLACKLIST = {
    "攻击", "防御", "生命", "法术", "魔法", "伤害", "护甲", "魔抗",
    "技能", "冷却", "移速", "暴击", "吸血", "穿透", "治疗", "护盾",
    "功能", "能力", "效果", "被动", "主动", "额外", "持续", "提供",
    "增加", "获得", "造成", "刷新", "重随", "隐藏", "收起", "强化",
    "符文", "海克斯", "白银", "黄金", "棱彩", "选择",
}

RARITY_MAP: dict[str, Rarity] = {
    "kSilver": "silver",
    "kGold": "gold",
    "kPrismatic": "prismatic",
    "silver": "silver",
    "gold": "gold",
    "prismatic": "prismatic",
}


def normalize_title(text: str) -> str:
    """NFKC-normalize and strip whitespace/punctuation for robust OCR comparison."""
    if not text:
        return ""
    normed = unicodedata.normalize("NFKC", text)
    return OCR_PUNCTUATION_RE.sub("", normed).strip().lower()


def clean_description(raw_desc: str, data_values: dict[str, Any] | None = None) -> str:
    """Strip Riot tooltip XML/icon macros and substitute simple @Var@ placeholders."""
    if not raw_desc:
        return ""

    values_map: dict[str, list[float]] = {}
    if isinstance(data_values, dict):
        for k, v in data_values.items():
            if isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v):
                values_map[k.lower()] = [float(x) for x in v]
            elif isinstance(v, (int, float)):
                values_map[k.lower()] = [float(v)]

    def _fmt(val: float, is_pct: bool = False) -> str:
        if is_pct:
            pct = val * 100.0
            return f"{int(round(pct))}%" if abs(pct - round(pct)) < 0.05 else f"{pct:.1f}%"
        if abs(val - round(val)) < 0.01:
            return str(int(round(val)))
        return f"{val:.1f}".rstrip("0").rstrip(".")

    def _replace_var(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        mult = 1.0
        for suffix, factor in (("*100", 100.0), ("*-100", -100.0), ("*10", 10.0)):
            if suffix in expr:
                mult = factor
                expr = expr.replace(suffix, "").strip()
                break
        key = expr.split(".", 1)[0].lower()
        vals = values_map.get(key)
        if vals is None:
            for k, arr in values_map.items():
                if k == key or key.endswith(k) or k.endswith(key):
                    vals = arr
                    break
        if vals:
            v0 = vals[0] * mult
            if mult == 1.0 and 0 < abs(v0) < 1.0 and any(
                h in key for h in ("pct", "percent", "ratio", "chance", "boost", "amp", "haste", "rate")
            ):
                return _fmt(v0, is_pct=True)
            return _fmt(v0, is_pct=False)
        return "一定"

    text = re.sub(r"%i:[^%]+%", "", raw_desc)
    text = re.sub(r"@([^@]+)@", _replace_var, text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("一定%", "一定比例")


def cdragon_icon_urls(icon_path: str) -> list[str]:
    """Convert an LCU /lol-game-data/assets/... path into lowercase CDragon fallback URLs."""
    clean = icon_path.strip().lstrip("/")
    if not clean:
        return []
    lower = clean.lower()
    urls: list[str] = []
    if lower.startswith("lol-game-data/assets/"):
        rel = lower[len("lol-game-data/assets/") :]
        urls.append(f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/{rel}")
        if rel.startswith("assets/"):
            urls.append(f"https://raw.communitydragon.org/latest/game/{rel}")
    elif lower.startswith("assets/"):
        urls.append(f"https://raw.communitydragon.org/latest/game/{lower}")
    else:
        urls.append(f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/{lower}")
    return urls


class AugmentCatalog:
    """In-memory augment dictionary with LCU/CDragon refresh, OCR matching, and icon cache."""

    def __init__(self, cache_dir: Path, bundled_path: Path = BUNDLED_CATALOG_PATH) -> None:
        self.cache_dir = Path(cache_dir)
        self.icons_dir = self.cache_dir / "icons"
        self.bundled_path = Path(bundled_path)
        self._lock = threading.RLock()
        self._augments: list[AugmentInfo] = []
        self._by_id: dict[int, AugmentInfo] = {}
        self._by_key: dict[str, AugmentInfo] = {}
        self._by_key_lower: dict[str, AugmentInfo] = {}
        self._by_norm_name: dict[str, list[AugmentInfo]] = {}
        self._sorted_norm_names: list[str] = []
        self.mayhem_pool_count = 0
        self._load_initial()

    def _load_initial(self) -> None:
        records: list[AugmentInfo] = []
        bundled_desc: dict[int, str] = {}
        bundled_mayhem: set[str] = set()

        if self.bundled_path.exists():
            raw = json.loads(self.bundled_path.read_text(encoding="utf-8"))
            for item in raw.get("augments") or []:
                aug = AugmentInfo.model_validate(item)
                aug.description = clean_description(aug.description)
                records.append(aug)
                if aug.description:
                    bundled_desc[aug.id] = aug.description
                if aug.in_mayhem_pool:
                    bundled_mayhem.add(aug.key)

        cached_file = self.cache_dir / "catalog.zh_cn.json"
        if cached_file.exists():
            try:
                cached = json.loads(cached_file.read_text(encoding="utf-8"))
                cached_records: list[AugmentInfo] = []
                for item in cached.get("augments") or []:
                    aug = AugmentInfo.model_validate(item)
                    if not aug.description and aug.id in bundled_desc:
                        aug.description = bundled_desc[aug.id]
                    if not aug.in_mayhem_pool and aug.key in bundled_mayhem:
                        aug.in_mayhem_pool = True
                    cached_records.append(aug)
                if len(cached_records) >= len(records):
                    records = cached_records
            except Exception:
                pass

        self._rebuild_index(records)

    def _rebuild_index(self, records: list[AugmentInfo]) -> None:
        # Prefer active Mayhem pool and newer (>=1000) augment IDs first
        ordered = sorted(records, key=lambda a: (0 if a.in_mayhem_pool else 1, -a.id))
        by_id: dict[int, AugmentInfo] = {}
        by_key: dict[str, AugmentInfo] = {}
        by_key_lower: dict[str, AugmentInfo] = {}
        by_norm: dict[str, list[AugmentInfo]] = {}

        for aug in ordered:
            by_id[aug.id] = aug
            by_key[aug.key] = aug
            by_key_lower[aug.key.lower()] = aug
            norm = normalize_title(aug.name)
            if norm:
                by_norm.setdefault(norm, []).append(aug)

        with self._lock:
            self._augments = ordered
            self._by_id = by_id
            self._by_key = by_key
            self._by_key_lower = by_key_lower
            self._by_norm_name = by_norm
            self._sorted_norm_names = sorted(by_norm.keys(), key=len, reverse=True)
            self.mayhem_pool_count = sum(1 for a in ordered if a.in_mayhem_pool)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._augments)

    def all_augments(self) -> list[AugmentInfo]:
        with self._lock:
            return list(self._augments)

    def get_by_id(self, aug_id: int) -> AugmentInfo | None:
        with self._lock:
            return self._by_id.get(aug_id)

    def get_by_key(self, key: str) -> AugmentInfo | None:
        if not key:
            return None
        clean = key.strip()
        with self._lock:
            hit = self._by_key.get(clean) or self._by_key_lower.get(clean.lower())
            if hit:
                return hit
            if clean.upper().startswith("ARAM_"):
                return self._by_key_lower.get(clean[5:].lower())
            return self._by_key_lower.get(f"aram_{clean.lower()}")

    def _pick_preferred(self, candidates: list[AugmentInfo]) -> AugmentInfo:
        return sorted(candidates, key=lambda a: (0 if a.in_mayhem_pool else 1, -a.id))[0]

    def match_ocr_text(self, raw_text: str) -> tuple[AugmentInfo | None, float]:
        """Match raw OCR line against augment names, refusing ambiguous ties."""
        norm = normalize_title(raw_text)
        if len(norm) < 2 or norm in OCR_BLACKLIST:
            return None, 0.0

        with self._lock:
            by_norm = self._by_norm_name
            sorted_names = self._sorted_norm_names

        # 1. Exact full match
        if norm in by_norm:
            return self._pick_preferred(by_norm[norm]), 1.0

        # 2. Substring match (from longest name to shortest)
        substr_hits: list[tuple[str, AugmentInfo, float]] = []
        for nm in sorted_names:
            if len(nm) < 3:
                continue
            if nm in norm:
                score = min(0.99, len(nm) / max(len(norm), len(nm)) + 0.15)
                substr_hits.append((nm, self._pick_preferred(by_norm[nm]), round(score, 4)))
        if substr_hits:
            # Prefer longest matched name; check if two distinct names share exact top score & length
            best_len = len(substr_hits[0][0])
            top_same_len = [h for h in substr_hits if len(h[0]) == best_len]
            distinct = {h[0] for h in top_same_len}
            if len(distinct) > 1:
                mayhem_only = [h for h in top_same_len if h[1].in_mayhem_pool]
                if len({h[0] for h in mayhem_only}) == 1:
                    return mayhem_only[0][1], mayhem_only[0][2]
                return None, 0.0
            return top_same_len[0][1], top_same_len[0][2]

        # 3. Sliding-window fuzzy match (handles 1-char OCR misreads)
        if len(norm) > 20:
            return None, 0.0

        scored: list[tuple[float, int, str, AugmentInfo]] = []
        for nm in sorted_names:
            nlen = len(nm)
            if nlen < 2 or abs(nlen - len(norm)) > 5:
                continue
            if nlen == 2:
                # 2-char names are too short for fuzzy edits unless exact (already checked)
                continue
            # Minimum character overlap fast filter
            if len(set(nm) & set(norm)) < max(2, int(0.5 * len(set(nm)))):
                continue

            best_ratio = difflib.SequenceMatcher(None, norm, nm).ratio()
            if len(norm) > nlen:
                for i in range(0, len(norm) - nlen + 1):
                    win = norm[i : i + nlen]
                    r = difflib.SequenceMatcher(None, win, nm).ratio()
                    if r > best_ratio:
                        best_ratio = r
            threshold = 0.66 if nlen == 3 else 0.72
            if best_ratio >= threshold:
                aug = self._pick_preferred(by_norm[nm])
                scored.append((round(best_ratio, 6), 1 if aug.in_mayhem_pool else 0, nm, aug))

        if not scored:
            return None, 0.0

        scored.sort(key=lambda x: (-x[0], -x[1], -len(x[2])))
        top_score = scored[0][0]
        tied = [s for s in scored if abs(s[0] - top_score) < 1e-5]
        if len({s[2] for s in tied}) > 1:
            # Break tie only if exactly one tied name is in the active Mayhem pool
            mayhem_tied = [s for s in tied if s[1] == 1]
            if len({s[2] for s in mayhem_tied}) == 1:
                return mayhem_tied[0][3], mayhem_tied[0][0]
            return None, 0.0
        return scored[0][3], scored[0][0]

    def search(self, query: str = "", rarity: str | None = None, only_mayhem: bool = False, limit: int = 30) -> list[AugmentInfo]:
        q = normalize_title(query)
        q_raw = query.strip().lower()
        results: list[tuple[int, int, AugmentInfo]] = []

        for aug in self.all_augments():
            if rarity and aug.rarity != rarity:
                continue
            if only_mayhem and not aug.in_mayhem_pool:
                continue
            if not q and not q_raw:
                results.append((0, 0 if aug.in_mayhem_pool else 1, aug))
                continue
            norm_name = normalize_title(aug.name)
            key_l = aug.key.lower()
            desc_l = aug.description.lower()
            if q and norm_name == q:
                rank = 0
            elif q and norm_name.startswith(q):
                rank = 1
            elif q and q in norm_name:
                rank = 2
            elif q_raw and q_raw in key_l:
                rank = 3
            elif q_raw and q_raw in desc_l:
                rank = 4
            else:
                continue
            results.append((rank, 0 if aug.in_mayhem_pool else 1, aug))

        results.sort(key=lambda item: (item[0], item[1], -item[2].id))
        return [item[2] for item in results[:limit]]

    def refresh_from_lcu(self, lcu_client: Any, locale: str = "zh_cn") -> tuple[bool, str]:
        """Refresh augment list from running League Client local asset endpoint."""
        cherry = lcu_client.get_json("/lol-game-data/assets/v1/cherry-augments.json")
        if not isinstance(cherry, list) or not cherry:
            return False, "LCU 未返回 cherry-augments.json"

        lists_data = lcu_client.get_json("/lol-game-data/assets/v1/augment-lists.json")
        mayhem_keys: set[str] = set()
        if isinstance(lists_data, list):
            for entry in lists_data:
                if isinstance(entry, dict):
                    for p in entry.get("augmentList") or []:
                        if isinstance(p, str) and p.strip():
                            mayhem_keys.add(p.strip().rsplit("/", 1)[-1])

        existing_by_id = {a.id: a for a in self.all_augments()}
        records: list[AugmentInfo] = []
        seen: set[int] = set()
        for row in cherry:
            if not isinstance(row, dict):
                continue
            aug_id = row.get("id")
            key = str(row.get("augmentNameId") or "").strip()
            name = str(row.get("nameTRA") or "").strip()
            rarity = RARITY_MAP.get(str(row.get("rarity") or "").strip())
            if not isinstance(aug_id, int) or aug_id <= 0 or not key or not name or not rarity:
                continue
            if aug_id in seen:
                continue
            seen.add(aug_id)
            prev = existing_by_id.get(aug_id)
            raw_desc = str(row.get("summaryDescription") or "").strip()
            desc = clean_description(raw_desc) if raw_desc else (prev.description if prev else "")
            icon_path = str(row.get("augmentSmallIconPath") or row.get("augmentLargeIconPath") or "").strip()
            in_mayhem = (key in mayhem_keys) if mayhem_keys else (
                key.startswith("ARAM_") or aug_id >= 1000 or (prev.in_mayhem_pool if prev else False)
            )
            records.append(
                AugmentInfo(
                    id=aug_id,
                    key=key,
                    name=name,
                    rarity=rarity,
                    icon_path=icon_path,
                    description=desc,
                    tags=list(row.get("augmentDisplayTags") or []),
                    in_mayhem_pool=in_mayhem,
                )
            )

        if not records:
            return False, "LCU 强化目录为空"

        self._save_cache(records, locale)
        self._rebuild_index(records)
        return True, f"已从本地客户端同步 {len(records)} 个海克斯"

    def refresh_from_cdragon(self, locale: str = "zh_cn", proxy_url: str = "") -> tuple[bool, str]:
        """Refresh augment list from CommunityDragon online JSON."""
        base = f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/{locale}/v1"
        cherry = _fetch_json_http(f"{base}/cherry-augments.json", proxy_url=proxy_url)
        if not isinstance(cherry, list) or not cherry:
            return False, "CDragon 返回空目录"
        try:
            lists_data = _fetch_json_http(f"{base}/augment-lists.json", proxy_url=proxy_url)
        except Exception:
            lists_data = []

        mayhem_keys: set[str] = set()
        if isinstance(lists_data, list):
            for entry in lists_data:
                if isinstance(entry, dict):
                    for p in entry.get("augmentList") or []:
                        if isinstance(p, str) and p.strip():
                            mayhem_keys.add(p.strip().rsplit("/", 1)[-1])

        existing_by_id = {a.id: a for a in self.all_augments()}
        records: list[AugmentInfo] = []
        seen: set[int] = set()
        for row in cherry:
            if not isinstance(row, dict):
                continue
            aug_id = row.get("id")
            key = str(row.get("augmentNameId") or "").strip()
            name = str(row.get("nameTRA") or "").strip()
            rarity = RARITY_MAP.get(str(row.get("rarity") or "").strip())
            if not isinstance(aug_id, int) or aug_id <= 0 or not key or not name or not rarity:
                continue
            if aug_id in seen:
                continue
            seen.add(aug_id)
            prev = existing_by_id.get(aug_id)
            raw_desc = str(row.get("summaryDescription") or "").strip()
            desc = clean_description(raw_desc) if raw_desc else (prev.description if prev else "")
            icon_path = str(row.get("augmentSmallIconPath") or row.get("augmentLargeIconPath") or "").strip()
            in_mayhem = (key in mayhem_keys) if mayhem_keys else (key.startswith("ARAM_") or aug_id >= 1000)
            records.append(
                AugmentInfo(
                    id=aug_id,
                    key=key,
                    name=name,
                    rarity=rarity,
                    icon_path=icon_path,
                    description=desc,
                    tags=list(row.get("augmentDisplayTags") or []),
                    in_mayhem_pool=in_mayhem,
                )
            )

        self._save_cache(records, locale)
        self._rebuild_index(records)
        return True, f"已从 CommunityDragon 同步 {len(records)} 个海克斯"

    def _save_cache(self, records: list[AugmentInfo], locale: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        out = self.cache_dir / f"catalog.{locale}.json"
        payload = {"schema_version": 1, "locale": locale, "augments": [r.model_dump() for r in records]}
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out)

    def get_icon_bytes(
        self,
        icon_path: str,
        lcu_client: Any = None,
        proxy_url: str = "",
    ) -> bytes | None:
        """Load augment icon from disk cache, LCU local assets, or CommunityDragon."""
        clean = icon_path.strip()
        if not clean or ".." in clean or "\\" in clean or "\0" in clean:
            return None

        digest = hashlib.sha1(clean.lower().encode("utf-8")).hexdigest()[:20]
        cache_file = self.icons_dir / f"{digest}.png"
        if cache_file.exists():
            try:
                data = cache_file.read_bytes()
                if len(data) > 64:
                    return data
            except OSError:
                pass

        # 1. Try local League Client asset server (fastest and zero external dependency)
        if lcu_client is not None and lcu_client.is_connected():
            endpoint = clean if clean.startswith("/") else f"/{clean}"
            data = lcu_client.get_bytes(endpoint)
            if data and len(data) > 64:
                self._cache_icon(cache_file, data)
                return data

        # 2. Fall back to CommunityDragon static CDN
        for url in cdragon_icon_urls(clean):
            data = _fetch_bytes_http(url, proxy_url=proxy_url)
            if data and len(data) > 64 and data[:8] == b"\x89PNG\r\n\x1a\n":
                self._cache_icon(cache_file, data)
                return data
        return None

    def _cache_icon(self, target: Path, data: bytes) -> None:
        try:
            self.icons_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
        except OSError:
            pass


def _candidate_proxies(proxy_url: str = "") -> list[str | None]:
    proxies: list[str | None] = []
    if proxy_url.strip():
        proxies.append(proxy_url.strip())
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k, "").strip()
        if v and v not in proxies:
            proxies.append(v)
    proxies.append(None)
    return proxies


def _fetch_json_http(url: str, proxy_url: str = "") -> Any:
    last_err: Exception | None = None
    for p in _candidate_proxies(proxy_url):
        try:
            opener = (
                urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
                if p
                else urllib.request.build_opener()
            )
            req = urllib.request.Request(url, headers={"User-Agent": "HextechOverlay/0.1"})
            with opener.open(req, timeout=12) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            last_err = exc
        if os.name == "nt":
            cmd = ["curl.exe", "-sS", "-L", "--ssl-no-revoke", "--max-time", "15", "-H", "User-Agent: HextechOverlay/0.1"]
            if p:
                cmd.extend(["-x", p])
            cmd.append(url)
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
                if res.returncode == 0 and res.stdout.strip():
                    return json.loads(res.stdout)
            except Exception as exc:
                last_err = exc
    raise RuntimeError(f"HTTP JSON fetch failed for {url}: {last_err}")


def _fetch_bytes_http(url: str, proxy_url: str = "") -> bytes | None:
    for p in _candidate_proxies(proxy_url):
        try:
            opener = (
                urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
                if p
                else urllib.request.build_opener()
            )
            req = urllib.request.Request(url, headers={"User-Agent": "HextechOverlay/0.1"})
            with opener.open(req, timeout=8) as resp:
                return resp.read()
        except Exception:
            pass
        if os.name == "nt":
            cmd = ["curl.exe", "-sS", "-L", "--ssl-no-revoke", "--max-time", "10", "-H", "User-Agent: HextechOverlay/0.1"]
            if p:
                cmd.extend(["-x", p])
            cmd.append(url)
            try:
                res = subprocess.run(cmd, capture_output=True, check=False)
                if res.returncode == 0 and res.stdout:
                    return res.stdout
            except Exception:
                pass
    return None
