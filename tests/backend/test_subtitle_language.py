"""字幕语言(可独立于配音):简/繁/英 + 中英双语。

覆盖:语言解析、简繁 OpenCC(s2twp)、跨语言批量翻译原语、字幕文本本地化四组合、
双语按标点成对拆块(时间保留)、双语「主大副小」两行 ASS 生成。
不依赖 ffmpeg / DB / 网络(LLM 用 monkeypatch)。
"""
import json
import tempfile
from pathlib import Path

import pytest

from backend.lib import zh_convert
from backend.lib.llm_client import LLMClient
from backend.services.pipeline_service import PipelineService


def _count_preserving_call(prefix):
    """Fake LLMClient._call that returns a JSON array with the same item count."""
    def _call(system, user, purpose=None):
        n = len([ln for ln in user.splitlines() if ln.strip()])
        return json.dumps([f"{prefix}{i}" for i in range(n)])
    return _call


def _ps_with_fake_llm(prefix="T"):
    ps = object.__new__(PipelineService)          # skip heavy __init__
    ps.llm = object.__new__(LLMClient)
    ps.llm._call = _count_preserving_call(prefix)
    return ps


# ── 语言解析 ────────────────────────────────────────────────
def test_resolve_defaults_follow_output_language():
    assert PipelineService._resolve_subtitle_language({}, "zh") == ("zh-Hans", None)
    assert PipelineService._resolve_subtitle_language({}, "en") == ("en", None)


def test_resolve_explicit_and_bilingual():
    assert PipelineService._resolve_subtitle_language({"subtitle_language": "zh-Hant"}, "zh") == ("zh-Hant", None)
    assert PipelineService._resolve_subtitle_language({"subtitle_language": "zh-Hant+en"}, "zh") == ("zh-Hant", "en")
    assert PipelineService._resolve_subtitle_language({"subtitle_language": "zh-Hans+en"}, "en") == ("zh-Hans", "en")


def test_lang_matches_narration():
    m = PipelineService._subtitle_lang_matches_narration
    assert m("zh-Hans", "zh") is True      # simplified over Chinese = no work
    assert m("en", "en") is True
    assert m("zh-Hant", "zh") is False     # traditional always needs conversion
    assert m("en", "zh") is False


# ── OpenCC 简→繁(s2twp)─────────────────────────────────────
def test_opencc_s2twp_converts_glyph_and_taiwan_wording(monkeypatch):
    monkeypatch.delenv("MEDIA_BUDDY_S2T_CONFIG", raising=False)
    out = zh_convert.to_traditional("这个视频用的软件")
    if out == "这个视频用的软件":
        pytest.skip("OpenCC not installed in this env; feature degrades to Simplified by design")
    assert "這" in out
    # s2twp does Taiwan wording: 视频→影片, 软件→軟體
    assert "影片" in out and "軟體" in out


def test_opencc_batch_order_preserving():
    out = zh_convert.to_traditional_batch(["软件", "视频", ""])
    assert len(out) == 3 and out[2] == ""      # empty preserved in place


# ── 批量翻译原语 ────────────────────────────────────────────
def test_translate_segments_count_preserved():
    llm = object.__new__(LLMClient)
    llm._call = _count_preserving_call("EN")
    assert llm.translate_segments(["甲", "乙", "丙"], "en") == ["EN0", "EN1", "EN2"]


def test_translate_segments_per_item_fallback_on_mismatch():
    """Batch drops a line → per-item fallback still returns a same-length result."""
    import re
    llm = object.__new__(LLMClient)

    def call(s, u, purpose=None):
        outs = [re.sub(r"^\d+\.\s*", "", ln) for ln in u.splitlines() if ln.strip()]
        if len(outs) > 1:
            outs = outs[:-1]          # simulate the model merging/dropping one line
        return json.dumps(["EN:" + o for o in outs])

    llm._call = call
    assert llm.translate_segments(["甲", "乙", "丙"], "en") == ["EN:甲", "EN:乙", "EN:丙"]


def test_translate_segments_all_fail_keeps_originals():
    llm = object.__new__(LLMClient)

    def boom(s, u, purpose=None):
        raise RuntimeError("llm down")

    llm._call = boom
    assert llm.translate_segments(["甲", "乙"], "en") == ["甲", "乙"]


def test_translate_segments_empty_passthrough():
    llm = object.__new__(LLMClient)
    llm._call = lambda s, u, purpose=None: (_ for _ in ()).throw(AssertionError("should not call"))
    assert llm.translate_segments([], "en") == []


# ── 文本本地化四组合 ────────────────────────────────────────
def test_localize_simplified_over_chinese_is_passthrough():
    ps = _ps_with_fake_llm()
    # no LLM, no opencc — identical text
    assert ps._localize_caption_texts(["你好", "世界"], "zh", "zh-Hans") == ["你好", "世界"]


def test_localize_traditional_uses_opencc():
    ps = _ps_with_fake_llm()
    out = ps._localize_caption_texts(["软件视频"], "zh", "zh-Hant")
    if out == ["软件视频"]:
        pytest.skip("OpenCC not installed; Traditional degrades to Simplified by design")
    assert "體" in out[0]


def test_localize_cross_language_calls_translation():
    ps = _ps_with_fake_llm("T")
    assert ps._localize_caption_texts(["甲", "乙"], "zh", "en") == ["T0", "T1"]
    assert ps._localize_caption_texts(["a", "b"], "en", "zh-Hans") == ["T0", "T1"]


