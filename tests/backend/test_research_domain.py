# -*- coding: utf-8 -*-
"""

## 发现的 bug

`travel` 的关键词里有裸的「日本」→ 任何提到日本的题材都被判成旅游：
    「2024年以来日本央行退出负利率」→ travel ❌

字典是**按顺序第一个匹配即返回**，travel 排在 finance 前面，所以它赢了。

## ⚠️ 为什么不能只删「日本」

`travel` 在 `MEDIA_BUDDY_GROUNDING_STRICT_DOMAINS`（finance,travel,wisdom,crime）里，
所以「日本央行」那题**恰好是靠这个 bug 才拿到强制联网核实的**。
只删「日本」会让它掉到 general → 强制核实没了 → 财经题材反而更容易编。
所以同时补了 finance 的宏观/民生金融词。

这些测试守的就是「修完之后强制核实一条都没掉」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.lib.llm_client import LLMClient  # noqa: E402

cli = LLMClient()


# ── 1. 裸国名不许再把财经题材拐到旅游 ────────────────────────────────────
def test_日本央行不该被判成旅游():
    assert cli.research_domain("2024年以来日本央行退出负利率，对普通人的钱包意味着什么", "") \
        == "finance"


def test_travel里不许有裸国名():
    """国名不是旅游信号。放进来会污染所有提到该国的题材。"""
    kw = set(LLMClient._RESEARCH_DOMAIN_KEYWORDS["travel"])
    for 国名 in ("日本", "美国", "中国", "韩国", "法国", "泰国", "英国", "德国"):
        assert 国名 not in kw, "travel 关键词里又混进了裸国名「%s」" % 国名


@pytest.mark.parametrize("topic", [
    "京都赏枫三日自由行怎么安排",
    "日本关西旅游签证怎么办",
    "大阪环球影城门票和营业时间",
])
def test_真旅游题材还是判得对(topic):
    """删掉「日本」不能误伤真正的旅游题材。"""
    assert cli.research_domain(topic, "") == "travel"


# ── 2. 强制联网核实一条都不许掉 ──────────────────────────────────────────
@pytest.mark.parametrize("topic", [
    "2024年以来日本央行退出负利率，对普通人的钱包意味着什么",   # 原来靠 travel 意外拿到
    "美联储加息对房贷的影响",
    "人民币汇率跌破 7.3 意味着什么",
    "京都赏枫三日自由行怎么安排",
])
def test_财经和旅游题材必须强制联网核实(topic):
    """这类题材编不得 —— 数字错了客户会拿去做决策。"""
    assert cli.grounding_required(topic, "") is True, \
        "「%s」丢了强制联网核实 —— 会开始编数字" % topic[:20]


# ── 3. 科普类要能分对（分不对就没法按题材选搜索源）────────────────────────
@pytest.mark.parametrize("topic", [
    "为什么深海鱼捞上来会爆炸？压力到底对身体做了什么",
    "黑洞是怎么形成的？爱因斯坦当年为什么不信",
    "为什么有些人怎么吃都不胖？代谢差异到底从哪来",
    "疫苗是怎么让免疫系统记住病毒的",
    "量子纠缠到底违不违反光速限制",
])
def test_科普题材判成science(topic):
    assert cli.research_domain(topic, "") == "science"


# ── 4. 别把不相干的都吸进来 ──────────────────────────────────────────────
@pytest.mark.parametrize("topic,not_domain", [
    ("任正非在1987年用2万块创办华为", "science"),
    ("任正非在1987年用2万块创办华为", "travel"),
])
def test_人物题材不该被科普或旅游吸走(topic, not_domain):
    assert cli.research_domain(topic, "") != not_domain


#
#   科普类         Sonar        数字合计 77  真网址 3
#                  DeepSeek联网 数字合计117  真网址10  → DeepSeek 赢
#                  千问         数字合计158  真网址 0（验不了）

@pytest.mark.parametrize("topic", [
    "为什么深海鱼捞上来会爆炸？压力到底对身体做了什么",
    "黑洞是怎么形成的？爱因斯坦当年为什么不信",
    "蜜獾为什么天不怕地不怕",
])
def test_科普类也走Sonar(topic, monkeypatch):
    """🚨 一度分流给 DeepSeek，依据是「数字多、网址多」—— 那个依据是错的。

    补做科普类逐条联网核实（每家 16~18 条）：

    **数字多不等于好**：多出来的一半是编的，客户拿到的是看着扎实、
    其实是假的稿子。少而真 > 多而假。
    """
    monkeypatch.delenv("MEDIA_BUDDY_SONAR_MODEL", raising=False)
    d = cli.research_domain(topic, "")
    assert LLMClient._search_model_for_domain(d) == "perplexity/sonar", \
        "科普类又被分流走了 —— Sonar 才是最准的(假 18% vs DeepSeek 33%)"


@pytest.mark.parametrize("topic", [
    "2024年以来日本央行退出负利率，对普通人的钱包意味着什么",
    "任正非在1987年用2万块创办华为",
    "京都赏枫三日自由行怎么安排",
])
def test_其他题材走Sonar(topic, monkeypatch):
    monkeypatch.delenv("MEDIA_BUDDY_SONAR_MODEL", raising=False)
    d = cli.research_domain(topic, "")
    assert LLMClient._search_model_for_domain(d) == "perplexity/sonar"


def test_env可以整体覆盖(monkeypatch):
    """A/B 时要能一键把所有题材切到同一个模型。"""
    monkeypatch.setenv("MEDIA_BUDDY_SONAR_MODEL", "perplexity/sonar-pro")
    assert LLMClient._search_model_for_domain("science") == "perplexity/sonar-pro"
    assert LLMClient._search_model_for_domain("finance") == "perplexity/sonar-pro"


def test_env可以按题材覆盖(monkeypatch):
    monkeypatch.delenv("MEDIA_BUDDY_SONAR_MODEL", raising=False)
    monkeypatch.setenv("MEDIA_BUDDY_SEARCH_MODEL_SCIENCE", "perplexity/sonar")
    assert LLMClient._search_model_for_domain("science") == "perplexity/sonar"
    assert LLMClient._search_model_for_domain("finance") == "perplexity/sonar"


def test_DeepSeek要有真正的墙上时钟超时():
    """

    ⚠️ 第一版只给 httpx 传 `timeout=90` —— **拦不住**。
    httpx 的 timeout 是**每次读取**的超时，不是总时长：上游只要一直慢慢
    只有 `future.result(timeout=)` 卡得住总时长。
    """
    import inspect
    src = inspect.getsource(LLMClient._sonar_research_pack)
    assert 'if ":online" in model:' in src
    assert "result(timeout=wall_deadline)" in src, (
        "又退回 httpx 的 timeout 了 —— 它拦不住慢速吐字节，实测能跑 587 秒")
    assert "search_wall_timeout" in src
    assert "shutdown(wait=False)" in src, (
        "等被放弃的线程会把超时又等回来，等于没超时")


def test_墙上超时真的会抛(monkeypatch):
    """行为测试：模拟一个慢到爆的上游，必须在设定秒数内抛错，而不是一直等。

    要真的验它会中断。
    """
    import time as _t
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("MEDIA_BUDDY_SEARCH_WALL_TIMEOUT", "1")
    monkeypatch.delenv("MEDIA_BUDDY_SONAR_MODEL", raising=False)

    def _slow(*a, **k):
        _t.sleep(30)          # 模拟上游慢慢吐字节，httpx 的 timeout 拦不住
        raise AssertionError("不该走到这 —— 墙上超时没生效")

    monkeypatch.setattr("httpx.post", _slow)
    # 现在默认全部走 Sonar，没有题材会命中 :online。用 env 显式指一个
    # :online 模型来触发墙上超时那条路 —— 这段代码要留着（以后恢复分流还用得上）。
    monkeypatch.setenv("MEDIA_BUDDY_SONAR_MODEL", "deepseek/deepseek-v4-flash:online")
    t0 = _t.monotonic()
    with pytest.raises(RuntimeError, match="search_wall_timeout"):
        LLMClient()._sonar_research_pack("话题", domain="science")
    elapsed = _t.monotonic() - t0
    assert elapsed < 10, "等了 %.1f 秒才抛 —— 墙上超时没起作用" % elapsed


def test_默认没有任何题材走online模型(monkeypatch):
    """现在全部走 Sonar。墙上超时的代码保留（恢复分流时还用得上），
    但默认不该有任何题材命中它。"""
    monkeypatch.delenv("MEDIA_BUDDY_SONAR_MODEL", raising=False)
    for d in ("science", "animal", "finance", "travel", "history", "general"):
        assert ":online" not in LLMClient._search_model_for_domain(d), d
