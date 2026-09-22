# -*- coding: utf-8 -*-
"""音色注册表：**一张表说了算。**


在此之前，音色定义散落在 **8 处**，靠注释里的「必须保持一致」人肉同步：

    运行时映射表(供应商 id)      客户可见目录(展示信息)
    AZURE_VOICES        35      _ELEVENLABS_ZH_CATALOG  12
    QWEN_VOICES         24      _ELEVENLABS_EN_CATALOG  15
    ELEVENLABS_VOICES   28      _GOOGLE_EN_CATALOG      15
    GOOGLE_TTS_VOICES   15      _QWEN_ZH_CATALOG        24

    另有 pipeline_service 里两个内联的 `_US_EN_ELEVEN` 集合、
    前端 qwenVoices.ts / voiceLabels.ts。

**加一个音色要改 8 处，漏一处就是「客户选了 A、出来是 B」。**

  · 中文 ElevenLabs 目录里 3 个音色**运行时表里根本没有**
    (`elevenlabs_mandarin_narrator` / `_mandarin_storyteller` / `elevenlabs_yichen`)
    → 三个不同音色出来是同一个人。藏了很久，没人发现。

**第二个 bug 就是建这张表的过程中自动暴露的** —— 那正是它的价值。

## 怎么用

所有下游**从这里派生**，不要再自己维护名单：

    AZURE_VOICES / QWEN_VOICES / ELEVENLABS_VOICES / GOOGLE_TTS_VOICES
    catalog_for("qwen_zh")        客户可见目录
    voice_language("qwen_cherry") 这个音色说什么语言

## ⚠️ 两条硬规矩

   （119 `qwen_cherry` + 38 `azure_yunyang`）。改名 = 数据订正。
2. **`catalog` 非空 = 客户能选。** 客户能选的音色**必须**在运行时表里有
   `native`，否则就是上面那个「三个音色一个声音」的 bug。
   守卫见 `tests/backend/test_voice_registry.py`。

## 本文件是**自动生成**的

从当时的 8 张表生成，并有断言证明「派生值 == 原始值」。
以后加音色**直接改这里**（手动），下游自动跟随。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Voice:
    """一个音色的全部事实。"""

    key: str                 # provider_key —— 客户配置里存的就是它，**绝不改名**
    vendor: str              # azure / qwen / elevenlabs
    native: object           # 供应商那边的 id
    language: str            # 这个音色说什么语言：zh / en / ""(未知)
    gender: str = ""
    catalog: str | None = None   # 出现在哪个客户可见目录；None = 不对客户暴露
    display_name: str = ""
    description: str = ""
    #    耦合在 `catalog` 上就会出这种事 —— 我改的时候真的出了一次。
    premium_english: bool = False
    # 🧊 **停用中的老供应商。** True = 不再对客户开放，只为在途/历史项目保留合成能力。
    #    见本文件末尾「停用清单」。改动这些音色的行为前先读那一节。
    legacy: bool = False
    # 官方给的**一词性格**（Informative / Firm / Warm …）。
    #    我原来的 15 个里有两对同性格(Orus/Kore 都是 Firm、Iapetus/Erinome 都是 Clear),
    #    他盲听时直接说「有几个是一模一样的」。技术上没问题、选品上是错的:
    #    客户看到 8 个美音,实际只听得出 4~5 种。守卫见 test_voice_registry。
    character: str = ""


# 🚨 **已知损坏的音色 —— 不许放回客户可选目录。**
#
# 这三个出现在旧的 `_ELEVENLABS_ZH_CATALOG` 里(客户能选),但
# `ELEVENLABS_VOICES` 运行时表里**根本没有它们**。代码兜底是:
#
#     el_voice = ELEVENLABS_VOICES.get(provider) or voice_id or <default>
#
# → 三个不同名字的音色,出来**是同一个人**。
#
# 修法是**从客户可选目录里拿掉**:一个会播成别人声音的音色,
# 比没有这个选项更糟。要恢复它们,得先补上真实的 ElevenLabs voice id。
#
KNOWN_BROKEN: frozenset[str] = frozenset({
    'elevenlabs_mandarin_narrator',
    'elevenlabs_mandarin_storyteller',
    'elevenlabs_yichen',
})


VOICES: tuple[Voice, ...] = (
    Voice('qwen_cherry', 'qwen', 'Cherry', 'zh',
          'female', 'qwen_zh',
          '芊悦 · 阳光女声（默认）',
          '阳光积极、亲切自然，适合大多数讲解口播。'),
    Voice('qwen_maia', 'qwen', 'Maia', 'zh',
          'female', 'qwen_zh',
          '四月 · 知性女声',
          '知性温柔、自然成熟，适合产品介绍和知识讲解。'),
    Voice('qwen_serena', 'qwen', 'Serena', 'zh',
          'female', 'qwen_zh',
          '苏瑶 · 温柔女声',
          '温柔舒缓，适合课程、陪伴和情感内容。'),
    Voice('qwen_elias', 'qwen', 'Elias', 'zh',
          'female', 'qwen_zh',
          '墨讲师 · 知识女声',
          '严谨又有叙事感，适合课程、科普和教程。'),
    Voice('qwen_neil', 'qwen', 'Neil', 'zh',
          'male', 'qwen_zh',
          '阿闻 · 主持男声',
          '字正腔圆的专业男主持，适合新闻和商业解说。'),
    Voice('qwen_ethan', 'qwen', 'Ethan', 'zh',
          'male', 'qwen_zh',
          '晨煦 · 阳光男声',
          '阳光温暖有活力，适合科技和知识讲解。'),
    Voice('qwen_kai', 'qwen', 'Kai', 'zh',
          'male', 'qwen_zh',
          '凯 · 松弛男声',
          '舒服松弛，适合长视频和有声内容。'),
    Voice('qwen_moon', 'qwen', 'Moon', 'zh',
          'male', 'qwen_zh',
          '月白 · 帅气男声',
          '自信帅气、年轻，适合科技和年轻化产品。'),
    Voice('qwen_vincent', 'qwen', 'Vincent', 'zh',
          'male', 'qwen_zh',
          '田叔 · 烟嗓男声',
          '沙哑烟嗓、有故事感，适合纪录片和历史。'),
    Voice('qwen_eldric_sage', 'qwen', 'Eldric Sage', 'zh',
          'male', 'qwen_zh',
          '沧明子 · 睿智老者',
          '沉稳睿智的老者，适合历史和传统文化。'),
    Voice('qwen_arthur', 'qwen', 'Arthur', 'zh',
          'male', 'qwen_zh',
          '徐大爷 · 质朴老者',
          '质朴沧桑的说书嗓，适合故事和怀旧。'),
    Voice('qwen_bellona', 'qwen', 'Bellona', 'zh',
          'female', 'qwen_zh',
          '燕铮莺 · 戏剧女声',
          '洪亮清楚、戏剧张力强，适合广告和预告片。'),
    Voice('qwen_nofish', 'qwen', 'Nofish', 'zh',
          'male', 'qwen_zh',
          '不吃鱼 · 设计师男声',
          '不卷舌的松弛男声，适合口播和播客。'),
    Voice('qwen_seren', 'qwen', 'Seren', 'zh',
          'female', 'qwen_zh',
          '小婉 · 舒缓女声',
          '温和舒缓，适合助眠、情感和有声书。'),
    Voice('qwen_nini', 'qwen', 'Nini', 'zh',
          'female', 'qwen_zh',
          '邻家妹妹 · 甜糯女声',
          '甜糯亲切，适合陪伴和生活方式内容。'),
    Voice('qwen_mia', 'qwen', 'Mia', 'zh',
          'female', 'qwen_zh',
          '乖小妹 · 乖巧女声',
          '乖巧温柔如清泉，适合温情内容。'),
    Voice('qwen_chelsie', 'qwen', 'Chelsie', 'zh',
          'female', 'qwen_zh',
          '千雪 · 软萌女声',
          '二次元软萌，适合娱乐和角色化内容。'),
    Voice('qwen_momo', 'qwen', 'Momo', 'zh',
          'female', 'qwen_zh',
          '茉兔 · 俏皮女声',
          '俏皮撒娇，适合娱乐和轻松内容。'),
    Voice('qwen_vivian', 'qwen', 'Vivian', 'zh',
          'female', 'qwen_zh',
          '十三 · 可爱女声',
          '可爱、略带小暴躁，适合角色化短视频。'),
    Voice('qwen_bella', 'qwen', 'Bella', 'zh',
          'female', 'qwen_zh',
          '萌宝 · 可爱女声',
          '可爱年轻，适合活泼轻松内容。'),
    Voice('qwen_bunny', 'qwen', 'Bunny', 'zh',
          'female', 'qwen_zh',
          '萌小姬 · 甜萌女声',
          '极致可爱、年轻，适合娱乐内容。'),
    Voice('qwen_stella', 'qwen', 'Stella', 'zh',
          'female', 'qwen_zh',
          '少女阿月 · 少女声',
          '迷糊少女，需要时充满激情，适合角色和剧情。'),
    Voice('qwen_mochi', 'qwen', 'Mochi', 'zh',
          'male', 'qwen_zh',
          '沙小弥 · 童声男',
          '早慧又天真的童声，适合角色和故事。'),
    Voice('qwen_pip', 'qwen', 'Pip', 'zh',
          'male', 'qwen_zh',
          '顽屁小孩 · 顽皮童声',
          '顽皮又天真的孩子声，适合角色化内容。'),
    Voice('elevenlabs_rachel', 'elevenlabs', '21m00Tcm4TlvDq8ikWAM', 'en',
          'female', 'elevenlabs_en',
          'Rachel · American female',
          'Calm and warm — great for narration', True, False),
    Voice('elevenlabs_bella', 'elevenlabs', 'EXAVITQu4vr4xnSDxMaL', 'en',
          'female', 'elevenlabs_en',
          'Bella · American female',
          'Soft and friendly', True, False),
    Voice('elevenlabs_domi', 'elevenlabs', 'AZnzlk1XvdvUeBnXmlld', 'en',
          'female', 'elevenlabs_en',
          'Domi · American female',
          'Confident and strong', True, False),
    Voice('elevenlabs_elli', 'elevenlabs', 'MF3mGyEYCl7XYWbV9V6O', 'en',
          'female', 'elevenlabs_en',
          'Elli · American female',
          'Expressive and emotional', True, False),
    Voice('elevenlabs_adam', 'elevenlabs', 'pNInz6obpgDQGcFmaJgB', 'en',
          'male', 'elevenlabs_en',
          'Adam · American male',
          'Deep and steady', True, False),
    Voice('elevenlabs_antoni', 'elevenlabs', 'ErXwobaYiN019PkySvjV', 'en',
          'male', 'elevenlabs_en',
          'Antoni · American male',
          'Warm and well-rounded', True, False),
    Voice('elevenlabs_josh', 'elevenlabs', 'TxGEqnHWrfWFTfGW9XjX', 'en',
          'male', 'elevenlabs_en',
          'Josh · American male',
          'Deep and resonant', True, False),
    Voice('elevenlabs_arnold', 'elevenlabs', 'VR6AewLTigWG4xSOukaG', 'en',
          'male', 'elevenlabs_en',
          'Arnold · American male',
          'Crisp and clear', True, False),
    Voice('elevenlabs_sam', 'elevenlabs', 'yoZ06aMxZJJ28mfd3POQ', 'en',
          'male', 'elevenlabs_en',
          'Sam · American male',
          'Smooth and expressive', True, False),
    Voice('elevenlabs_alice', 'elevenlabs', 'Xb7hH8MSUJpSbSDYk0k2', 'en',
          'female', 'elevenlabs_en',
          'Alice · British female',
          'Confident and clear', True, False),
    Voice('elevenlabs_lily', 'elevenlabs', 'pFZP5JQG7iQjIQuC4Bku', 'en',
          'female', 'elevenlabs_en',
          'Lily · British female',
          'Warm and gentle', True, False),
    Voice('elevenlabs_charlotte', 'elevenlabs', 'XB0fDUnXU5powFXDhCwa', 'en',
          'female', 'elevenlabs_en',
          'Charlotte · British female',
          'Elegant and refined', True, False),
    Voice('elevenlabs_dorothy', 'elevenlabs', 'ThT5KcBeYPX3keUQqHPh', 'en',
          'female', 'elevenlabs_en',
          'Dorothy · British female',
          'Pleasant and friendly', True, False),
    Voice('elevenlabs_george', 'elevenlabs', 'JBFqnCBsd6RMkjVDRZzb', 'en',
          'male', 'elevenlabs_en',
          'George · British male',
          'Warm and mature', True, False),
    Voice('elevenlabs_daniel', 'elevenlabs', 'onwK4e9ZLuTAKqWW03F9', 'en',
          'male', 'elevenlabs_en',
          'Daniel · British male',
          'Authoritative, news-presenter style', True, False),
    Voice('azure_yunyang', 'azure', 'zh-CN-YunyangNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaoxiao', 'azure', 'zh-CN-XiaoxiaoNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_yunxi', 'azure', 'zh-CN-YunxiNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_yunjian', 'azure', 'zh-CN-YunjianNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaoyi', 'azure', 'zh-CN-XiaoyiNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaochen', 'azure', 'zh-CN-XiaochenNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaohan', 'azure', 'zh-CN-XiaohanNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaomeng', 'azure', 'zh-CN-XiaomengNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaomo', 'azure', 'zh-CN-XiaomoNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaoqiu', 'azure', 'zh-CN-XiaoqiuNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaorou', 'azure', 'zh-CN-XiaorouNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaorui', 'azure', 'zh-CN-XiaoruiNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaoshuang', 'azure', 'zh-CN-XiaoshuangNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaoyan', 'azure', 'zh-CN-XiaoyanNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_xiaoyou', 'azure', 'zh-CN-XiaoyouNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_yunfeng', 'azure', 'zh-CN-YunfengNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_yunhao', 'azure', 'zh-CN-YunhaoNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_yunjie', 'azure', 'zh-CN-YunjieNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_yunxia', 'azure', 'zh-CN-YunxiaNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_yunye', 'azure', 'zh-CN-YunyeNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_yunze', 'azure', 'zh-CN-YunzeNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_tw_hsiaochen', 'azure', 'zh-TW-HsiaoChenNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_tw_hsiaoyu', 'azure', 'zh-TW-HsiaoYuNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_tw_yunjhe', 'azure', 'zh-TW-YunJheNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_hk_hiugaai', 'azure', 'zh-HK-HiuGaaiNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_hk_hiumaan', 'azure', 'zh-HK-HiuMaanNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_hk_wanlung', 'azure', 'zh-HK-WanLungNeural', 'zh',
          '', None,
          '',
          '', False, True),
    Voice('azure_ava', 'azure', 'en-US-AvaNeural', 'en',
          '', None,
          '',
          '', False, True),
    Voice('azure_andrew', 'azure', 'en-US-AndrewNeural', 'en',
          '', None,
          '',
          '', False, True),
    Voice('azure_aria', 'azure', 'en-US-AriaNeural', 'en',
          '', None,
          '',
          '', False, True),
    Voice('azure_brian', 'azure', 'en-US-BrianNeural', 'en',
          '', None,
          '',
          '', False, True),
    Voice('azure_guy', 'azure', 'en-US-GuyNeural', 'en',
          '', None,
          '',
          '', False, True),
    Voice('azure_jenny', 'azure', 'en-US-JennyNeural', 'en',
          '', None,
          '',
          '', False, True),
    Voice('azure_davis', 'azure', 'en-US-DavisNeural', 'en',
          '', None,
          '',
          '', False, True),
    Voice('azure_steffan', 'azure', 'en-US-SteffanNeural', 'en',
          '', None,
          '',
          '', False, True),
)

BY_KEY: dict[str, Voice] = {v.key: v for v in VOICES}


def _native_map(vendor: str) -> dict:
    return {v.key: v.native for v in VOICES if v.vendor == vendor}


def catalog_for(name: str) -> list[dict]:
    """某个客户可见目录的展示数据。**顺序 = 前端下拉框的顺序。**"""

    return [
        {"provider_key": v.key, "display_name": v.display_name,
         "description": v.description, "gender": v.gender}
        for v in VOICES if v.catalog == name
    ]


#
#    本仓只发布尔标志,不决定金额。
_PREMIUM_EN_CATALOGS = frozenset({"premium_en", "elevenlabs_en"})


def is_premium_english(key: str) -> bool:
    """

       一处定义才不会再分叉。
    """

    v = BY_KEY.get(str(key or ""))
    return bool(v and v.premium_english)


def voice_language(key: str) -> str:
    """这个音色说什么语言。认不出来返回 `""` —— **绝不瞎猜**。

    ⚠️ 返回 `""` 时守卫应当**放行**，不要当成「不会说英文」去替换 ——
    """

    v = BY_KEY.get(str(key or ""))
    return v.language if v else ""


def is_customer_visible(key: str) -> bool:
    """客户能不能在界面上选到它。"""

    v = BY_KEY.get(str(key or ""))
    return bool(v and v.catalog)


def vendor_of(key: str) -> str:
    v = BY_KEY.get(str(key or ""))
    return v.vendor if v else ""


#
#
# ## 为什么**不删代码**
#
# 删掉运行时映射 = 它们重跑时找不到音色 → 又变成「客户选了 A、出来是 B」。
# 所以：**从客户目录下架（`catalog=None`），但保留合成能力。**
#
# ## 现在它们在哪
#
#   本文件            `legacy=True` 的记录（Azure 35 个 + ElevenLabs 28 个）
#   pipeline_service  `MEDIA_BUDDY_LONG_VIDEO_AZURE_ONLY` 应急开关
#
# ## 🚨 日常开发不要碰它们
#
# 真要动停用的，先想清楚在途项目会不会被打断。
#


def active_voices() -> tuple[Voice, ...]:
    """在用的音色（排除停用供应商）。**日常开发只该关心这些。**"""

    return tuple(v for v in VOICES if not v.legacy)


def legacy_voices() -> tuple[Voice, ...]:
    """停用中、仅为在途/历史项目保留的音色。见上面的「停用清单」。"""

    return tuple(v for v in VOICES if v.legacy)


# ════════════════════════════════════════════════════════════
# 停用音色 → 在用音色的**自动接管**
# ════════════════════════════════════════════════════════════
#
#
# 客户的配音设定是**存下来的**。他几个月前选了 `elevenlabs_adam`,
# 这个值就一直躺在 `projects.tts_provider` 里。后来我们停用了 ElevenLabs
# 和 Azure —— 但**存量设定不会自己变**。
#
# 于是出片走进一条死链(`tts_service.synthesize` 里):
#
#     客户存的是 ElevenLabs 音色
#        → `_wants_elevenlabs = True`
#        → **跳过千问、跳过谷歌**            ← 关键:它主动绕开了还活着的那两条
#        → ElevenLabs(密钥已停) 失败
#        → 整条出片挂掉
#
#
#
#
#    反过来 = 我们白送。两个都不行,所以 `premium_english` 参与匹配。
#
# ⚠️ **不改客户存的值**,只在合成那一刻接管。改存量值是另一回事(要单独问客户),
#    而且万一哪天供应商恢复了,原值还在。


def _stable_pick(key: str, candidates: "list[Voice]") -> str:
    """从候选里挑一个 —— **同一个 key 永远挑到同一个**。

    ⚠️ 不用 `hash()`:Python 的字符串 hash 每个进程都不一样(PYTHONHASHSEED),
       那会让同一个客户**每次出片换一个声音**。用内容哈希才稳。
    """

    import hashlib

    ordered = sorted(candidates, key=lambda v: v.key)
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
    return ordered[h % len(ordered)].key


def replacement_for(key: str) -> str | None:
    """这个停用音色该由谁接管。在用 / 不认识 → 返回 `None`(不接管)。

    ⚠️ **fail-safe 到「不接管」**:找不到同档同性别的替代就返回 None,
       让上层按原来的路走 —— 宁可维持现状,也不要静默换成一个
    """

    src = BY_KEY.get(str(key or ""))
    if src is None or not src.legacy:
        return None                      # 在用的音色不接管;不认识的也不碰

    #
    #
    # ⚠️ 语言只能「尽量」,因为目录里**英语全是 premium**(谷歌那 15 个)。
    #    老的 Azure 普通英语音色(azure_aria 等)在同档里找不到英语替身,
    #    只能落到千问 —— **而这正是产品现在「普通英语」本来走的路**
    live = [v for v in active_voices()
            if v.premium_english == src.premium_english]
    if not live:
        return None
    if src.language:
        same_lang = [v for v in live if v.language == src.language]
        if same_lang:
            live = same_lang
    if src.gender:
        same_sex = [v for v in live if v.gender == src.gender]
        if same_sex:
            live = same_sex
    return _stable_pick(src.key, live)


#
# `elevenlabs_*` 在 tts_service 里会主动跳过千问，一超时就掉进同样坏掉的 Azure，
#
# ⚠️ 两条规矩：
#   1. **key 不改、数据库不动** —— 顶替只发生在合成那一刻，随时能关。
#   2. **premium_english 必须一致** —— 高级英语顶替成高级英语，
LEGACY_SUBSTITUTE: dict[str, str] = {
    # 中文女声 → 千问默认女声
    'elevenlabs_amy': 'qwen_cherry',
    # KNOWN_BROKEN 那三个：本来就在播别人的声音，顶替成中文默认女声
    'elevenlabs_yichen': 'qwen_cherry',
    'elevenlabs_mandarin_narrator': 'qwen_cherry',
    'elevenlabs_mandarin_storyteller': 'qwen_cherry',
}


def substitute_legacy(key: str) -> str:
    """老音色 → 现役音色。不是老音色 / 顶不了 就原样返回。

    顺序：显式对照表 → 按(语言,性别,是否高级英语)兜底 → 原样返回。
    兜底这一层是给「以后又冒出一个没列进表里的老音色」用的，
    不然加一个音色就得记得回来改这里，迟早漏。
    """
    key = str(key or '').strip()
    if not key:
        return key
    hit = LEGACY_SUBSTITUTE.get(key)
    if hit and hit in BY_KEY and BY_KEY[hit].catalog:
        return hit
    # ⚠️ **azure_* 一律不顶替。** 它们在 tts_service 里本来就走千问,而且
    #    `_AZURE_TO_QWEN_VOICE` 里有正确的男女对应(云扬=男→Ethan)。在这里按
    #    (语言,性别)兜底会出事:azure 音色的 gender 字段是空的,兜底会挑到第一个
    #    这个错是打补丁时自查抓到的,不是猜的。
    if key.startswith('azure_'):
        return key
    v = BY_KEY.get(key)
    if v is None:
        # 注册表里都没有(如 KNOWN_BROKEN)且没列进对照表 → 中文默认女声
        if key.startswith('elevenlabs_'):
            return 'qwen_cherry'
        return key
    if not v.legacy:
        return key
    # 语言字段是空的(几个老的自定义音色就是)→ 按语言匹配永远匹配不上,
    # 会静静地留在老引擎上。这类一律给中文默认女声(本产品主力市场)。
    # 这个漏网是 test_每个停用音色都顶得动 当场抓到的,不是想出来的。
    if not v.language:
        return 'qwen_cherry'
    for want_gender in (v.gender, ''):
        for cand in VOICES:
            if not cand.catalog or cand.legacy:
                continue
            if cand.language != v.language:
                continue
            if cand.premium_english != v.premium_english:
                continue
            if want_gender and cand.gender != want_gender:
                continue
            return cand.key
    return key
