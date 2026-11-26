#!/usr/bin/env python3
"""Sync and bundle CommunityDragon ARAM Mayhem augment catalog + Chinese descriptions."""

from __future__ import annotations

import json
import math
import os
import re
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT_DIR / "hextech_overlay" / "assets" / "bundled_augments.zh_cn.json"

CDRAGON_CHERRY_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1/cherry-augments.json"
)
CDRAGON_LISTS_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1/augment-lists.json"
)
CDRAGON_ARENA_URL = "https://raw.communitydragon.org/latest/cdragon/arena/zh_cn.json"
CDRAGON_KIWI_BIN_URL = "https://raw.communitydragon.org/latest/game/maps/modespecificdata/kiwi.bin.json"
CDRAGON_ZH_STRINGS_URL = "https://raw.communitydragon.org/latest/game/zh_cn/data/menu/en_us/lol.stringtable.json"

RARITY_MAP = {
    "kSilver": "silver",
    "kGold": "gold",
    "kPrismatic": "prismatic",
}

# ---- XXH3-64 (for CDragon hashed lol.stringtable keys, <= 240 bytes) & FNV-1a 32 (for bin hashes)
_U64 = (1 << 64) - 1
_U32 = 0xFFFFFFFF
_PRIME64_1 = 0x9E3779B185EBCA87
_PRIME64_2 = 0xC2B2AE3D27D4EB4F
_PRIME64_3 = 0x165667B19E3779F9
_PRIME_MX1 = 0x165667919E3779F9
_PRIME_MX2 = 0x9FB21C651E98DF25
_SECRET = bytes.fromhex(
    "b8fe6c3923a44bbe7c01812cf721ad1cded46de9839097db7240a4a4b7b3671f"
    "cb79e64eccc0e578825ad07dccff7221b8084674f743248ee03590e6813a264c"
    "3c2852bb91c300cb88d0658b1b532ea371644897a20df94e3819ef46a9deacd8"
    "a8fa763fe39c343ff9dcbbc7c70b4f1d8a51e04bcdb45931c89f7ec9d9787364"
    "eac5ac8334d3ebc3c581a0fffa1363eb170ddd51b7f0da49d316552629d4689e"
    "2b16be587d47a1fc8ff8b8d17ad031ce45cb3a8f95160428afd7fbcabb4b407e"
)


def _bin_hash(name: str) -> str:
    h = 0x811C9DC5
    for ch in name.lower():
        h = ((h ^ ord(ch)) * 0x01000193) & _U32
    return f"{{{h:08x}}}"


def _rotl64(x: int, bits: int) -> int:
    return ((x << bits) | (x >> (64 - bits))) & _U64


def _read64(buf: bytes, at: int) -> int:
    return struct.unpack_from("<Q", buf, at)[0]


def _read32(buf: bytes, at: int) -> int:
    return struct.unpack_from("<I", buf, at)[0]


def _folded_mul(a: int, b: int) -> int:
    prod = a * b
    return (prod & _U64) ^ (prod >> 64)


def _xxh64_avalanche(h: int) -> int:
    h = ((h ^ (h >> 33)) * _PRIME64_2) & _U64
    h = ((h ^ (h >> 29)) * _PRIME64_3) & _U64
    return h ^ (h >> 32)


def _xxh3_avalanche(h: int) -> int:
    h = ((h ^ (h >> 37)) * _PRIME_MX1) & _U64
    return h ^ (h >> 32)


def _xxh3_mix16(data: bytes, at: int, sec_at: int) -> int:
    return _folded_mul(
        _read64(data, at) ^ _read64(_SECRET, sec_at),
        _read64(data, at + 8) ^ _read64(_SECRET, sec_at + 8),
    )


