# -*- coding: utf-8 -*-
"""长视频专用脚本生成器 —— 与短视频完全隔离。

思路(从短视频"诙谐幽默/通俗易懂"策略复制+改写而来,自成一套):
  ① DeepSeek `:online` 联网抓真实数据 → 结构化事实资料包
  ② 用【长视频专用】策略提示(反差脊 + 每个硬事实配比喻 + 网感 + 长片节奏)
     让 DeepSeek 直接写一条约 10 分钟的连贯口播稿。

隔离铁律:本模块**绝不调用**短视频的 `LLMClient._build_script_system_prompt`
(short/long 共用的那个),所以打磨长视频脚本永远污染不到短视频。所有旋钮走
独立 env 命名空间 `MEDIA_BUDDY_LONG_SCRIPT_*`。
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 默认走 DeepSeek(与短视频 grounding 同源,但这是长视频自己的 env,互不影响)
_DEFAULT_WRITER = "deepseek/deepseek-v4-flash"
_DEFAULT_SEARCH = "deepseek/deepseek-v4-flash:online"
# 注意:语速在时长上会抵消(脚本字数×语速、播放又÷语速),所以这个常数与语速档无关。
# env 可微调而不重新部署。
_CHARS_PER_SEC = float(os.environ.get("MEDIA_BUDDY_LONG_CHARS_PER_SEC", "4.2") or "4.2")


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off", "")


def long_script_v2_enabled() -> bool:
    """总开关。关掉即回退到旧的 outline→expand 路径。"""
    return _flag("MEDIA_BUDDY_LONG_SCRIPT_V2", "1")


def _grounding_enabled() -> bool:
    return _flag("MEDIA_BUDDY_LONG_SCRIPT_GROUNDING", "1")


def _writer_model() -> str:
    return (os.environ.get("MEDIA_BUDDY_LONG_SCRIPT_WRITER_MODEL", "").strip()
            or _DEFAULT_WRITER)


def _search_model() -> str:
    return (os.environ.get("MEDIA_BUDDY_LONG_SCRIPT_SEARCH_MODEL", "").strip()
            or _DEFAULT_SEARCH)


def _target_chars(duration_seconds: int, speed: float = 1.0) -> int:
    # ×语速:语速越快、读得越快,同样时长要写越多字,成片才等于用户设的时长。
    sp = max(0.7, min(2.0, float(speed or 1.0)))
    return max(1600, int(duration_seconds * _CHARS_PER_SEC * sp))


def _story_scope(duration_seconds: int):
    """内容量随目标时长缩放:短片讲【更少但完整】的案例。返回(案例/底层逻辑数量文案, 时长说明)。

    ⚠️铁律:压时长靠【减案例数量】,绝不靠砍故事——短片是一个完整的小故事,
    不是被截断的大故事。之前 prompt 无论长短都要"3-6 个案例"(≈10 分钟的内容量),
    要么为压字数把故事讲一半。这里让案例数跟着分钟数走,把两头都堵住。"""
    # 🚨 **短视频不是「被压短的长视频」。**
    #
    # 另一半是这里 —— 最短的一档写着「这是一条约 6 分钟的短长视频,
    # 2-3 个案例」,于是模型按 6 分钟的内容量去铺,60 秒当然装不下。
    #
    # ⚠️ 一分钟只够讲**一件事**。多讲一个就意味着每件都只讲半句 ——
    if duration_seconds <= 240:      # 4 分钟以内 = 短视频
        mins = max(1, round(duration_seconds / 60))
        unit = f"{duration_seconds} 秒" if duration_seconds < 90 else f"约 {mins} 分钟"
        return (
            "1 个",
            f"这是一条【{unit}】的短视频,**只讲 1 个点**,把它讲透。"
            "⚠️ 不要铺陈背景、不要列举多个案例 —— 时长只够一件事。"
            "宁可少讲一件,也不要每件都只讲半句。"
        )
    if duration_seconds <= 390:      # 约 6 分钟
        return "2-3 个", "这是一条【约 6 分钟】的短长视频,只挑 2-3 个最能打的底层逻辑/案例,每个都从头讲到尾、讲透。"
    if duration_seconds <= 540:      # 约 8 分钟
        return "3 个", "这是一条【约 8 分钟】的长视频,围绕 3 个底层逻辑/案例,每个完整讲透。"
    return "3-4 个", "这是一条【约 10 分钟】的长视频,3-4 个底层逻辑/案例,层层递进、各自完整。"


def build_grounding_pack(llm: Any, topic: str, model: Optional[str] = None) -> str:
    """就主题联网核实,返回结构化中文事实资料包(话题无关的通用版)。

    `model` 不传 → 全局的 `MEDIA_BUDDY_LONG_SCRIPT_SEARCH_MODEL`(默认 DeepSeek `:online`),
    行为与从前逐字节一致。

    Sonar 的来源网址在返回体的 `message.annotations` 里,正文只有 [1][2] 脚注 ——
    走这里的通用 `_script_via_openrouter_direct` 会把网址**整个丢掉**,
    那就等于白换(选 Sonar 的全部理由就是「来源能查证」)。
    `llm._sonar_research_pack` 里有调好的 6 节提示词 + annotations 合并。
    """
    if str(model or _search_model()).startswith("perplexity/"):
        return llm._sonar_research_pack(topic, max_output_tokens=3500, timeout=200)
    research_system = (
        "你是严谨的中文资料研究员,具备联网搜索能力。就给定主题联网核实,输出一个结构化【事实资料包】,"
        "供后续写口播稿使用。要求:\n"
        "1. 给出 10-18 条已核实事实,每条尽量带具体数字/年份/出处线索(人名、机构、事件、书名等)。\n"
        "2. 【最重要·案例优先】专门挖出该主题最具代表性的 3-6 个【完整真实案例 / 亲历故事】——"
        "每个要有人物、地点、年份、具体数字和完整来龙去脉(某笔具体投资、某次创业、某个真实事件的前因后果),"
        "越具体越好,例如'某年他花X钱做了Y、结果Z'。这些真实招牌案例是口播稿的骨架,比抽象结论重要得多,"
        "务必挖深挖细,宁可少给结论、多给一个讲得清的真实故事。\n"
        "3. 覆盖:核心人物/事物是什么、关键时间线、来龙去脉与机制、主要争议或反直觉点、常被误传需纠正处。\n"
        "4. 数字/年份拿不准就标注'(待核实)',绝不编造精确数字;区分'已证实/常见说法/待核验'。\n"
        "只输出资料包本身,分条列出,不要写任何口播稿、不要写开场白。"
    )
    research_user = f"主题:{topic}\n请联网核实,给出中文事实资料包。"
    return llm._script_via_openrouter_direct(
        research_system, research_user, model or _search_model())


def _language_override(output_language: str) -> str:
    """成片不是中文时，把上面写死的「简体中文」全部覆盖掉。

    （`Series.output_language`），照搬中文硬约束会写出中文稿 → 出片时被管线的
    「语言救援」机翻 → **客户确认过的那一版不是成片那一版**

    ⚠️ 做法是**在最后追加覆盖**，不是去改上面那些句子 ——
    改句子等于对长视频那条**跑熟的**提示词动刀，而它同时在服务频道页出片。
    追加的话 `output_language="zh"` 时返回空串，**行为逐字节不变**
    （有测试断言这一点）。

    ⚠️ 放在**最后**是有意的：同一段提示词里前后矛盾时，靠后的那条更容易被遵守。
    """

    lang = (output_language or "zh").strip().lower().split("-")[0]
    if lang == "zh":
        return ""
    name = {"en": "English"}.get(lang, lang)
    return (
        "\n【⚠️ 语言覆盖 —— 以这一条为准，上面所有关于「简体中文」的要求作废】\n"
        f"**全文用 {name} 写**（标题、正文都是）。\n"
        f"资料包可能是中文的，照样用 {name} 写 —— 成片的配音和字幕用的就是它。\n"
    )


def _build_system_prompt(topic: str, duration_seconds: int, pack: str, speed: float = 1.0,
                         output_language: str = "zh") -> str:
    target = _target_chars(duration_seconds, speed)
    lo, hi, cap = int(target * 0.9), int(target * 1.1), int(target * 1.15)
    minutes = max(1, round(duration_seconds / 60))
    points, scope_note = _story_scope(duration_seconds)
    strategy = (
        "你是顶级中文 YouTube 长视频口播编剧,专写'把硬核知识讲成人话'的通俗易懂长片。\n"
        f"写一条约 {duration_seconds} 秒(≈{target} 字,严格控制在 {lo}-{hi} 字,绝不超过 {cap} 字)的"
        "【简体中文】口播稿。\n"
        f"\n【时长与内容量·硬约束】{scope_note}\n"
        "⚠️【完整性 > 时长·最高铁律】把选定的每个案例【从头讲到尾、有始有终、有升华收尾】;"
        "宁可字数略超也要把故事讲完整,【绝不】为压字数把故事讲一半、【更不能砍掉结尾】。"
        "压时长只能靠【少讲一个案例】,不能靠【把案例讲一半】。\n"
        "\n【核心风格(最高优先级,压过除安全/语言外一切)】\n"
        f"像一个很会讲故事、很有网感的朋友,拉着你把一件事从头讲到尾,{minutes} 分钟不走神、一遍就听懂:\n"
        "1. 【开头:钩子+承诺+后钩子】黄金3秒甩出最反差/最颠覆的事实勾住人,紧接着用一句话向观众承诺"
        f"'今天把它拆成 {points}普通人也能听懂、能落地的底层逻辑',再留一个后钩子"
        "(例:'最硬核的那个,藏在最后一个案例里,一定看到最后')。【绝对禁止】"
        "'警告/你敢信/千万别/你绝对想不到/今天我要讲/大家好/在这个'这类套话起手。\n"
        "2. 【一条反差脊咬到底】全片拢在一句反差脊上(例:'一个看似X的人,凭什么Y'),所有内容为它服务,跑题即删。\n"
        f"3. 【案例骨架·全片最重要】主体拆成 {points}'底层逻辑/境界',层层递进、越到后面越硬核。"
        "每一个逻辑 = 一句大白话原则 + 【深挖一个真实案例讲透:有人物、地点、数字、完整来龙去脉,"
        "慢慢讲、讲出悬念(像'我们来算一笔账''先别急,结局有三种'),别一句带过】 + "
        "'普通人会怎么想 vs 高手怎么做'的对比。案例必须来自下面资料包里的真实招牌案例——这是全片的脊梁。\n"
        "4. 【铁律:真案例优先,绝不用自编通用比喻顶替案例】绝不用'买彩票/煎饼摊/路边小店'这类自己编的通用例子"
        "去替代真实案例;生活化比喻只用来把硬数字/术语翻成人话(例:'年化29%=100万八年变760万'),"
        "不能顶替'讲一个真实发生过的故事'。全片缺真实案例、靠自编例子撑=失败必重写。\n"
        "5. 【幽默克制】适度网感口语、偶尔玩梗/拟人调侃即可——长视频比短视频克制得多,"
        "幽默是调味不是主菜,别每句都抖机灵;主要靠'把事讲顺讲透'和语气起伏(悬念→揭秘→走心)抓人。\n"
        "6. 【段间钩子】每讲完一个逻辑/案例,用一句小钩子承上启下(例'但真正让他封神的,是他抄错之后干的事');"
        "别让中段发闷,段落自然衔接、不重复上文。\n"
        f"7. 【升华收尾+作业式CTA】回收开头的反差脊 + 把这 {points}逻辑浓缩成观众能直接用的'招';"
        "结尾用'留个作业'式收尾——让观众在评论区写下一个【具体答案】(如某个名字 + 他打算怎么做),"
        "比泛泛的开放问题更催评论,可以说'挑几个最好的下期拆解'。禁空洞套路('记得关注点赞/我们下期见'及任何送礼抽奖式 CTA)。\n"
        "8. 【平台安全·收益表述】讲投资/理财收益时,绝不写'年化XXX%''翻N倍保证''稳赚''收益承诺'这类包装"
        "(易触发金融审核/限流);可以说'X个月赚了Y%'这种具体事实,但重点落在背后的逻辑和'为什么便宜/为什么低风险',"
        "别包装成'年化收益率'或对观众的收益承诺。\n"
        "\n【叙事连贯红线·全片最高要求】全片必须是【一条故事线/命运线】,不是知识点清单:"
        "用因果+时间把主角的经历串成一条线,后一个点由前一个自然引出(他因为X→所以Y→Y又逼出Z),"
        "各个'底层逻辑'是这条线上的转折/顿悟,是串在线上的珠子,绝不能写成'第一点、第二点'式并列罗列。"
        "观众听完要像听完一个完整的人物故事,而不是记了一堆散的知识条。"
        "开场交代背景-人物-动机搭好因果链;抛出的悬念/钩子文内必须回收(抛-引-收);全篇同一人称视角与语气。\n"
        "【禁词】高级感/氛围感/极致/匠心/情怀/完美/顶级/打造/综上所述/值得注意的是/"
        "首先其次最后/让我们一起探索/这不仅是…更是/在当今社会。\n"
        "【输出】只输出配音要读的口播正文本身:无小标题/序号/markdown/破折号/'旁白:'类标签/网址/出处。"
        "第一句直接进内容。\n"
        "【语言】全文简体中文(人名/地名/专有名词可保留原文)。\n"
    )
    if pack and pack.strip():
        grounding = (
            "\n【联网事实资料包(正文里的年份/数字/金额/百分比/案例/人名/机构名必须来自这里;"
            "这里没有的具体数字一律模糊化,绝不另编)】\n" + pack.strip() + "\n"
            "【事实红线】具体数字/年份/持仓/业绩只能用资料包里的;拿不准就用'据部分资料''大致''接近'等"
            "模糊说法,绝不编造精确值;引号里的名言必须是资料包中出现过的原话,否则只能转述、不加引号。\n"
        )
    else:
        grounding = (
            "\n【无联网资料包 → 事实红线】没有外部资料支撑时,绝不编造精确数字/年份/金额/百分比;"
            "凡具体数字都用模糊表达('据说''大致''好几倍'),把不确定的说成不确定,宁可少说、绝不说错。\n"
        )
    arc = (
        "\n【结构=一条人物命运线(仅脑内顺序,成稿是连贯口播,绝不出现这些标记/序号/小标题)】\n"
        f"① 黄金3秒反差钩子 + 一句话承诺'拆成他成功的 {points}底层逻辑' + 后钩子(最硬的藏在最后)\n"
        "② 起点:主角原本是谁、干嘛的,一个转折让他走上这条反常的路(尽量用真实案例开场,别干讲道理)\n"
        "③ 顺着时间/因果往下走:他遇到什么 → 悟到什么 → 于是做了什么;每个关键动作落到一个真实案例,"
        "顺带把一个底层逻辑讲透 + 一句'普通人会怎么想 vs 高手怎么做'的对比\n"
        "④ 高光:他靠这套打法封神(最亮的那个真实案例/数字,配比喻)\n"
        "⑤ 转折/至暗:他也栽过跟头(把'反面'讲出来、别只吹),以及他怎么反思迭代——这既兑现后钩子,也让人物立体\n"
        "⑥ 升华:回到开头的反差脊,把他这套逻辑浓缩成观众能直接用的话\n"
        "⑦ 开放式问题收尾\n"
        "(全程是'一个人一步步怎么走到今天'的故事线,各逻辑/案例是这条线上用因果串起来的珠子,绝不并列罗列。)\n"
    )
    return strategy + grounding + arc + _language_override(output_language)


def generate_long_script(
    llm: Any, topic: str, *, duration_seconds: int = 600,
    pack: Optional[str] = None, speed: float = 1.0,
    writer: Optional[str] = None, searcher: Optional[str] = None,
) -> str:
    """长视频专用:DeepSeek 联网抓数据 + 长视频专用策略写 ~N 分钟口播稿。返回口播正文。

    pack:
      - None(默认)→ DeepSeek `:online` 自动联网抓资料包(适合规模化、任意主题)。
      - 传入字符串 → 用这份【人工核实过的资料包】写稿、跳过自动抓(适合重要视频,案例保真)。
    """
    if pack is not None:
        logger.info("long-script using SUPPLIED verified pack (%d chars); skip auto-grounding",
                    len(pack))
    elif _grounding_enabled():
        try:
            t0 = time.time()
            pack = build_grounding_pack(llm, topic, searcher) or ""
            logger.info("long-script grounding pack=%d chars in %.1fs",
                        len(pack), time.time() - t0)
        except Exception as e:
            logger.warning("long-script grounding failed (%s); writing without pack", e)
            pack = ""
    else:
        pack = ""
    system = _build_system_prompt(topic, duration_seconds, pack, speed)
    target = _target_chars(duration_seconds, speed)
    user = (
        f"主题:{topic}\n"
        f"严格按系统风格,写这条约 {target} 字(绝不超过 {int(target * 1.15)} 字)的中文长视频口播稿:"
        "先在心里定一条反差脊,一条脊到底、全程不发闷。只输出口播正文。" + chr(10)
        # 🚨 **删掉了「每个硬事实配比喻」。**
        #
        # 数据通路」这类**生造词**,还有「把一根变成…」(掉名词)、
        # 「硬冻免费」(打错字)。
        #
        # 病因:这句在 user 的**最后**、权重最高,要求「**每个**硬事实配比喻」。
        # 而写稿模型是 `deepseek-v4-flash`(最便宜的一档)——
        # 弱模型被要求「每个事实都必须配比喻」,凑不出来就**硬造**。
        #
        # ⚠️ 同一个文件第 164 行本来就写着「生活化比喻**只用来**把硬数字
        #    翻成人话」—— 这一句把那条规矩推翻了。现在改成和它一致:
        #    **能自然用就用,凑不出来宁可不用。**
        + "⚠️ 比喻只在**能让人一下听懂**时才用(把数字/术语翻成人话),"
        + "凑不出自然的比喻就**不要用** —— 生硬的比喻比没有比喻糟得多。" + chr(10)
        + "⚠️ 每句话都要读得通、是完整的中文。宁可平实,也不要为了「有网感」造词。"
    )
    t0 = time.time()
    # 不影响老系统)。不传 → 全局那个,行为与从前逐字节一致。
    #
    #    对主写稿路径**从来没生效过**(端到端拦 HTTP 才发现:设了
    #    MB_RADAR_SCRIPT_WRITER_MODEL=claude,实际打的还是 deepseek)。
    script = llm._script_via_openrouter_direct(
        system, user, writer or _writer_model())
    # 兜底清掉模型偶尔漏出的标题行(短视频的 _clean_script_output 没覆盖这几个)
    script = re.sub(
        r"^\s*(?:口播稿正文|口播稿|口播文案|正文|全文)\s*[:：]?\s*\n+",
        "", str(script or ""),
    ).strip()
    logger.info("long-script written=%d chars (target~%d) in %.1fs",
                len(script), target, time.time() - t0)
    return script
