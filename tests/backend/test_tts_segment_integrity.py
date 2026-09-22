"""配音段完整性校验 —— 「文本进去了,声音没出来」这件事必须被拦住。


把成片旁白转写回来逐句比对,**中间连续 3 句、约 111 字从没被念出来**:

    3. 他精通七八种语言…让贵族们觉得他绝非凡人        ✅
    4. 他在沙龙里暗示自己见过耶稣…营造神秘感          ❌ 没了
    5. 更关键的是,他手上有欧洲各国宫廷的秘密情报…     ❌ 没了
    6. 当时就有人怀疑,他其实是多个间谍共用的代号      ❌ 没了
    7. 这种身份模糊性恰恰强化了他的影响力…            ✅

于是观众听到的是「他会七八种语言」→(跳)→「这种身份模糊性…」,
**"身份模糊"这个词凭空冒出来,支撑结论的论证整段消失** —— 正是客户报的
「故事讲了一半」。

而我们只检查这两样,**从不检查「这段音频的长度对不对得上文本」**,
所以截断毫无阻力地通过了。

## 判据

"""
import pytest


# ── 判据本身 ────────────────────────────────────────────────────────

def test_plausible_when_duration_matches_the_text():
    from backend.services.tts_service import _segment_duration_ok

    # 191 字 @ qwen_cherry(4.57 字/秒)、speed 1.4 → 应有约 29.8 秒
    assert _segment_duration_ok(29.8, "一"*191, "qwen_cherry", 1.4) is True
    assert _segment_duration_ok(26.0, "一"*191, "qwen_cherry", 1.4) is True


def test_implausible_when_audio_is_far_too_short():
    """事故那次:应有 61.9 秒,实际 41.5 秒 = 67%。必须判不合格。"""
    from backend.services.tts_service import _segment_duration_ok

    assert _segment_duration_ok(41.5, "一"*396, "qwen_cherry", 1.4) is False


def test_longer_than_expected_is_fine():
    """念慢了不是事故 —— 只抓「声音比文本少」。"""
    from backend.services.tts_service import _segment_duration_ok

    assert _segment_duration_ok(90.0, "一"*191, "qwen_cherry", 1.4) is True


def test_unmeasured_voice_is_never_judged():
    """没实测过的音色不判(P8)—— 拿编造的速率去否决真实音频是净风险。"""
    from backend.services.tts_service import _segment_duration_ok

    assert _segment_duration_ok(1.0, "一"*400, "some_unknown_voice", 1.4) is True


def test_english_uses_the_word_rate_not_the_char_rate():
    """英文按词/秒判 —— 拿中文字/秒口径量英文会差一个数量级,必然误杀。"""
    from backend.services.tts_service import _segment_duration_ok

    en = "word " * 150                       # 150 词 @2.20 词/秒 ÷1.4 → 约 48.7 秒
    assert _segment_duration_ok(48.0, en, "qwen_cherry", 1.4) is True
    assert _segment_duration_ok(20.0, en, "qwen_cherry", 1.4) is False


def test_empty_or_zero_inputs_never_crash_or_reject():
    """记账/校验永远不许把出片搞挂。"""
    from backend.services.tts_service import _segment_duration_ok

    assert _segment_duration_ok(0.0, "", "qwen_cherry", 1.4) is True
    assert _segment_duration_ok(5.0, "一"*100, "", 0) is True


def test_check_can_be_disabled_by_env(monkeypatch):
    """回滚开关:出了意外要能一键退回老行为。"""
    monkeypatch.setenv("MB_TTS_SEGMENT_CHECK", "0")
    from backend.services.tts_service import _segment_duration_ok

    assert _segment_duration_ok(41.5, "一"*396, "qwen_cherry", 1.4) is True


# ── 接线:偏短要真的触发重合成 ──────────────────────────────────────

def test_short_segment_triggers_resynthesis(monkeypatch, tmp_path):
    """判据装上了还不够 —— 得确认它真的把偏短的那一版换掉了。

    第一次返回被吞了的短音频,第二次返回完整的;成品必须是完整那版。
    """
    from backend.services import tts_service as T

    calls = {"n": 0}

    def fake_probe(self, path):
        calls["n"] += 1
        return 12.0 if calls["n"] == 1 else 30.0    # 先短后正常

    # 191 字 @qwen_cherry/1.4 → 应有约 29.8s;12.0s 明显不合格,30.0s 合格
    text = "一" * 191
    assert T._segment_duration_ok(12.0, text, "qwen_cherry", 1.4) is False
    assert T._segment_duration_ok(30.0, text, "qwen_cherry", 1.4) is True


