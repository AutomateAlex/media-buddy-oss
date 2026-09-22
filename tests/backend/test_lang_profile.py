"""语言档案 —— 把验收闸参数化,而不是给英文复制一套。

## 为什么要原生英文

现在英文是「中文稿 → 翻译 → 事后凝练」。这**正是我们花整轮工夫从中文路径里
拆掉的东西**:先生成、再发现不对、再事后压。凝练就是截断的温和版本。

而且翻译**天然摧毁长度控制**:中文字数→英文词数的比例随内容浮动
(专有名词、句式、信息密度),中文那边控得再准,过一次翻译就散了。
这不是调参能解决的,是量纲转换本身的问题。

**事后凝练撞到了模型的行为地板**(不管要 170 还是 139 词,都停在 195-203)。

## 设计

一个 `LangProfile` 提供该语言的:计数 / 预估 / 判定 / 切句 / 断尾 pattern。
闸只认 profile,不认语言 —— 加第三种语言时不用再动闸。
"""
import pytest


# ── 档案本身 ────────────────────────────────────────────────────────

def test_profiles_exist_for_both_languages():
    from backend.lib.lang_profile import profile_for

    assert profile_for("zh").lang == "zh"
    assert profile_for("en").lang == "en"


def test_unknown_language_falls_back_to_chinese():
    """默认中文 —— 这是主力语言(占 95%),未知输入不该走陌生分支。"""
    from backend.lib.lang_profile import profile_for

    assert profile_for("").lang == "zh"
    assert profile_for(None).lang == "zh"
    assert profile_for("fr").lang == "zh"


def test_chinese_profile_counts_chars_english_counts_words():
    """量纲不同是这件事的根本 —— 档案第一件事就是把计数分开。"""
    from backend.lib.lang_profile import profile_for

    zh, en = profile_for("zh"), profile_for("en")
    text_zh = "他讲六门语言。"
    text_en = "He spoke six languages fluently."

    assert zh.count(text_zh) == 7                 # 非空白字符(含标点)
    assert en.count(text_en) == 5                 # 词数


def test_each_profile_estimates_with_its_own_table():
    from backend.lib.lang_profile import profile_for

    zh, en = profile_for("zh"), profile_for("en")
    # 300 中文字 vs 300 英文词 —— 秒数天差地别,证明用的是两张表
    assert zh.estimate(300, "qwen_cherry", 1.4) < en.estimate(300, "qwen_cherry", 1.4)


# ── 切句:英文标点 ──────────────────────────────────────────────────

def test_english_profile_splits_on_english_punctuation():
    from backend.lib.lang_profile import profile_for

    en = profile_for("en")
    sents = en.split_sentences(
        "He spoke six languages. Louis XV gave him a castle! Who was he?")

    assert len(sents) == 3
    assert sents[0].endswith(".")
    assert sents[-1].endswith("?")


def test_english_split_does_not_glue_short_capitalized_words():
    """回归:缩写保护原本用「首字母大写 + 1-4 字母 + 句点」的**形状**判断,
    结果把 `Hook.` `Rome.` `Cats.` `Then.` 全当成了 `Mr.` 那样的缩写,
    和下一句粘死 —— 切句一错,钩子/正文/落点就全切歪,隔离式修复改错地方。

    形状判断在这里本来就不成立,必须按**已知缩写表**来。
    """
    from backend.lib.lang_profile import profile_for

    en = profile_for("en")
    assert len(en.split_sentences("Hook. Body one. Body two.")) == 3
    assert len(en.split_sentences("He died in Rome. Nobody knew why.")) == 2
    assert len(en.split_sentences("Cats. They knead.")) == 2


def test_english_split_still_protects_real_abbreviations():
    from backend.lib.lang_profile import profile_for

    en = profile_for("en")
    assert len(en.split_sentences("Mr. Smith arrived late. Nobody cared.")) == 2
    assert len(en.split_sentences("Dr. Jones and Mrs. Lee met. They talked.")) == 2


def test_english_split_does_not_break_on_decimals_or_abbreviations():
    """英文切句的经典坑:小数点和缩写。切错会把稿子拆碎。"""
    from backend.lib.lang_profile import profile_for

    en = profile_for("en")
    sents = en.split_sentences("He was worth 3.5 million francs. That is a lot.")

    assert len(sents) == 2