def _xxh3_64(data: bytes) -> int | None:
    ln = len(data)
    if ln == 0:
        return _xxh64_avalanche(_read64(_SECRET, 56) ^ _read64(_SECRET, 64))
    if ln <= 3:
        combined = (data[0] << 16) | (data[ln >> 1] << 24) | data[ln - 1] | (ln << 8)
        return _xxh64_avalanche(combined ^ (_read32(_SECRET, 0) ^ _read32(_SECRET, 4)))
    if ln <= 8:
        combined = (_read32(data, ln - 4) + (_read32(data, 0) << 32)) & _U64
        h = combined ^ (_read64(_SECRET, 8) ^ _read64(_SECRET, 16))
        h ^= _rotl64(h, 49) ^ _rotl64(h, 24)
        h = (h * _PRIME_MX2) & _U64
        h ^= (h >> 35) + ln
        h = (h * _PRIME_MX2) & _U64
        return h ^ (h >> 28)
    if ln <= 16:
        low = _read64(data, 0) ^ (_read64(_SECRET, 24) ^ _read64(_SECRET, 32))
        high = _read64(data, ln - 8) ^ (_read64(_SECRET, 40) ^ _read64(_SECRET, 48))
        swapped = int.from_bytes(low.to_bytes(8, "little"), "big")
        return _xxh3_avalanche((ln + swapped + high + _folded_mul(low, high)) & _U64)
    acc = (ln * _PRIME64_1) & _U64
    if ln <= 128:
        if ln > 96:
            acc += _xxh3_mix16(data, 48, 96) + _xxh3_mix16(data, ln - 64, 112)
        if ln > 64:
            acc += _xxh3_mix16(data, 32, 64) + _xxh3_mix16(data, ln - 48, 80)
        if ln > 32:
            acc += _xxh3_mix16(data, 16, 32) + _xxh3_mix16(data, ln - 32, 48)
        acc += _xxh3_mix16(data, 0, 0) + _xxh3_mix16(data, ln - 16, 16)
        return _xxh3_avalanche(acc & _U64)
    if ln <= 240:
        for i in range(8):
            acc += _xxh3_mix16(data, 16 * i, 16 * i)
        acc = _xxh3_avalanche(acc & _U64)
        for i in range(8, ln // 16):
            acc += _xxh3_mix16(data, 16 * i, 16 * (i - 8) + 3)
        acc += _xxh3_mix16(data, ln - 16, 119)
        return _xxh3_avalanche(acc & _U64)
    return None


def _string_hash(key: str) -> str | None:
    h = _xxh3_64(key.lower().encode("utf-8"))
    if h is None:
        return None
    return f"{{{h & ((1 << 38) - 1):010x}}}"


def _lookup_string(strings: dict[str, str], key: str) -> str | None:
    low = key.lower()
    if low in strings:
        return strings[low]
    sh = _string_hash(low)
    return strings.get(sh) if sh else None


def fetch_json(url: str, proxy_url: str | None = None) -> Any:
    proxies = [proxy_url] if proxy_url else []
    for env_key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(env_key)
        if val and val not in proxies:
            proxies.append(val)
    if "http://127.0.0.1:7897" not in proxies:
        proxies.append("http://127.0.0.1:7897")
    proxies.append(None)

    last_err: Exception | None = None
    for p in proxies:
        cmd = ["curl.exe", "-sS", "-L", "--ssl-no-revoke", "--max-time", "45", "-H", "User-Agent: HextechOverlay/0.1"]
        if p:
            cmd.extend(["-x", p])
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                return json.loads(proc.stdout)
            last_err = RuntimeError(f"curl rc={proc.returncode}")
        except Exception as exc:
            last_err = exc

        try:
            opener = (
                urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
                if p
                else urllib.request.build_opener()
            )
            req = urllib.request.Request(url, headers={"User-Agent": "HextechOverlay/0.1"})
            with opener.open(req, timeout=35) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            last_err = exc

    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def _fmt(val: float) -> str:
    rounded = round(val, 2)
    if abs(rounded - round(rounded)) < 0.01:
        return str(int(round(rounded)))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def clean_text(raw: str) -> str:
    text = re.sub(r"%i:[^%]+%", "", raw)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"@[^@]+@", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _collect_kiwi_descriptions(kiwi_bin: dict[str, Any], stringtable: dict[str, Any]) -> dict[int, str]:
    strings: dict[str, str] = stringtable.get("entries") or {}
    aug_nodes: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("__type") == "AugmentData":
                aug_nodes.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(kiwi_bin)

    stat_label = {0: "法强", 1: "护甲", 2: "攻击力", 4: "攻速", 6: "魔抗", 8: "暴击", 11: "技能急速", 12: "生命值"}
    out: dict[int, str] = {}

    for aug in aug_nodes:
        aug_id = aug.get("AugmentPlatformId")
        if not isinstance(aug_id, int) or aug_id in out:
            continue
        loc_key = str(aug.get("DescriptionTra") or aug.get("AugmentTooltipTra") or "").strip()
        if not loc_key:
            continue
        raw = _lookup_string(strings, loc_key)
        if not raw:
            continue

        # Expand {{ ... }} references
        for _ in range(4):
            if "{{" not in raw:
                break
            raw = re.sub(
                r"\{\{\s*([^}\s]+)\s*\}\}",
                lambda m: _lookup_string(strings, m.group(1)) or "",
                raw,
            )

        # Build data values & simple calculations from RootSpell + AdditionalSpells
        values: dict[str, tuple[float | None, str | None, bool]] = {}
        spells = [
            kiwi_bin[p].get("mSpell")
            for p in [aug.get("RootSpell"), *(aug.get("AdditionalSpells") or [])]
            if isinstance(p, str) and isinstance(kiwi_bin.get(p), dict) and kiwi_bin[p].get("mSpell")
        ]

        dv_names: list[str] = []
        for sp in spells:
            for dv in sp.get("DataValues") or []:
                nm = dv.get("name")
                vals = dv.get("values") or []
                if nm and vals and isinstance(vals[0], (int, float)):
                    dv_names.append(nm)
                    values.setdefault(nm.lower(), (float(vals[0]), None, False))
                    values.setdefault(_bin_hash(nm), (float(vals[0]), None, False))
            cd = sp.get("Cooldown") or sp.get("cooldownTime")
            if isinstance(cd, (int, float)):
                values.setdefault("cooldown", (float(cd), None, False))
        if "cooldown" not in values and "basecooldown" in values:
            values["cooldown"] = values["basecooldown"]

        # SpellName hint
        empowered = re.search(r"_([QWER])$", str(aug.get("AugmentNameId") or ""))
        slot = empowered.group(1) if empowered else next(
            (m.group(1) for n in dv_names if (m := re.match(r"^([QWER])[A-Z]", str(n)))),
            "技能",
        )
        raw = re.sub(r"\{\{\s*SpellName\s*\}\}", slot, raw, flags=re.IGNORECASE)

        for sp in spells:
            for cname, calc in (sp.get("mSpellCalculations") or {}).items():
                if not isinstance(calc, dict):
                    continue
                is_pct = calc.get("mDisplayAsPercent") is True
                mult = 1.0
                mp = calc.get("mMultiplier")
                if isinstance(mp, dict):
                    if mp.get("__type") == "NumberCalculationPart" and isinstance(mp.get("mNumber"), (int, float)):
                        mult = float(mp["mNumber"])
                    elif mp.get("__type") == "NamedDataValueCalculationPart":
                        dv_key = str(mp.get("mDataValue") or "").lower()
                        if dv_key in values and values[dv_key][0] is not None:
                            mult = values[dv_key][0]  # type: ignore[assignment]
                        else:
                            continue
                    else:
                        continue

                parts = calc.get("mFormulaParts") or []
                terms: list[str] = []
                ok = True
                for pt in parts:
                    if not isinstance(pt, dict):
                        ok = False
                        break
                    pt_type = pt.get("__type")
                    if pt_type == "NumberCalculationPart" and isinstance(pt.get("mNumber"), (int, float)):
                        v = float(pt["mNumber"]) * mult
                        terms.append(f"{_fmt(v * 100)}%" if is_pct else _fmt(v))
                    elif pt_type == "NamedDataValueCalculationPart":
                        dv_k = str(pt.get("mDataValue") or "").lower()
                        ent = values.get(dv_k)
                        if ent and ent[0] is not None:
                            v = ent[0] * mult
                            terms.append(f"{_fmt(v * 100)}%" if is_pct else _fmt(v))
                        else:
                            ok = False
                            break
                    elif pt_type == "ByCharLevelInterpolationCalculationPart":
                        s_v = float(pt.get("mStartValue") or 0) * mult
                        e_v = float(pt.get("mEndValue") or 0) * mult
                        terms.append(
                            f"{_fmt(s_v * 100)}%-{_fmt(e_v * 100)}%" if is_pct else f"{_fmt(s_v)}-{_fmt(e_v)}"
                        )
                    elif pt_type in ("StatByNamedDataValueCalculationPart", "StatByCoefficientCalculationPart"):
                        lbl = stat_label.get(pt.get("mStat", 0), "属性")
                        coeff = pt.get("mCoefficient")
                        if coeff is None:
                            dv_k = str(pt.get("mDataValue") or "").lower()
                            coeff = values[dv_k][0] if dv_k in values else None
                        if isinstance(coeff, (int, float)):
                            pct = float(coeff) * mult * 100 * (100 if is_pct else 1)
                            terms.append(f"{_fmt(pct)}%{lbl}")
                        else:
                            ok = False
                            break
                    else:
                        ok = False
                        break
                if ok and terms:
                    rendered = terms[0] if len(terms) == 1 else f"({'+'.join(terms)})"
                    values.setdefault(cname.lower(), (None, rendered, is_pct))
                    values.setdefault(_bin_hash(cname), (None, rendered, is_pct))

        def repl_ph(m: re.Match[str]) -> str:
            name, op, operand, pct_sign = m.group(1), m.group(2), m.group(3), m.group(4) or ""
            ent = values.get(name.lower()) or values.get(_bin_hash(name))
            if not ent:
                return ""
            val, txt, is_pct = ent
            if txt is not None:
                return txt if (is_pct or op) else txt + pct_sign
            if val is None or math.isnan(val):
                return ""
            if op == "*":
                return _fmt(val * float(operand)) + pct_sign
            if op == "/":
                return _fmt(val / float(operand)) + pct_sign
            return f"{_fmt(val * 100)}%" if is_pct else _fmt(val) + pct_sign

        filled = re.sub(r"@([A-Za-z_][\w.]*)\s*(?:([*/])\s*([\d.]+))?@(%(?!i:))?", repl_ph, raw)
        cleaned = clean_text(filled)
        if cleaned:
            out[aug_id] = cleaned

    return out


def build_catalog(
    cherry_augments: list[dict[str, Any]],
    augment_lists: list[dict[str, Any]],
    arena_data: dict[str, Any],
    kiwi_descs: dict[int, str],
) -> dict[str, Any]:
    mayhem_keys: set[str] = set()
    for entry in augment_lists:
        if isinstance(entry, dict):
            for p in entry.get("augmentList") or []:
                if isinstance(p, str) and p.strip():
                    mayhem_keys.add(p.strip().rsplit("/", 1)[-1])

    arena_by_api: dict[str, dict[str, Any]] = {}
    arena_by_name: dict[str, dict[str, Any]] = {}
    for item in arena_data.get("augments") or []:
        if not isinstance(item, dict):
            continue
        api = str(item.get("apiName") or "").strip()
        nm = str(item.get("name") or "").strip()
        if api:
            arena_by_api[api.lower()] = item
        if nm:
            arena_by_name[nm] = item

    records: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for row in cherry_augments:
        if not isinstance(row, dict):
            continue
        aug_id = row.get("id")
        key = str(row.get("augmentNameId") or "").strip()
        name = str(row.get("nameTRA") or "").strip()
        rarity = RARITY_MAP.get(str(row.get("rarity") or "").strip())
        if not isinstance(aug_id, int) or aug_id <= 0 or not key or not name or not rarity:
            continue
        if aug_id in seen_ids:
            continue
        seen_ids.add(aug_id)

        icon_path = str(row.get("augmentSmallIconPath") or row.get("augmentLargeIconPath") or "").strip()
        desc = kiwi_descs.get(aug_id, "")
        if not desc:
            raw_summary = str(row.get("summaryDescription") or "").strip()
            if raw_summary:
                desc = clean_text(raw_summary)
        if not desc:
            arena_item = (
                arena_by_api.get(key.lower())
                or arena_by_api.get(re.sub(r"^ARAM_", "", key, flags=re.IGNORECASE).lower())
                or arena_by_name.get(name)
            )
            if arena_item:
                desc = clean_text(str(arena_item.get("desc") or ""))

        in_mayhem = key in mayhem_keys or key.startswith("ARAM_") or aug_id >= 1000 or aug_id in kiwi_descs
        records.append(
            {
                "id": aug_id,
                "key": key,
                "name": name,
                "rarity": rarity,
                "icon_path": icon_path,
                "description": desc,
                "tags": list(row.get("augmentDisplayTags") or []),
                "in_mayhem_pool": bool(in_mayhem),
            }
        )

    records.sort(key=lambda r: (0 if r["in_mayhem_pool"] else 1, -r["id"]))
    return {
        "schema_version": 1,
        "locale": "zh_cn",
        "count": len(records),
        "mayhem_pool_count": sum(1 for r in records if r["in_mayhem_pool"]),
        "augments": records,
    }


def main() -> int:
    proxy_url = sys.argv[1] if len(sys.argv) > 1 else None
    print("Fetching cherry-augments.json...")
    cherry = fetch_json(CDRAGON_CHERRY_URL, proxy_url=proxy_url)
    print("Fetching augment-lists.json...")
    lists_data = fetch_json(CDRAGON_LISTS_URL, proxy_url=proxy_url)
    print("Fetching arena/zh_cn.json...")
    arena = fetch_json(CDRAGON_ARENA_URL, proxy_url=proxy_url)

    kiwi_descs: dict[int, str] = {}
    try:
        print("Fetching kiwi.bin.json & zh_cn lol.stringtable.json...")
        kiwi_bin = fetch_json(CDRAGON_KIWI_BIN_URL, proxy_url=proxy_url)
        str_table = fetch_json(CDRAGON_ZH_STRINGS_URL, proxy_url=proxy_url)
        kiwi_descs = _collect_kiwi_descriptions(kiwi_bin, str_table)
        print(f"Extracted {len(kiwi_descs)} official Mayhem Chinese descriptions from kiwi.bin!")
    except Exception as exc:
        print(f"Warning: kiwi.bin description extraction skipped ({exc})")

    catalog = build_catalog(cherry, lists_data, arena, kiwi_descs)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    with_desc = sum(1 for a in catalog["augments"] if a["description"])
    mayhem_with_desc = sum(1 for a in catalog["augments"] if a["in_mayhem_pool"] and a["description"])
    print(
        f"Saved {catalog['count']} augments (mayhem_pool={catalog['mayhem_pool_count']}, "
        f"with_desc={with_desc}, mayhem_with_desc={mayhem_with_desc}) -> {OUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
