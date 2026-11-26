"""Integration tests for FastAPI routes, WebSocket snapshots, Demo flow, and security checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from hextech_overlay.engine import OverlayEngine
from hextech_overlay.server import create_app


class TestServerApi(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.engine = OverlayEngine(
            data_dir=base / "data",
            cache_dir=base / "cache",
            enable_background_monitors=False,
        )
        self.app = create_app(self.engine)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_pages_and_icon_security(self) -> None:
        r_ov = self.client.get("/overlay")
        self.assertEqual(r_ov.status_code, 200)
        self.assertIn("海克斯", r_ov.text)

        r_ctrl = self.client.get("/control")
        self.assertEqual(r_ctrl.status_code, 200)
        self.assertIn("OBS", r_ctrl.text)

        # Path traversal guard
        r_bad = self.client.get("/api/icon/..%5Csecret.png")
        self.assertEqual(r_bad.status_code, 400)

        # Offline missing icon falls back to clean inline SVG (never broken image)
        r_fb = self.client.get("/api/icon/fallback")
        self.assertEqual(r_fb.status_code, 200)
        self.assertIn("image/svg+xml", r_fb.headers["content-type"])

    def test_picks_crud_config_and_demo(self) -> None:
        # Search augments
        res = self.client.get("/api/augments", params={"q": "尖端"})
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertTrue(len(items) >= 1)
        aug_id = items[0]["id"]

        # Add, replace, undo, clear
        add_r = self.client.post("/api/picks/add", json={"augment_id": aug_id, "level": 3})
        self.assertEqual(add_r.status_code, 200)
        uid = add_r.json()["pick"]["uid"]

        other = self.client.get("/api/augments", params={"q": "回归"}).json()["items"][0]
        rep_r = self.client.post("/api/picks/replace", json={"uid": uid, "augment_id": other["id"]})
        self.assertEqual(rep_r.status_code, 200)
        self.assertEqual(rep_r.json()["pick"]["augment_id"], other["id"])

        self.client.post("/api/picks/undo")
        self.assertEqual(len(self.client.get("/api/state").json()["state"]["picks"]), 0)

        # Config persistence
        cfg_r = self.client.post("/api/config", json={"theme": "showcase", "scale": 1.25})
        self.assertEqual(cfg_r.status_code, 200)
        self.assertEqual(cfg_r.json()["theme"], "showcase")
        self.assertEqual(cfg_r.json()["scale"], 1.25)

        # Demo step progression + WebSocket check
        with self.client.websocket_connect("/ws") as ws:
            snap = ws.receive_json()
            self.assertEqual(snap["type"], "snapshot")

            self.client.post("/api/demo/start", json={"auto_play": False})
            finished = False
            for _ in range(6):
                step_res = self.client.post("/api/demo/next").json()
                finished = step_res["finished"]
                if finished:
                    break
            self.assertTrue(finished)
            final_picks = self.client.get("/api/state").json()["state"]["picks"]
            self.assertEqual(len(final_picks), 5)

            self.client.post("/api/demo/reset")
            self.assertEqual(len(self.client.get("/api/state").json()["state"]["picks"]), 0)


if __name__ == "__main__":
    unittest.main()
