"""Unit tests for 3/4-card layout geometry, click zoning, and visual selection state machine."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from hextech_overlay.catalog import AugmentCatalog
from hextech_overlay.models import PickedAugment
from hextech_overlay.vision_detector import (
    FOUR_CARD_LAYOUT,
    THREE_CARD_LAYOUT,
    GameGeometry,
    VisualSelectionStateMachine,
    classify_click_zone,
    detect_card_layout,
    get_card_geometry,
    parse_tooltip_metrics,
    title_crop_boxes,
    tooltip_crop_rect,
)


def synthesize_card_frame(width: int, height: int, layout) -> np.ndarray:
    """Draw synthetic bright card borders and dark card interiors matching LoL's modal layout."""
    frame = np.full((height, width, 3), 30, dtype=np.uint8)
    cw, cg, _, gl = get_card_geometry(width, height, layout)
    y_top = int(round(height * 0.16))
    y_bot = int(round(height * 0.67))
    border_thick = max(3, int(round(height * 0.015)))

    for slot in range(layout.slot_count):
        left = int(round(gl + slot * (cw + cg)))
        right = int(round(left + cw))
        # Dark interior
        frame[y_top:y_bot, left:right] = (20, 22, 28)
        # Gold border top, left, right
        frame[y_top : y_top + border_thick, left:right] = (195, 165, 110)
        frame[y_top:y_bot, left : left + border_thick] = (195, 165, 110)
        frame[y_top:y_bot, right - border_thick : right] = (195, 165, 110)
    return frame


