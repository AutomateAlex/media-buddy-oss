# -*- coding: utf-8 -*-
"""

## 发生了什么

（whisper 听到的字和我们稿子对上的比例）：


**0.00 不是「差一点」，是完全对不上。**

根因：`forced_align.py` 里写死 `language="zh"`。英文旁白被强制按中文转写 →
出来一堆音译汉字 → 和英文稿逐字比对 → 几乎零匹配 → 字幕时间全靠插值猜。

⚠️ 而且日志一律打「forced align OK」，**没有任何阈值**，0.00 也照样发货。

## 守什么

1. 对齐脚本不许再写死语言
2. 调用方必须把稿子的语言传下去（中文 zh / 其他 en）
3. 语言判定复用现成的 `_qwen_language`，别再写一套
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

ALIGN = Path(__file__).resolve().parents[2] / "src/backend/scripts/forced_align.py"


def test_对齐脚本不许写死语言():
    """⚠️ 只看**真代码**，不看注释。

    上一版守卫直接在全文里搜 `language=` 加中文码，结果被自己写的
    「原来是 …」那句注释绊倒 —— 本项目第 4 次栽在「守卫被自己的注释骗了」。
    改成走 AST：只检查 `transcribe(...)` 这一次调用的实参。
    """
    tree = ast.parse(ALIGN.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "transcribe"]
    assert calls, "找不到 transcribe 调用 —— 守卫失效了，先修守卫"
    for c in calls:
        for kw in c.keywords:
            if kw.arg != "language":
                continue
            assert not isinstance(kw.value, ast.Constant), (
                "语言又被写死成 %r —— 英文片子的字幕会完全对不齐"
                % (kw.value.value,))
    src = ALIGN.read_text(encoding="utf-8")
    assert "MEDIA_BUDDY_ALIGN_LANG" in src, "没有让调用方指定语言的口子"


def test_没给语言时让whisper自己判():
    """写死中文比自动判更糟：自动判至少英文能对上。"""
    src = ALIGN.read_text(encoding="utf-8")
    assert "lang = None" in src or "= None" in src


def test_调用方把语言传下去():
    from backend.services.tts_service import TTSService
    src = inspect.getsource(TTSService._forced_align_tokens)
    assert "MEDIA_BUDDY_ALIGN_LANG" in src, "没把语言传给子进程 —— 脚本只能靠自动判"
    assert "env=_env" in src, "构造了 env 却没传给 subprocess"
    assert "_qwen_language" in src, "又写了一套语言判定 —— 复用现成的那个"


@pytest.mark.parametrize("text,expect", [
    ("这是一段中文旁白，讲的是华为的故事。", "zh"),
    ("This is an English narration about lightning strikes.", "en"),
    ("The Empire State Building gets struck by lightning 20 times a year.", "en"),
])
def test_语言判定对得上(text, expect):
    from backend.services.tts_service import _qwen_language
    got = "zh" if _qwen_language(text) == "Chinese" else "en"
    assert got == expect, "「%s…」被判成了 %s" % (text[:18], got)


def test_match_rate太低要吼出来():
    """以前不管多低都打「forced align OK」，0.00 也照样发货。

    没有阈值 = 下一次同类回归依然静悄悄上线，客户先发现、我们后知道。
    """
    from backend.services.tts_service import TTSService
    src = inspect.getsource(TTSService._forced_align_tokens)
    assert "MEDIA_BUDDY_ALIGN_MIN_MATCH" in src, "没有可调的阈值"
    assert "LOW MATCH" in src, "低匹配没有独立的、能 grep 的日志标记"
    tail = src.split("if _rate < _floor:")[1]
    assert "logger.warning" in tail.split("else:")[0], "低匹配还是打的 info"
    # ⚠️ 低了仍要照用 —— 退回「整段平均分」没量过，不许顺手改行为
    assert src.count("return data[\"tokens\"]") == 1,         "低匹配时不许直接 return None：那等于用没量过的估算换掉真实段落时间"


MIXED_EN = (
    "Ren Zhengfei founded Huawei in Shenzhen in 1987. "
    "His partners 郑宝用 and 孙亚芳 joined later, and the Shenzhen 南山区 office "
    "became the base for 华为技术有限公司 across 中国大陆 and 香港 markets."
)
MIXED_ZH = (
    "1987 年任正非在深圳创办华为，早期团队里有 Ren Zhengfei 自己写的 BASIC 程序，"
    "后来又引入 CDMA、GSM、LTE、5G NR 等一连串 technology standards。"
)


def test_有设定时听设定不听猜的():
    """

    按文本猜（数中日韩字 vs 拉丁字母）在混合内容上会猜反，
    一猜反就又回到「听错语言 → 一个字对不上 → 字幕时间全靠猜」的老坑。
    """
    from backend.services.tts_service import TTSService
    svc = TTSService()
    svc.output_language = "en"
    assert svc._align_language(MIXED_EN) == "en"
    svc.output_language = "zh"
    assert svc._align_language(MIXED_ZH) == "zh"


def test_混合内容上猜会猜反_所以才要设定():
    """证明「猜」确实不够用 —— 不是我多此一举。

    这两段是真实会出现的形态：英文稿里塞中文人名/地名，中文稿里引一串英文术语。
    """
    from backend.services.tts_service import TTSService, _qwen_language
    guess_en = "zh" if _qwen_language(MIXED_EN) == "Chinese" else "en"
    guess_zh = "zh" if _qwen_language(MIXED_ZH) == "Chinese" else "en"
    猜反了 = (guess_en != "en") or (guess_zh != "zh")
    assert 猜反了, "这两段样本已经骗不到猜法了，换两段更混的，否则这条测试没在守东西"

    svc = TTSService()
    for setting, text in (("en", MIXED_EN), ("zh", MIXED_ZH)):
        svc.output_language = setting
        assert svc._align_language(text) == setting, "设定没能盖过猜"


def test_没设定或设定不认识时退回猜():
    """桌面端老调用、或将来多出别的语言 → 不能因此报错，退回猜就好。"""
    from backend.services.tts_service import TTSService
    svc = TTSService()
    for bad in (None, "", "fr", "auto", "  "):
        svc.output_language = bad
        assert svc._align_language("This is plain English narration.") == "en"
        assert svc._align_language("这是一段纯中文旁白。") == "zh"


def test_设定每次合成都重设不留上一条的():
    """⚠️ 存在实例上就要防陈旧：上一条英文片的设定不能漏给下一条中文片。"""
    import inspect
    from backend.services.tts_service import TTSService
    src = inspect.getsource(TTSService.synthesize)
    assert "self.output_language = (" in src, "没有在每次 synthesize 时重设"
    assert "output_language: Optional[str] = None" in src, "参数默认值不是 None（会改变老调用的行为）"


def test_管线真的把设定传下去了():
    """开关设了 ≠ 接进了调用链 —— 这个项目栽过很多次。"""
    import inspect
    from backend.services.pipeline_service import PipelineService
    run_src = inspect.getsource(PipelineService)
    assert 'output_language=str(project_data.get("output_language") or "zh").lower()' in run_src,         "项目的设定没传进 _stage_assets"
    stage = inspect.getsource(PipelineService._stage_assets)
    assert "output_language=output_language" in stage, "_stage_assets 没往下传给 synthesize"


# ────────── 不止字幕：配音本身也要听设定 ──────────

def test_配音语言听设定不听猜的():
    """🚨 这个值决定发给千问的 language_type 和用哪套风格指令。

    猜反 = **整条片子念错语言**，比字幕对不齐严重得多。
    """
    from backend.services.tts_service import TTSService
    svc = TTSService()
    svc.output_language = "zh"
    assert svc._delivery_language(MIXED_ZH) == "Chinese"
    assert svc._delivery_language("Pure English text here.") == "Chinese",         "设定说中文就该念中文，哪怕文本看着像英文"
    svc.output_language = "en"
    assert svc._delivery_language(MIXED_EN) == "English"


def test_配音没设定时退回猜():
    from backend.services.tts_service import TTSService
    svc = TTSService()
    svc.output_language = None
    assert svc._delivery_language("这是一段中文。") == "Chinese"
    assert svc._delivery_language("This is English.") == "English"


def test_千问合成真的用了这个判定():
    """开关设了 ≠ 接进了调用链。"""
    import inspect
    from backend.services.tts_service import TTSService
    src = inspect.getsource(TTSService._synthesize_qwen)
    assert "self._delivery_language(text)" in src, "千问合成还在自己猜"
    assert "language = _qwen_language(text)" not in src


def test_段长校验也听设定():
    """中英文语速表是两个量纲（中文按字/秒、英文按词/秒）。

    """
    import inspect
    from backend.services.tts_service import _segment_duration_ok, TTSService
    src = inspect.getsource(_segment_duration_ok)
    assert "lang: str | None = None" in src, "没有让调用方传设定的口子"
    assert 'if lang not in ("zh", "en")' in src, "没有「设定不认识就退回猜」的兜底"
    qwen = inspect.getsource(TTSService._synthesize_qwen)
    assert qwen.count("lang=self.output_language") == 2, "两个调用点没都把设定传进去"


def test_一条片子的设定不会漏给下一条():
    """⚠️ 存在实例上就要防陈旧：上一条英文片的设定不能留给下一条中文片。"""
    from backend.services.tts_service import TTSService
    svc = TTSService()
    svc.output_language = "en"
    assert svc._align_language("这是中文。") == "en"      # 这条片子设定就是英文
    svc.output_language = "zh"                            # 下一条片子换成中文
    assert svc._align_language("这是中文。") == "zh"
    assert svc._delivery_language("This is English.") == "Chinese"