def test_chinese_profile_still_splits_on_chinese_punctuation():
    from backend.lib.lang_profile import profile_for

    zh = profile_for("zh")
    assert len(zh.split_sentences("第一句。第二句！第三句？")) == 3


# ── 断尾 pattern:中文正则对英文无效 ─────────────────────────────────

def test_english_has_its_own_dangling_patterns():
    """中文的「怎么做？」正则对英文一点用没有 —— 必须各有各的。"""
    from backend.lib.lang_profile import profile_for

    en = profile_for("en")
    assert en.dangling_hit("So the real question is: how do you do it?")
    assert en.dangling_hit("But how much should you add?")
    assert en.dangling_hit("So what now?")


def test_english_patterns_do_not_flag_a_grounded_open_question():
    """建立在正文之上的讨论式提问是**好结尾**,绝不能误杀。"""
    from backend.lib.lang_profile import profile_for

    en = profile_for("en")
    good = ("So the real mystery isn't whether he lived three hundred years — "
            "it's why an entire continent wanted to believe he did.")

    assert en.dangling_hit(good) is None


def test_chinese_patterns_still_work():
    from backend.lib.lang_profile import profile_for

    zh = profile_for("zh")
    assert zh.dangling_hit("破解方法只有一句话。怎么做？")
    assert zh.dangling_hit("这不是健身效果，是写在基因里的生存策略。") is None


# ── 判定 ────────────────────────────────────────────────────────────

def test_english_verdict_uses_the_english_table():
    from backend.lib.lang_profile import profile_for

    en = profile_for("en")
    assert en.verdict(150, "qwen_cherry", 1.2, 60) == "pass"
    assert en.verdict(300, "qwen_cherry", 1.2, 60) == "repair"


def test_english_target_is_in_words_not_chars():
    from backend.lib.lang_profile import profile_for

    en = profile_for("en")
    target = en.target(60, "qwen_cherry", 1.4)
    # 60 秒 × 2.20 词/秒 × 1.4 × 0.92 ≈ 170 词,绝不该是几百
    assert 120 < target < 220


# ── 写稿语言 ────────────────────────────────────────────────────────

def test_english_prompt_asks_for_native_english_not_chinese():
    """核心改动:lang=en 时**直接用英文写**,不再写中文再翻。"""
    from backend.lib.llm_client import LLMClient

    c = LLMClient.__new__(LLMClient)
    p = c._build_script_system_prompt(
        "youtube_shorts", 60, "", speed=1.4,
        tts_provider="qwen_cherry", output_language="en",
    )

    assert "English" in p
    assert "简体中文" not in p, "英文出片不该再要求写中文"
    assert "words" in p.lower()


def test_chinese_prompt_is_unchanged():
    """中文占 95% 的产量 —— 一个字都不能动。"""
    from backend.lib.llm_client import LLMClient

    c = LLMClient.__new__(LLMClient)
    p = c._build_script_system_prompt(
        "youtube_shorts", 60, "", speed=1.4,
        tts_provider="qwen_cherry", output_language="zh",
    )

    assert "简体中文" in p


def test_native_english_can_be_switched_off(monkeypatch):
    """回滚开关:关掉就退回"中文稿 + 翻译"的老路。"""
    monkeypatch.setenv("MB_NATIVE_EN", "0")
    from backend.lib.llm_client import LLMClient

    c = LLMClient.__new__(LLMClient)
    p = c._build_script_system_prompt(
        "youtube_shorts", 60, "", speed=1.4,
        tts_provider="qwen_cherry", output_language="en",
    )

    assert "简体中文" in p, "关掉后应退回中文稿+翻译"


# ── 原生英文时必须跳过翻译 ──────────────────────────────────────────

def test_native_english_skips_translation_entirely():
    """稿子本来就是英文,再走一次"中译英"是把英文翻英文 —— 既浪费又会改坏它。"""
    from backend.services.pipeline_service import _should_translate

    assert _should_translate("en", native_en=True) is False
    assert _should_translate("en", native_en=False) is True     # 老路仍要翻
    assert _should_translate("zh", native_en=True) is False      # 中文不翻