class TestVisionStateMachine(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.catalog = AugmentCatalog(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_geometry_and_layout_detection(self) -> None:
        g1080 = GameGeometry.from_frame(1920, 1080)
        self.assertEqual(g1080.off_x, 0)
        g_wide = GameGeometry.from_frame(3440, 1440)
        self.assertGreater(g_wide.off_x, 400)

        # 4-card frame at 1920x1080 and 1280x720
        f4 = synthesize_card_frame(1920, 1080, FOUR_CARD_LAYOUT)
        self.assertEqual(detect_card_layout(f4), FOUR_CARD_LAYOUT)
        self.assertEqual(len(title_crop_boxes(1920, 1080, FOUR_CARD_LAYOUT)), 4)

        f3 = synthesize_card_frame(1280, 720, THREE_CARD_LAYOUT)
        self.assertEqual(detect_card_layout(f3), THREE_CARD_LAYOUT)

        # Plain gameplay frame should NOT trigger card gate
        blank = np.full((540, 960, 3), 45, dtype=np.uint8)
        self.assertIsNone(detect_card_layout(blank))

        # Prismatic holographic colorful borders with a curved center notch still match
        f_prism = np.full((540, 960, 3), 30, dtype=np.uint8)
        cw, cg, _, gl = get_card_geometry(960, 540, THREE_CARD_LAYOUT)
        y_top = int(round(540 * 0.16))
        y_bot = int(round(540 * 0.67))
        for slot in range(3):
            left = int(round(gl + slot * (cw + cg)))
            right = int(round(left + cw))
            f_prism[y_top:y_bot, left:right] = (18, 20, 26)
            mid_l = left + int(cw * 0.42)
            mid_r = left + int(cw * 0.58)
            f_prism[y_top : y_top + 4, left:mid_l] = (139, 233, 207)
            f_prism[y_top : y_top + 4, mid_r:right] = (201, 154, 245)
            f_prism[y_top:y_bot, left : left + 4] = (201, 154, 245)
            f_prism[y_top:y_bot, right - 4 : right] = (139, 233, 207)
        self.assertEqual(detect_card_layout(f_prism), THREE_CARD_LAYOUT)

        # Bright snowy/floor gameplay frame without dark card interiors must NOT trigger
        bright = np.full((540, 960, 3), (175, 190, 205), dtype=np.uint8)
        self.assertIsNone(detect_card_layout(bright))

    def test_click_classification_and_hide_revert(self) -> None:
        sm = VisualSelectionStateMachine(self.catalog)
        a0 = self.catalog.get_by_key("ARAM_ApexInventor")
        a1 = self.catalog.get_by_key("ARAM_Stats")
        a2 = self.catalog.get_by_key("ARAM_ADAPt")
        assert a0 and a1 and a2

        # Open 3-card panel
        out1 = sm.process_frame(
            10.0,
            THREE_CARD_LAYOUT,
            stats=[{"mean": 50, "bright_pct": 1.0}] * 3,
            ocr_reader=lambda: {0: (a0, 1.0), 1: (a1, 1.0), 2: (a2, 1.0)},
        )
        self.assertTrue(out1.panel_open)
        self.assertEqual([c[1].id for c in out1.candidates if c[1]], [a0.id, a1.id, a2.id])

        # Click middle card (slot 1)
        cw, cg, _, gl = get_card_geometry(1920, 1080, THREE_CARD_LAYOUT)
        mid_x = (gl + 1 * (cw + cg) + cw * 0.5) / 1920.0
        zone, slot = classify_click_zone(mid_x, 0.40, THREE_CARD_LAYOUT, 1920, 1080)
        self.assertEqual((zone, slot), ("card", 1))
        sm.record_click(11.0, mid_x, 0.40, 1920, 1080)

        # Reroll button check
        rz, rslot = classify_click_zone(mid_x, 0.72, THREE_CARD_LAYOUT, 1920, 1080)
        self.assertEqual((rz, rslot), ("reroll", 1))

        # Hide button check
        hz, _ = classify_click_zone(0.50, 0.85, THREE_CARD_LAYOUT, 1920, 1080)
        self.assertEqual(hz, "hide")

        # Panel closes across 3 miss frames -> confirms click pick
        sm.process_frame(11.2, None)
        sm.process_frame(11.4, None)
        closed = sm.process_frame(11.6, None)
        self.assertFalse(closed.panel_open)
        self.assertIsNotNone(closed.picked)
        assert closed.picked is not None
        self.assertEqual(closed.picked.augment.id, a1.id)
        self.assertEqual(closed.picked.source, "vision_click")

        # Re-opening exact same 3 cards within 180s means it was only hidden -> revert pick
        reopen = sm.process_frame(
            15.0,
            THREE_CARD_LAYOUT,
            stats=[{"mean": 50, "bright_pct": 1.0}] * 3,
            ocr_reader=lambda: {0: (a0, 1.0), 1: (a1, 1.0), 2: (a2, 1.0)},
        )
        self.assertEqual(reopen.reverted_augment_id, a1.id)

    def test_flare_fallback_pick(self) -> None:
        sm = VisualSelectionStateMachine(self.catalog)
        a0 = self.catalog.get_by_key("ARAM_ApexInventor")
        a1 = self.catalog.get_by_key("ARAM_Stats")
        a2 = self.catalog.get_by_key("ARAM_ADAPt")
        assert a0 and a1 and a2

        sm.process_frame(
            20.0,
            THREE_CARD_LAYOUT,
            stats=[{"mean": 50, "bright_pct": 1.0}, {"mean": 50, "bright_pct": 1.0}, {"mean": 50, "bright_pct": 1.0}],
            ocr_reader=lambda: {0: (a0, 1.0), 1: (a1, 1.0), 2: (a2, 1.0)},
        )
        # Slot 2 flashes bright white right before closing
        sm.process_frame(
            21.5,
            THREE_CARD_LAYOUT,
            stats=[{"mean": 50, "bright_pct": 1.0}, {"mean": 52, "bright_pct": 2.0}, {"mean": 180, "bright_pct": 42.0}],
        )
        sm.process_frame(21.6, None)
        sm.process_frame(21.7, None)
        closed = sm.process_frame(21.8, None)
        self.assertIsNotNone(closed.picked)
        assert closed.picked is not None
        self.assertEqual(closed.picked.augment.id, a2.id)
        self.assertEqual(closed.picked.source, "vision_flare")

    def test_tooltip_crop_and_ocr_metrics_parsing(self) -> None:
        game = (100, 50, 1920, 1080)
        # Bottom HUD cursor triggers valid clamped crop
        crop_hud = tooltip_crop_rect(game, (960, 1040), tab_held=False)
        self.assertIsNotNone(crop_hud)
        assert crop_hud is not None
        x0, y0, cw, ch = crop_hud
        self.assertGreaterEqual(x0, 100)
        self.assertGreaterEqual(y0, 50)
        self.assertLessEqual(x0 + cw, 2020)
        self.assertLessEqual(y0 + ch, 1130)

        # Mid-screen without Tab does not capture tooltips
        self.assertIsNone(tooltip_crop_rect(game, (960, 500), tab_held=False))
        self.assertIsNotNone(tooltip_crop_rect(game, (960, 500), tab_held=True))

        tank = self.catalog.get_by_key("ARAM_TankEngine")
        conduit = self.catalog.get_by_key("ARAM_InfernalConduit")
        assert tank and conduit
        owned = [
            PickedAugment(uid="u1", augment_id=tank.id, key=tank.key, name=tank.name, rarity=tank.rarity, picked_at=1.0),
            PickedAugment(uid="u2", augment_id=conduit.id, key=conduit.key, name=conduit.name, rarity=conduit.rarity, picked_at=2.0),
        ]

        res_tank = parse_tooltip_metrics(
            ["坦 克 引 擎", "已 叠 加 ： 14 层", "已 提 供 护 盾 ： 1,280"],
            owned,
            self.catalog,
        )
        assert res_tank is not None
        uid, metrics = res_tank
        self.assertEqual(uid, "u1")
        by_kind = {m.kind: m for m in metrics}
        self.assertEqual(by_kind["stack"].value, "14层")
        self.assertEqual(by_kind["shield"].value, "1,280")

        res_dmg = parse_tooltip_metrics(
            ["炼 狱 导 管", "已 造 成 伤 害 ： 5,420", "已 减 少 冷 却 ： 32 秒"],
            owned,
            self.catalog,
        )
        assert res_dmg is not None
        self.assertEqual(res_dmg[0], "u2")
        self.assertEqual([(m.kind, m.label, m.value) for m in res_dmg[1]], [
            ("damage", "已造成伤害", "5,420"),
            ("stat", "减少冷却", "32秒"),
        ])

        # Non-owned item tooltip must be ignored
        self.assertIsNone(parse_tooltip_metrics(["兰 顿 之 兆", "已 造 成 伤 害 ： 2,000"], owned, self.catalog))


if __name__ == "__main__":
    unittest.main()
