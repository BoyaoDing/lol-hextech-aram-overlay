"""Unit tests for bundled augment catalog loading, OCR matching, and search."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hextech_overlay.catalog import AugmentCatalog, clean_description, normalize_title


class TestCatalogAndMatcher(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.catalog = AugmentCatalog(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bundled_catalog_loaded_and_populated(self) -> None:
        self.assertGreater(self.catalog.count, 500)
        self.assertGreater(self.catalog.mayhem_pool_count, 250)
        rarities = {a.rarity for a in self.catalog.all_augments()}
        self.assertEqual(rarities, {"silver", "gold", "prismatic"})

        apex = self.catalog.get_by_key("ARAM_ApexInventor")
        self.assertIsNotNone(apex)
        assert apex is not None
        self.assertEqual(apex.name, "尖端发明家")
        self.assertEqual(apex.rarity, "gold")
        self.assertTrue(apex.description)

        # Exact & case-insensitive match + ARAM_ prefix fallback
        self.assertEqual(self.catalog.get_by_key("aram_apexinventor").id, apex.id)  # type: ignore[union-attr]
        all_keys_lower = {a.key.lower() for a in self.catalog.all_augments()}
        aram_only = next(
            a for a in self.catalog.all_augments()
            if a.key.startswith("ARAM_") and a.key[5:].lower() not in all_keys_lower
        )
        self.assertEqual(self.catalog.get_by_key(aram_only.key[5:]).id, aram_only.id)  # type: ignore[union-attr]

    def test_ocr_matching_and_safety_guards(self) -> None:
        self.assertEqual(normalize_title("  质变：棱彩阶！ "), "质变棱彩阶")

        # Exact match
        aug, score = self.catalog.match_ocr_text("尖端发明家")
        self.assertIsNotNone(aug)
        assert aug is not None
        self.assertEqual(aug.name, "尖端发明家")
        self.assertEqual(score, 1.0)

        # 1-char OCR typo tolerance
        aug2, score2 = self.catalog.match_ocr_text("回归基本工")
        self.assertIsNotNone(aug2)
        assert aug2 is not None
        self.assertEqual(aug2.name, "回归基本功")
        self.assertGreaterEqual(score2, 0.72)

        # Blacklist and short garbage rejection
        for noise in ("攻击", "技能", "刷新", "a", ""):
            m, s = self.catalog.match_ocr_text(noise)
            self.assertIsNone(m)
            self.assertEqual(s, 0.0)

    def test_search_and_description_cleaning(self) -> None:
        hits = self.catalog.search("质变", rarity="prismatic")
        self.assertTrue(any("质变" in h.name for h in hits))
        cleaned = clean_description("获得<scaleAH>100</scaleAH>技能急速%i:scaleAH%<br>每秒触发一次")
        self.assertEqual(cleaned, "获得100技能急速 每秒触发一次")


if __name__ == "__main__":
    unittest.main()
