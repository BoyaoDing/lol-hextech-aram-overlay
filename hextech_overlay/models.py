"""Pydantic data models for Hextech ARAM live overlay state and configuration."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field

Rarity = Literal["silver", "gold", "prismatic", "unknown"]
ThemeName = Literal["vertical", "tray", "strip", "showcase"]
AlignName = Literal["left", "right", "center"]
PickSource = Literal["tencent_log", "tencent_reconcile", "vision_click", "vision_flare", "manual", "demo"]
ChannelStatus = Literal["idle", "active", "ready", "unavailable", "fallback", "error"]


class AugmentInfo(BaseModel):
    """Static metadata for a single Hextech augment."""

    id: int
    key: str
    name: str
    rarity: Rarity = "unknown"
    icon_path: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    in_mayhem_pool: bool = False


class PickedAugment(BaseModel):
    """One augment acquired by the streamer in the current match."""

    uid: str
    augment_id: int
    key: str
    name: str
    rarity: Rarity = "unknown"
    icon_path: str = ""
    description: str = ""
    level: int | None = None
    game_time: float | None = None
    picked_at: float
    source: PickSource = "manual"
    slot_index: int | None = None


class CandidateSlot(BaseModel):
    """One offered card currently visible during augment selection."""

    slot_index: int
    augment_id: int | None = None
    key: str = ""
    name: str = ""
    rarity: Rarity = "unknown"
    icon_path: str = ""
    description: str = ""
    confidence: float = 1.0


class ChannelHealth(BaseModel):
    """Runtime status of a single data/detection channel."""

    status: ChannelStatus = "idle"
    detail: str = ""


class DiagnosticEvent(BaseModel):
    """Recent diagnostic timeline item (never contains secrets or PUUIDs)."""

    ts: float
    level: Literal["info", "warn", "error"] = "info"
    channel: str
    message: str


class OverlayConfig(BaseModel):
    """User-editable settings for the overlay widget and detectors."""

    port: int = Field(default=18777, ge=1024, le=65535)
    theme: ThemeName = "vertical"
    show_description: bool = True
    show_level_badge: bool = True
    show_champion_header: bool = True
    max_visible: int = Field(default=6, ge=1, le=12)
    scale: float = Field(default=1.0, ge=0.5, le=2.0)
    align: AlignName = "left"
    enable_tencent_log: bool = True
    enable_vision_fallback: bool = True
    allow_non_kiwi_mode: bool = False
    locale: str = "zh_cn"
    proxy_url: str = ""


class GameState(BaseModel):
    """Live match and detection state pushed to both overlay and control panel."""

    phase: str = "None"
    game_mode: str = ""
    is_mayhem: bool = False
    region: str = ""
    champion_alias: str = ""
    champion_name: str = ""
    level: int = 0
    game_time: float = 0.0
    active_channel: str = "standby"
    demo_active: bool = False
    demo_step: int = 0
    lcu: ChannelHealth = Field(default_factory=ChannelHealth)
    live_api: ChannelHealth = Field(default_factory=ChannelHealth)
    tencent_log: ChannelHealth = Field(default_factory=ChannelHealth)
    vision_ocr: ChannelHealth = Field(default_factory=ChannelHealth)
    candidates: list[CandidateSlot] = Field(default_factory=list)
    picks: list[PickedAugment] = Field(default_factory=list)
    events: list[DiagnosticEvent] = Field(default_factory=list)
    catalog_count: int = 0
    mayhem_pool_count: int = 0