# ── 双语拆块(按标点成对拆,时间保留)──────────────────────────
def _write_sidecar(tmp: Path):
    narration = tmp / "narration.mp3"
    narration.write_bytes(b"")
    sidecar = tmp / "narration.words.json"
    sidecar.write_text(json.dumps([
        {"kind": "sentence", "text": "蜜獾根本不怕眼镜蛇，就算被咬也没事", "start": 0.0, "end": 3.0},
        {"kind": "sentence", "text": "它的皮肤又厚又松", "start": 3.0, "end": 5.0},
    ], ensure_ascii=False), encoding="utf-8")
    return narration


def _write_char_sidecar(tmp_path, sentences):
    """Qwen-style sidecar: ONLY char tokens (no sentence tokens). Text comes from
    `sentences`, timing from the chars."""
    concat = "".join(sentences)
    toks = [{"kind": "char", "text": ch, "start": round(i * 0.1, 3), "end": round((i + 1) * 0.1, 3)}
            for i, ch in enumerate(concat)]
    narration = tmp_path / "n.mp3"
    narration.write_bytes(b"")
    (tmp_path / "n.words.json").write_text(json.dumps(toks, ensure_ascii=False), encoding="utf-8")
    return narration


def test_build_bilingual_pairs_splits_at_punctuation_and_keeps_time(tmp_path):
    ps = _ps_with_fake_llm("EN")
    narration = _write_sidecar(tmp_path)   # Edge-style sentence tokens
    sents = ["蜜獾根本不怕眼镜蛇，就算被咬也没事", "它的皮肤又厚又松"]
    pairs = ps._build_bilingual_pairs(sents, narration, [], "zh", "zh-Hans", "en")
    assert pairs and len(pairs) >= 3           # first sentence splits at the comma
    assert all("，" not in p["primary"] for p in pairs)   # Chinese punctuation stripped
    assert all(p["primary"] and p["secondary"] for p in pairs)
    assert pairs[0]["start"] == 0.0
    assert all(p["end"] >= p["start"] for p in pairs)


def test_build_bilingual_pairs_qwen_char_tokens(tmp_path):
    """
    timing from the chars (regression for the 'dual fell back to single' bug)."""
    ps = _ps_with_fake_llm("EN")
    sents = ["蜜獾根本不怕眼镜蛇，就算被咬也没事。", "它的皮肤又厚又松。"]
    narration = _write_char_sidecar(tmp_path, sents)
    pairs = ps._build_bilingual_pairs(sents, narration, [], "zh", "zh-Hans", "en")
    assert pairs and len(pairs) >= 3
    assert all(p["primary"] and p["secondary"] for p in pairs)
    assert all(p["end"] >= p["start"] for p in pairs)


def test_build_localized_srt_translates_whole_sentences(tmp_path):
    ps = _ps_with_fake_llm("EN")
    narration = _write_sidecar(tmp_path)
    srt = tmp_path / "subtitles.srt"
    ok = ps._build_localized_srt(["蜜獾根本不怕眼镜蛇，就算被咬也没事", "它的皮肤又厚又松"],
                                 narration, [], srt, "zh", "en")
    assert ok is True
    content = srt.read_text(encoding="utf-8")
    assert "-->" in content and "EN" in content


def test_build_localized_srt_no_windows_returns_false(tmp_path):
    ps = _ps_with_fake_llm()
    assert ps._build_localized_srt(["x"], tmp_path / "nope.mp3", [], tmp_path / "s.srt", "zh", "en") is False


def test_build_bilingual_pairs_without_windows_returns_none(tmp_path):
    ps = _ps_with_fake_llm()
    assert ps._build_bilingual_pairs(["x"], tmp_path / "nope.mp3", [], "zh", "zh-Hans", "en") is None
    assert ps._build_bilingual_pairs(["x"], None, [], "zh", "zh-Hans", "en") is None


# ── 双语 ASS(主大副小两行)─────────────────────────────────
def test_pairs_to_ass_two_lines_with_subfont_override(tmp_path):
    ass = tmp_path / "out.ass"
    pairs = [{"start": 0.0, "end": 2.0, "primary": "蜜獾不怕蛇", "secondary": "Honey badgers fear nothing"}]
    assert PipelineService._pairs_to_ass(
        pairs, ass, width=1080, height=1920,
        main_font=94, sub_font=61, outline=3, margin_v=268, margin_l=76, margin_r=76,
    ) is True
    content = ass.read_text(encoding="utf-8")
    assert "PlayResY: 1920" in content
    assert ",94," in content                       # main font size in the Default style
    assert r"{\fs61}" in content                    # secondary line dropped to sub font
    assert r"\N" in content                         # two lines in one cue
    assert "Noto Sans CJK SC" in content            # OFL font, never YaHei
    dialogue = [ln for ln in content.splitlines() if ln.startswith("Dialogue")]
    assert len(dialogue) == 1
    assert r"蜜獾不怕蛇\N{\fs61}Honey badgers fear nothing" in dialogue[0]


def test_pairs_to_ass_empty_returns_false(tmp_path):
    assert PipelineService._pairs_to_ass(
        [], tmp_path / "e.ass", width=1080, height=1920,
        main_font=94, sub_font=61, outline=3, margin_v=268, margin_l=76, margin_r=76,
    ) is False
