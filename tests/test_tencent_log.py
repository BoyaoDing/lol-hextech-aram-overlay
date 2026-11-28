"""Unit tests for Tencent icreate encrypted log codec, tail reader, and 693/694/696 state machine."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hextech_overlay.config import SessionStore
from hextech_overlay.engine import OverlayEngine
from hextech_overlay.models import AugmentMetric
from hextech_overlay.tencent_log import (
    StateAction,
    TencentAugmentStateMachine,
    TencentLogTail,
    classify_augment_transform,
    decode_tencent_log_line,
    derive_tencent_log_key,
    is_stat_anvil_key,
    is_tencent_region,
    parse_icreate_log_dir,
    probe_tencent_gdp_enabled,
)


def make_raw_log_line(plaintext: str, puuid: str, ts_epoch: float) -> str:
    key = derive_tencent_log_key(puuid)
    stamp = datetime.fromtimestamp(ts_epoch).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    raw = plaintext.encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    enc = cipher.encryptor()
    hex_ct = (enc.update(padded) + enc.finalize()).hex()
    return f"[{stamp}] [info]  {hex_ct}\n"


def make_log_line(payload: dict, puuid: str, ts_epoch: float) -> str:
    return make_raw_log_line("game-log-> " + json.dumps(payload), puuid, ts_epoch)


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

    def test_first_pick_transmute_chaos_binds_and_reconciles(self) -> None:
        puuid = "synthetic-chaos-001"
        key = derive_tencent_log_key(puuid)
        sm = TencentAugmentStateMachine()
        t0 = 1795652000.0

        self.assertEqual(classify_augment_transform("ARAM_TransmuteChaos"), "chaos")
        self.assertEqual(classify_augment_transform("ARAM_TransmutePrismatic"), "transmute")
        self.assertEqual(classify_augment_transform("ARAM_PandorasBox"), "pandora")
        self.assertEqual(classify_augment_transform("ARAM_ApexInventor"), "normal")

        # Lv 3: player picks ARAM_TransmuteChaos as their very first augment
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 693,
                        "params": {
                            "astAugments": [
                                {"szAugmentName": "ARAM_TransmuteChaos"},
                                {"szAugmentName": "ARAM_Eureka"},
                                {"szAugmentName": "ARAM_BacktoBasics"},
                            ]
                        },
                    },
                    puuid,
                    t0,
                ),
                key,
            )
        )
        pick_acts = sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 1.0),
                key,
            )
        )
        self.assertEqual(pick_acts[0].picked_key, "ARAM_TransmuteChaos")

        # 696 shows player 202 jumped from 0 -> 2 random augments while 201 has 1
        rec_acts = sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 696,
                        "params": {
                            "astPlayerHextechAugmentSelectionList": [
                                {"dwHeroNetID": 201, "astAugments": [{"szAugmentName": "ARAM_Stats"}]},
                                {
                                    "dwHeroNetID": 202,
                                    "astAugments": [
                                        {"szAugmentName": "ARAM_Eureka"},
                                        {"szAugmentName": "ARAM_BacktoBasics"},
                                    ],
                                },
                            ]
                        },
                    },
                    puuid,
                    t0 + 1.4,
                ),
                key,
            )
        )
        self.assertEqual(sm.local_hero_net_id, 202)
        self.assertEqual(len(rec_acts), 1)
        self.assertEqual(rec_acts[0].kind, "reconcile_owned")
        self.assertEqual(rec_acts[0].keys, ["ARAM_Eureka", "ARAM_BacktoBasics"])

    def test_first_pick_transmute_prismatic_binds_and_reconciles(self) -> None:
        puuid = "synthetic-prism-001"
        key = derive_tencent_log_key(puuid)
        sm = TencentAugmentStateMachine()
        t0 = 1795653000.0

        # Initial 696 shows 301 already has an augment before local player picks
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 696,
                        "params": {
                            "astPlayerHextechAugmentSelectionList": [
                                {"dwHeroNetID": 301, "astAugments": [{"szAugmentName": "ARAM_Stats"}]},
                                {"dwHeroNetID": 302, "astAugments": []},
                            ]
                        },
                    },
                    puuid,
                    t0,
                ),
                key,
            )
        )
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 693,
                        "params": {
                            "astAugments": [
                                {"szAugmentName": "ARAM_TransmutePrismatic"},
                                {"szAugmentName": "ARAM_AllForYou"},
                                {"szAugmentName": "ARAM_ADAPt"},
                            ]
                        },
                    },
                    puuid,
                    t0 + 1.0,
                ),
                key,
            )
        )
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 2.0),
                key,
            )
        )
        acts = sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 696,
                        "params": {
                            "astPlayerHextechAugmentSelectionList": [
                                {"dwHeroNetID": 301, "astAugments": [{"szAugmentName": "ARAM_Stats"}]},
                                {"dwHeroNetID": 302, "astAugments": [{"szAugmentName": "ARAM_BacktoBasics"}]},
                            ]
                        },
                    },
                    puuid,
                    t0 + 2.3,
                ),
                key,
            )
        )
        self.assertEqual(sm.local_hero_net_id, 302)
        self.assertEqual(acts[0].kind, "reconcile_owned")
        self.assertEqual(acts[0].keys, ["ARAM_BacktoBasics"])

    def test_ambiguous_first_transmute_narrows_on_second_pick(self) -> None:
        puuid = "synthetic-narrow-001"
        key = derive_tencent_log_key(puuid)
        sm = TencentAugmentStateMachine()
        t0 = 1795654000.0

        # Round 1 (Lv 3): Transmute Gold -> two players both go 0 -> 1 simultaneously
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 693,
                        "params": {
                            "astAugments": [
                                {"szAugmentName": "ARAM_TransmuteGold"},
                                {"szAugmentName": "ARAM_Stats"},
                                {"szAugmentName": "ARAM_ADAPt"},
                            ]
                        },
                    },
                    puuid,
                    t0,
                ),
                key,
            )
        )
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 1.0),
                key,
            )
        )
        acts1 = sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 696,
                        "params": {
                            "astPlayerHextechAugmentSelectionList": [
                                {"dwHeroNetID": 401, "astAugments": [{"szAugmentName": "ARAM_AllForYou"}]},
                                {"dwHeroNetID": 402, "astAugments": [{"szAugmentName": "ARAM_ApexInventor"}]},
                            ]
                        },
                    },
                    puuid,
                    t0 + 1.5,
                ),
                key,
            )
        )
        self.assertIsNone(sm.local_hero_net_id)
        self.assertEqual(acts1, [])

        # Round 2 (Lv 7): Normal pick ARAM_Eureka -> narrows down to 402 and backfills Lv 3 + Lv 7
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 693,
                        "params": {
                            "astAugments": [
                                {"szAugmentName": "ARAM_Eureka"},
                                {"szAugmentName": "ARAM_Stats"},
                                {"szAugmentName": "ARAM_ADAPt"},
                            ]
                        },
                    },
                    puuid,
                    t0 + 60.0,
                ),
                key,
            )
        )
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 61.0),
                key,
            )
        )
        acts2 = sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 696,
                        "params": {
                            "astPlayerHextechAugmentSelectionList": [
                                {"dwHeroNetID": 401, "astAugments": [{"szAugmentName": "ARAM_AllForYou"}, {"szAugmentName": "ARAM_ADAPt"}]},
                                {"dwHeroNetID": 402, "astAugments": [{"szAugmentName": "ARAM_ApexInventor"}, {"szAugmentName": "ARAM_Eureka"}]},
                            ]
                        },
                    },
                    puuid,
                    t0 + 61.4,
                ),
                key,
            )
        )
        self.assertEqual(sm.local_hero_net_id, 402)
        self.assertEqual(acts2[0].kind, "reconcile_owned")
        self.assertEqual(acts2[0].keys, ["ARAM_ApexInventor", "ARAM_Eureka"])

    def test_pandoras_box_clears_stable_keys_and_reconciles(self) -> None:
        puuid = "synthetic-pandora-001"
        key = derive_tencent_log_key(puuid)
        sm = TencentAugmentStateMachine()
        t0 = 1795655000.0

        # Pick 1: normal augment
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 693,
                        "params": {"astAugments": [{"szAugmentName": "ARAM_Stats"}, {"szAugmentName": "ARAM_ADAPt"}, {"szAugmentName": "ARAM_AllForYou"}]},
                    },
                    puuid,
                    t0,
                ),
                key,
            )
        )
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 1.0),
                key,
            )
        )
        # Pick 2: Pandora's Box before any 696 bound yet
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 693,
                        "params": {"astAugments": [{"szAugmentName": "ARAM_PandorasBox"}, {"szAugmentName": "ARAM_Eureka"}, {"szAugmentName": "ARAM_BacktoBasics"}]},
                    },
                    puuid,
                    t0 + 20.0,
                ),
                key,
            )
        )
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 21.0),
                key,
            )
        )
        acts = sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 696,
                        "params": {
                            "astPlayerHextechAugmentSelectionList": [
                                {"dwHeroNetID": 501, "astAugments": [{"szAugmentName": "ARAM_Eureka"}, {"szAugmentName": "ARAM_BacktoBasics"}]},
                            ]
                        },
                    },
                    puuid,
                    t0 + 21.5,
                ),
                key,
            )
        )
        self.assertEqual(sm.local_hero_net_id, 501)
        self.assertEqual(acts[0].keys, ["ARAM_Eureka", "ARAM_BacktoBasics"])

    def test_same_millisecond_696_not_dropped_and_engine_reconcile(self) -> None:
        puuid = "synthetic-ms-001"
        key = derive_tencent_log_key(puuid)
        sm = TencentAugmentStateMachine()
        t0 = 1795656000.123

        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 693,
                        "params": {"astAugments": [{"szAugmentName": "ARAM_TransmuteChaos"}, {"szAugmentName": "ARAM_Stats"}, {"szAugmentName": "ARAM_ADAPt"}]},
                    },
                    puuid,
                    t0,
                ),
                key,
            )
        )
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 1.0),
                key,
            )
        )
        # Two 696 lines emitted within the exact same millisecond (t0 + 1.200)
        self.assertEqual(
            sm.observe(
                decode_tencent_log_line(  # type: ignore[arg-type]
                    make_log_line(
                        {
                            "form": "game",
                            "dwSubCmd": 696,
                            "params": {"astPlayerHextechAugmentSelectionList": [{"dwHeroNetID": 601, "astAugments": []}]},
                        },
                        puuid,
                        t0 + 1.2,
                    ),
                    key,
                )
            ),
            [],
        )
        acts = sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 696,
                        "params": {
                            "astPlayerHextechAugmentSelectionList": [
                                {"dwHeroNetID": 601, "astAugments": [{"szAugmentName": "ARAM_Eureka"}, {"szAugmentName": "BrandNewHotfixAugment"}]},
                            ]
                        },
                    },
                    puuid,
                    t0 + 1.2,
                ),
                key,
            )
        )
        self.assertEqual(sm.local_hero_net_id, 601)
        self.assertEqual(acts[0].keys, ["ARAM_Eureka", "BrandNewHotfixAugment"])

        # Verify OverlayEngine replaces ARAM_TransmuteChaos cleanly, inherits Lv.3, and keeps unknown hotfix keys
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            engine = OverlayEngine(base / "data", base / "cache", enable_background_monitors=False)
            engine._state.level = 3
            engine._apply_tencent_action(StateAction(kind="picked", at=t0 + 1.0, picked_index=0, picked_key="ARAM_TransmuteChaos"))
            self.assertEqual([p.key for p in engine._state.picks], ["ARAM_TransmuteChaos"])
            engine._state.level = 4
            applied = engine._apply_tencent_action(acts[0])
            self.assertTrue(applied)
            picks = engine._state.picks
            self.assertEqual([p.key for p in picks], ["ARAM_Eureka", "BrandNewHotfixAugment"])
            self.assertEqual([p.level for p in picks], [3, 3])
            self.assertEqual(picks[1].name, "BrandNewHotfixAugment")

    def test_transmute_gold_chaos_and_pandora_chained_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            engine = OverlayEngine(base / "data", base / "cache", enable_background_monitors=False)
            engine._state.champion_alias = "Ezreal"
            engine._state.game_mode = "KIWI"
            engine._state.game_time = 30.0
            engine._state.level = 3

            # Lv.3: Transmute Chaos splits into Tank Engine + Infernal Conduit sharing origin_group_id
            engine._apply_tencent_action(StateAction(kind="picked", at=1.0, picked_index=0, picked_key="ARAM_TransmuteChaos"))
            engine._apply_tencent_action(
                StateAction(kind="reconcile_owned", at=1.5, keys=["ARAM_TankEngine", "ARAM_InfernalConduit"])
            )
            p0, p1 = engine._state.picks
            self.assertIsNotNone(p0.origin_group_id)
            self.assertEqual(p0.origin_group_id, p1.origin_group_id)
            self.assertEqual(p0.origin_name, "质变：混沌")
            self.assertEqual([s.via_key for s in p0.lineage], ["ARAM_TransmuteChaos"])
            self.assertEqual([s.via_key for s in p1.lineage], ["ARAM_TransmuteChaos"])

            # Lv.7: Transmute Gold -> Apex Inventor (keeps prior group picks intact)
            engine._state.level = 7
            engine._state.game_time = 240.0
            engine._apply_tencent_action(StateAction(kind="picked", at=240.0, picked_index=0, picked_key="ARAM_TransmuteGold"))
            engine._apply_tencent_action(
                StateAction(
                    kind="reconcile_owned",
                    at=241.0,
                    keys=["ARAM_TankEngine", "ARAM_InfernalConduit", "ARAM_ApexInventor"],
                )
            )
            self.assertEqual(len(engine._state.picks), 3)
            self.assertEqual(engine._state.picks[0].uid, p0.uid)
            self.assertEqual(engine._state.picks[2].level, 7)
            self.assertIsNone(engine._state.picks[2].origin_group_id)
            self.assertEqual([s.via_key for s in engine._state.picks[2].lineage], ["ARAM_TransmuteGold"])

            # Lv.11: Pandora's Box chains all 3 existing picks + adds its own slot
            engine._state.level = 11
            engine._state.game_time = 520.0
            engine._apply_tencent_action(StateAction(kind="picked", at=520.0, picked_index=1, picked_key="ARAM_PandorasBox"))
            engine._apply_tencent_action(
                StateAction(
                    kind="reconcile_owned",
                    at=521.0,
                    keys=["ARAM_JeweledGauntlet", "ARAM_Eureka", "ARAM_BladeWaltz", "ARAM_ClownCollege"],
                )
            )
            final = engine._state.picks
            self.assertEqual(len(final), 4)
            self.assertEqual(final[0].origin_group_id, p0.origin_group_id)
            self.assertEqual(final[1].origin_group_id, p0.origin_group_id)
            self.assertEqual(
                [(s.from_key, s.via_key) for s in final[0].lineage],
                [("", "ARAM_TransmuteChaos"), ("ARAM_TankEngine", "ARAM_PandorasBox")],
            )
            self.assertEqual(
                [(s.from_key, s.via_key) for s in final[2].lineage],
                [("", "ARAM_TransmuteGold"), ("ARAM_ApexInventor", "ARAM_PandorasBox")],
            )
            self.assertEqual([(s.from_key, s.via_key) for s in final[3].lineage], [("", "ARAM_PandorasBox")])

            # Update metric and verify SessionStore round-trip restores lineage, group, and metrics
            updated = engine.update_pick_metrics(
                final[0].uid,
                [AugmentMetric(label="已造成伤害", value="6,120", kind="damage", source="manual")],
            )
            self.assertIsNotNone(updated)
            restored = SessionStore(base / "data" / "session_state.json").try_restore(
                champion_alias="Ezreal",
                game_mode="KIWI",
                game_time=530.0,
            )
            assert restored is not None
            r_picks, _ = restored
            self.assertEqual(r_picks[0].origin_group_id, p0.origin_group_id)
            self.assertEqual(len(r_picks[0].lineage), 2)
            self.assertEqual(r_picks[0].metrics[0].value, "6,120")

    def test_kingme_bonus_prismatic_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            engine = OverlayEngine(base / "data", base / "cache", enable_background_monitors=False)
            engine._state.level = 7
            engine._apply_tencent_action(StateAction(kind="picked", at=10.0, picked_index=0, picked_key="KingMe"))
            self.assertEqual([p.key for p in engine._state.picks], ["KingMe"])

            engine._state.level = 10
            engine._apply_tencent_action(StateAction(kind="reconcile_owned", at=120.0, keys=["KingMe", "ARAM_Eureka"]))
            self.assertEqual([p.key for p in engine._state.picks], ["KingMe", "ARAM_Eureka"])
            self.assertEqual([s.via_key for s in engine._state.picks[1].lineage], ["KingMe"])

    def test_stat_anvils_ignored_and_do_not_pollute_binding(self) -> None:
        self.assertTrue(is_stat_anvil_key("ARAM_StatAnvil_Hybrid2"))
        self.assertTrue(is_stat_anvil_key("ARAM_GoldStatAnvil_HP"))
        self.assertFalse(is_stat_anvil_key("ARAM_Eureka"))

        puuid = "synthetic-anvil-001"
        key = derive_tencent_log_key(puuid)
        sm = TencentAugmentStateMachine()
        t0 = 1795658000.0

        # Stat Anvil 693 -> 694 -> empty 693 must produce no candidate/pick actions and stay out of stable keys
        r_anvil_cands = decode_tencent_log_line(
            make_log_line(
                {
                    "form": "game",
                    "dwSubCmd": 693,
                    "params": {
                        "astAugments": [
                            {"szAugmentName": "ARAM_StatAnvil_Hybrid2"},
                            {"szAugmentName": "ARAM_StatAnvil_Lethality"},
                            {"szAugmentName": "ARAM_StatAnvil_AS"},
                            {"szAugmentName": ""},
                        ]
                    },
                },
                puuid,
                t0,
            ),
            key,
        )
        r_anvil_empty = decode_tencent_log_line(
            make_log_line(
                {"form": "game", "dwSubCmd": 693, "params": {"astAugments": [{"szAugmentName": ""}, {"szAugmentName": ""}, {"szAugmentName": ""}]}},
                puuid,
                t0 + 1.0,
            ),
            key,
        )
        r_anvil_pick = decode_tencent_log_line(
            make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 1}}, puuid, t0 + 1.2),
            key,
        )
        assert r_anvil_cands and r_anvil_empty and r_anvil_pick
        self.assertEqual(sm.observe(r_anvil_cands), [])
        self.assertEqual(sm.observe(r_anvil_empty), [])
        self.assertEqual(sm.observe(r_anvil_pick), [])

        # Gold Stat Anvil offer too
        r_gold_anvil = decode_tencent_log_line(
            make_log_line(
                {
                    "form": "game",
                    "dwSubCmd": 693,
                    "params": {
                        "astAugments": [
                            {"szAugmentName": "ARAM_GoldStatAnvil_Hybrid3"},
                            {"szAugmentName": "ARAM_GoldStatAnvil_HP"},
                            {"szAugmentName": "ARAM_GoldStatAnvil_Hybrid1"},
                        ]
                    },
                },
                puuid,
                t0 + 5.0,
            ),
            key,
        )
        r_gold_pick = decode_tencent_log_line(
            make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 5.5),
            key,
        )
        assert r_gold_anvil and r_gold_pick
        self.assertEqual(sm.observe(r_gold_anvil), [])
        self.assertEqual(sm.observe(r_gold_pick), [])
        self.assertEqual(sm._confirmed_stable_keys, [])

        # Real augment offer afterwards still binds cleanly via 694 -> 696
        sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 693,
                        "params": {"astAugments": [{"szAugmentName": "ARAM_Eureka"}, {"szAugmentName": "ARAM_Stats"}, {"szAugmentName": "ARAM_ADAPt"}]},
                    },
                    puuid,
                    t0 + 20.0,
                ),
                key,
            )
        )
        pick_acts = sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 21.0),
                key,
            )
        )
        self.assertEqual(pick_acts[0].picked_key, "ARAM_Eureka")
        self.assertEqual(sm._confirmed_stable_keys, ["ARAM_Eureka"])

        rec_acts = sm.observe(
            decode_tencent_log_line(  # type: ignore[arg-type]
                make_log_line(
                    {
                        "form": "game",
                        "dwSubCmd": 696,
                        "params": {
                            "astPlayerHextechAugmentSelectionList": [
                                {"dwHeroNetID": 701, "astAugments": [{"szAugmentName": "ARAM_Stats"}]},
                                {"dwHeroNetID": 702, "astAugments": [{"szAugmentName": "ARAM_Eureka"}]},
                            ]
                        },
                    },
                    puuid,
                    t0 + 21.5,
                ),
                key,
            )
        )
        self.assertEqual(sm.local_hero_net_id, 702)
        self.assertEqual(rec_acts[0].keys, ["ARAM_Eureka"])

    def test_gdp_status_detection_and_probe(self) -> None:
        puuid = "synthetic-gdp-001"
        key = derive_tencent_log_key(puuid)
        t0 = 1795659000.0

        ln_false = make_raw_log_line("settingEnableChange gdp false", puuid, t0)
        ln_true = make_raw_log_line("settingEnableChange gdp true", puuid, t0 + 10.0)
        ln_game = make_log_line({"form": "game", "dwSubCmd": 694, "params": {"dwAugmentIndex": 0}}, puuid, t0 + 20.0)

        rec_f = decode_tencent_log_line(ln_false, key)
        rec_t = decode_tencent_log_line(ln_true, key)
        assert rec_f and rec_t
        self.assertEqual(rec_f.sub_cmd, -1)
        self.assertIs(rec_f.gdp_enabled, False)
        self.assertEqual(rec_t.sub_cmd, -1)
        self.assertIs(rec_t.gdp_enabled, True)

        sm = TencentAugmentStateMachine()
        self.assertEqual(sm.observe(rec_f), [])
        self.assertTrue(sm.is_stale(t0))

        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "main.log"
            log_path.write_text(ln_true + ln_false, encoding="utf-8")
            self.assertIs(probe_tencent_gdp_enabled(log_path, key), False)

            log_path.write_text(ln_false + ln_game, encoding="utf-8")
            self.assertIs(probe_tencent_gdp_enabled(log_path, key), True)


if __name__ == "__main__":
    unittest.main()
