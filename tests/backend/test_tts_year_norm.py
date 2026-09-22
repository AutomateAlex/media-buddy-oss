"""TTS 年份归一化 —— 正向(送 TTS)逐字读、反向(字幕)还原数字。

复现并守住 "1749年" 被 qwen TTS 读成 "十七四十九年" 的修复:送 TTS 前转 "一七四九年"。
稳妥口径:只转明确年份,不误伤时长("持续了3000年"=三千年)。
"""
from backend.lib.tts_text_norm import normalize_years_for_tts, restore_years_for_display


# ── 正向:明确年份 → 逐字 ──────────────────────────────────────────────
def test_bare_year_digit_by_digit():
    assert normalize_years_for_tts("这发生在1749年的春天") == "这发生在一七四九年的春天"


def test_year_in_1800s_1900s():
    assert normalize_years_for_tts("1893年") == "一八九三年"
    assert normalize_years_for_tts("1900年爆发") == "一九零零年爆发"


def test_gongyuan_marks_year():
    assert normalize_years_for_tts("公元1749年") == "公元一七四九年"
    assert normalize_years_for_tts("公元前221年统一") == "公元前二二一年统一"
    assert normalize_years_for_tts("公元前221") == "公元前二二一"


def test_year_range_both_converted():
    assert normalize_years_for_tts("1749-1755年") == "一七四九-一七五五年"
    assert normalize_years_for_tts("1749至1755年之间") == "一七四九至一七五五年之间"


# ── 正向:不误伤时长(稳妥) ────────────────────────────────────────────
def test_round_thousand_left_as_duration():
    # 整千多为时长,留给 TTS 读"三千年/两千年"(不逐字读成 三零零零)
    assert normalize_years_for_tts("持续了3000年") == "持续了3000年"
    assert normalize_years_for_tts("公元2000年") == "公元2000年"
    assert normalize_years_for_tts("2000年") == "2000年"


def test_duration_cue_before_number_skipped():
    assert normalize_years_for_tts("长达1800年") == "长达1800年"
    assert normalize_years_for_tts("存在了2500年") == "存在了2500年"


def test_short_numbers_untouched():
    assert normalize_years_for_tts("才3年") == "才3年"
    assert normalize_years_for_tts("100年后") == "100年后"


# ── 反向:字幕还原数字 ────────────────────────────────────────────────
def test_restore_digits_for_display():
    assert restore_years_for_display("一七四九年") == "1749年"
    assert restore_years_for_display("公元一七四九年发生") == "公元1749年发生"
    assert restore_years_for_display("二零二零年") == "2020年"


def test_restore_range():
    assert restore_years_for_display("一七四九-一七五五年") == "1749-1755年"


def test_restore_does_not_touch_cardinals():
    # 基数词/序数(含 十/百/千,或单字)不还原
    for s in ("三千年", "十八世纪", "第一年", "这两年", "近三年"):
        assert restore_years_for_display(s) == s


# ── 正反往返 ──────────────────────────────────────────────────────────
def test_round_trip_year_back_to_digits():
    for src in ("1749年", "公元前221年", "1749-1755年", "1893年"):
        norm = normalize_years_for_tts(src)
        assert norm != src  # 确实转过
        assert restore_years_for_display(norm) == src  # 显示还原回原样
