"""Main orchestration engine coordinating LCU, Live Client API, Tencent logs, Vision OCR, and Demo mode."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

from .catalog import AugmentCatalog
from .config import ConfigStore, SessionStore
from .lcu import LcuClient
from .live_api import LiveClientApi, LiveSnapshot
from .models import (
    AugmentInfo,
    AugmentMetric,
    CandidateSlot,
    ChannelHealth,
    DiagnosticEvent,
    GameState,
    OverlayConfig,
    PickedAugment,
    PickSource,
    TransformStep,
)
from .tencent_log import (
    TencentAugmentStateMachine,
    TencentLogTail,
    classify_augment_transform,
    decode_tencent_log_line,
    derive_tencent_log_key,
    is_tencent_region,
    locate_icreate_log,
)
from .vision_detector import (
    PassiveMouseTracker,
    VisualSelectionStateMachine,
    WindowsOcrWorker,
    capture_rect_rgb,
    compute_card_stats,
    detect_card_layout,
    find_game_window_rect,
    get_cursor_pos,
    is_tab_key_down,
    parse_tooltip_metrics,
    title_crop_boxes,
    tooltip_crop_rect,
)


class OverlayEngine:
    """Thread-safe core service driving augment detection and broadcasting state updates."""

    def __init__(
        self,
        data_dir: Path,
        cache_dir: Path,
        on_change: Callable[[], None] | None = None,
        enable_background_monitors: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cache_dir = Path(cache_dir)
        self.config_store = ConfigStore(self.data_dir / "config.json")
        self.session_store = SessionStore(self.data_dir / "session_state.json")
        self.catalog = AugmentCatalog(self.cache_dir)
        self.lcu = LcuClient()
        self.live_api = LiveClientApi()

        self.tencent_sm = TencentAugmentStateMachine()
        self.tencent_tail: TencentLogTail | None = None
        self.tencent_key: bytes | None = None
        self.tencent_log_path: Path | None = None

        self.vision_sm = VisualSelectionStateMachine(self.catalog)
        self.ocr_worker = WindowsOcrWorker(self.cache_dir / "tmp")
        self.mouse_tracker = PassiveMouseTracker()

        self._on_change = on_change
        self._enable_bg = enable_background_monitors
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._demo_timer: threading.Timer | None = None

        self._state = GameState(
            catalog_count=self.catalog.count,
            mayhem_pool_count=self.catalog.mayhem_pool_count,
        )
        self._lcu_synced_catalog = False
        self._last_lcu_poll = 0.0
        self._last_live_poll = 0.0
        self._last_vision_scan = 0.0
        self._last_tooltip_scan = 0.0
        self._hover_anchor: tuple[int, int] | None = None
        self._hover_since = 0.0
        self._last_game_time = 0.0
        self._last_champion_alias = ""
        self._in_active_match = False
        self._ocr_title_cache: dict[int, tuple[AugmentInfo | None, float]] = {}

    def set_on_change(self, cb: Callable[[], None] | None) -> None:
        self._on_change = cb

    def _notify(self) -> None:
        cb = self._on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def start(self) -> None:
        if not self._enable_bg:
            return
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._run_loop, name="overlay-engine", daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._cancel_demo_timer()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2.5)
            self._worker_thread = None
        self.ocr_worker.close()

    def snapshot(self) -> tuple[GameState, OverlayConfig]:
        with self._lock:
            self._state.catalog_count = self.catalog.count
            self._state.mayhem_pool_count = self.catalog.mayhem_pool_count
            return self._state.model_copy(deep=True), self.config_store.get()

    def update_config(self, patch: dict[str, Any]) -> OverlayConfig:
        cfg = self.config_store.update(patch)
        self._notify()
        return cfg

    # ------------------------------------------------------------------ Manual / UI actions

    def add_pick(
        self,
        augment_id: int,
        level: int | None = None,
        source: PickSource = "manual",
        slot_index: int | None = None,
    ) -> PickedAugment | None:
        aug = self.catalog.get_by_id(augment_id)
        if aug is None:
            return None
        with self._lock:
            pick = PickedAugment(
                uid=uuid.uuid4().hex[:10],
                augment_id=aug.id,
                key=aug.key,
                name=aug.name,
                rarity=aug.rarity,
                icon_path=aug.icon_path,
                description=aug.description,
                level=level if level is not None else (self._state.level or None),
                game_time=self._state.game_time or None,
                picked_at=time.time(),
                source=source,
                slot_index=slot_index,
            )
            self._state.picks.append(pick)
            self._add_event_locked("info", source, f"添加海克斯：{aug.name}（{aug.rarity}）")
            self._persist_session_locked()
        self._notify()
        return pick

    def replace_pick(self, uid: str, new_augment_id: int) -> PickedAugment | None:
        aug = self.catalog.get_by_id(new_augment_id)
        if aug is None:
            return None
        with self._lock:
            for idx, item in enumerate(self._state.picks):
                if item.uid == uid:
                    updated = PickedAugment(
                        uid=item.uid,
                        augment_id=aug.id,
                        key=aug.key,
                        name=aug.name,
                        rarity=aug.rarity,
                        icon_path=aug.icon_path,
                        description=aug.description,
                        level=item.level,
                        game_time=item.game_time,
                        picked_at=item.picked_at,
                        source="manual",
                        slot_index=item.slot_index,
                    )
                    self._state.picks[idx] = updated
                    self._add_event_locked("info", "manual", f"将「{item.name}」替换为「{aug.name}」")
                    self._persist_session_locked()
                    self._notify()
                    return updated
        return None

    def remove_pick(self, uid: str) -> bool:
        with self._lock:
            for idx, item in enumerate(self._state.picks):
                if item.uid == uid:
                    removed = self._state.picks.pop(idx)
                    self._add_event_locked("info", "manual", f"移除海克斯：{removed.name}")
                    self._persist_session_locked()
                    self._notify()
                    return True
        return False

    def undo_pick(self) -> PickedAugment | None:
        with self._lock:
            if not self._state.picks:
                return None
            removed = self._state.picks.pop()
            self._add_event_locked("info", "manual", f"撤销最后一个海克斯：{removed.name}")
            self._persist_session_locked()
        self._notify()
        return removed

    def update_pick_metrics(self, uid: str, metrics: list[AugmentMetric]) -> PickedAugment | None:
        now = time.time()
        cleaned = [
            m.model_copy(update={"updated_at": m.updated_at if m.updated_at is not None else now})
            for m in metrics
            if m.label.strip() and m.value.strip()
        ]
        with self._lock:
            for idx, item in enumerate(self._state.picks):
                if item.uid == uid:
                    updated = item.model_copy(update={"metrics": cleaned})
                    self._state.picks[idx] = updated
                    summary = "、".join(f"{m.label}:{m.value}" for m in cleaned) or "已清空"
                    self._add_event_locked("info", "metrics", f"更新数值「{item.name}」：{summary}")
                    self._persist_session_locked()
                    self._notify()
                    return updated
        return None

    def clear_picks(self) -> None:
        with self._lock:
            self._state.picks.clear()
            self._state.candidates.clear()
            self.tencent_sm.reset(keep_picks=False)
            self.vision_sm.reset()
            self.session_store.clear()
            self._add_event_locked("info", "manual", "已清空当前海克斯列表")
        self._notify()

    def refresh_catalog(self) -> tuple[bool, str]:
        cfg = self.config_store.get()
        if self.lcu.discover():
            ok, msg = self.catalog.refresh_from_lcu(self.lcu, locale=cfg.locale)
            if ok:
                self._add_event("info", "catalog", msg)
                self._notify()
                return True, msg
        try:
            ok, msg = self.catalog.refresh_from_cdragon(locale=cfg.locale, proxy_url=cfg.proxy_url)
            self._add_event("info" if ok else "warn", "catalog", msg)
            self._notify()
            return ok, msg
        except Exception as exc:
            msg = f"同步目录失败：{exc}"
            self._add_event("warn", "catalog", msg)
            return False, msg

    # ------------------------------------------------------------------ Demo simulation

    def _cancel_demo_timer(self) -> None:
        if self._demo_timer is not None:
            self._demo_timer.cancel()
            self._demo_timer = None

    def _demo_pick_by_keys(self, *keys: str) -> AugmentInfo:
        for k in keys:
            hit = self.catalog.get_by_key(k)
            if hit is not None:
                return hit
        pool = [a for a in self.catalog.all_augments() if a.in_mayhem_pool] or self.catalog.all_augments()
        return pool[0]

    def reset_demo(self) -> None:
        self._cancel_demo_timer()
        with self._lock:
            self._state.demo_active = False
            self._state.demo_step = 0
            self._state.picks.clear()
            self._state.candidates.clear()
            if not self._in_active_match:
                self._state.champion_alias = ""
                self._state.champion_name = ""
                self._state.level = 0
                self._state.game_time = 0.0
                self._state.game_mode = ""
                self._state.is_mayhem = False
                self._state.active_channel = "standby"
            self._add_event_locked("info", "demo", "已重置模拟演示")
        self._notify()

    def start_demo(self, auto_play: bool = True) -> None:
        self.reset_demo()
        with self._lock:
            self._state.demo_active = True
            self._state.demo_step = 0
            self._state.game_mode = "KIWI"
            self._state.is_mayhem = True
            self._state.champion_alias = "Ezreal"
            self._state.champion_name = "探险家 伊泽瑞尔"
            self._state.level = 3
            self._state.game_time = 22.0
            self._state.active_channel = "demo"
            self._add_event_locked("info", "demo", "开始模拟海克斯大乱斗对局演示")
        self._notify()
        if auto_play:
            self._schedule_demo_next(0.7)

    def _schedule_demo_next(self, delay_s: float) -> None:
        self._cancel_demo_timer()
        t = threading.Timer(delay_s, self._auto_advance_demo)
        t.daemon = True
        self._demo_timer = t
        t.start()

    def _auto_advance_demo(self) -> None:
        with self._lock:
            if not self._state.demo_active:
                return
        done = self.next_demo_step()
        if not done:
            self._schedule_demo_next(2.2)

    def next_demo_step(self) -> bool:
        """Advance one realistic Mayhem step; returns True when demo sequence finishes."""
        with self._lock:
            if not self._state.demo_active:
                self._state.demo_active = True
                self._state.demo_step = 0
                self._state.game_mode = "KIWI"
                self._state.is_mayhem = True
                self._state.champion_alias = "Ezreal"
                self._state.champion_name = "探险家 伊泽瑞尔"

            step = self._state.demo_step + 1
            self._state.demo_step = step
            self._state.active_channel = "demo"

            if step == 1:
                # Lv 3: 3-card selection featuring Transmute Chaos
                self._state.level = 3
                self._state.game_time = 25.0
                cands = [
                    self._demo_pick_by_keys("ARAM_TransmuteChaos", "TransmuteChaos"),
                    self._demo_pick_by_keys("ARAM_ApexInventor", "ApexInventor"),
                    self._demo_pick_by_keys("ARAM_Dashing", "Dashing"),
                ]
                self._state.candidates = self._to_candidate_slots(cands)
                self._add_event_locked("info", "demo", "3 级弹出海克斯三选一（含质变：混沌）")
                finished = False
            elif step == 2:
                # Pick Lv 3 Transmute Chaos -> splits into 2 grouped augments with live metrics
                chaos = self._demo_pick_by_keys("ARAM_TransmuteChaos", "TransmuteChaos")
                tank = self._demo_pick_by_keys("ARAM_TankEngine", "TankEngine")
                conduit = self._demo_pick_by_keys("ARAM_InfernalConduit", "InfernalConduit")
                self._state.candidates.clear()
                chaos_step = TransformStep(via_key=chaos.key, via_name=chaos.name, via_rarity=chaos.rarity)
                grp_id = "demo_chaos_1"
                self._append_demo_pick_locked(
                    tank,
                    level=3,
                    game_time=29.0,
                    slot=0,
                    origin_group_id=grp_id,
                    origin_key=chaos.key,
                    origin_name=chaos.name,
                    origin_rarity=chaos.rarity,
                    lineage=[chaos_step],
                    metrics=[AugmentMetric(label="叠层", value="12层", kind="stack", source="demo")],
                )
                self._append_demo_pick_locked(
                    conduit,
                    level=3,
                    game_time=29.2,
                    slot=None,
                    origin_group_id=grp_id,
                    origin_key=chaos.key,
                    origin_name=chaos.name,
                    origin_rarity=chaos.rarity,
                    lineage=[chaos_step],
                    metrics=[AugmentMetric(label="已造成伤害", value="4,280", kind="damage", source="demo")],
                )
                self._add_event_locked("info", "demo", f"3 级触发一变二：{chaos.name} → {tank.name} / {conduit.name}")
                finished = False
            elif step == 3:
                # Lv 7: 4-card (Mayhem 4.0) selection featuring Transmute Gold
                self._state.level = 7
                self._state.game_time = 265.0
                cands = [
                    self._demo_pick_by_keys("ARAM_TransmuteGold", "TransmuteGold"),
                    self._demo_pick_by_keys("ARAM_Stats", "Stats"),
                    self._demo_pick_by_keys("ARAM_OceanSoul", "OceanSoul"),
                    self._demo_pick_by_keys("ARAM_Deft", "Deft"),
                ]
                self._state.candidates = self._to_candidate_slots(cands)
                self._add_event_locked("info", "demo", "7 级触发海斗 4.0 四选一")
                finished = False
            elif step == 4:
                # Lv 7 Transmute Gold -> Apex Inventor (3 picks total)
                t_gold = self._demo_pick_by_keys("ARAM_TransmuteGold", "TransmuteGold")
                apex = self._demo_pick_by_keys("ARAM_ApexInventor", "ApexInventor")
                self._state.candidates.clear()
                self._append_demo_pick_locked(
                    apex,
                    level=7,
                    game_time=271.0,
                    slot=0,
                    lineage=[TransformStep(via_key=t_gold.key, via_name=t_gold.name, via_rarity=t_gold.rarity)],
                    metrics=[AugmentMetric(label="减少冷却", value="48秒", kind="stat", source="demo")],
                )
                self._add_event_locked("info", "demo", f"7 级质变：{t_gold.name} → {apex.name}")
                finished = False
            elif step == 5:
                # Lv 11: Normal Prismatic pick (4 picks total)
                self._state.level = 11
                self._state.game_time = 540.0
                pick_aug = self._demo_pick_by_keys("ARAM_BacktoBasics", "BacktoBasics")
                self._append_demo_pick_locked(
                    pick_aug,
                    level=11,
                    game_time=544.0,
                    slot=1,
                    metrics=[AugmentMetric(label="额外伤害", value="3,150", kind="damage", source="demo")],
                )
                finished = False
            else:
                # Lv 15: Pandora's Box rerolls all 4 existing picks + adds its own 5th Prismatic slot
                self._state.level = 15
                self._state.game_time = 820.0
                pandora = self._demo_pick_by_keys("ARAM_PandorasBox", "PandorasBox")
                self._append_demo_pick_locked(pandora, level=15, game_time=823.5, slot=0)
                targets = [
                    self._demo_pick_by_keys("ARAM_JeweledGauntlet", "JeweledGauntlet"),
                    self._demo_pick_by_keys("ARAM_Eureka", "Eureka"),
                    self._demo_pick_by_keys("ARAM_BladeWaltz", "BladeWaltz"),
                    self._demo_pick_by_keys("ARAM_TapDancer", "TapDancer", "UltimateAwakening"),
                    self._demo_pick_by_keys("ARAM_ClownCollege", "ClownCollege"),
                ]
                self._reconcile_picks_locked(targets, event_source="demo", assign_source="demo")
                demo_metrics_by_slot = [
                    [AugmentMetric(label="已造成伤害", value="8,640", kind="damage", source="demo")],
                    [AugmentMetric(label="技能急速", value="+96", kind="stat", source="demo")],
                    [AugmentMetric(label="已造成伤害", value="5,120", kind="damage", source="demo")],
                    [AugmentMetric(label="叠层", value="18层", kind="stack", source="demo")],
                    [AugmentMetric(label="已造成伤害", value="2,890", kind="damage", source="demo")],
                ]
                for idx, m_list in enumerate(demo_metrics_by_slot):
                    if idx < len(self._state.picks):
                        self._state.picks[idx] = self._state.picks[idx].model_copy(update={"metrics": m_list})
                self._add_event_locked("info", "demo", "15 级连锁质变：潘朵拉的盒子重随全部海克斯并更新统计")
                finished = True

        self._notify()
        return finished

    def _append_demo_pick_locked(
        self,
        aug: AugmentInfo,
        *,
        level: int,
        game_time: float,
        slot: int | None,
        source: PickSource = "demo",
        origin_group_id: str | None = None,
        origin_key: str = "",
        origin_name: str = "",
        origin_rarity: Any = "unknown",
        lineage: list[TransformStep] | None = None,
        metrics: list[AugmentMetric] | None = None,
    ) -> None:
        self._state.picks.append(
            PickedAugment(
                uid=uuid.uuid4().hex[:10],
                augment_id=aug.id,
                key=aug.key,
                name=aug.name,
                rarity=aug.rarity,
                icon_path=aug.icon_path,
                description=aug.description,
                level=level,
                game_time=game_time,
                picked_at=time.time(),
                source=source,
                slot_index=slot,
                origin_group_id=origin_group_id,
                origin_key=origin_key,
                origin_name=origin_name,
                origin_rarity=origin_rarity,
                lineage=list(lineage or []),
                metrics=list(metrics or []),
            )
        )
        self._add_event_locked("info", source, f"模拟选取：{aug.name}（Lv.{level}）")

    # ------------------------------------------------------------------ Background polling loop

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            now = time.time()
            cfg = self.config_store.get()

            # 1. Poll LCU every 3 seconds
            if now - self._last_lcu_poll >= 3.0:
                self._last_lcu_poll = now
                self._poll_lcu(cfg)

            # 2. Poll Live Client API every 1s in match or 2s outside match
            live_interval = 0.8 if self._in_active_match else 2.0
            if now - self._last_live_poll >= live_interval:
                self._last_live_poll = now
                self._poll_live(cfg)

            # 3. Run active augment detection when in a Mayhem match
            with self._lock:
                in_mayhem_match = self._in_active_match and self._state.is_mayhem and not self._state.demo_active

            if in_mayhem_match:
                tencent_ready = self._step_tencent_channel(cfg, now)
                if not tencent_ready and cfg.enable_vision_fallback:
                    self._step_vision_channel(cfg, now)
                else:
                    with self._lock:
                        if tencent_ready:
                            self._state.vision_ocr = ChannelHealth(status="idle", detail="已由腾讯精准通道接管，视觉识别休眠中")
                if cfg.enable_tooltip_ocr and not self.vision_sm.is_open:
                    self._step_tooltip_metrics_ocr(cfg, now)
                self._stop_event.wait(0.08 if (not tencent_ready and cfg.enable_vision_fallback) else 0.25)
            else:
                self._stop_event.wait(0.5)

    def _poll_lcu(self, cfg: OverlayConfig) -> None:
        connected = self.lcu.discover()
        if not connected:
            with self._lock:
                changed = self._state.lcu.status != "unavailable"
                self._state.lcu = ChannelHealth(status="unavailable", detail="未检测到英雄联盟客户端")
                if not self._in_active_match and not self._state.demo_active:
                    self._state.phase = "None"
            if changed:
                self._notify()
            return

        phase = self.lcu.get_gameflow_phase() or "Unknown"
        region, _ = self.lcu.get_region_locale()
        if not self._lcu_synced_catalog:
            self._lcu_synced_catalog = True
            ok, msg = self.catalog.refresh_from_lcu(self.lcu, locale=cfg.locale)
            if ok:
                self._add_event("info", "catalog", msg)

        if is_tencent_region(region) and self.tencent_key is None:
            summoner = self.lcu.get_current_summoner()
            if summoner and summoner.get("puuid"):
                self.tencent_key = derive_tencent_log_key(summoner["puuid"])

        with self._lock:
            prev_phase = self._state.phase
            prev_region = self._state.region
            self._state.lcu = ChannelHealth(status="ready", detail=f"已连接 ({phase})")
            if not self._state.demo_active:
                self._state.phase = phase
            self._state.region = region
            changed = prev_phase != self._state.phase or prev_region != region
        if changed:
            self._notify()

    def _poll_live(self, cfg: OverlayConfig) -> None:
        snap = self.live_api.poll(allow_non_kiwi_mode=cfg.allow_non_kiwi_mode)
        if snap is None:
            with self._lock:
                was_active = self._in_active_match
                self._in_active_match = False
                self._state.live_api = ChannelHealth(status="idle", detail="等待对局开始 (127.0.0.1:2999)")
                if was_active:
                    self._state.candidates.clear()
                    self._state.active_channel = "standby"
                    self.tencent_sm.reset(keep_picks=True)
                    self.vision_sm.reset()
                    self._add_event_locked("info", "live_api", "对局已结束，保留本局海克斯展示直至下一局开始")
                    self._notify()
            return

        changed = False
        with self._lock:
            is_new_match = (
                not self._in_active_match
                and (
                    snap.game_time < self._last_game_time - 10.0
                    or (self._last_champion_alias and snap.champion_alias and snap.champion_alias != self._last_champion_alias)
                    or not self._state.picks
                )
            ) or (
                self._in_active_match
                and snap.game_time + 10.0 < self._last_game_time
            )

            if self._state.demo_active:
                self._state.demo_active = False
                self._cancel_demo_timer()
                is_new_match = True

            if is_new_match:
                self._state.picks.clear()
                self._state.candidates.clear()
                self.tencent_sm.reset(keep_picks=False)
                self.vision_sm.reset()
                if self.tencent_tail is not None:
                    self.tencent_tail.reset_position()

                restored = self.session_store.try_restore(
                    champion_alias=snap.champion_alias,
                    game_mode=snap.game_mode,
                    game_time=snap.game_time,
                )
                if restored and restored[0]:
                    picks, net_id = restored
                    self._state.picks = picks
                    self.tencent_sm.seed_confirmed_keys([p.key for p in picks if p.key], local_hero_net_id=net_id)
                    self._add_event_locked("info", "session", f"已恢复同局 {len(picks)} 个已选海克斯")
                else:
                    self.session_store.clear()
                    self._add_event_locked("info", "live_api", f"进入新对局：{snap.champion_name or snap.champion_alias} ({snap.game_mode})")
                changed = True

            self._in_active_match = True
            self._last_game_time = snap.game_time
            if snap.champion_alias:
                self._last_champion_alias = snap.champion_alias

            if (
                self._state.champion_alias != snap.champion_alias
                or self._state.level != snap.level
                or self._state.is_mayhem != snap.is_mayhem
                or self._state.game_mode != snap.game_mode
                or abs(self._state.game_time - snap.game_time) >= 2.0
            ):
                changed = True

            self._state.game_mode = snap.game_mode
            self._state.is_mayhem = snap.is_mayhem
            self._state.champion_alias = snap.champion_alias
            self._state.champion_name = snap.champion_name
            self._state.level = snap.level
            self._state.game_time = snap.game_time
            self._state.live_api = ChannelHealth(
                status="ready" if snap.is_mayhem else "idle",
                detail=f"{snap.game_mode} · {snap.champion_name} · Lv.{snap.level}",
            )

        if changed:
            self._notify()

    def _step_tencent_channel(self, cfg: OverlayConfig, now: float) -> bool:
        if not cfg.enable_tencent_log:
            self._set_tencent_status("idle", "已在设置中关闭")
            return False

        if not self._state.region:
            self._set_tencent_status("unavailable", "等待 LCU 获取区服信息")
            return False

        if not is_tencent_region(self._state.region):
            self._set_tencent_status("unavailable", f"当前区服 {self._state.region} 非国服，使用视觉识别")
            return False

        if self.tencent_key is None:
            self._set_tencent_status("unavailable", "未获取召唤师身份，回退视觉识别")
            return False

        if self.tencent_log_path is None or not self.tencent_log_path.is_file():
            self.tencent_log_path = locate_icreate_log()
            if self.tencent_log_path is None:
                self._set_tencent_status("unavailable", "未找到腾讯 icreate 日志，使用视觉识别")
                return False
            self.tencent_tail = TencentLogTail(self.tencent_log_path, start_at_end=True)

        assert self.tencent_tail is not None
        try:
            lines, was_reset = self.tencent_tail.read_new_lines()
        except OSError as exc:
            self.tencent_tail = None
            self.tencent_log_path = None
            self._set_tencent_status("fallback", f"读取日志失败，回退视觉识别 ({exc})")
            return False

        if was_reset:
            self.tencent_sm.reset(keep_picks=True)

        dirty = False
        for line in lines:
            rec = decode_tencent_log_line(line, self.tencent_key)
            if rec is None:
                continue
            for action in self.tencent_sm.observe(rec):
                if self._apply_tencent_action(action):
                    dirty = True

        if not self.tencent_sm.is_stale(now):
            self._set_tencent_status("ready", "国服腾讯精准通道已激活")
            with self._lock:
                self._state.active_channel = "tencent_log"
            if dirty:
                self._notify()
            return True

        self._set_tencent_status("fallback", "等待腾讯日志心跳，视觉回退就绪")
        return False

    def _apply_tencent_action(self, action: Any) -> bool:
        if action.kind == "candidates":
            slots: list[CandidateSlot] = []
            for idx, key in enumerate(action.keys):
                aug = self.catalog.get_by_key(key)
                slots.append(
                    CandidateSlot(
                        slot_index=idx,
                        augment_id=aug.id if aug else None,
                        key=key,
                        name=aug.name if aug else key,
                        rarity=aug.rarity if aug else "unknown",
                        icon_path=aug.icon_path if aug else "",
                        description=aug.description if aug else "",
                        confidence=1.0 if aug else 0.5,
                    )
                )
            with self._lock:
                self._state.candidates = slots
                if slots:
                    names = " / ".join(s.name for s in slots)
                    self._add_event_locked("info", "tencent_log", f"识别到候选：{names}")
            return True

        if action.kind == "picked":
            aug = self.catalog.get_by_key(action.picked_key or "") if action.picked_key else None
            with self._lock:
                self._state.candidates.clear()
                if aug is not None:
                    self._state.picks.append(
                        PickedAugment(
                            uid=uuid.uuid4().hex[:10],
                            augment_id=aug.id,
                            key=aug.key,
                            name=aug.name,
                            rarity=aug.rarity,
                            icon_path=aug.icon_path,
                            description=aug.description,
                            level=self._state.level or None,
                            game_time=self._state.game_time or None,
                            picked_at=time.time(),
                            source="tencent_log",
                            slot_index=action.picked_index,
                        )
                    )
                    self._add_event_locked("info", "tencent_log", f"精准记录选取：{aug.name}（卡槽 {action.picked_index + 1}）")
                    self._persist_session_locked()
                else:
                    self._add_event_locked("warn", "tencent_log", f"收到选取下标 {action.picked_index}，但未能映射海克斯 ({action.picked_key})")
            return True

        if action.kind == "reconcile_owned":
            augs = [
                self.catalog.get_by_key(k)
                or AugmentInfo(id=-1, key=k, name=k, rarity="unknown")
                for k in action.keys
            ]
            with self._lock:
                return self._reconcile_picks_locked(augs)

        return False

    def _reconcile_picks_locked(
        self,
        augs: list[AugmentInfo],
        *,
        event_source: str = "tencent_reconcile",
        assign_source: PickSource = "tencent_reconcile",
    ) -> bool:
        """Reconcile 696 owned augments while recording transform chains and Chaos/Pandora lineage."""
        prev_auto = [p for p in self._state.picks if p.source != "manual"]
        target_keys = [a.key for a in augs]
        if [p.key for p in prev_auto] == target_keys:
            return False

        manual_extras = [p for p in self._state.picks if p.source == "manual" and p.key not in target_keys]
        now_ts = time.time()
        new_picks: list[PickedAugment] = []

        # Branch 1: Pandora's Box rerolls all prior slots into new Prismatics (+ its own slot).
        pandora_pick = next(
            (p for p in prev_auto if classify_augment_transform(p.key) == "pandora" and p.key not in target_keys),
            None,
        )
        if pandora_pick is not None:
            pre_pandora = [p for p in prev_auto if p.uid != pandora_pick.uid]
            for idx, aug in enumerate(augs):
                if idx < len(pre_pandora):
                    old = pre_pandora[idx]
                    if old.key == aug.key:
                        new_picks.append(old)
                    else:
                        steps = list(old.lineage)
                        if classify_augment_transform(old.key) != "normal" and not any(
                            st.via_key == old.key for st in steps
                        ):
                            steps.append(TransformStep(via_key=old.key, via_name=old.name, via_rarity=old.rarity))
                        steps.append(
                            TransformStep(
                                via_key=pandora_pick.key,
                                via_name=pandora_pick.name,
                                via_rarity=pandora_pick.rarity,
                                from_key=old.key,
                                from_name=old.name,
                                from_rarity=old.rarity,
                            )
                        )
                        new_picks.append(
                            PickedAugment(
                                uid=uuid.uuid4().hex[:10],
                                augment_id=aug.id,
                                key=aug.key,
                                name=aug.name,
                                rarity=aug.rarity,
                                icon_path=aug.icon_path,
                                description=aug.description,
                                level=old.level or pandora_pick.level or (self._state.level or None),
                                game_time=old.game_time if old.game_time is not None else (self._state.game_time or None),
                                picked_at=now_ts,
                                source=assign_source,
                                origin_group_id=old.origin_group_id,
                                origin_key=old.origin_key,
                                origin_name=old.origin_name,
                                origin_rarity=old.origin_rarity,
                                lineage=steps,
                            )
                        )
                else:
                    new_picks.append(
                        PickedAugment(
                            uid=uuid.uuid4().hex[:10],
                            augment_id=aug.id,
                            key=aug.key,
                            name=aug.name,
                            rarity=aug.rarity,
                            icon_path=aug.icon_path,
                            description=aug.description,
                            level=pandora_pick.level or (self._state.level or None),
                            game_time=pandora_pick.game_time if pandora_pick.game_time is not None else (self._state.game_time or None),
                            picked_at=now_ts,
                            source=assign_source,
                            lineage=[
                                TransformStep(
                                    via_key=pandora_pick.key,
                                    via_name=pandora_pick.name,
                                    via_rarity=pandora_pick.rarity,
                                )
                            ],
                        )
                    )
        else:
            # Branch 2: Match surviving keys in order; assign un-reconciled transforms to newly revealed slots.
            queues: dict[str, list[PickedAugment]] = {}
            for p in prev_auto:
                queues.setdefault(p.key, []).append(p)

            matched_slots: list[PickedAugment | None] = [None] * len(augs)
            new_indices: list[int] = []
            for idx, aug in enumerate(augs):
                q = queues.get(aug.key)
                if q:
                    matched_slots[idx] = q.pop(0)
                else:
                    new_indices.append(idx)

            unmatched_prev = [p for p in prev_auto if p in sum(queues.values(), [])]
            replaced_transforms = [p for p in unmatched_prev if classify_augment_transform(p.key) != "normal"]

            assignments: dict[int, tuple[PickedAugment | None, str | None]] = {}
            if len(replaced_transforms) == 1:
                tr = replaced_transforms[0]
                use_group = classify_augment_transform(tr.key) == "chaos" or len(new_indices) >= 2
                grp_id = f"grp_{uuid.uuid4().hex[:8]}" if use_group else None
                for idx in new_indices:
                    assignments[idx] = (tr, grp_id)
            elif len(replaced_transforms) > 1:
                cursor = 0
                for tr in replaced_transforms:
                    if cursor >= len(new_indices):
                        break
                    is_chaos = classify_augment_transform(tr.key) == "chaos"
                    take = min(2 if is_chaos else 1, len(new_indices) - cursor)
                    grp_id = f"grp_{uuid.uuid4().hex[:8]}" if (is_chaos or take >= 2) else None
                    for off in range(take):
                        assignments[new_indices[cursor + off]] = (tr, grp_id)
                    cursor += take
                if cursor < len(new_indices):
                    tail_tr = replaced_transforms[-1]
                    for idx in new_indices[cursor:]:
                        assignments[idx] = (tail_tr, None)
            else:
                king = next((p for p in prev_auto if p.key.lower().endswith("kingme")), None)
                already_crowned = any(
                    st.via_key.lower().endswith("kingme")
                    for p in prev_auto
                    for st in p.lineage
                )
                if king is not None and not already_crowned and len(new_indices) == 1 and augs[new_indices[0]].rarity == "prismatic":
                    assignments[new_indices[0]] = (king, None)

            for idx, aug in enumerate(augs):
                existing = matched_slots[idx]
                if existing is not None:
                    new_picks.append(existing)
                    continue
                tr, grp_id = assignments.get(idx, (None, None))
                steps: list[TransformStep] = []
                level = self._state.level or None
                game_time = self._state.game_time or None
                origin_key = ""
                origin_name = ""
                origin_rarity: Any = "unknown"
                if tr is not None:
                    steps = list(tr.lineage) + [
                        TransformStep(via_key=tr.key, via_name=tr.name, via_rarity=tr.rarity)
                    ]
                    if tr.level is not None and not tr.key.lower().endswith("kingme"):
                        level = tr.level
                    if grp_id is not None:
                        origin_key = tr.origin_key or tr.key
                        origin_name = tr.origin_name or tr.name
                        origin_rarity = tr.origin_rarity if tr.origin_rarity != "unknown" else tr.rarity
                new_picks.append(
                    PickedAugment(
                        uid=uuid.uuid4().hex[:10],
                        augment_id=aug.id,
                        key=aug.key,
                        name=aug.name,
                        rarity=aug.rarity,
                        icon_path=aug.icon_path,
                        description=aug.description,
                        level=level,
                        game_time=game_time,
                        picked_at=now_ts,
                        source=assign_source,
                        origin_group_id=grp_id,
                        origin_key=origin_key,
                        origin_name=origin_name,
                        origin_rarity=origin_rarity,
                        lineage=steps,
                    )
                )

        new_picks.extend(manual_extras)
        self._state.picks = new_picks
        self._add_event_locked("info", event_source, f"全量校准已选海克斯：{' / '.join(a.name for a in augs)}")
        self._persist_session_locked()
        return True

    def _step_tooltip_metrics_ocr(self, cfg: OverlayConfig, now: float) -> None:
        """Passively OCR hover tooltips over bottom-HUD/Tab augment icons to track stacks & damage."""
        with self._lock:
            picks_copy = [p.model_copy() for p in self._state.picks]
        if not picks_copy:
            return

        rect = find_game_window_rect()
        pos = get_cursor_pos()
        if rect is None or pos is None:
            self._hover_anchor = None
            return

        if (
            self._hover_anchor is None
            or abs(pos[0] - self._hover_anchor[0]) > 16
            or abs(pos[1] - self._hover_anchor[1]) > 16
        ):
            self._hover_anchor = pos
            self._hover_since = now
            return

        if (now - self._hover_since) < 0.35 or (now - self._last_tooltip_scan) < 0.9:
            return

        crop = tooltip_crop_rect(rect, pos, tab_held=is_tab_key_down())
        if crop is None:
            return

        self._last_tooltip_scan = now
        rgb = capture_rect_rgb(crop)
        if rgb is None or not rgb.any():
            return

        lines = self.ocr_worker.recognize_image(Image.fromarray(rgb))
        parsed = parse_tooltip_metrics(lines, picks_copy, self.catalog)
        if parsed is None:
            return

        target_uid, new_metrics = parsed
        with self._lock:
            for idx, item in enumerate(self._state.picks):
                if item.uid != target_uid:
                    continue
                old_sig = [(m.kind, m.label, m.value) for m in item.metrics]
                new_sig = [(m.kind, m.label, m.value) for m in new_metrics]
                if old_sig == new_sig:
                    return
                self._state.picks[idx] = item.model_copy(update={"metrics": new_metrics})
                summary = "、".join(f"{m.label} {m.value}" for m in new_metrics)
                self._add_event_locked("info", "tooltip_ocr", f"悬停识别数值：{item.name} ({summary})")
                self._persist_session_locked()
                self._notify()
                return

    def _step_vision_channel(self, cfg: OverlayConfig, now: float) -> None:
        rect = find_game_window_rect()
        if rect is None:
            self._set_vision_status("idle", "未检测到前台游戏窗口（建议无边框全屏）")
            return

        # Always poll passive click every tick when game window is present
        click = self.mouse_tracker.poll_click()
        if click is not None:
            gl, gt, gw, gh = rect
            rel_x = (click[0] - gl) / max(1, gw)
            rel_y = (click[1] - gt) / max(1, gh)
            self.vision_sm.record_click(now, rel_x, rel_y, frame_w=gw, frame_h=gh)

        scan_interval = 0.28 if self.vision_sm.is_open else 0.9
        if now - self._last_vision_scan < scan_interval:
            return
        self._last_vision_scan = now

        gl, gt, gw, gh = rect
        det_h = 540
        det_w = max(64, int(round(det_h * gw / max(1, gh))))
        rgb_small = capture_rect_rgb(rect, out_w=det_w, out_h=det_h)
        if rgb_small is None or not rgb_small.any():
            self._set_vision_status("error", "屏幕采集返回黑帧，请确认无边框全屏或窗口化")
            return

        layout = detect_card_layout(rgb_small)
        stats = None
        if layout is not None:
            gray_small = np.dot(rgb_small[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)
            stats = compute_card_stats(gray_small, layout)

        def _run_ocr() -> dict[int, tuple[AugmentInfo | None, float]]:
            full_rgb = capture_rect_rgb(rect)
            if full_rgb is None:
                return {}
            fh, fw, _ = full_rgb.shape
            boxes = title_crop_boxes(fw, fh, layout)
            out: dict[int, tuple[AugmentInfo | None, float]] = {}
            for slot_idx, (x0, y0, x1, y1) in enumerate(boxes):
                crop = Image.fromarray(full_rgb[y0:y1, x0:x1])
                lines = self.ocr_worker.recognize_image(crop)
                best_aug: AugmentInfo | None = None
                best_score = 0.0
                for line in lines:
                    aug, score = self.catalog.match_ocr_text(line)
                    if aug is not None and score > best_score:
                        best_aug = aug
                        best_score = score
                out[slot_idx] = (best_aug, best_score)
            return out

        outcome = self.vision_sm.process_frame(now, layout, stats=stats, ocr_reader=_run_ocr if layout else None)
        with self._lock:
            self._state.active_channel = "vision_ocr"
            lang_tag = self.ocr_worker.active_language or "Windows OCR"
            if self.ocr_worker.last_error and not self.ocr_worker.active_language:
                self._state.vision_ocr = ChannelHealth(status="error", detail=f"OCR 初始化失败：{self.ocr_worker.last_error}")
            else:
                self._state.vision_ocr = ChannelHealth(
                    status="ready" if outcome.panel_open else "active",
                    detail=f"视觉 OCR 监测中 ({lang_tag})",
                )

            dirty = False
            new_cands = [
                CandidateSlot(
                    slot_index=idx,
                    augment_id=aug.id if aug else None,
                    key=aug.key if aug else "",
                    name=aug.name if aug else "识别中...",
                    rarity=aug.rarity if aug else "unknown",
                    icon_path=aug.icon_path if aug else "",
                    description=aug.description if aug else "",
                    confidence=score,
                )
                for idx, aug, score in outcome.candidates
            ]
            if new_cands != self._state.candidates:
                self._state.candidates = new_cands
                dirty = True

            if outcome.reverted_augment_id is not None:
                for i in range(len(self._state.picks) - 1, -1, -1):
                    if self._state.picks[i].augment_id == outcome.reverted_augment_id:
                        rev = self._state.picks.pop(i)
                        self._add_event_locked("info", "vision", f"卡片重新展开，撤销收起误记：{rev.name}")
                        self._persist_session_locked()
                        dirty = True
                        break

            if outcome.picked is not None:
                aug = outcome.picked.augment
                self._state.picks.append(
                    PickedAugment(
                        uid=uuid.uuid4().hex[:10],
                        augment_id=aug.id,
                        key=aug.key,
                        name=aug.name,
                        rarity=aug.rarity,
                        icon_path=aug.icon_path,
                        description=aug.description,
                        level=self._state.level or None,
                        game_time=self._state.game_time or None,
                        picked_at=time.time(),
                        source=outcome.picked.source,
                        slot_index=outcome.picked.slot_index,
                    )
                )
                self._add_event_locked("info", outcome.picked.source, f"视觉识别记录选取：{aug.name}")
                self._persist_session_locked()
                dirty = True

        if dirty:
            self._notify()

    # ------------------------------------------------------------------ Helpers

    def _set_tencent_status(self, status: Any, detail: str) -> None:
        with self._lock:
            changed = self._state.tencent_log.status != status or self._state.tencent_log.detail != detail
            self._state.tencent_log = ChannelHealth(status=status, detail=detail)
        if changed:
            self._notify()

    def _set_vision_status(self, status: Any, detail: str) -> None:
        with self._lock:
            changed = self._state.vision_ocr.status != status or self._state.vision_ocr.detail != detail
            self._state.vision_ocr = ChannelHealth(status=status, detail=detail)
        if changed:
            self._notify()

    def _add_event(self, level: Any, channel: str, message: str) -> None:
        with self._lock:
            self._add_event_locked(level, channel, message)

    def _add_event_locked(self, level: Any, channel: str, message: str) -> None:
        self._state.events.insert(0, DiagnosticEvent(ts=time.time(), level=level, channel=channel, message=message))
        if len(self._state.events) > 40:
            self._state.events = self._state.events[:40]

    def _persist_session_locked(self) -> None:
        if self._state.demo_active:
            return
        self.session_store.save(
            champion_alias=self._state.champion_alias,
            game_mode=self._state.game_mode,
            game_time=self._state.game_time,
            picks=self._state.picks,
            local_hero_net_id=self.tencent_sm.local_hero_net_id,
        )

    @staticmethod
    def _to_candidate_slots(augs: list[AugmentInfo]) -> list[CandidateSlot]:
        return [
            CandidateSlot(
                slot_index=idx,
                augment_id=a.id,
                key=a.key,
                name=a.name,
                rarity=a.rarity,
                icon_path=a.icon_path,
                description=a.description,
                confidence=1.0,
            )
            for idx, a in enumerate(augs)
        ]