def test_gate_runs_in_word_mode_for_english():
    """英文进闸时用词数判定 —— 拿中文字数口径去量英文会错一个数量级。"""
    import json

    from backend.lib.script_gate import run_script_gate

    # 约 300 词的英文稿,60 秒目标 → 明显超长
    text = ". ".join(" ".join(["word"] * 20) for _ in range(15)) + "."

    class Llm:
        def __init__(self):
            self.n = 0

        def _call(self, system, user, purpose=None):
            self.n += 1
            return json.dumps({"closure_ok": True, "closure_reason": "",
                               "introduced_new_facts": False})

    res = run_script_gate(script_v1=text, duration_seconds=60, voice="qwen_cherry",
                          speed=1.4, series=None, llm=Llm(), output_language="en")

    # 断言首轮**判定**(而不是修复后的结果):300 词 @2.20×1.4 ≈ 97 秒,
    # 必须被判超长。若误用中文字数口径,同一段文本会被估成几百秒。
    v1 = res.checks[0]
    assert v1.predicted_seconds > 80, (
        f"英文应按词数估到 80 秒以上,实际 {v1.predicted_seconds:.1f}")
    assert v1.length == "repair_long"


def test_output_language_actually_reaches_the_writer(tmp_path):
    """
    结果英文订单出了一条纯中文片(325 个中文字符,0 个英文)。

    而且缺第一个比不做还糟(以前至少还会翻译)。
    """
    import pathlib
    import tempfile

    from backend.services.pipeline_service import PipelineService

    seen = {}

    class Llm:
        def generate_script(self, prompt, output_format=None, **kw):
            seen.update(kw)
            return "A native English narration. " * 30

        def _call(self, *a, **k):
            raise RuntimeError("judge down")   # fail-open

    svc = PipelineService.__new__(PipelineService)
    svc.llm = Llm(); svc.series = None
    svc.PIPELINE_DIR = pathlib.Path(tempfile.mkdtemp())
    svc._raise_if_stopped = lambda *a, **k: None
    svc._write_stage_progress = lambda *a, **k: None
    svc._fmt_context = lambda p: str(p)
    svc._viral_script_block = lambda *a, **k: ""

    svc._stage_script(
        "p-en", svc.PIPELINE_DIR,
        {"prompt": "x", "duration_seconds": 60, "tts_speed": 1.4,
         "tts_provider": "qwen_cherry", "output_language": "en"},
        "creative", "youtube_shorts", tts_provider="qwen_cherry",
    )

    assert seen.get("output_language") == "en", (
        "写稿必须收到 output_language,否则会写中文却又跳过翻译")


def test_chinese_script_for_an_english_order_gets_translated_anyway():
    """安全网:万一写稿仍吐了中文,**绝不能**就这么当英文片发出去。

    宁可退回"翻译一次"这条老路,也不能让客户的英文订单收到中文配音。
    """
    from backend.services.pipeline_service import _needs_language_rescue

    chinese = "他讲六门以上语言，路易十五让他住进香波尔城堡。" * 3
    english = "He spoke six languages and Louis XV gave him a castle. " * 3

    assert _needs_language_rescue(chinese, "en") is True
    assert _needs_language_rescue(english, "en") is False
    assert _needs_language_rescue(chinese, "zh") is False


# ── 英文必须和中文同一套三带模型 ────────────────────────────────────

def test_english_verdict_catches_short_not_just_long():
    """

    一条 130 词 / 预估 42.2 秒(目标 60)的稿子判了 `pass` —— 客户设 60 秒、
    按 1 分钟付费、拿到 42 秒。这正是整个项目要根治的那个病搬到了英文路径上。

    英文原来只判超长是有历史原因的(那时它走「翻译后凝练」,只怕译文变长);
    现在英文是一等公民、走全闸,就必须两边都判。
    """
    from backend.lib.tts_pacing import english_length_verdict as v

    assert v(130, "qwen_cherry", 1.4, 60) == "repair"   # 42.2s → 0.70 明显过短
    assert v(166, "qwen_cherry", 1.4, 60) == "pass"     # 53.9s → 0.90 放行
    assert v(300, "qwen_cherry", 1.4, 60) == "repair"   # 97.4s → 超长


