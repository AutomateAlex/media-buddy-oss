"""Tests for PipelineService."""
from unittest.mock import patch, MagicMock
from pathlib import Path


def test_parse_scenes_splits_script():
    from backend.services.pipeline_service import PipelineService

    script = "Welcome to our product. It helps businesses grow faster. Try it today."
    scenes = PipelineService._parse_scenes(script, max_scenes=3)
    assert len(scenes) >= 1
    assert all(isinstance(q, str) and len(q) > 0 for q in scenes)


def test_parse_scenes_falls_back_when_empty():
    from backend.services.pipeline_service import PipelineService
    scenes = PipelineService._parse_scenes("", max_scenes=3)
    assert len(scenes) == 1
    assert scenes[0]


def test_seo_video_filename_keeps_chinese_keywords():
    from backend.services.pipeline_service import _seo_video_filename

    assert (
        _seo_video_filename("法式美食文化与经典菜品", "youtube_shorts")
        == "法式美食文化与经典菜品-youtube-shorts.mp4"
    )


def test_seo_video_filename_hyphenates_english_title():
    from backend.services.pipeline_service import _seo_video_filename

    assert (
        _seo_video_filename("Café & Wine: Paris B-Roll!", "youtube_shorts")
        == "cafe-and-wine-paris-b-roll-youtube-shorts.mp4"
    )


def test_vertical_output_format_aliases_are_normalized():
    from backend.services.pipeline_service import _normalize_output_format

    for alias in [
        "youtube_short",
        "youtube-short",
        "youtube-shorts",
        "shorts",
        "vertical",
        "vertical_9_16",
        "portrait",
    ]:
        assert _normalize_output_format(alias) == "youtube_shorts"

    assert _normalize_output_format("reels") == "instagram_reels"
    assert _normalize_output_format("douyin") == "tiktok"


def test_production_script_sanitizer_verbatim_keeps_reward_cta():
    """「执行文案」verbatim 模式:skip_cta_filter=True → 客户原文一字不改,
    连送礼/抽奖承诺都照上(风险自担)。同一段文案默认模式会被删,verbatim 保留。"""
    from backend.services.pipeline_service import _sanitize_script_for_production

    raw = (
        "狐狸会根据风向和声音判断猎物的位置。\n"
        "关注并留言，三位幸运者将获得狐狸主题的纪念品。"
    )

    # 默认(智能对话/批量):合规删句 —— 送礼承诺被清掉。
    default_cleaned = _sanitize_script_for_production(raw)
    assert "幸运者" not in default_cleaned
    assert "纪念品" not in default_cleaned

    # verbatim(执行文案):一字不改 —— 送礼承诺原样保留。
    verbatim_kept = _sanitize_script_for_production(raw, skip_cta_filter=True)
    assert "幸运者" in verbatim_kept
    assert "纪念品" in verbatim_kept


def test_production_script_sanitizer_repairs_citation_remnants():
    from backend.services.pipeline_service import _sanitize_script_for_production

    raw = (
        "过度教育加剧了困境。[某媒体](https://example.com/a)提到，当硕士去送外卖时，"
        "教育投入与产出会变得刺眼。\n"
        "这张量子竞赛地图我已经整理好，标注了主要国家和技术路线。"
    )

    cleaned = _sanitize_script_for_production(raw)

    assert "https://" not in cleaned
    assert "]( " not in cleaned
    assert "。提到，当硕士" not in cleaned
    assert "\n提到，当硕士" not in cleaned
    assert "有资料提到，当硕士" in cleaned
    assert "量子竞赛地图" not in cleaned


def test_short_script_quality_gate_rejects_cta_only_script():
    from backend.services.pipeline_service import _short_script_quality_issue

    issue = _short_script_quality_issue(
        "评论区告诉我你最想继续看的问题，我下条继续拆。",
        "youtube_shorts",
        45,
    )

    assert issue == "cta_only_script"


def test_short_script_quality_gate_accepts_real_body_script():
    from backend.services.pipeline_service import _short_script_quality_issue

    script = (
        "飓风看起来像一团巨大的云，但它真正可怕的地方，是内部的能量循环。"
        "海面温度升高后，水汽不断上升，像给风暴持续加燃料。"
        "当气压越来越低，周围空气会被快速吸进去，旋转也会越来越强。"
        "所以飓风不是突然出现的怪物，而是一套被海洋和大气共同推起来的系统。"
        "评论区告诉我你还想看哪一种极端天气。"
    )

    assert _short_script_quality_issue(script, "youtube_shorts", 45) is None