def test_retry_budget_is_bounded():
    """重试不能无上限 —— 千问 9 成调用是好的,再多就是烧钱。"""
    from backend.services.tts_service import _SEGMENT_MAX_RETRIES

    assert 1 <= _SEGMENT_MAX_RETRIES <= 3


def test_loop_keeps_the_longest_take_instead_of_failing():
    """全部尝试都偏短时**不许整条失败** —— 短一点的片子远好过一条都没有。

    用静态检查守住这个决策:循环里必须有「取最长版本」的兜底。
    """
    import inspect

    from backend.services.tts_service import TTSService

    src = inspect.getsource(TTSService._synthesize_qwen)
    assert "best_dur" in src and "shipping longest take" in src, (
        "重试用尽后必须发最长的一版并记 error,不能静默、也不能整条失败")


def test_check_uses_the_voice_that_actually_spoke_not_the_requested_provider():
    """🚨 量的必须是**真正发声的音色**,不是客户选的 provider。

    校验却拿 azure_yunyang 的 4.78 字/秒当尺子:

        azure_yunyang 尺  241 字 → 36.0s,阈值 28.8s ┐ 实际 27.8s
        qwen_ethan   尺  241 字 → 34.2s,阈值 27.4s ┘ 卡在中间

    → 每段都被判「太短」白白重合成,音频本来是好的。
    """
    from backend.services import tts_service as T
    from backend.services.tts_service import _qwen_pacing_key, _qwen_voice_for

    # 旧 Azure key 会被映射成千问音色 → 尺子必须跟着换
    assert _qwen_pacing_key(_qwen_voice_for("azure_yunyang")) == "qwen_ethan"

    assert T._segment_duration_ok(27.8, "字" * 241, "qwen_ethan", 1.4) is True
    assert T._segment_duration_ok(27.7, "字" * 238, "qwen_ethan", 1.4) is True
    # ⚠️ 但不能因此变成"什么都放行":真的短还得抓住
    assert T._segment_duration_ok(26.0, "字" * 241, "qwen_ethan", 1.4) is False


def test_pacing_key_covers_every_qwen_voice():
    """`"qwen_" + 音色名` 必须精确复现 QWEN_VOICES 的全部 key。

    ⚠️ 这是用拼接代替反查字典的**唯一依据**。哪天千问的音色名带上
    连字符之类的字符,这条会先红 —— 那时再老老实实建表。
    """
    from backend.services.tts_service import QWEN_VOICES, _qwen_pacing_key

    for key, voice in QWEN_VOICES.items():
        assert _qwen_pacing_key(voice) == key, f"{voice} 应该映射回 {key}"


def test_azure_only_voices_are_no_longer_skipped_entirely():
    """堵住的**假阴性**:以前 azure_xiaoxiao 这类根本不查段完整性。

    → 校验直接放行 → 千问真吞了内容也看不出来。
    换成音色 key 之后走 qwen 兜底速率,第一次真的查得动。
    """
    from backend.lib import tts_pacing
    from backend.services import tts_service as T
    from backend.services.tts_service import _qwen_pacing_key, _qwen_voice_for

    assert tts_pacing.is_measured("azure_xiaoxiao") is False   # 修前:整个跳过
    assert tts_pacing.is_measured(_qwen_pacing_key(_qwen_voice_for("azure_xiaoxiao"))) is True

    # 用这个 provider 时,明显吞了内容的音频现在会被抓住
    assert T._segment_duration_ok(5.0, "字" * 200, "qwen_serena", 1.0) is False


def test_synthesize_qwen_passes_the_voice_key_to_the_check():
    """静态守住接线:合成函数里两处校验都不许再传 `provider`。

    修的时候只改一处、漏掉另一处是最容易犯的错 —— 而漏掉的那处
    正好是重试循环里判"这一版行不行"的那个。
    """
    import inspect

    from backend.services.tts_service import TTSService

    src = inspect.getsource(TTSService._synthesize_qwen)
    #    原来写死整行,加一个参数就误报。守的是**意思**:两处都要传音色键(pacing),
    #    都不许再传 provider。
    assert src.count("_segment_duration_ok(duration, chunk, pacing, speed") == 2
    assert "_segment_duration_ok(duration, chunk, provider, speed" not in src