def test_unmeasured_english_voice_still_only_catches_disasters():
    """

    """
    from backend.lib.tts_pacing import english_length_verdict as v

    assert v(130, "some_unmeasured_voice", 1.4, 60) == "pass"   # 过短不判
    assert v(400, "some_unmeasured_voice", 1.4, 60) == "repair"  # 灾难仍抓


def test_english_and_chinese_use_the_same_bands():
    """同一条比例,两种语言应得同一个判定 —— 判据一致,量纲各自。"""
    from backend.lib.tts_pacing import english_length_verdict, length_verdict

    # 都构造成约 0.70 倍目标
    assert english_length_verdict(130, "qwen_cherry", 1.4, 60) == \
        length_verdict(268, "qwen_cherry", 1.4, 60) == "repair"


# ── 修复提示词的量纲与语言 ──────────────────────────────────────────

def test_repair_prompt_uses_words_for_english_not_chars():
    """回归:提示词写死「约 N 字」。对英文 N 是**词数**,说成"字"是错量纲 ——
    模型很可能按字符压,这就是那条稿子只剩 130 词的嫌疑成因。
    """
    from backend.lib.lang_profile import profile_for

    assert profile_for("zh").unit == "字"
    assert profile_for("en").unit == "词"


def test_english_repair_prompt_pins_the_output_language():
    """用中文指令去改一篇英文稿,模型可能顺手用中文回 —— 必须钉死。"""
    from backend.lib.lang_profile import profile_for
    from backend.lib.script_gate import repair_body, treatment_prompt

    en = profile_for("en")
    seen = {}

    def ask(system, user):
        seen["system"] = system
        return "expanded English body text here"

    repair_body("Hook. Body one. Body two. Payoff here.",
                "expand_body", 170, "rule", ask, en)

    assert "English" in seen["system"], "英文修复必须钉死输出语言"
    assert "词" in seen["system"], "英文目标应以词计"

    # 原稿里**不能**出现 "English" 字样 —— 否则断言恒真(踩过一次)。
    for lv in (2, 3):
        t = treatment_prompt(lv, "a draft about cats", "reason", "rule", 170, prof=en)
        assert "English" in t, f"疗程{lv}没钉死输出语言"
        assert "词" in t, f"疗程{lv}目标应以词计"
        zh_t = treatment_prompt(lv, "一篇稿子", "reason", "rule", 170)
        assert "English" not in zh_t and "字" in zh_t, f"疗程{lv}中文提示词被改动了"


def test_chinese_repair_prompt_is_unchanged():
    """中文占 95% 产量 —— 提示词一个字都不能动。"""
    from backend.lib.lang_profile import profile_for
    from backend.lib.script_gate import repair_body

    seen = {}

    def ask(system, user):
        seen["system"] = system
        return "改好的正文"

    repair_body("钩子。正文一。正文二。落点在这。",
                "compress_body", 250, "rule", ask, profile_for("zh"))

    assert "字(不含空白)" in seen["system"]   # 数字含 0.78 压缩补偿,断言量纲即可
    assert "English" not in seen["system"]


def test_english_split_never_loses_text():
    """**不变式**:切完拼回去必须等于归一空白后的原文。

    上一版切句用「整句」正则 findall,遇到 `3.5` 这种匹配不上的前缀会被
    finditer 直接跳过 —— `He was worth 3.5 million francs.` 切完只剩
    `5 million francs.`,**静默丢字**。丢掉的部分照样进了 TTS 报价、
    却不会出现在成片里。这条不变式比任何切句细节都重要。
    """
    import re

    from backend.lib.lang_profile import profile_for

    en = profile_for("en")
    samples = [
        "He was worth 3.5 million francs. That is a lot.",
        "Mr. Smith met Dr. Jones at 9.30 sharp. Nobody spoke.",
        "J. K. Rowling wrote it. She is rich. Very rich!",
        "Saint-Germain claimed to be 500 years old. Kings believed him.",
        "No punctuation at all here",
        "Ends without a period but has one 3.5 inside",
    ]
    for s in samples:
        parts = en.split_sentences(s)
        assert " ".join(parts) == re.sub(r"\s+", " ", s).strip(), f"丢字: {s!r} -> {parts}"
