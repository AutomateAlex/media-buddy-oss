"""must_show 兜底地板 + 判官配额(修"老鼠那句配了蜜蜂")。

根因:每句判官配额=2(+每条30)太抠,判官没机会审到对的老鼠,然后无判官的 motif 兜底
把一个跑题镜头(蜜蜂"Bee on Concrete Surface in Bangkok")硬塞进了片子。修法:放开配额 +
motif 兜底必须命中 must_show。
"""
import os

from backend.services.orchestrator import Orchestrator


class _Cand:
    def __init__(self, tags):
        self.source_tags = tags


def test_motif_floor_drops_offtopic_clip():
    # 老鼠那句:must_show=rats;蜜蜂素材标签里没 rat → 兜底地板刷掉。
    plan = {"must_show": ["rats"], "hard_anchors": ["rats"]}
    assert Orchestrator._v2_candidate_matches_must_show(
        _Cand("Bee on Concrete Surface in Bangkok"), plan
    ) is False


def test_motif_floor_keeps_must_show_clip():
    plan = {"must_show": ["rats"], "hard_anchors": ["rats"]}
    assert Orchestrator._v2_candidate_matches_must_show(
        _Cand("Pair of wild rats interacting on sandy ground"), plan
    ) is True
    # 单数 rat 也算
    assert Orchestrator._v2_candidate_matches_must_show(
        _Cand("a single rat in the subway"), plan
    ) is True


def test_motif_floor_off_when_no_must_show():
    # 没有 must_show 要求 → 不设地板(宽松,不误伤)。
    assert Orchestrator._v2_candidate_matches_must_show(
        _Cand("anything at all"), {"must_show": []}
    ) is True


def test_motif_floor_conservative_when_no_tags():
    # 有 must_show 但候选无标签 → 兜底处宁严勿松,视为不匹配。
    assert Orchestrator._v2_candidate_matches_must_show(
        _Cand(""), {"must_show": ["rats"]}
    ) is False


def test_observer_caps_raised(monkeypatch):
    monkeypatch.delenv("MEDIA_BUDDY_SHORT_V2_MAX_OBSERVER_PER_CHUNK", raising=False)
    monkeypatch.delenv("MEDIA_BUDDY_SHORT_V2_MAX_OBSERVER_PER_VIDEO", raising=False)
    assert Orchestrator._short_v2_max_observer_per_chunk() == 6
    assert Orchestrator._short_v2_max_observer_per_video() == 120


def test_observer_caps_env_override(monkeypatch):
    monkeypatch.setenv("MEDIA_BUDDY_SHORT_V2_MAX_OBSERVER_PER_CHUNK", "3")
    monkeypatch.setenv("MEDIA_BUDDY_SHORT_V2_MAX_OBSERVER_PER_VIDEO", "50")
    assert Orchestrator._short_v2_max_observer_per_chunk() == 3
    assert Orchestrator._short_v2_max_observer_per_video() == 50