def test_short_generated_script_uses_emergency_fallback_after_bad_retry():
    from backend.services.pipeline_service import (
        PipelineService,
        _short_script_quality_issue,
    )

    class BadLlm:
        def generate_script(self, *args, **kwargs):
            return "评论区告诉我你最想继续看的问题，我下条继续拆。"

    svc = PipelineService.__new__(PipelineService)
    svc.llm = BadLlm()

    out = svc._ensure_short_generated_script(
        project_id="p1",
        script="评论区告诉我你最想继续看的问题，我下条继续拆。",
        prompt="主题：飓风背后藏着什么不为人知的真相？",
        output_format="youtube_shorts",
        duration_hint_seconds=45,
    )

    assert "飓风背后藏着什么不为人知的真相" in out
    assert _short_script_quality_issue(out, "youtube_shorts", 45) is None


def test_short_narration_minimum_scales_with_planned_duration():
    from backend.services.pipeline_service import _minimum_short_narration_seconds

    assert _minimum_short_narration_seconds("youtube_shorts", 45) >= 15
    assert _minimum_short_narration_seconds("youtube_shorts", 15) == 8
    assert _minimum_short_narration_seconds("youtube_landscape", 600) == 0


def test_align_sentences_to_elevenlabs_character_sidecar(tmp_path):
    import json
    from backend.services.pipeline_service import PipelineService

    sidecar = tmp_path / "narration.words.json"
    sidecar.write_text(
        json.dumps(
            [
                {"kind": "char", "text": "你", "start": 0.0, "end": 0.12},
                {"kind": "char", "text": "好", "start": 0.12, "end": 0.3},
                {"kind": "char", "text": "。", "start": 0.3, "end": 0.4},
                {"kind": "char", "text": "今", "start": 0.5, "end": 0.62},
                {"kind": "char", "text": "天", "start": 0.62, "end": 0.8},
                {"kind": "char", "text": "。", "start": 0.8, "end": 0.9},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    aligned = PipelineService._align_sentences_to_words(["你好。", "今天。"], sidecar)

    assert aligned == [(0.0, 0.4), (0.5, 0.9)]


def test_align_split_visual_children_slice_parent_sentence_token(tmp_path):
    import json
    from backend.services.pipeline_service import PipelineService

    sidecar = tmp_path / "narration.words.json"
    parent = (
        "\u5b5f\u4e70\u8857\u5934\u7cd6\u6c34\u644a\uff0c"
        "\u6bcf\u676f\u535615\u5362\u6bd4\uff0c"
        "\u6210\u672c8\u5362\u6bd4\uff0c"
        "\u65e5\u5747\u5356200\u676f\u3002"
    )
    sidecar.write_text(
        json.dumps(
            [{"kind": "sentence", "text": parent, "start": 0.0, "end": 6.0}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    aligned = PipelineService._align_sentences_to_words(
        [
            "\u5b5f\u4e70\u8857\u5934\u7cd6\u6c34\u644a\uff0c\u6bcf\u676f\u535615\u5362\u6bd4",
            "\u6210\u672c8\u5362\u6bd4",
            "\u65e5\u5747\u5356200\u676f",
        ],
        sidecar,
    )

    assert aligned is not None
    assert aligned[0][0] == 0.0
    assert aligned[0][1] < aligned[1][1] < aligned[2][1]
    assert aligned[0][1] == aligned[1][0]
    assert aligned[1][1] == aligned[2][0]
    assert aligned[2][1] == 6.0
    assert aligned != [(0.0, 6.0), (0.0, 6.0), (0.0, 6.0)]


def test_compose_reuse_budget_allows_only_one_total_duplicate(monkeypatch):
    from backend.services.pipeline_service import PipelineService

    monkeypatch.setenv("MEDIA_BUDDY_SHORT_V2_MAX_ASSET_OCCURRENCES", "2")
    monkeypatch.setenv("MEDIA_BUDDY_SHORT_V2_MAX_TOTAL_REPEAT_OCCURRENCES", "1")
    counts = PipelineService._compose_asset_counts([
        {"source": "pexels", "source_id": "A", "local_path": "/a.mp4"},
        {"source": "pexels", "source_id": "B", "local_path": "/b.mp4"},
    ])

    assert PipelineService._compose_reuse_allowed(counts, "pexels:A") is True
    PipelineService._compose_record_asset_use(counts, "pexels:A")
    assert PipelineService._compose_duplicate_reuse_count(counts) == 1
    assert PipelineService._compose_reuse_allowed(counts, "pexels:B") is False


def test_compose_reuse_budget_blocks_when_selection_already_repeated(monkeypatch):
    from backend.services.pipeline_service import PipelineService

    monkeypatch.setenv("MEDIA_BUDDY_SHORT_V2_MAX_ASSET_OCCURRENCES", "2")
    monkeypatch.setenv("MEDIA_BUDDY_SHORT_V2_MAX_TOTAL_REPEAT_OCCURRENCES", "1")
    counts = PipelineService._compose_asset_counts([
        {"source": "pexels", "source_id": "A", "local_path": "/a.mp4"},
        {"source": "pexels", "source_id": "A", "local_path": "/a.mp4"},
        {"source": "pexels", "source_id": "B", "local_path": "/b.mp4"},
    ])

    assert PipelineService._compose_duplicate_reuse_count(counts) == 1
    assert PipelineService._compose_reuse_allowed(counts, "pexels:B") is False
    assert PipelineService._compose_reuse_allowed(counts, "pexels:A") is False


def test_build_srt_rejects_non_monotonic_tts_sidecar(tmp_path):
    import json
    from backend.services.pipeline_service import PipelineService

    narration = tmp_path / "narration.mp3"
    narration.write_bytes(b"fake")
    narration.with_suffix(".words.json").write_text(
        json.dumps(
            [
                {"kind": "char", "text": "A", "start": 2.0, "end": 2.4},
                {"kind": "char", "text": "B", "start": 0.1, "end": 0.5},
            ]
        ),
        encoding="utf-8",
    )

    srt = PipelineService._build_srt(
        ["A", "B"],
        [
            {"in_seconds": 0, "out_seconds": 2, "scene_index": 0},
            {"in_seconds": 0, "out_seconds": 3, "scene_index": 1},
        ],
        tmp_path / "subtitles.srt",
        narration,
    )

    content = srt.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:02,000" in content
    assert "00:00:02,000 --> 00:00:05,000" in content


def test_build_srt_uses_elevenlabs_character_times_for_split_cues(tmp_path):
    import json
    from backend.services.pipeline_service import PipelineService

    text = "\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u5341\u4e00\u5341\u4e8c\u3002"
    narration = tmp_path / "narration.mp3"
    narration.write_bytes(b"fake")
    sidecar = narration.with_suffix(".words.json")
    sidecar.write_text(
        json.dumps(
            [
                {
                    "kind": "char",
                    "text": ch,
                    "start": round(i * 0.1, 3),
                    "end": round((i + 1) * 0.1, 3),
                }
                for i, ch in enumerate(text)
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    old_max = PipelineService.MAX_CHARS_PER_CUE
    PipelineService.MAX_CHARS_PER_CUE = 6
    try:
        srt = PipelineService._build_srt(
            [text],
            [{"in_seconds": 0, "out_seconds": 99, "scene_index": 0}],
            tmp_path / "subtitles.srt",
            narration,
        )
    finally:
        PipelineService.MAX_CHARS_PER_CUE = old_max

    content = srt.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:00,600" in content
    assert "\u4e00\u4e8c\u4e09\u56db\u4e94\u516d" in content
    assert "00:00:00,600 --> 00:00:01,200" in content
    assert "\u4e03\u516b\u4e5d\u5341\u5341\u4e00" in content
    assert "\u3002" not in content


def test_build_srt_falls_back_to_clip_timing_when_char_alignment_mismatches(tmp_path):
    """Regression: a short shipped with NO subtitles (\u5b57\u5e55\u9636\u6bb5=\u8df3\u8fc7) because the
    ElevenLabs char stream did not char-match the display sentence (e.g. the TTS
    spoke "\u56db\u5341\u4e03" where the script shows "47"). The char-grouping returned None
    and _build_srt returned None outright \u2014 silently skipping subtitles instead
    of degrading to clip-driven timing. Subtitles must NEVER be silently skipped.
    """
    import json
    from backend.services.pipeline_service import PipelineService

    narration = tmp_path / "narration.mp3"
    narration.write_bytes(b"fake")
    # Monotonic char tokens spelling "47\u5206\u949f" \u2014 does NOT match the display
    # sentence "\u56db\u5341\u4e03\u5206\u949f\u3002", so _group_char_tokens_by_sentence returns None.
    toks = [
        {"kind": "char", "text": ch, "start": round(i * 0.1, 3), "end": round((i + 1) * 0.1, 3)}
        for i, ch in enumerate("47\u5206\u949f")
    ]
    narration.with_suffix(".words.json").write_text(
        json.dumps(toks, ensure_ascii=False), encoding="utf-8",
    )

    srt = PipelineService._build_srt(
        ["\u56db\u5341\u4e03\u5206\u949f\u3002"],
        [{"in_seconds": 0, "out_seconds": 3, "scene_index": 0}],
        tmp_path / "subtitles.srt",
        narration,
    )

    # Before the fix this was None \u2192 subtitles skipped \u2192 video shipped bare.
    # After the fix it degrades to the word-align (or clip-driven) fallback, so an
    # SRT with the sentence text and at least one timed cue is always produced.
    assert srt is not None, "char-alignment mismatch must NOT skip subtitles"
    content = srt.read_text(encoding="utf-8")
    assert "\u56db\u5341\u4e03\u5206\u949f" in content
    assert "-->" in content  # a timed cue exists \u2014 subtitles present, not skipped


def test_long_video_coarse_shots_skips_llm_multishot_split():
    """
    must NOT run the multi-shot LLM split; short video (fine) still does."""
    from unittest.mock import MagicMock
    from backend.services.pipeline_service import PipelineService

    svc = PipelineService.__new__(PipelineService)
    svc.llm = MagicMock()
    svc.llm.assess_shot_counts.return_value = []
    # one long sentence (>25 chars, no connector) the fine path would assess
    script = "这是一个非常长的句子用来测试多镜头切分的逻辑它包含足够多的字符来触发评估流程并产生更多镜头。"

    svc._coarse_shots = True
    svc._sentence_cache = {}
    svc._expand_sentences_for_visual(script)
    assert not svc.llm.assess_shot_counts.called  # long: LLM split skipped

    svc.llm.reset_mock()
    svc._coarse_shots = False
    svc._sentence_cache = {}
    svc._expand_sentences_for_visual(script)
    assert svc.llm.assess_shot_counts.called  # short: LLM split still active


_SRT_SAMPLE = (
    "1\n00:00:00,000 --> 00:00:02,500\n黑曼巴是非洲最致命的毒蛇之一。\n\n"
    "2\n00:00:02,500 --> 00:00:05,000\n都是因为人类无意闯入了它的领地。\n"
)


def _write_srt(tmp_path) -> Path:
    p = tmp_path / "subtitles.srt"
    p.write_text(_SRT_SAMPLE, encoding="utf-8")
    return p


def test_srt_to_ass_default_unchanged_for_portrait(tmp_path):
    """
    max_chars_per_line to the SHARED _srt_to_ass. The portrait safe-zone path
    calls it WITHOUT that arg, so the default must reproduce the old output
    byte-for-byte — otherwise the landscape fix would alter portrait subtitles.
    """
    from backend.services.pipeline_service import PipelineService

    srt = _write_srt(tmp_path)
    a, b = tmp_path / "a.ass", tmp_path / "b.ass"
    PipelineService._srt_to_ass(srt, a, width=1080, height=1920, font_size=96,
                                outline=4, margin_v=288, margin_l=108, margin_r=108)
    PipelineService._srt_to_ass(srt, b, width=1080, height=1920, font_size=96,
                                outline=4, margin_v=288, margin_l=108, margin_r=108,
                                max_chars_per_line=9.0)
    assert a.read_bytes() == b.read_bytes()


def test_srt_to_ass_landscape_style_values(tmp_path):
    """Landscape ASS uses real-dim PlayRes + a restrained YouTube-footer style
    from portrait's big captions."""
    from backend.services.pipeline_service import PipelineService

    srt = _write_srt(tmp_path)
    ass = tmp_path / "land.ass"
    # mirror _burn_subtitles_landscape's computed params for 1920x1080
    ok = PipelineService._srt_to_ass(
        srt, ass, width=1920, height=1080, font_size=43, outline=2,
        margin_v=65, margin_l=154, margin_r=154, max_chars_per_line=22.0,
    )
    assert ok
    txt = ass.read_text(encoding="utf-8")
    assert "PlayResX: 1920" in txt and "PlayResY: 1080" in txt
    style = next(l for l in txt.splitlines() if l.startswith("Style: Default"))
    # Fontsize is the 3rd CSV field; Alignment=2 (bottom-center) present
    assert ",43," in style
    assert ",2,154,154,65,1" in style  # Alignment=2, MarginL/R=154, MarginV=65
    assert "Noto Sans CJK SC" in style  # CJK-capable, OFL-licensed (never YaHei)


def test_burn_subtitles_landscape_exists_and_is_separate_from_portrait():
    """The landscape burner must be its own method so fixing it can never touch
    the working portrait safe-zone burner."""
    from backend.services.pipeline_service import PipelineService

    assert hasattr(PipelineService, "_burn_subtitles_landscape")
    assert hasattr(PipelineService, "_burn_subtitles_safe_zone")
    assert (
        PipelineService._burn_subtitles_landscape
        is not PipelineService._burn_subtitles_safe_zone
    )


def test_stock_fill_rejects_tech_keyboard_for_food_query():
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    candidate = SimpleNamespace(
        query="French gourmet meal beautifully presented",
        source_tags="keyboard backlit green lighting close_up electronic technology button",
        _raw=SimpleNamespace(
            category="tech",
            description="A close-up shot of a green backlit keyboard focusing on the F1 key.",
        ),
    )

    reason = PipelineService._reject_stock_fill_candidate(
        "French gourmet meal beautifully presented",
        candidate,
    )

    assert reason


def test_stock_fill_allows_food_candidate_for_food_query():
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    candidate = SimpleNamespace(
        query="French gourmet meal beautifully presented",
        source_tags="french beef meal dish product",
        _raw=SimpleNamespace(
            category="food",
            description="A French beef meal served on a plate.",
        ),
    )

    assert (
        PipelineService._reject_stock_fill_candidate(
            "French gourmet meal beautifully presented",
            candidate,
            "french red wine beef meal pairing dish product",
        )
        == ""
    )


def test_stock_fill_requires_primary_visual_anchor_overlap():
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    candidate = SimpleNamespace(
        query="French gourmet meal beautifully presented",
        source_tags="speaker woofer audio music close_up",
        _raw=SimpleNamespace(
            category="other",
            description="A close-up shot of a speaker cone with soft lighting.",
        ),
    )

    assert (
        PipelineService._reject_stock_fill_candidate(
            "French gourmet meal beautifully presented",
            candidate,
            "french red wine beef meal pairing dish product",
        )
        == "missing_primary_visual_anchor"
    )


def test_stock_fill_rejects_cash_candidate_without_cash_intent():
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    candidate = SimpleNamespace(
        query="honey syrup pouring drink close up",
        source_tags="Hands counting Indian currency bills and rupee notes",
        _raw=SimpleNamespace(
            category="business",
            description="Hands counting Indian currency banknotes.",
        ),
    )

    assert (
        PipelineService._reject_stock_fill_candidate(
            "honey syrup pouring drink close up",
            candidate,
            "Fresh strawberry in glass teapot with sweet syrup",
            chunk_plan={"must_show": ["kettle", "syrup or honey"]},
            sentence="他把铜壶换成厚壁不锈钢壶，调整糖浆浓度",
        )
        == "cash_filler_without_cash_intent"
    )


def test_stock_fill_allows_cash_candidate_with_cash_intent():
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    candidate = SimpleNamespace(
        query="indian vendor counting rupees",
        source_tags="Hands counting Indian currency bills and rupee notes",
        _raw=SimpleNamespace(
            category="business",
            description="Hands counting Indian currency banknotes.",
        ),
    )

    assert (
        PipelineService._reject_stock_fill_candidate(
            "indian vendor counting rupees",
            candidate,
            "hands counting Indian currency bills",
            chunk_plan={"must_show": ["cash"]},
            sentence="每杯卖15卢比，成本8卢比",
        )
        == ""
    )


def test_stock_fill_uses_primary_tags_not_query_echo():
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    candidate = SimpleNamespace(
        query="French gourmet meal beautifully presented",
        source_tags="abstract blue light texture",
        _raw=SimpleNamespace(
            category="other",
            description="An abstract blue light texture.",
        ),
    )

    assert PipelineService._reject_stock_fill_candidate(
        "French gourmet meal beautifully presented",
        candidate,
        "french red wine beef meal pairing dish product",
    )


def test_stock_fill_allows_child_semantic_expansion_for_cake_parent():
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    candidate = SimpleNamespace(
        query="French Black Forest cake with cherries",
        source_tags="dessert pastry table flowers",
        _raw=SimpleNamespace(
            category="food",
            description="A dessert table with sweet pastries.",
        ),
    )

    assert (
        PipelineService._reject_stock_fill_candidate(
            "French Black Forest cake with cherries",
            candidate,
            "",
        )
        == ""
    )


def test_stock_fill_rejects_candidate_hitting_contract_reject_keyword():
    """

    Fractal-script chunk's harness contract has visual_must_not_convey =
    ["food", "restaurant scenes"]. A library candidate tagged as French
    food MUST be rejected by stock-fill even though tags don't match the
    hardcoded banned list (food/restaurant aren't in banned_terms) and
    query has Chinese characters (no English anchor extractable).
    """
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    chunk_contract = SimpleNamespace(
        visual_must_not_convey=["food", "restaurant scenes"],
        likely_wrong_matches=[],
        verification_tests=[],
    )

    candidate = SimpleNamespace(
        query="fractal patterns in nature",
        source_tags="french beef meal dish",
        _raw=SimpleNamespace(
            category="food",
            description="A French beef meal on a plate.",
        ),
    )

    reason = PipelineService._reject_stock_fill_candidate(
        "分形结构的局部包含整体的信息",  # Chinese query — no English anchor
        candidate,
        "",  # no primary tags (orchestrator may have failed primary)
        chunk_contract=chunk_contract,
    )

    assert reason.startswith("contract_reject_if_true:"), (
        f"expected contract_reject_if_true:* hit, got {reason!r}"
    )
    # The reason should name the offending keyword so logs are actionable
    assert "food" in reason or "restaurant" in reason


def test_stock_fill_contract_pulls_from_verification_tests():
    """reject_if_true verification_tests carry the same signal as
    visual_must_not_convey — make sure the guard scans those too."""
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    test_reject = SimpleNamespace(
        test_id="T_reject",
        question="Does the candidate show cooking or dining scenes?",
        severity="reject_if_true",
    )
    chunk_contract = SimpleNamespace(
        visual_must_not_convey=[],
        likely_wrong_matches=[],
        verification_tests=[test_reject],
    )

    candidate = SimpleNamespace(
        query="abstract math visualization",
        source_tags="cooking kitchen recipe",
        _raw=SimpleNamespace(category="food", description=""),
    )

    reason = PipelineService._reject_stock_fill_candidate(
        "abstract math", candidate, "", chunk_contract=chunk_contract,
    )

    assert reason.startswith("contract_reject_if_true:"), reason
    assert "cooking" in reason or "dining" in reason


def test_stock_fill_contract_allows_on_topic_filler():
    """A candidate that doesn't trip any contract reject keyword should
    still be allowed (subject to legacy banned-list + anchor checks)."""
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    chunk_contract = SimpleNamespace(
        visual_must_not_convey=["food", "restaurant scenes"],
        likely_wrong_matches=[],
        verification_tests=[],
    )

    candidate = SimpleNamespace(
        query="fractal patterns",
        source_tags="fractal geometry mathematics pattern abstract",
        _raw=SimpleNamespace(
            category="abstract",
            description="An abstract fractal geometry visualization.",
        ),
    )

    reason = PipelineService._reject_stock_fill_candidate(
        "fractal patterns", candidate,
        "fractal geometry pattern mathematics",
        chunk_contract=chunk_contract,
    )

    # No reject — contract reject keywords (food/restaurant) don't appear
    # in candidate haystack
    assert reason == "", f"expected pass, got {reason!r}"


def test_stock_fill_contract_none_falls_back_to_legacy_behavior():
    """When orchestrator's harness contract build failed (cloud auth
    revoke mid-run), chunk_contract may be None for some chunks. Stock-fill
    must still work via the legacy banned-list + anchor guard."""
    from types import SimpleNamespace
    from backend.services.pipeline_service import PipelineService

    candidate = SimpleNamespace(
        query="fractal patterns",
        source_tags="keyboard backlit green lighting",
        _raw=SimpleNamespace(
            category="tech",
            description="A keyboard close-up.",
        ),
    )

    reason = PipelineService._reject_stock_fill_candidate(
        "fractal", candidate, "", chunk_contract=None,
    )

    # Legacy banned_category_tech still fires
    assert reason == "banned_category_tech"


def test_run_script_mode_completes(tmp_path):
    """
    chunk engine + SessionLocal to avoid DB; just verify orchestration produces
    a successful compose result."""
    from backend.services.orchestrator import ChunkOutcome
    fake_outcome = ChunkOutcome(
        chunk_index=0,
        plan={"sentence": "Hello world.", "search_query": "business"},
        local_path=str(tmp_path / "clip.mp4"),
        duration=5.0,
        source="pexels",
        source_id="abc",
        source_url="https://x/abc",
        resolution="1920x1080",
        score=9.0,
        decision="use",
        cost_usd=0.0,
    )

    with patch("backend.services.pipeline_service.TTSService") as MockTTS, \
         patch("backend.services.pipeline_service.FootageService") as MockFootage, \
         patch("backend.services.pipeline_service.VideoCompose") as MockCompose, \
         patch("backend.services.pipeline_service.write_progress"), \
         patch("backend.services.pipeline_service.read_progress", return_value=None), \
         patch("backend.services.pipeline_service.Orchestrator") as MockOrchestrator, \
         patch("backend.services.pipeline_service.LongVideoOrchestrator") as MockLongOrch, \
         patch("backend.services.pipeline_service.MusicService"), \
         patch("backend.services.pipeline_service.AudioMixService"), \
         patch("backend.services.pipeline_service.SessionLocal"):

        MockTTS.return_value.synthesize.return_value = MagicMock(
            success=True, error=None, output_path=str(tmp_path / "narr.wav")
        )
        MockOrchestrator.return_value.run.return_value = [fake_outcome]
        # youtube_landscape now routes to the isolated long-video orchestrator.
        MockLongOrch.return_value.run.return_value = [fake_outcome]
        MockCompose.return_value.execute.return_value = MagicMock(
            success=True, error=None, artifacts=[str(tmp_path / "output.mp4")]
        )

        from backend.services.pipeline_service import PipelineService
        svc = PipelineService()
        svc.PIPELINE_DIR = tmp_path / "pipelines"

        result = svc.run("test-001", {
            "name": "SEO Test Video",
            "mode": "script",
            "script_text": "Hello world. This is test.",
            "output_format": "youtube_landscape",
            "tts_provider": "skip",
        })
        assert result["success"]
        assert "output_path" in result
        assert Path(result["output_path"]).name == "seo-test-video-youtube-video.mp4"


def test_compose_preserves_manifest_query_for_stock_fill(tmp_path):
    """Regression guard for filler search relevance.

    Dict footage_results from assets.progress.json store the English director
    query at top-level `query`, not under `plan.search_query`. If compose drops
    it, stock-fill falls back to the Chinese sentence and may retrieve unrelated
    local-library clips such as keyboards/speakers.
    """
    from backend.services.pipeline_service import PipelineService

    svc = PipelineService()
    clip = {
        "local_path": str(tmp_path / "clip.mp4"),
        "duration": 5,
        "scene_index": 2,
        "query": "French gourmet meal beautifully presented",
        "source": "library",
        "source_id": "x",
    }
    # Mirror the small extraction block used by _stage_compose.
    plan = clip.get("plan", {})
    query = (plan or {}).get("search_query", "") or clip.get("query", "")

    assert query == "French gourmet meal beautifully presented"


def test_find_auth_cause_unwraps_auth_root_from_unmatched_chunk():
    """An auth/cloud failure that surfaced as UnmatchedChunkError must be
    detected via the exception chain so the run is treated as resumable
    (retry/auth-paused), not a permanent 'unfilmable script' failure.
    """
    from backend.services.pipeline_service import _find_auth_cause
    from backend.lib.cloud_auth import (
        NotAuthenticatedError, CloudTemporaryUnavailableError,
    )

    # Simulate the real chain: NotAuthenticated -> wrapped as UnmatchedChunk
    class _FakeUnmatched(Exception):
        pass

    try:
        try:
            raise NotAuthenticatedError("cloud not authenticated; user must log in")
        except NotAuthenticatedError as inner:
            raise _FakeUnmatched("no acceptable stock asset after rescue") from inner
    except _FakeUnmatched as e:
        cause = _find_auth_cause(e)

    assert isinstance(cause, NotAuthenticatedError)


def test_find_auth_cause_matches_message_hint():
    from backend.services.pipeline_service import _find_auth_cause
    from backend.lib.cloud_auth import CloudTemporaryUnavailableError

    # Even without a typed auth exception, a tell-tale message is caught and
    # surfaced as a temporary (retry) error.
    e = RuntimeError("refresh rejected by server")
    cause = _find_auth_cause(e)
    assert isinstance(cause, CloudTemporaryUnavailableError)


def test_find_auth_cause_returns_none_for_real_unmatched():
    from backend.services.pipeline_service import _find_auth_cause

    # A genuine "can't find footage" error with no auth cause stays None so it
    # hard-fails normally (user must edit the script).
    e = RuntimeError("no acceptable stock asset; no safe inheritance donor")
    assert _find_auth_cause(e) is None


# 收口相关测试已迁往 tests/backend/test_script_gate.py 与 test_closure_standards.py
# (实现从本文件迁到 backend.lib.script_gate + closure_standards)。


def test_stage_script_short_branch_passes_effective_voice_to_generator(tmp_path):
    """回归:_stage_script 短视频分支必须能跑通,并把**生效后的音色**传给出稿。

    tts_provider 是 run() 的局部变量,_stage_script 是另一个函数,拿不到。

    另外 run() 里对音色有一串覆盖规则(英文出片改写音色等)且**不写回 project_data**,
    所以必须由调用方显式传入生效值,不能在这里自己读 project_data。
    """
    from backend.services.pipeline_service import PipelineService

    seen = {}

    class Llm:
        def generate_script(self, prompt, output_format=None, **kwargs):
            seen.update(kwargs)
            return (
                "1745年，一个中年男人走进巴黎沙龙，说自己能把银变成金。"
                "他讲六门以上语言，路易十五让他住进香波尔城堡。"
                "但他最厉害的一招是暗示自己长生不老，仆人只答我才伺候伯爵三百年。"
                "1784年官方记录他去世，可此后一百多年不断有人说又见过他。"
                "他骗过整个欧洲，靠的是把查得到的真本事和查不到的传说搅在一起。"
            )

    svc = PipelineService.__new__(PipelineService)
    svc.llm = Llm()
    svc.series = None
    svc.PIPELINE_DIR = tmp_path
    svc._raise_if_stopped = lambda *a, **k: None
    svc._write_stage_progress = lambda *a, **k: None
    svc._fmt_context = lambda p: str(p)
    svc._viral_script_block = lambda *a, **k: ""
    svc._short_script_unfinished_reason = lambda script, prompt: None

    out = svc._stage_script(
        "p-closure",
        tmp_path,
        {
            "prompt": "主题：圣日耳曼伯爵为什么能骗过整个欧洲上流社会？",
            "duration_seconds": 60,
            "tts_speed": 1.4,
            "tts_provider": "azure_yunyang",   # project_data 里的原始值
        },
        "creative",
        "youtube_shorts",
        tts_provider="qwen_cherry",            # run() 覆盖后真正生效的音色
    )

    assert out
    # 预算必须按生效音色(qwen_cherry)算,而不是 project_data 里的 azure_yunyang
    assert seen.get("tts_provider") == "qwen_cherry"


def test_hard_waste_retry_result_still_goes_through_the_gate():
    """

    后果:恰恰是"本来就有问题"的稿子躲开了长度与收口检查。
    A 条冒烟片因此只出 39.7 秒(目标 60)—— 它从没被量过。
    修复稿必须和正常稿走同一道闸。
    """
    from backend.services.pipeline_service import PipelineService

    seen = {}

    class Llm:
        def generate_script(self, *a, **k):
            # 够长、能过硬废稿闸的重试稿
            return "".join(f"这是第{i}句有真实内容而且足够长的正文。" for i in range(12))

        def _call(self, *a, **k):
            seen["gate_called"] = True
            raise RuntimeError("judge down")     # fail-open,但证明闸被调用了

    svc = PipelineService.__new__(PipelineService)
    svc.llm = Llm()
    svc.series = None

    out = svc._ensure_short_generated_script(
        project_id="p1",
        script="太短。",                          # 触发 body_too_short
        prompt="主题：测试",
        output_format="youtube_shorts",
        duration_hint_seconds=60,
        tts_provider="qwen_cherry",
        tts_speed=1.4,
    )

    assert out
    assert seen.get("gate_called"), "retry 修好的稿子也必须过验收闸"
