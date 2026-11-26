"""Unit tests for Tencent icreate encrypted log codec, tail reader, and 693/694/696 state machine."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hextech_overlay.tencent_log import (
    TencentAugmentStateMachine,
    TencentLogTail,
    decode_tencent_log_line,
    derive_tencent_log_key,
    is_tencent_region,
    parse_icreate_log_dir,
)


def make_log_line(payload: dict, puuid: str, ts_epoch: float) -> str:
    key = derive_tencent_log_key(puuid)
    stamp = datetime.fromtimestamp(ts_epoch).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    raw = ("game-log-> " + json.dumps(payload)).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    enc = cipher.encryptor()
    hex_ct = (enc.update(padded) + enc.finalize()).hex()
    return f"[{stamp}] [info] {hex_ct}\n"


class TestTencentLog(unittest.TestCase):
    def test_region_and_logdir_validation(self) -> None:
        self.assertTrue(is_tencent_region("TENCENT"))
        self.assertTrue(is_tencent_region("cn"))
        self.assertTrue(is_tencent_region("China"))
        self.assertFalse(is_tencent_region("TW2"))
        self.assertFalse(is_tencent_region("zh-CN"))
        self.assertFalse(is_tencent_region(None))

        self.assertEqual(
            parse_icreate_log_dir('icreateLol.exe --logDir "D:\\Tencent\\lol" --other 1'),
            Path(r"D:\Tencent\lol\iCreateLogs\main.log"),
        )
        self.assertEqual(
            parse_icreate_log_dir("icreateLol.exe --logDir=C:/Apps/lol"),
            Path(r"C:\Apps\lol\iCreateLogs\main.log"),
        )
        for bad in ("--logDir relative", '--logDir "\\\\server\\share"', "--logDir C:rel", "--other C:\\x"):
            self.assertIsNone(parse_icreate_log_dir(bad))

    def test_decode_693_694_696_and_rejections(self) -> None:
        puuid = "synthetic-player-001"
        key = derive_tencent_log_key(puuid)
        t0 = 1795650000.123

        line_693 = make_log_line(
            {
                "form": "game",
                "dwSubCmd": 693,
                "params": {
                    "astAugments": [
                        {"szAugmentName": "ARAM_ApexInventor", "chSpellSlot": -1},
                        {"szAugmentName": "ARAM_Stats", "chSpellSlot": -1},
                        {"szAugmentName": "ARAM_ADAPt", "chSpellSlot": -1},
                        {"szAugmentName": "", "chSpellSlot": -1},
                    ]
                },
            },
            puuid,
            t0,
        )
        rec = decode_tencent_log_line(line_693, key)
        self.assertIsNotNone(rec)
        assert rec is not None and rec.event is not None
        self.assertEqual(rec.event.kind, "candidates")
        self.assertEqual(rec.event.keys, ["ARAM_ApexInventor", "ARAM_Stats", "ARAM_ADAPt", ""])

        line_694 = make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 1}}, puuid, t0 + 2)
        rec_pick = decode_tencent_log_line(line_694, key)
        assert rec_pick is not None and rec_pick.event is not None
        self.assertEqual(rec_pick.event.kind, "selected")
        self.assertEqual(rec_pick.event.index, 1)

        # Reject wrong PUUID, TFT form, bad augment key, huge line
        self.assertIsNone(decode_tencent_log_line(make_log_line({"form": "tft", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0), key))
        self.assertIsNone(decode_tencent_log_line(line_694, derive_tencent_log_key("other-player")))
        self.assertIsNone(
            decode_tencent_log_line(
                make_log_line(
                    {"form": "game", "dwSubCmd": 693, "params": {"astAugments": [{"szAugmentName": "../bad"}, {"szAugmentName": "A"}, {"szAugmentName": "B"}]}},
                    puuid,
                    t0,
                ),
                key,
            )
        )
        self.assertIsNone(decode_tencent_log_line("a" * (300 * 1024), key))

    def test_state_machine_empty_693_and_safe_696_binding(self) -> None:
        puuid = "synthetic-player-002"
        key = derive_tencent_log_key(puuid)
        sm = TencentAugmentStateMachine()
        t0 = 1795651000.0

        # 696 before any 694 must NOT bind player 0 blindly
        r_696_early = decode_tencent_log_line(
            make_log_line(
                {
                    "form": "game",
                    "dwSubCmd": 696,
                    "params": {
                        "astPlayerHextechAugmentSelectionList": [
                            {"dwHeroNetID": 101, "astAugments": [{"szAugmentName": "ARAM_Stats"}]},
                            {"dwHeroNetID": 102, "astAugments": [{"szAugmentName": "ARAM_ApexInventor"}]},
                        ]
                    },
                },
                puuid,
                t0,
            ),
            key,
        )
        assert r_696_early is not None
        self.assertEqual(sm.observe(r_696_early), [])
        self.assertIsNone(sm.local_hero_net_id)

        # 693 offers 4 cards (Mayhem 4.0)
        r_693 = decode_tencent_log_line(
            make_log_line(
                {
                    "form": "game",
                    "dwSubCmd": 693,
                    "params": {
                        "astAugments": [
                            {"szAugmentName": "ARAM_ApexInventor"},
                            {"szAugmentName": "ARAM_Stats"},
                            {"szAugmentName": "ARAM_ADAPt"},
                            {"szAugmentName": "ARAM_OceanSoul"},
                        ]
                    },
                },
                puuid,
                t0 + 1,
            ),
            key,
        )
        assert r_693 is not None
        acts = sm.observe(r_693)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0].kind, "candidates")
        self.assertEqual(len(acts[0].keys), 4)

        # Empty 693 followed by 694 must still map picked_key correctly
        r_693_empty = decode_tencent_log_line(
            make_log_line(
                {
                    "form": "game",
                    "dwSubCmd": 693,
                    "params": {"astAugments": [{"szAugmentName": ""}, {"szAugmentName": ""}, {"szAugmentName": ""}]},
                },
                puuid,
                t0 + 2,
            ),
            key,
        )
        r_694 = decode_tencent_log_line(
            make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 2}}, puuid, t0 + 2.2),
            key,
        )
        assert r_693_empty is not None and r_694 is not None
        sm.observe(r_693_empty)
        acts_pick = sm.observe(r_694)
        self.assertEqual(acts_pick[0].kind, "picked")
        self.assertEqual(acts_pick[0].picked_key, "ARAM_ADAPt")

        # Ambiguous 696 (two players both have ARAM_ADAPt) must NOT bind yet
        r_696_ambig = decode_tencent_log_line(
            make_log_line(
                {
                    "form": "game",
                    "dwSubCmd": 696,
                    "params": {
                        "astPlayerHextechAugmentSelectionList": [
                            {"dwHeroNetID": 101, "astAugments": [{"szAugmentName": "ARAM_ADAPt"}]},
                            {"dwHeroNetID": 102, "astAugments": [{"szAugmentName": "ARAM_ADAPt"}]},
                        ]
                    },
                },
                puuid,
                t0 + 3,
            ),
            key,
        )
        assert r_696_ambig is not None
        self.assertEqual(sm.observe(r_696_ambig), [])
        self.assertIsNone(sm.local_hero_net_id)

        # Unique 696 match (only netId 102 matches ARAM_ADAPt and also received bonus transmute augment!)
        r_696_unique = decode_tencent_log_line(
            make_log_line(
                {
                    "form": "game",
                    "dwSubCmd": 696,
                    "params": {
                        "astPlayerHextechAugmentSelectionList": [
                            {"dwHeroNetID": 101, "astAugments": [{"szAugmentName": "ARAM_Stats"}]},
                            {
                                "dwHeroNetID": 102,
                                "astAugments": [{"szAugmentName": "ARAM_ADAPt"}, {"szAugmentName": "ARAM_Eureka"}],
                            },
                        ]
                    },
                },
                puuid,
                t0 + 4,
            ),
            key,
        )
        assert r_696_unique is not None
        acts_rec = sm.observe(r_696_unique)
        self.assertEqual(sm.local_hero_net_id, 102)
        self.assertEqual(acts_rec[0].kind, "reconcile_owned")
        self.assertEqual(acts_rec[0].keys, ["ARAM_ADAPt", "ARAM_Eureka"])

    def test_log_tail_incremental_and_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / "main.log"
            log_file.write_text("old-line-1\nold-line-2\n", encoding="utf-8")

            tail = TencentLogTail(log_file, start_at_end=True)
            lines, reset = tail.read_new_lines()
            self.assertEqual(lines, [])
            self.assertFalse(reset)

            with open(log_file, "a", encoding="utf-8") as f:
                f.write("new-line-1\nnew-line-2\n")
            lines2, reset2 = tail.read_new_lines()
            self.assertEqual(lines2, ["new-line-1", "new-line-2"])
            self.assertFalse(reset2)

            # Truncation / file replacement
            log_file.write_text("fresh-line\n", encoding="utf-8")
            lines3, reset3 = tail.read_new_lines()
            self.assertEqual(lines3, ["fresh-line"])
            self.assertTrue(reset3)


if __name__ == "__main__":
    unittest.main()
