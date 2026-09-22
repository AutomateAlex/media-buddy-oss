import json
import logging
import os
import random
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

from backend.lib.cultural_filter import inject_cultural_anchor


_VALID_SHOT_TYPES = {
    "static", "atmosphere", "product", "interior", "establishing",
    "human_action", "performance", "interaction",
    "complex_motion", "vfx",
}

_ATOM_BUCKETS = (
    "O_OBJECT", "A_ACTION", "N_NUMBER", "M_METAPHOR",
    "C_CONCEPT", "S_SCALE", "E_EMOTION", "P_PARALLEL",
)
# excluded: L4 is a binary state (uses style_tokens or not).
_VALID_CASCADE_DEPTHS = {2, 3, 5}


# Transition / CTA chunks reuse the previous chunk's clip (physical
# short-circuit at orchestrator entry, 0 LLM calls). See spec section 1.1.A.
#
# Conservative keyword list — we deliberately do NOT include 因果连词
# ("因此/所以/于是") because those typically introduce conclusion sentences
# with their own visual subject ("因此分形是宇宙的语言"). False-positive on
# those would replace a real visual with the previous shot. Add new triggers
# via unit test only.
_TRANSITION_BODY_HINTS = (
    "反映出", "也就是说", "总之", "你瞧", "你看",
    "其实", "说白了", "换句话说", "这就是",
)
_TRANSITION_HEAD_HINTS = (
    "这", "它", "那", "此",   # must pair with —/… ending
)
_TRANSITION_TAIL_MARKERS = ("——", "—", "…", "...", "......")

_CTA_SENTENCE_HINTS = (
    "评论", "点赞", "关注", "订阅", "投票",
    "告诉我", "告诉你", "你怎么看",
    "you think", "comment below", "let me know",
    "subscribe", "like this video",
)






# Culture → first-tag anchor for search_keywords lists. Mirrors
# inject_cultural_anchor() for natural-language queries: every non-Universal
# script gets its culture prepended as the first stock-search tag so the
# library index doesn't return Japanese ramen for a French dining script.
_CULTURE_TAG_ANCHORS = {
    "France": "french",
    "China": "chinese",
    "Japan": "japanese",
    "Korea": "korean",
    "Italy": "italian",
    "Spain": "spanish",
    "USA": "american",
    "UK": "british",
    "Generic-Western": "european",
}








def _flatten_atoms(atoms: dict[str, list[str]]) -> list[str]:
    """All atom tokens across all 8 buckets, deduped, lowercase."""
    seen: set[str] = set()
    flat: list[str] = []
    for bucket in _ATOM_BUCKETS:
        for tok in atoms.get(bucket, []):
            if tok not in seen:
                seen.add(tok)
                flat.append(tok)
    return flat














def _normalize_music_segments(segments: list, total_chunks: int) -> list[dict]:
    """Validate + repair LLM-returned music segments. Ensures coverage,
    contiguity, and minimum size. Drops invalid entries gracefully."""
    cleaned: list[dict] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            start = int(seg.get("start_chunk_index", -1))
            end = int(seg.get("end_chunk_index", -1))
        except (TypeError, ValueError):
            continue
        if start < 0 or end < start or end >= total_chunks:
            continue
        cleaned.append({
            "start_chunk_index": start,
            "end_chunk_index": end,
            "narrative_role": str(seg.get("narrative_role", "intro")),
            "mood": str(seg.get("mood", "calm")),
            "energy": str(seg.get("energy", "medium")),
            "search_keywords": str(seg.get("search_keywords", "ambient music")).strip()
                or "ambient music",
        })
    if not cleaned:
        return [{
            "start_chunk_index": 0,
            "end_chunk_index": total_chunks - 1,
            "narrative_role": "intro",
            "mood": "calm",
            "energy": "medium",
            "search_keywords": "ambient cinematic background music",
        }]
    # Ensure coverage: first must start at 0, last must end at total-1
    cleaned.sort(key=lambda s: s["start_chunk_index"])
    cleaned[0]["start_chunk_index"] = 0
    cleaned[-1]["end_chunk_index"] = total_chunks - 1
    # Stitch contiguity (each seg starts where prev ended +1)
    for i in range(1, len(cleaned)):
        cleaned[i]["start_chunk_index"] = cleaned[i - 1]["end_chunk_index"] + 1
    # Drop trailing segments that overran
    cleaned = [s for s in cleaned if s["start_chunk_index"] <= s["end_chunk_index"]]
    return cleaned


def _coerce_query_to_string(q) -> str:
    """LLM sometimes returns structured dicts ({subject, action, setting}) when
    we wanted plain strings. Flatten them into a search-friendly phrase."""
    if isinstance(q, str):
        return q.strip()
    if isinstance(q, dict):
        # Pull out the conceptually ordered fields if present
        ordered_keys = ("subject", "action", "setting", "mood", "lighting", "angle")
        parts = []
        for k in ordered_keys:
            v = q.get(k)
            if v:
                parts.append(str(v).strip())
        # Anything else not in our ordered list, append after
        for k, v in q.items():
            if k not in ordered_keys and v:
                parts.append(str(v).strip())
        return " ".join(parts)
    return str(q).strip()


# Provider auto-detection priority — prefer aggregators (one key, many models)
PROVIDER_PRIORITY = ["openrouter"]
PROVIDER_KEY_VAR = {
    "openrouter": "OPENROUTER_API_KEY",
}


def auto_detect_provider() -> str:
    """Every LLM call in this build goes through OpenRouter (one key, every model)."""
    return "openrouter"


class LLMClient:
    def __init__(
        self,
        provider: str = "auto",
        model: Optional[str] = None,
        series: Optional[object] = None,
    ):
        """`series` is a `models.series.Series` ORM instance (or any duck-typed
        equivalent with director_prompt / industry_tag / forbidden_topics /
        learned_patterns attributes). When set, every _call prepends a Series
        prefix to the system prompt — this is the L1 hard isolation point in
        """
        self.provider = auto_detect_provider() if provider == "auto" else provider
        self.model = model
        self.series = series
        self.project_id: Optional[str] = None

    def set_project_id(self, project_id: Optional[str]) -> None:
        self.project_id = project_id

    def _series_prefix(self) -> str:
        """Build the per-Series system-prompt prefix. Returns "" when no Series
        is bound (so behavior matches pre-Series LLMClient exactly)."""
        s = self.series
        if s is None:
            return ""
        parts: list[str] = []
        director = getattr(s, "director_prompt", None)
        if director:
            parts.append(str(director).strip())
        industry = getattr(s, "industry_tag", None)
        if industry:
            parts.append(f"Industry: {industry}")
        forbidden = list(getattr(s, "forbidden_topics", None) or [])
        if forbidden:
            parts.append(
                "Forbidden topics (NEVER suggest content that would trigger "
                "these flags): " + ", ".join(forbidden)
            )
        patterns = getattr(s, "learned_patterns", None) or {}
        # Memory inject: just the most actionable subset, kept short to avoid
        # blowing the system prompt. Dict ordering is preserved (Python 3.7+).
        memory_lines: list[str] = []
        for key in ("preferred_hooks", "preferred_pacing", "good_sources", "bad_sources"):
            val = patterns.get(key)
            if val:
                memory_lines.append(f"- {key}: {val}")
        if memory_lines:
            parts.append(
                "Series memory (accumulated client preferences from prior "
                "approved videos):\n" + "\n".join(memory_lines)
            )
        if not parts:
            return ""
        return "\n\n".join(parts) + "\n\n---\n\n"

    def _call(
        self,
        system: str,
        user_prompt: str,
        *,
        purpose: Optional[str] = None,
    ) -> str:
        """`purpose` routes to a per-purpose env-configured model (see
        _purpose_model). **实际用哪个模型以 env 为准,别在代码/注释里认定某个模型**
        —— 本部署 verify 档指向的就不是内置默认值,且 Anthropic 根本没配 key。
        改模型 = 改 env,不动代码(见 _PURPOSE_ENV_VARS)。

        so per-chunk × per-purpose × per-model spend is observable.
        Counter does nothing when no Orchestrator is active.
        """
        full_system = self._series_prefix() + system
        chain = self._provider_chain(purpose)
        last_err: Optional[Exception] = None
        for prov in chain:
            # 计数:记本次实际尝试的 provider/model(导入放方法内避免循环依赖)。
            try:
                from backend.services.llm_call_counter import bump_active
                bump_active(purpose or "unknown",
                            self._purpose_model(purpose, prov) or self.model or "_unknown_")
            except Exception:
                pass
            try:
                return self._dispatch(prov, full_system, user_prompt, purpose)
            except Exception as e:
                last_err = e
                if prov != chain[-1]:
                    logger.warning(
                        "LLM provider '%s' failed for purpose=%s (%s); 回落下一个",
                        prov, purpose, repr(e)[:160],
                    )
                continue
        raise last_err or RuntimeError("all LLM providers failed")

    def _provider_chain(self, purpose: Optional[str]) -> list[str]:
        """Try order for a purpose: OpenRouter only."""
        return [self.provider]

    def _dispatch(self, provider: str, full_system: str, user_prompt: str,
                  purpose: Optional[str]) -> str:
        if provider == "openrouter":
            return self._call_openrouter(full_system, user_prompt, purpose=purpose)
        raise ValueError(f"Unknown provider: {provider}")




    # 活体版本在下方(带 strict / timeout / 真实案例链)。

    _RESEARCH_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
        "wisdom": (
            "名言", "语录", "金句", "纳瓦尔", "naval", "ravikant", "巴菲特",
            "buffett", "warren buffett", "芒格", "munger", "查理芒格",
            "名人名言", "almanack", "shareholder letter",
        ),
        "travel": (
            "旅游", "旅行", "自由行", "攻略", "关西", "京都", "大阪", "奈良",
            # ⚠️ 这里原来有裸的 "日本" —— 任何提到日本的题材(哪怕是财经)都会被
            #    仍会被 旅游/旅行/攻略/关西/京都/大阪/奈良/签证/景点 捞住。
            "票价", "营业时间", "交通", "jr", "酒店", "签证", "景点",
        ),
        "finance": (
            "投资", "股票", "美股", "财报", "13f", "sec", "基金", "桥水",
            "英伟达", "nvidia", "营收", "现金流", "估值", "巴菲特",
            # 「日本央行退出负利率」落不到 finance —— 它此前是靠 travel 里那个
            # 裸 "日本" 才**意外**拿到强制联网核实的(travel 在 STRICT_DOMAINS 里)。
            # 删掉那个词后必须由这里接住,否则财经题材反而更容易编。
            "央行", "利率", "加息", "降息", "汇率", "通胀", "通缩",
            "货币政策", "房贷", "存款", "理财", "养老金", "gdp", "经济",
        ),
        "crime": ("犯罪", "悬案", "凶案", "失踪", "案件", "嫌疑", "fbi", "court"),
        "animal": ("动物", "物种", "毒性", "捕食", "蜜獾", "章鱼", "黑曼巴", "生物"),
        "science": (
            "科学", "科普", "论文", "研究", "医学", "物理", "宇宙",
            # 分不对题材,「按题材选搜索源」就无从谈起。
            "化学", "生理", "细胞", "基因", "进化", "实验", "大气压", "压强",
            "深海", "黑洞", "引力", "量子", "代谢", "免疫", "神经", "病毒",
            "疫苗", "光速", "元素", "分子", "原子", "地质", "气候",
        ),
        "tech": ("ai", "人工智能", "科技", "芯片", "模型", "benchmark", "半导体"),
        "history": (
            "历史", "国家", "战争", "帝国", "王朝", "冷战", "殖民", "世纪", "古代", "近代",
            "王国", "部落", "起义", "革命", "奴隶", "文明", "朝代", "传记", "生平", "叙事",
        ),
    }

    def research_domain(self, topic: str, context: str = "") -> str:
        text = f"{topic}\n{context}".lower()
        for domain, keywords in self._RESEARCH_DOMAIN_KEYWORDS.items():
            if any(k.lower() in text for k in keywords):
                return domain
        return "general"

    @staticmethod
    def _env_csv(name: str, default: str) -> set[str]:
        raw = os.environ.get(name, default)
        return {p.strip().lower() for p in str(raw).split(",") if p.strip()}

    # 人物/真实事件/公司类题材的信号:这类爆款的留存全靠真实案例,必须强制 grounding
    # (否则要么编案例=串题幻觉,要么空泛)。命中任一即强制联网取真实资料。
    _PERSON_EVENT_GROUNDING_HINTS = (
        "人物", "传记", "创始人", "ceo", "老板", "富豪", "亿万", "白手起家", "发家",
        "公司", "企业", "品牌", "案例", "真实", "故事", "历史", "事件", "战争",
        "王朝", "帝国", "首富", "巨头", "崛起", "破产", "丑闻", "传奇", "生平",
    )

    def grounding_required(self, topic: str, context: str = "") -> bool:
        strict_domains = self._env_csv(
            "MEDIA_BUDDY_GROUNDING_STRICT_DOMAINS",
            "finance,travel,wisdom,crime",
        )
        if self.research_domain(topic, context) in strict_domains:
            return True
        # 人物/真实事件/公司类:即便领域判成 general，也强制 grounding —— 真实案例
        # 只能来自检索,不能编。可用 MEDIA_BUDDY_GROUNDING_PERSON_EVENT=0 关。
        if os.environ.get("MEDIA_BUDDY_GROUNDING_PERSON_EVENT", "1").strip().lower() not in (
            "0", "false", "no", "",
        ):
            t = f"{topic}\n{context}".lower()
            if any(h.lower() in t for h in self._PERSON_EVENT_GROUNDING_HINTS):
                return True
        return False

    @staticmethod
    def _short_grounding_enabled() -> bool:
        return os.environ.get("MEDIA_BUDDY_SHORT_GROUNDING", "1").strip().lower() not in (
            "0", "false", "no", "off", "",
        )

    @staticmethod
    def _short_grounding_max_tokens() -> int:
        # qwen-plus + enable_search(非推理)无 reasoning 开销,精简包 3000 够用。
        try:
            v = int(os.environ.get("MEDIA_BUDDY_SHORT_GROUNDING_MAX_TOKENS", "3000") or "3000")
        except Exception:
            v = 3000
        return max(1200, min(8000, v))

    @staticmethod
    def _short_grounding_timeout() -> int:
        try:
            v = int(os.environ.get("MEDIA_BUDDY_SHORT_GROUNDING_TIMEOUT", "90") or "90")
        except Exception:
            v = 90
        return max(30, min(420, v))

    # 事实解说信号:科普/冷知识/动物题材常不含领域关键词(如"沙鼠尾巴为什么能当温度计"),
    # 但明显是会陈述可核实事实的解说。命中即判定值得联网核实;纯个人 vlog/心情不含这些。
    _FACTUAL_EXPLAINER_HINTS = (
        "为什么", "为何", "原理", "真相", "揭秘", "冷知识", "鲜为人知", "竟然", "其实",
        "原来", "科学", "研究", "机制", "背后", "秘密", "到底", "真的吗", "惊人",
    )

    def _short_should_ground(self, topic: str, context: str = "") -> bool:
        """短视频是否值得联网搜事实包:任何被识别出的事实领域(历史/科普/动物/科技/
        财经/旅游/智慧/犯罪)、人物/真实事件、或带"事实解说信号/数字"的题材都搜;
        纯闲聊(无任何信号)跳过。比 grounding_required 更宽(短视频要的是事实准,不是
        strict 硬闸),且不改长视频语义。"""
        if self.research_domain(topic, context) != "general":
            return True
        if self.grounding_required(topic, context):
            return True
        t = str(topic or "").lower()
        if any(ch.isdigit() for ch in t) or any(u in t for u in ("°c", "℃", "%", "倍")):
            return True
        return any(h in t for h in self._FACTUAL_EXPLAINER_HINTS)

    @staticmethod
    def grounding_pack_looks_usable(pack: str, *, min_chars: int = 900) -> bool:
        text = str(pack or "").strip()
        if len(text) < min_chars:
            return False
        lowered = text.lower()
        return ("http://" in lowered) or ("https://" in lowered) or ("url" in lowered)

    @staticmethod
    def _research_domain_instruction(domain: str) -> str:
        instructions = {
            "wisdom": (
                "名人名言/语录主题：优先找原始出处，例如官方访谈、演讲全文、书籍、"
                "巴菲特致股东信、Berkshire Hathaway材料、Naval Ravikant公开采访或The Almanack。"
                "输出必须分成：A.可核验原话；B.广泛归因但未找到原始出处；C.只能转述的观点。"
                "没有原始出处的句子不要写成引号原话。"
            ),
            "travel": (
                "旅行主题：价格、营业时间、交通政策、税费、签证和限流信息优先使用官方网站、"
                "交通运营商、城市/景区/旅游局页面。每个具体数字都要带日期或适用期间；"
                "无法核验的新政策必须标为不建议写死。"
            ),
            "finance": (
                "金融/商业主题：财报、SEC filings、13F、公司IR、官方新闻稿和权威财经数据优先。"
                "每个数字必须说明口径、期间和来源；区分营收、净利润、自由现金流、持仓、市值。"
                "不要输出买卖建议。"
            ),
            "crime": (
                "犯罪/悬案主题：优先官方档案、法院文件、FBI/警方资料和可信新闻。"
                "事实、争议和推测必须分开；不要断定动机或罪责。"
            ),
            "animal": (
                "动物/科普主题：优先IUCN、动物园/博物馆、大学、论文或权威百科。"
                "毒性、速度、寿命、致死率和攻击行为都要保守，不要夸大。"
            ),
            "science": "科学主题：优先论文、大学、NASA/NIH/WHO等机构；区分已证实、主流理论和假说。",
            "tech": "科技主题：优先公司官方文档、论文、基准测试原文；不要把单项benchmark泛化成全面领先。",
            "history": "历史主题：优先博物馆、档案、学术机构和权威史料；具体年份、人数、因果链要保守。",
        }
        return instructions.get(domain, "通用主题：优先权威、可追溯、日期明确的来源。")

    @staticmethod
    def _strict_grounding_script_rules(domain: str) -> str:
        base = (
            "\n【硬事实写稿红线】\n"
            "1. 正文禁止新增资料包外的具体年份、金额、百分比、倍数、票价、税额、营业时间、机构持仓、财报数字、人物行为细节。\n"
            "2. 资料包没有明确给出的数字，必须改成模糊表达，例如“明显增加”“部分路段”“较高成本”，不要自行推算。\n"
            "3. 任何引号里的名言，尤其英文原句，必须出现在资料包的“可核验原话/A类原话”里；否则只能转述，不能加引号。\n"
            "4. 不要为了故事感编人物亲历、研究动作、会议细节、专利清单、供应链细节或现场画面。\n"
            "5. 如果资料包只给趋势，不给数字，正文只能写趋势；如果资料包只给争议，正文必须写成争议。\n"
        )
        domain_rules = {
            "finance": (
                "金融专用：财报、CapEx、持仓、市值、营收、利润、现金流、机构名和竞品份额必须逐项来自资料包；"
                "禁止“唯一、所有、确定、必然、抄底、买入”等绝对化或投资建议表达。\n"
            ),
            "travel": (
                "旅行专用：票价、停车费、住宿税、交通卡覆盖范围、开放时间和政策变化必须来自官方/运营方来源；"
                "没有字段就写“出发前查官方页面”，不要补具体日元金额和线路区间。\n"
            ),
            "wisdom": (
                "名言专用：未核验归因只能写“常被归因于/更稳妥地说”，不能写成某人亲口说；"
                "涉及投资人物时，不要补资料包外的投资案例、年份、研究行为和动机。\n"
            ),
            "history": (
                "历史专用：真实历史人物必须放在其真实在世年代，绝不能把人物挪到错误的年份/朝代/事件里"
                "(例如把生于18世纪的人写进19世纪末)；人物生卒年、事件年份、因果归属、参与者，"
                "资料包没有支撑就不要写死，改成“据载/某位…/约…前后”或直接省略；"
                "伤亡、人数、金额等不确定数字一律模糊化，不要为戏剧感编造具体场景与对话。\n"
            ),
        }
        return base + domain_rules.get(domain, "")

    # (没网址 = 没法验它有没有编)。Sonar 13-19s,稳定给 7-10 个可 curl 验证的真实来源。
    # 且两家并行跑 → 墙上时间 = 慢的那家,和现在一样。
    #
    # ⚠️ Sonar 的来源网址在 `message.annotations` 里,正文只有 [1][2] 脚注 ——
    _SONAR_SYSTEM = (
        "你是中文视频文案的事实研究员。必须联网搜索核实,只输出可用于写稿的事实资料包,"
        "不要编造来源。输出中文资料包,严格按下面 6 节:\n"
        "1. 【核心事实】8-12 条可确定事实。★每一条都必须含至少一个**具体数字或年份**"
        "(金额、人数、面积、百分比、日期),没有具体数字的事实不要写。\n"
        "2. 【来源表】标题、完整 URL、发布日期、支持哪条事实。\n"
        "3. 【反差点】适合爆款叙事的反差、冲突或误解点。\n"
        "4. 【高风险说法】哪些说法不能写死、哪些数字只能模糊化。\n"
        "5. 【事实模块顺序】适合长视频的讲述顺序。\n"
        "6. ★【真实案例链(最重要)】3-4 个【真实、可溯源】案例,递进排列。每个必须写清:"
        "①人物/公司/地点/年份;②**具体数字**(钱、人数、天数、面积);③它证明了什么;"
        "④段尾可抛的一个新问题。搜不到就少给,绝不编造。\n"
        "没有明确来源的说法必须标注「不建议写死」。"
    )



    #
    #                     → Sonar 快3倍、可验证、造假率是千问的 1/2.5
    #
    #   科普类(深海鱼/黑洞/代谢)  Sonar        数字合计 77  真网址 3
    #                            DeepSeek联网 数字合计117  真网址10  ← 赢
    #                            千问         数字合计158  真网址 0  ← 验不了
    #                     → 科普走 DeepSeek:密度接近千问,来源还能查
    #
    # ⚠️ DeepSeek 有长尾:深海鱼那次 **519 秒**(另两次 35 秒)。给它单独超时,
    #    超了就落 Sonar —— 出片不能卡在搜索上。
    #
    # 第一版把科普类分给 DeepSeek,依据是「数字多、网址多」——**这个依据错了**。
    # 补做了科普类的逐条联网核实(每家 16~18 条):
    #
    #
    # **数字多不等于好**:多出来的一半是编的,反而更危险 —— 客户拿到的是
    # 看着扎实、其实是假的稿子。少而真 > 多而假。
    #
    # 要恢复分流,往这里加一条即可(env 也能按题材覆盖)。
    _SEARCH_DOMAIN_MODELS: dict[str, str] = {}

    @classmethod
    def _search_model_for_domain(cls, domain: str) -> str:
        """这个题材该用哪个搜索模型。env 可整体覆盖,也可按题材覆盖。"""
        per = os.environ.get("MEDIA_BUDDY_SEARCH_MODEL_%s" % str(domain or "").upper(), "").strip()
        if per:
            return per
        forced = os.environ.get("MEDIA_BUDDY_SONAR_MODEL", "").strip()
        if forced:
            return forced        # 显式指定就一律用它(便于 A/B)
        return cls._SEARCH_DOMAIN_MODELS.get(str(domain or ""), "perplexity/sonar")

    def _sonar_research_pack(
        self,
        topic: str,
        *,
        context: str = "",
        domain: str = "",
        max_output_tokens: int = 3500,
        timeout: int = 120,
    ) -> str:
        """Perplexity Sonar 出联网资料包。worker 直连 OpenRouter(key 在 .env)。

        失败一律抛异常,交给上层处理 —— 绝不因为搜索挂了就拖垮出片。
        """
        import httpx

        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError("sonar_no_openrouter_key")
        model = self._search_model_for_domain(domain)
        #
        # ⚠️ 第一版只写了 `timeout=90` 传给 httpx —— **没用**。httpx 的 timeout 是
        #    **每次读取**的超时,不是总时长:上游只要一直慢慢吐字节,连接就不算超时,
        #
        # 所以改成:把请求丢进线程,用 future.result(timeout=) 卡总时长。
        # 被放弃的那个线程会自己跑完退出(每条片子只一次,不会堆积)。
        wall_deadline = None
        if ":online" in model:
            wall_deadline = int(
                os.environ.get("MEDIA_BUDDY_SEARCH_WALL_TIMEOUT", "90").strip() or 90
            )
        parts = ["主题:%s" % topic, ""]
        ctx = str(context or "").strip()[:5000]
        if ctx:
            parts += ["频道/模板上下文:", ctx, ""]
        if domain:
            parts += ["领域规则:%s" % self._research_domain_instruction(domain), ""]
        parts.append("请联网核实,输出中文事实资料包。")
        user = "\n".join(parts)

        def _do_post():
            return httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": "Bearer %s" % key,
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": self._SONAR_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": max_output_tokens,
                    "usage": {"include": True},
                },
                timeout=timeout,
            )

        if wall_deadline:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TO
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                r = pool.submit(_do_post).result(timeout=wall_deadline)
            except _TO:
                raise RuntimeError(
                    "search_wall_timeout: %s 超过 %d 秒未返回" % (model, wall_deadline)
                )
            finally:
                # 不等被放弃的线程(它会自己跑完退出),否则 shutdown 会把超时又等回来。
                pool.shutdown(wait=False)
        else:
            r = _do_post()
        r.raise_for_status()
        data = r.json()
        try:
            cost = (data.get("usage") or {}).get("cost")
            if cost:
                from backend.services.llm_call_counter import add_active_openrouter_cost
                add_active_openrouter_cost("openrouter_direct", model, float(cost))
        except Exception:
            pass

        msg = (data.get("choices") or [{}])[0].get("message") or {}
        pack = str(msg.get("content") or "").strip()

        # ⚠️ 关键:把 annotations 里的真实网址并回正文,否则下游「来源表 / 事实规则」
        #    那套框架一个 URL 都拿不到。
        seen: list[str] = []
        rows: list[str] = []
        for ann in (msg.get("annotations") or []):
            if not isinstance(ann, dict):
                continue
            cite = ann.get("url_citation")
            cite = cite if isinstance(cite, dict) else {}
            url = str(cite.get("url") or ann.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.append(url)
            rows.append("[%d] %s %s" % (len(seen), str(cite.get("title") or "-"), url))
        if pack and rows:
            pack += "\n\n【来源清单】\n" + "\n".join(rows)
        return pack


    # 千问造假率是 Sonar 的 2.5 倍,还会**编出处**(机构真、文件名和日期是编的)。
    #
    _VERIFY_SYSTEM = (
        "你是严格的中文事实核查员,具备联网搜索。给你一份写稿用的事实资料包,"
        "逐条核实其中**带具体数字/年份/金额**的陈述,以及它标注的出处文件是否真实存在。"
        "只输出 JSON,不要任何解释:"
        "{\"false\":[\"被证伪的原句片段\",…],\"unverified\":[\"查不到支撑的原句片段\",…]}"
        "。规则:找到来源明确否定、或它标注的文件根本不存在 → 放进 false;"
        "既搜不到支持也搜不到否定 → 放进 unverified;能确认属实的**不要**列出来。"
        "片段用原文里的 10-30 个字,便于定位。绝不猜。"
    )



    def build_research_pack(
        self,
        topic: str,
        *,
        context: str = "",
        max_output_tokens: int = 5500,
        strict: bool = False,
        timeout: int | None = None,
        single_search: bool = False,
    ) -> str:
        """Build a source-grounded research pack via Perplexity Sonar (OpenRouter).

        ``timeout`` overrides the per-attempt web-search timeout; ``None`` keeps
        the long-video defaults (520s then 420s). Short videos pass a smaller
        value to cap tail latency.
        ``single_search`` uses ONE fast web_search-only attempt (no full-page
        web_extractor) — much faster, good enough for short-video fact-checks.
        """
        if os.environ.get("MEDIA_BUDDY_RESEARCH_PACK", "1").strip().lower() in {
            "0", "false", "no", "off",
        }:
            return ""
        topic = str(topic or "").strip()
        if not topic:
            return ""

        domain = self.research_domain(topic, context)

        try:
            pack = self._sonar_research_pack(
                topic, context=context, domain=domain,
                max_output_tokens=max_output_tokens, timeout=timeout or 120,
            )
        except Exception as exc:
            logger.warning("research pack failed", exc_info=True)
            if strict:
                raise RuntimeError("grounding_pack_unavailable") from exc
            return ""
        if pack and (not strict or self.grounding_pack_looks_usable(pack)):
            return pack
        if strict:
            raise RuntimeError("grounding_pack_unavailable")
        return ""

    # Lets us flip ONE purpose at a time to Haiku for cost experiments
    # (smoke 16/17/18/19) while keeping everything else on Sonnet as the
    # verify → Haiku (was hardcoded), everything else → provider default
    # which resolves to Sonnet 4.6 via self.model.
    #
    # Implementation note: returning None means "use provider default"
    # (i.e. self.model, typically claude-sonnet-4-6 for anthropic). When
    # an env var explicitly names Sonnet, we return it explicitly — this
    # is functionally identical but lets summaries see the model name.
    _PURPOSE_ENV_VARS: dict[str, str] = {
        "verify":                "MEDIA_BUDDY_VERIFY_MODEL",
        # (每条片子最多 5 次调用),而不牵动其它同样用 verify 档的功能。
        # 不配置 → 回落 verify 档(见 _PURPOSE_FALLBACK)。
        "closure":               "MEDIA_BUDDY_CLOSURE_MODEL",
        "plan_chunks":           "MEDIA_BUDDY_PLAN_MODEL",
        "build_chunk_contracts": "MEDIA_BUDDY_CONTRACTS_MODEL",
        "build_video_contract":  "MEDIA_BUDDY_CONTRACTS_MODEL",  # same family
        "cinematic_ideation":    "MEDIA_BUDDY_IDEATION_MODEL",
        "global_director":       "MEDIA_BUDDY_DIRECTOR_MODEL",
        "global_director_query_repair": "MEDIA_BUDDY_DIRECTOR_QUERY_REPAIR_MODEL",
        "publish_seo":           "MEDIA_BUDDY_PUBLISH_SEO_MODEL",
        "channel_topics":        "MEDIA_BUDDY_CHANNEL_TOPICS_MODEL",
        # 自定义频道:AI 从一段描述生成整套频道规则 + 轻量红线判定。
        "channel_design":        "MEDIA_BUDDY_CHANNEL_DESIGN_MODEL",
        "channel_guard":         "MEDIA_BUDDY_CHANNEL_GUARD_MODEL",
        # 中国古代/考古选题兜底判定(词表追不全的长尾)。flash 会判反,必须 qwen-plus。
        "china_ancient_filter":  "MEDIA_BUDDY_CHINA_ANCIENT_FILTER_MODEL",
        "studio_long":           "MEDIA_BUDDY_STUDIO_LONG_MODEL",
        "radar_profile":         "MEDIA_BUDDY_RADAR_PROFILE_MODEL",
        "radar_brief":           "MEDIA_BUDDY_RADAR_BRIEF_MODEL",
        "radar_news_policy":     "MEDIA_BUDDY_RADAR_NEWS_POLICY_MODEL",
        "radar_shape":           "MEDIA_BUDDY_RADAR_SHAPE_MODEL",
        "radar_editor":          "MEDIA_BUDDY_RADAR_EDITOR_MODEL",
        "radar_term_audit":      "MEDIA_BUDDY_RADAR_TERM_AUDIT_MODEL",
        "radar_semantic":        "MEDIA_BUDDY_RADAR_SEMANTIC_MODEL",
        "radar_fit":             "MEDIA_BUDDY_RADAR_FIT_MODEL",
        #                     **真能搜到东西**的说法(不是逐字翻译)。
        #                     单独立一档:它的输出直接决定要不要花 100 units 去搜,
        #                     译错一个词就是白烧一次配额,值得能独立换模型。
        "radar_xlang_terms":     "MEDIA_BUDDY_RADAR_XLANG_MODEL",
        "radar_cluster":         "MEDIA_BUDDY_RADAR_CLUSTER_MODEL",
        "radar_adjacent":        "MEDIA_BUDDY_RADAR_ADJACENT_MODEL",
        "radar_search_audit":    "MEDIA_BUDDY_RADAR_SEARCH_AUDIT_MODEL",
        #   ⚠️ 纯翻译，**不改写**；译文只进 `zh_preview` 列，永远不进出片链路。
        "radar_zh_preview":      "MEDIA_BUDDY_RADAR_ZH_PREVIEW_MODEL",
        #      搜到东西的说法」,会故意改写、翻不好还整条丢掉;这里要的恰恰
        #      相反 —— 忠实、一条不少(少一条客户面板上就少一个分类)。
        "radar_label_translate": "MEDIA_BUDDY_RADAR_LABEL_TRANSLATE_MODEL",
        # blind_observe routes through CloudGateway not LLMClient, see
        # blind_observe.py — its env is OMNI_MODEL (existing, unchanged).
    }
    # 档位回落表:{新档位: 没配时借用哪个老档位}。
    _PURPOSE_FALLBACK: dict[str, str] = {
        "closure": "verify",
        # 🚨 **新档位没配 env 就必须在这里回落。**
        #
        # 于是 `_purpose_model` 返回 None → 路由落到一个**没有认证的通道**
        # → `RuntimeError: cloud not authenticated` → 客户看到「翻译暂不可用」。
        # 而且这个错**只在真实请求里出现**（测试用假 LLM，跑得好好的）。
        #
        #    （deepseek-v3.2），而翻译对模型的要求比写 brief 低，够用。
        "radar_zh_preview": "radar_brief",
        #    返回 None → 路由落到没认证的通道 → `RuntimeError` →
        #    日志「支柱名翻译失败，先保持原文（6 条）」。
        #    **同一个坑,第二次。** 新档位必须同时在这里回落。
        "radar_label_translate": "radar_brief",
    }
    _PURPOSE_MAX_TOKENS: dict[str, int] = {
        # 收口档要同时承担"返回一小段 JSON 判定"和"返回整篇修复稿",按后者给。
        "closure": 2000,
        "studio_long": 8000,
        "channel_topics": 800,  # a short topic list — small cap keeps it fast
        "channel_design": 2500,  # a full ChannelRule JSON (positioning + pools)
        "channel_guard": 300,    # a tiny classification JSON
        # brief 含教学字段(为什么推给你/要验证什么);红线只回一个小数组。
        "radar_profile": 1500,
        "radar_brief": 2000,
        "radar_news_policy": 800,
        "radar_shape": 800,
        # 中文对照：要把**整篇稿子**翻出来。10 分钟长片的中文正文是
        # 1500~2100 字 ≈ 3k+ token，再加标题和 JSON 外壳。
        # ⚠️ 给窄了会被**截断在半路** —— 客户拿到半篇译文，
        "radar_zh_preview": 8000,
        # AI 编辑要**对每一条候选表态**,40 条就是 40 行 JSON,再加分组、
        # LLMBadOutput —— JSON 被截断在半路。这一层一轮只调一次,给宽点不心疼。
        "radar_editor": 3000,
        # V2 语义层:每条要输出十来个字段 + 2~3 个方向,比编辑重得多。
        # ⚠️ 它**分批**(core/semantic.MAX_ITEMS_PER_CALL),不是一次全塞。
        #    **3 批里 2 批被截断**(8 条真实输出 7,545 中文字符 ≈ 7k+ token,紧贴上限)。
        #    两头一起留余量:批量降到 5 条,上限提到 12000。
        #    编辑当年按 3000 给也被截断过 —— 这一层字段多得多,别再省这点钱。
        "radar_semantic": 12000,
        # 给到 6000 是留两倍余量 —— 被截断一次就白花一次钱(语义层那笔学费别再交)。
        "radar_fit": 6000,
        # 跨语言译词:一次最多 3 个词,每个词就几个单词。输出极小。
        # 给 800 是留足余量 —— 被截断一次就等于这一门语言这一轮白跑。
        "radar_xlang_terms": 800,
        # 支柱名翻译:最多几条,每条 2~4 个词。输出极小,800 留足余量。
        "radar_label_translate": 800,
        # V3 聚类:最多 8 个支柱 × (key/label/2~6 个词),输出很小;
        # 但输入是 80 条标题,所以贵在输入不在输出。
        "radar_cluster": 1500,
        # 邻接领域每个要带 why + 3 个具体选题（中文长句）→ 比聚类费 token。
        #    `json.loads` 整份失败 → 一个领域都没产出，日志上像「模型没答」。
        "radar_adjacent": 4500,
        # 搜索词质检:一批 6 个词 × 一行判定,输出很小;贵在输入(每词 6 条标题)。
        "radar_search_audit": 1200,
        # 每个词一段判定 + 理由 + 建议替代词,8 个词也就千把 token。
        "radar_term_audit": 1500,
        "china_ancient_filter": 400,  # a short {"drop":[...]} index list
        "global_director": 6000,
        "global_director_query_repair": 1200,
        # caps (skeleton 1200 / expand 2200) truncated the back-half of longer
        # scripts (15+ chunks) → empty/repeated late-chunk queries ("后半段空镜").
        # The tight caps existed only to dodge the cloud's 45s OpenRouter timeout;
        # ample headroom to let the director fill EVERY chunk. Env-overridable
        # (clamped ≤8192) via MEDIA_BUDDY_GLOBAL_DIRECTOR_SKELETON_MAX_TOKENS /
        # MEDIA_BUDDY_GLOBAL_DIRECTOR_EXPAND_MAX_TOKENS — see _purpose_max_tokens.
        "global_director_skeleton": 3000,
        "global_director_expand": 4500,
        "translate": 8000,  # 整稿翻译(出片前中→英),防长稿被截断
    }

    # 都由 env 覆盖(如 verify → env 指定的模型),所以**不要把下面的模型名当作
    # Defaults applied ONLY when the env var is unset. verify keeps the
    # non-verify purposes had NO _PURPOSE_DEFAULT_MODEL entry → desktop sent
    # we assumed). Fix: explicit Haiku 4.5 defaults for all 4 purposes.
    # Haiku validated on verify; these tasks are simpler structured outputs.
    _PURPOSE_DEFAULT_MODEL: dict[str, dict[str, Optional[str]]] = {
        "verify": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        "plan_chunks": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        "build_video_contract": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        "build_chunk_contracts": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        "cinematic_ideation": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        # shot queries for science/explainer topics ("infrared", "TRPV1 receptor",
        # "neural pathway") because it couldn't follow the "real filmable only +
        # abstract→analogy + niche→broaden" constraint. qwen-max follows it (drops
        # infrared, maps neural→"man wincing", niche→"desert rodent"). Only 1-2
        # calls per video, so the cost delta is small. Judge stays qwen3-omni-flash.
        "global_director": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        "global_director_skeleton": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        "global_director_expand": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        "global_director_query_repair": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        # Runtime niche-broaden ('kangaroo rat'->'desert rodent'). Cheap + must go
        # query. qwen-plus is plenty for a 2-word category rewrite.
        "stock_query_broaden": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        "publish_seo": {
            # 「藏答案 + 禁虚词 + 逼具体细节」是一组很吃指令遵从的创意约束,
            # qwen-plus 常滑(漏「秘密」虚词、剧透答案)。
            #
            #
            # 🚨 **换的是「更听话」这条,不是「更便宜」这条** —— 当初换 max 是因为
            #    所以要盯着:发布时如果开始出现「揭秘/秘密/震惊」这类虚词,
            #    或者标题把答案直接剧透了,就是这次换模型的锅,
            #    env MEDIA_BUDDY_PUBLISH_SEO_MODEL 可以随时覆盖回 qwen-max。
            #
            # ⚠️ openrouter 那格本来就是 deepseek —— 也就是说走 OpenRouter 的部署
            #    一直在用 deepseek 写 SEO,并没有出问题的记录。这是换过去的一点底气。
            "openrouter": "deepseek/deepseek-chat",
        },
        "channel_topics": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        # 画像:赛道词写错 → 整个频道采到垃圾数据,且用户看不出来。
        "radar_profile": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        # brief:要写出"为什么这条推给你频道",很吃指令遵从(同 publish_seo 的理由)。
        #    我们千问 Max 不要使用它…其实可以用 DeepSeek,因为这个东西便宜」)。
        #    代码默认留着 qwen-max 只会让下一个读代码的人判断错,
        #    而且**换台机器部署就会悄悄变回 qwen**。
        #    所以它跟着一起变成 deepseek —— 翻译本来就该用便宜的。
        "radar_brief": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        # 形状归纳:从头尾各几条标题里看出「什么形状的题在这个号能跑」。
        #   qwen-plus     把「单一事件解释」判成成功形状
        #   deepseek-v3.2 把同一个形状判成失败形状
        # 真实数据是「哥伦比亚地震地质」6 播放 —— **deepseek 判对了**。
        # 另外 deepseek 稳定输出中文,qwen-plus 在中文提示词下会飘成英文。
        # ⚠️ 但两个模型在这个样本量上都不够稳,所以下游对低置信结果
        # **只降级不排除**(见 core/diagnosis.losing_shapes_are_advisory)。
        "radar_shape": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        # 挑今日首选 / 写一句总结。**只做编辑不做裁判**,改不了档位和证据。
        # 中文提示词下 deepseek 稳定输出中文,qwen-plus 会飘成英文;
        # 而且这一层要读几十条标题做取舍,便宜且长上下文更划算。
        # ⚠️ 它一轮只调一次,所以**不用为省钱牺牲质量**。
        # 赛道词体检:拿真搜回来的标题判「这个词是不是被理解成别的意思了」。
        # 一次性、判错代价大(整条供给会跑偏一整周),用和形状归纳同档的模型。
        "radar_term_audit": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        "radar_editor": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        # 生成 2~3 个可执行方向、认出「这几条其实是同一件事」。
        # 🚨 它**没有删除权**(见 core/semantic.py 的宪章)。
        # 同样用 deepseek:中文提示词下稳定输出中文(qwen-plus 会飘成英文),
        # 而且要读几十条标题做长文本推理,便宜且长上下文更划算。
        "radar_semantic": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        "radar_fit": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        # 中文提示词下稳定输出中文,而且这一档要的是**多语种常识**
        # (「日本人搜这个主题会怎么说」),不是推理能力。
        "radar_xlang_terms": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        # **多语种常识 + 忠实**,不是推理能力,和 xlang 同源。
        "radar_label_translate": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        # 🚨 **一次性、判错代价极大** —— 聚错了整个频道后面三个月都会被
        # 推错方向(V2 就是这么把 61 张卡里 27 张堆到 NVIDIA/OpenAI 上的)。
        "radar_search_audit": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        "radar_cluster": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        # 这里恰恰要模型干这件事，但必须锁死人群并说出理由。
        # 同一档模型：两边都是「一次性、判错要错三个月」的归纳任务。
        "radar_adjacent": {
            "openrouter": "deepseek/deepseek-v3.2",
        },
        # 新闻红线兜底:判错会让客户做出无法变现的视频。
        # 二元合规判定上会判反。
        "radar_news_policy": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        # 自定义频道生成:一次性、质量敏感 → qwen-plus(比选题的 flash 强)。
        "channel_design": {
            "openrouter": "deepseek/deepseek-chat",
        },
        # 红线判定:便宜的分类,flash 足够。
        "channel_guard": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
        # 必须 qwen-plus 才判得准(7/8)。
        "china_ancient_filter": {
            "openrouter": "deepseek/deepseek-chat",
        },
        "studio_long": {
            "openrouter": "qwen/qwen3-235b-a22b-2507:online",
        },
        "script": {
            "openrouter": "qwen/qwen3-235b-a22b-2507:online",
        },
        # 每次摔 openrouter → 落写死通用模板。
        "studio": {
            "openrouter": "qwen/qwen3-235b-a22b-2507:online",
        },
        "translate": {
            "openrouter": "anthropic/claude-haiku-4-5",
        },
    }


    @staticmethod
    def _normalize_model_id_for_provider(
        model_id: str, provider: str,
    ) -> str:
        """

        OpenRouter requires `<vendor>/<model>` ids; bare names silently
        404 (memory `feedback_fal_ai_paths.md` — same trap). When a user
        sets `MEDIA_BUDDY_PLAN_MODEL=claude-haiku-4-5` (the natural form),
        upgrade it to `anthropic/claude-haiku-4-5` for the openrouter
        provider. Anthropic direct provider keeps the bare form.
        """
        if provider != "openrouter":
            return model_id
        # OpenRouter's public model name includes "Instruct", but the API slug
        # is qwen/qwen3-235b-a22b-2507. Accept the more explicit alias too.
        if model_id == "qwen/qwen3-235b-a22b-instruct-2507":
            return "qwen/qwen3-235b-a22b-2507"
        if "/" in model_id:
            return model_id     # already vendor-prefixed
        # Heuristic by model-name prefix
        if model_id.startswith(("claude-", "claude.")):
            return "anthropic/" + model_id
        if model_id.startswith(("gpt-", "o1-", "o3-", "o4-")):
            return "openai/" + model_id
        if model_id.startswith(("gemini-",)):
            return "google/" + model_id
        if model_id.startswith(("llama-", "llama3", "llama4")):
            return "meta-llama/" + model_id
        # Unknown vendor — leave as-is; let cloud surface the 404 explicitly
        return model_id

    @classmethod
    def _purpose_model(
        cls, purpose: Optional[str], provider: str,
    ) -> Optional[str]:
        """Map (purpose, provider) → model_id, or None to use provider default.

          1. env var named in _PURPOSE_ENV_VARS → use, then normalize for
             provider (bare "claude-haiku-4-5" → "anthropic/claude-haiku-4-5"
             when provider==openrouter)
          2. _PURPOSE_DEFAULT_MODEL[purpose][provider] → built-in default
             (now covers 5 purposes: verify + the 4 director roles)
          3. None → caller falls back to self.model, then "_unknown_"
        """
        if not purpose:
            return None
        provider_env = f"MEDIA_BUDDY_{provider.upper()}_{purpose.upper()}_MODEL"
        provider_val = os.environ.get(provider_env)
        if provider_val:
            return cls._normalize_model_id_for_provider(
                provider_val.strip(), provider,
            )
        env_key = cls._PURPOSE_ENV_VARS.get(purpose)
        if env_key:
            env_val = os.environ.get(env_key)
            if env_val:
                return cls._normalize_model_id_for_provider(
                    env_val.strip(), provider,
                )
        # Built-in defaults (already provider-correct strings)
        prov_map = cls._PURPOSE_DEFAULT_MODEL.get(purpose, {})
        resolved = prov_map.get(provider)
        if resolved:
            return resolved
        # 档位回落:新档位没配时落到一个语义相近的老档位,而不是掉到 self.model
        # (那通常是贵得多的主力模型)。这样"新增一个可单独调价的档位"是零风险的。
        fallback = cls._PURPOSE_FALLBACK.get(purpose)
        if fallback:
            return cls._purpose_model(fallback, provider)
        return None

    @classmethod
    def _purpose_max_tokens(cls, purpose: Optional[str]) -> Optional[int]:
        if not purpose:
            return None
        raw = os.environ.get(f"MEDIA_BUDDY_{purpose.upper()}_MAX_TOKENS")
        if raw:
            try:
                return max(256, min(8192, int(raw)))
            except Exception:
                pass
        return cls._PURPOSE_MAX_TOKENS.get(purpose)


    def broaden_subject_for_stock(
        self, lead_query: str, sentence: str = "",
    ) -> list[str]:
        """Runtime niche-broaden: when the exact subject's stock search collided
        or came up empty (e.g. 'kangaroo rat tail' returns real kangaroos /
        wallabies, 'dog listening' returns a boy with headphones), ask for a
        BROADENED, generic, stock-searchable category for the SAME subject.

        The director cannot know our real-footage library's inventory, so this is
        a search-informed fallback: it only runs after the on-subject cascade
        levels failed. Returns 1-2 short English queries; [] on failure (caller
        then falls through to the existing metaphor/similar levels).
        """
        system = (
            "You repair a failed stock-footage search. The search used a niche / "
            "over-specific subject that our REAL-footage stock library lacks, so "
            "it returned keyword-collision or empty results. Give a BROADENED, "
            "generic, stock-searchable English query for the SAME subject using a "
            "common category that a stock library actually has.\n"
            "Examples:\n"
            "  'kangaroo rat tail' -> 'desert rodent'\n"
            "  'fennec fox' -> 'small desert fox'\n"
            "  'axolotl' -> 'aquatic salamander'\n"
            "  'pangolin walking' -> 'scaly anteater'\n"
            "  'dog listening' -> 'dog ears alert'\n"
            "Rules: keep the SAME real subject (do not switch animals/objects); "
            "drop the rare proper name for its common category; 2-3 words each; "
            "real filmable footage only (no infrared/animation/diagram).\n"
            "Return a JSON ARRAY of 1-2 plain English strings. No prose."
        )
        user = (
            f"Failed query: {lead_query}\n"
            f"Sentence context: {sentence}\n"
            "Broadened generic stock queries (JSON array):"
        )
        try:
            raw = self._call(system, user, purpose="stock_query_broaden").strip()
        except Exception as e:
            logger.warning("broaden_subject_for_stock LLM call failed: %s", e)
            return []
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M)
        match = re.search(r"\[.*\]", raw, flags=re.S)
        if not match:
            return []
        try:
            values = json.loads(match.group(0))
        except Exception:
            return []
        if not isinstance(values, list):
            return []
        cleaned: list[str] = []
        for v in values:
            s = _coerce_query_to_string(v).strip()
            if s and s.lower() != (lead_query or "").strip().lower():
                cleaned.append(s)
        # de-dup preserving order
        seen: set[str] = set()
        out: list[str] = []
        for s in cleaned:
            k = s.lower()
            if k not in seen:
                seen.add(k)
                out.append(s)
        return out[:2]




    def assess_shot_counts(self, sentences: list[str]) -> list[int]:
        """
        For each sentence, decide how many cinematic shots best express it.
        Simple sentences → 1 shot. Multi-clause / contrast / list / progression
        sentences → 2 or 3 shots. Returns parallel list of integers (1..3).
        """
        if not sentences:
            return []
        n = len(sentences)
        system = (
            "You are a video director. For each sentence, decide how many "
            "distinct cinematic shots best express it.\n"
            "\n"
            "Rules:\n"
            "- Simple statement → 1 shot\n"
            "- Sentence with contrast / 'but' / two-clause structure → 2 shots\n"
            "- Sentence with enumeration of 3+ items → 3 shots\n"
            "- Never more than 3 shots per sentence\n"
            "\n"
            f"Output a JSON array of EXACTLY {n} integers (each 1, 2, or 3), "
            "matching input order. Output ONLY the JSON array."
        )
        user = (
            "Sentences:\n"
            + json.dumps(sentences, ensure_ascii=False, indent=2)
            + f"\n\nReturn JSON array of {n} integers:"
        )
        try:
            raw = self._call(system, user).strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M)
            match = re.search(r"\[.*\]", raw, flags=re.S)
            if not match:
                raise ValueError("no JSON array")
            counts = json.loads(match.group(0))
            if not isinstance(counts, list):
                raise ValueError(f"not a list: {counts!r}")
            cleaned = [max(1, min(3, int(c))) for c in counts]
            if len(cleaned) < n:
                cleaned += [1] * (n - len(cleaned))
            return cleaned[:n]
        except Exception:
            # Conservative fallback: 1 shot per sentence
            return [1] * n


    def split_sentence_into_subs(self, sentence: str, n: int) -> list[str]:
        """
        ``n`` shorter visual sub-units for B-roll editing (Path A multi-shot).

        Caller computes ``n`` via :meth:`assess_shot_counts` and only invokes
        this when ``n > 1``. Returns ``[sentence]`` (unchanged) on LLM
        failure so the caller can keep the original 1-shot path.

        Note: this is LLM-driven splitting for sentences that have NO
        natural connector ("比如/例如/像/如同/好比/譬如"). When a connector IS
        present the caller (PipelineService._expand_sentences_for_visual)
        uses a regex split — cheaper + reliable. This method only fires
        for sentences with shot_count > 1 but no connector.
        """
        sentence = (sentence or "").strip()
        if not sentence or n <= 1:
            return [sentence] if sentence else []
        n = max(2, min(3, int(n)))
        system = (
            "You split a long narration sentence into shorter visual sub-units\n"
            f"for B-roll editing. Produce EXACTLY {n} sub-strings that:\n"
            "  • cover the original sentence end-to-end\n"
            "  • split at natural semantic boundaries (clause comma, '比如')\n"
            "  • each sub-string is a coherent unit (avoid mid-phrase cuts)\n"
            "  • keep the ORIGINAL LANGUAGE (Chinese stays Chinese; do NOT\n"
            "    translate)\n"
            "  • do NOT paraphrase — the joined subs must read like the\n"
            "    original (you may drop a redundant comma but no other edits)\n\n"
            f'Output ONLY: {{"subs": [<{n} strings>]}}'
        )
        user = f"Sentence: {sentence}\n\nReturn JSON:"
        try:
            raw = self._call(system, user).strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M)
            match = re.search(r"\{.*\}", raw, flags=re.S)
            if not match:
                return [sentence]
            data = json.loads(match.group(0))
            subs = data.get("subs", [])
            if not isinstance(subs, list):
                return [sentence]
            cleaned = [str(s).strip() for s in subs if str(s).strip()]
            # Defensive: require at least 2 non-empty subs and total length
            joined_len = sum(len(s) for s in cleaned)
            if len(cleaned) < 2 or joined_len < len(sentence) * 0.5:
                return [sentence]
            return cleaned[:n]
        except Exception as e:
            logger.warning(
                "split_sentence_into_subs failed for sentence=%r: %s: %s",
                sentence[:60], type(e).__name__, e,
            )
            return [sentence]

    @staticmethod
    def _build_music_context_block(content_context: Optional[dict]) -> str:
        """P2-14 — build the optional CONTENT CONTEXT block injected into the
        music-arc prompt when the AI BGM director is enabled.

        Returns "" when content_context is falsy, so the default (director-off)
        prompt is byte-identical to the historical prompt — zero behaviour
        change when MEDIA_BUDDY_AI_BGM_DIRECTOR is off.
        """
        if not content_context:
            return ""
        title = str(content_context.get("title") or "").strip()
        series_name = str(content_context.get("series_name") or "").strip()
        description = str(content_context.get("description") or "").strip()
        industry = str(content_context.get("industry") or "").strip()
        mood_lock = str(content_context.get("mood_lock") or "").strip()

        lines = [
            "\nCONTENT CONTEXT — choose music that fits THIS content's tonality. "
            "Do NOT default every video to the same soft/soothing/calm-ambient "
            "style; a finance explainer, a suspense story and a light lifestyle "
            "clip must sound clearly different.\n",
        ]
        if series_name:
            lines.append(f"- Series / theme: {series_name}\n")
        if description:
            lines.append(f"- Theme description: {description[:300]}\n")
        if industry:
            lines.append(f"- Industry / genre tag: {industry}\n")
        if title:
            lines.append(f"- Video title: {title[:200]}\n")
        lines.append(
            "Tonality guidance (map the content to a direction, then vary mood/"
            "energy across the arc):\n"
            "- finance / business / knowledge / explainer → understated, modern, "
            "neutral, low-key; avoid saccharine strings\n"
            "- suspense / mystery / crime / thriller → tense, dark, sparse, "
            "cinematic tension\n"
            "- light story / lifestyle / comedy / kids → warm, playful, melodic\n"
            "- history / documentary / science → cinematic, restrained, dignified\n"
            "- The 'search_keywords' of each segment MUST reflect this direction "
            "(genre + instrumentation + mood), not generic 'ambient background music'.\n"
        )
        if mood_lock:
            lines.append(
                f"- HARD CONSTRAINT: the producer locked the overall music mood to "
                f"'{mood_lock}'. Every segment must honour this locked mood in its "
                "mood + search_keywords; you may still vary energy across the arc.\n"
            )
        lines.append("\n")
        return "".join(lines)

    def analyze_music_arc(
        self,
        sentences: list[str],
        durations: list[float],
        content_context: Optional[dict] = None,
    ) -> list[dict]:
        """
        Analyze the entire script's narrative arc and return 1-N music segments.

        Inputs are parallel: sentences[i] is one chunk's narration, durations[i]
        is its TTS duration in seconds.

        ``content_context`` (P2-14, optional): when provided — only by the
        pipeline when ``MEDIA_BUDDY_AI_BGM_DIRECTOR`` is enabled — a dict with any
        of ``title``/``series_name``/``description``/``industry``/``mood_lock``.
        It injects a CONTENT CONTEXT block so the composer matches music to the
        content's tonality instead of collapsing every video to calm ambient.
        When ``None`` the prompt is identical to the historical one (zero change).

        Output: list of segment dicts, each with keys:
            start_chunk_index, end_chunk_index, narrative_role,
            mood, energy, search_keywords

        Rules enforced by prompt + post-validation:
          - 1 segment for ≤5 chunks
          - 2-3 segments for 6-15 chunks
          - 3-5 segments for 16+ chunks
          - Each segment ≥3 chunks (when total allows)
          - Adjacent segments must differ in mood OR energy
        """
        n = len(sentences)
        if n == 0:
            return []
        if len(durations) != n:
            durations = list(durations) + [0.0] * (n - len(durations))

        # Decide expected segment count range from script length.
        if n <= 5:
            min_segs, max_segs = 1, 1
        elif n <= 15:
            min_segs, max_segs = 2, 3
        else:
            min_segs, max_segs = 3, 5

        total_dur = sum(durations) or 0.0
        chunks_view = [
            {"i": i, "sentence": s, "dur": round(d, 1)}
            for i, (s, d) in enumerate(zip(sentences, durations))
        ]

        system = (
            "You are a film score composer. Read the entire script and identify "
            "its narrative arc: where the mood or energy SHIFTS. Output 1-N "
            "music segments matching that arc.\n\n"
            "Each segment covers a contiguous range of chunks (by index) and "
            "has its own mood/energy/search_keywords for picking music.\n\n"
            + self._build_music_context_block(content_context)
            + "Rules:\n"
            f"- Pick exactly {min_segs} to {max_segs} segments for this {n}-chunk script\n"
            "- Each segment must cover at least 3 chunks (unless total chunks < 3)\n"
            "- Adjacent segments MUST differ in either mood OR energy (not both same)\n"
            "- start_chunk_index of segment[k+1] = end_chunk_index of segment[k] + 1\n"
            "- Cover all chunks: first segment starts at 0, last ends at " f"{n - 1}\n\n"
            "Schema for each segment:\n"
            '  "start_chunk_index": int,\n'
            '  "end_chunk_index": int,\n'
            '  "narrative_role": "intro" | "build-up" | "climax" | "resolution" | "outro" | "transition",\n'
            '  "mood": "calm" | "uplifting" | "tense" | "melancholic" | "playful" | "dramatic" | "reflective",\n'
            '  "energy": "low" | "medium" | "high",\n'
            '  "search_keywords": "4-8 English words for searching stock music libraries"\n\n'
            "Output ONLY this JSON object: "
            '{"segments": [...]}'
        )
        user = (
            f"Total chunks: {n}; total narration duration: {total_dur:.1f}s\n\n"
            "Chunks (index | duration | sentence):\n"
            + json.dumps(chunks_view, ensure_ascii=False, indent=2)
            + f"\n\nReturn JSON {{\"segments\": [...]}} with {min_segs}-{max_segs} entries:"
        )
        try:
            raw = self._call(system, user).strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M)
            match = re.search(r"\{.*\}", raw, flags=re.S)
            if not match:
                raise ValueError("no JSON object found")
            data = json.loads(match.group(0))
            segments = data.get("segments", [])
            if not isinstance(segments, list) or not segments:
                raise ValueError(f"no segments array: {data!r}")
            return _normalize_music_segments(segments, n)
        except Exception as e:
            # Conservative fallback: single calm-medium segment over whole script
            return [{
                "start_chunk_index": 0,
                "end_chunk_index": n - 1,
                "narrative_role": "intro",
                "mood": "calm",
                "energy": "medium",
                "search_keywords": "ambient cinematic background music",
                "_fallback_reason": str(e),
            }]

    def extract_scene_queries(self, sentences: list[str]) -> list[str]:
        """
        Given a list of script sentences, return a parallel list of director-style
        English queries — ONE query per sentence, same order, same length.
        This 1:1 alignment is required so subtitles burned for sentence i match
        the footage clip retrieved by query i.
        """
        if not sentences:
            return []

        n = len(sentences)
        system = (
            "You are a video director picking B-roll for narration.\n"
            "Input: a JSON array of script sentences (any language).\n"
            "Output: a JSON array of CONCRETE visual scenes — one English shot "
            "description per sentence, in the SAME ORDER, with the SAME LENGTH.\n"
            "\n"
            "Each shot describes one literal scene a camera could film. "
            "Subject + action + setting (+ optional mood/lighting/angle).\n"
            "\n"
            "❌ Bad (abstract): \"artificial intelligence\", \"future\", \"success\"\n"
            "✓ Good (concrete): \"woman typing on laptop in sunlit cafe\", "
            "\"drone shot of city skyline at golden hour\", "
            "\"chef plating sushi in restaurant kitchen\"\n"
            "\n"
            "RULES:\n"
            f"1. Output array MUST have EXACTLY {n} items.\n"
            "2. queries[i] MUST visually represent sentences[i] — preserve order.\n"
            "3. Each query: 4-8 English words. Filmable subject required.\n"
            "4. Output ONLY the JSON array. No commentary, no markdown fences."
        )
        user = (
            "Sentences (length = " + str(n) + "):\n"
            + json.dumps(sentences, ensure_ascii=False, indent=2)
            + "\n\nReturn JSON array of " + str(n) + " queries:"
        )
        raw = self._call(system, user).strip()

        # Strip markdown fences if any
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M)

        # Find a JSON array anywhere in the response
        match = re.search(r"\[.*\]", raw, flags=re.S)
        if not match:
            raise ValueError(f"No JSON array in LLM response: {raw[:200]}")

        try:
            queries = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"Bad JSON in LLM response: {e} | raw: {raw[:200]}")

        if not isinstance(queries, list):
            raise ValueError(f"Expected JSON array, got {type(queries).__name__}")

        cleaned = [str(q).strip() for q in queries]

        # Enforce length match — pad or truncate. We log when LLM disobeys.
        if len(cleaned) < n:
            # Pad with raw sentences (best effort fallback)
            cleaned += [s.strip() or "scenic background" for s in sentences[len(cleaned):]]
        elif len(cleaned) > n:
            cleaned = cleaned[:n]

        return cleaned

    def generate_script(
        self,
        prompt: str,
        output_format: str = "youtube_landscape",
        duration_hint_seconds: int = 60,
        system_context: str = "",
        speed: float = 1.0,
        research_pack: str = "",
        tts_provider: str = "",
        output_language: str = "zh",
    ) -> str:
        duration = max(1, int(duration_hint_seconds or 60))
        # 语速平衡:选的语速越快、读得越快,同样时长要写越多字,成片才等于用户设的时长。
        speed = max(0.7, min(2.0, float(speed or 1.0)))
        if output_format == "youtube_landscape" and duration >= 300:
            return self._generate_long_form_script(
                prompt,
                duration_hint_seconds=duration,
                system_context=system_context,
                speed=speed,
            )

        # 「诙谐幽默」风格触发:用户在"风格"里选了它 → 前端把"风格：诙谐幽默"拼进 prompt/context,
        # 检测到就注入搞笑配方;其余风格(真实口播等)保持原样。
        funny = any(
            k in (str(prompt) + " " + str(system_context))
            for k in ("诙谐幽默", "幽默搞笑", "搞笑")
        )

        # 短视频也先联网核实"事实"再写稿(智能触发:只对历史/财经/人物事件等事实风险
        # 题材搜,闲聊跳过)。两种联网方式,失败一律优雅回落无 grounding,绝不拖垮出片:
        #   ① DeepSeek :online 自己联网搜+写(配 MEDIA_BUDDY_SCRIPT_OPENROUTER_SEARCH_MODEL):
        or_model = os.environ.get("MEDIA_BUDDY_SCRIPT_OPENROUTER_MODEL", "").strip()
        search_model = os.environ.get(
            "MEDIA_BUDDY_SCRIPT_OPENROUTER_SEARCH_MODEL", ""
        ).strip()
        has_or_key = bool(os.environ.get("OPENROUTER_API_KEY"))
        writer_model = or_model  # 默认:普通直连模型(闲聊/无需联网时,不为搜索多花钱)
        research_pack = str(research_pack or "").strip()
        if research_pack:
            # 外部(如 Studio 会话缓存)已提供联网资料包 → 直接沿用,跳过再次联网
            # (省钱省时 + 多轮事实一致)。用与老路径②完全相同的框架注入,约束不漂移。
            domain = self.research_domain(prompt, system_context)
            fun_note = (
                "（短视频要有趣好懂：联网核实只为不说错,不是用来罗列。每个用到的事实/数字都要"
                "立刻配一个生活化比喻或梗讲成人话,别把一堆数字/日期/术语干搬进口播。）\n"
            ) if funny else ""
            system_context = (
                f"{system_context}\n\n"
                "【联网事实资料包】\n"
                f"{research_pack}\n\n"
                "事实规则：正文里的具体年份、数字、人物生卒、事件归属、机构名称，"
                "必须来自上面的资料包；资料包没有支撑的说法只能写成推测或删除。\n"
                f"{fun_note}"
                f"{self._strict_grounding_script_rules(domain)}\n"
            )
        elif (
            self._short_grounding_enabled()
            and self._short_should_ground(prompt, system_context)
        ):
            domain = self.research_domain(prompt, system_context)
            fun_note = (
                "（短视频要有趣好懂：联网核实只为不说错,不是用来罗列。每个用到的事实/数字都要"
                "立刻配一个生活化比喻或梗讲成人话,别把一堆数字/日期/术语干搬进口播。）\n"
            ) if funny else ""
            # 这里必须**跳过**「让写稿模型自己联网」那条路 —— 否则搜的还是 DeepSeek,
            use_self_search = False  # research packs come from Sonar; the writer never self-searches
            if use_self_search:
                writer_model = search_model
                system_context = (
                    f"{system_context}\n\n"
                    "【联网事实核实】\n"
                    "你具备联网搜索能力：写稿前先在线核实关键事实(具体年份、数字、人物生卒、"
                    "事件归属、机构名称),只采用可溯源的权威信息;查不到支撑的说法写成推测或"
                    "删除,绝不编造。\n"
                    f"{fun_note}"
                    f"{self._strict_grounding_script_rules(domain)}\n"
                )
            else:
                #    兜底,失败退回无 grounding。
                pack = ""
                try:
                    pack = self.build_research_pack(
                        prompt,
                        context=system_context,
                        max_output_tokens=self._short_grounding_max_tokens(),
                        strict=False,
                        timeout=self._short_grounding_timeout(),
                        single_search=True,
                    )
                except Exception:
                    logger.warning(
                        "short-video grounding failed; proceeding ungrounded",
                        exc_info=True,
                    )
                    pack = ""
                if pack:
                    system_context = (
                        f"{system_context}\n\n"
                        "【联网事实资料包】\n"
                        f"{pack}\n\n"
                        "事实规则：正文里的具体年份、数字、人物生卒、事件归属、机构名称，"
                        "必须来自上面的资料包；资料包没有支撑的说法只能写成推测或删除。\n"
                        f"{fun_note}"
                        f"{self._strict_grounding_script_rules(domain)}\n"
                    )

        system = self._build_script_system_prompt(
            output_format, duration, system_context, funny=funny, speed=speed,
            tts_provider=tts_provider, output_language=output_language,
        )
        # 出稿:writer_model 已按"是否需要自联网"选好(:online 自搜 / 普通模型)。
        # 联网事实(资料包或自搜要求)已注入 system_context。失败回退原路由。
        if writer_model and has_or_key:
            try:
                return self._script_via_openrouter_direct(system, prompt, writer_model)
            except Exception:
                logger.warning(
                    "direct OpenRouter script (%s) failed; falling back to router",
                    writer_model, exc_info=True,
                )
        # purpose="script" → 短视频/单次产稿走联网 model 路由(见 _call_openrouter)。
        return self._call(system, prompt, purpose="script")

    #     403 "This model is not available in your region"
    _REGION_BLOCKED_PREFIXES: tuple[str, ...] = ()

    def _script_via_gateway(self, system: str, user: str, model: str) -> str:
        """

        出片流程里是认证过的;离线跑没 token 会抛错,交给上层兜底。
        """
        import asyncio as _asyncio
        import uuid as _uuid

        from backend.lib.cloud_gateway import get_default_gateway

        gw = get_default_gateway()
        result = _asyncio.run(gw.llm_completion(
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            idempotency_key=str(_uuid.uuid4()),
            model_id=model,
            max_tokens=self._purpose_max_tokens("studio_long"),
        ))
        # 否则这条片子的账上看不到写稿这笔钱(DeepSeek 直连那条漏记过一次)。
        try:
            cost = float(result.get("cost_eur") or 0)
            if cost:
                from backend.services.llm_call_counter import add_active_openrouter_cost
                add_active_openrouter_cost("gateway", model, cost * 1.08)
        except Exception:
            pass
        return self._clean_script_output(str(result.get("content") or ""))

    def _script_via_openrouter_direct(self, system: str, user: str, model: str) -> str:
        """

        """
        import httpx

        if str(model or "").startswith(self._REGION_BLOCKED_PREFIXES):
            return self._script_via_gateway(system, user, model)
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
                "usage": {"include": True}},
            timeout=180,
        )
        # 自愈:万一冒出一个没列进 _REGION_BLOCKED_PREFIXES 的区域受限模型,
        if r.status_code == 403 and "region" in (r.text or "").lower():
            logger.warning("直连 %s 吃到区域 403,改走云网关", model)
            return self._script_via_gateway(system, user, model)
        r.raise_for_status()
        data = r.json()
        # 记账绝不打断出片:任何异常都吞掉。
        try:
            cost = (data.get("usage") or {}).get("cost")
            if cost:
                from backend.services.llm_call_counter import add_active_openrouter_cost
                add_active_openrouter_cost("openrouter_direct", model, float(cost))
        except Exception:
            pass
        return self._clean_script_output(
            data["choices"][0]["message"]["content"]
        )



    def _clean_script_output(self, text: str) -> str:
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"```(?:\w+)?", "", cleaned).replace("```", "")
        cleaned = re.sub(r"(?im)^\s*#{1,6}\s*", "", cleaned)
        cleaned = re.sub(
            r"(?im)^\s*(?:第\s*\d+\s*稿|约\s*\d+\s*字|10\s*分钟口播脚本|口播脚本)\s*[：:·\-—]*\s*",
            "",
            cleaned,
        )
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def translate_script(self, text: str, target_language: str) -> str:
        """出片前把已定稿的旁白脚本翻成目标语言(给 TTS 朗读)。
        保留段落/句界与终止标点(一句对一句),让下游拆句 + 时间戳照常工作。
        走统一路由器 purpose="translate"(OpenRouter),不联网。"""
        if not text or not text.strip():
            return text
        lang_name = {"en": "English", "zh": "Chinese"}.get(
            str(target_language or "").lower(), "English"
        )
        system = (
            f"You are a professional video-narration translator. Translate the script "
            f"into natural, spoken {lang_name} for a TTS voiceover. Rules: "
            "(1) Preserve paragraph breaks and sentence boundaries EXACTLY — one source "
            "sentence maps to one target sentence; keep terminal punctuation (. ! ?). "
            "(2) Output ONLY the translated script — no titles, notes, markdown, quotes, "
            "or explanations. (3) Make it fluent for the ear, not word-for-word literal. "
            "(4) Do not add, drop, merge, or reorder sentences."
        )
        return self._clean_script_output(self._call(system, text, purpose="translate"))

    def translate_titles(self, titles: list[str], target_language: str = "en") -> list[str]:
        """Translate a batch of short topic TITLES in ONE call (1:1, order preserved).
        Used so an English-UI user sees English topic recommendations while the channel
        rules / dedup keep running on the canonical Chinese internally. Degrades safely:
        any failure / count mismatch returns the original titles unchanged."""
        clean = [str(t or "").strip() for t in (titles or [])]
        if not any(clean):
            return list(titles or [])
        lang_name = {"en": "English", "zh": "Chinese"}.get(
            str(target_language or "").lower(), "English"
        )
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(clean))
        system = (
            f"You translate short video TOPIC TITLES into natural, catchy {lang_name}. "
            "Keep each title punchy and faithful (a question stays a question). Translate "
            "every numbered item, preserving order and count. Output ONLY a JSON array of "
            "strings, e.g. [\"...\", \"...\"] — no numbering, keys, or commentary."
        )
        try:
            raw = self._call(system, numbered, purpose="translate") or ""
            start, end = raw.find("["), raw.rfind("]")
            if start == -1 or end == -1 or end <= start:
                return list(titles or [])
            arr = json.loads(raw[start:end + 1])
            out = [str(x).strip() for x in arr]
            if len(out) != len(clean) or not all(out):
                logger.warning("translate_titles count mismatch (%d→%d); keeping originals",
                               len(clean), len(out))
                return list(titles or [])
            return out
        except Exception:
            logger.warning("translate_titles failed; keeping originals", exc_info=True)
            return list(titles or [])

    def translate_segments(self, segments: list[str], target_language: str = "en") -> list[str]:
        """Translate a batch of SUBTITLE blocks in ONE call (1:1, order preserved).

        Used for bilingual / cross-language subtitles: each on-screen caption block is
        translated independently so the Chinese and English break at the SAME
        punctuation boundary (never «half a Chinese sentence» paired with «half an
        English one»). Degrades safely — any failure or count mismatch returns the
        originals unchanged, so a subtitle keeps its source language rather than going
        out of sync with the audio. Same contract as translate_titles, tuned for
        spoken captions instead of topic titles."""
        clean = [str(s or "").strip() for s in (segments or [])]
        if not any(clean):
            return list(segments or [])
        lang_name = {"en": "English", "zh": "Chinese"}.get(
            str(target_language or "").lower(), "English"
        )
        system = (
            f"You translate short VIDEO SUBTITLE lines into natural, spoken {lang_name}. "
            "Each numbered line is ONE on-screen caption — keep it concise and faithful, "
            "matching the register of speech (not literal, not wordy). Translate EVERY "
            "item, preserving order and count EXACTLY (one line in → one line out; never "
            "merge, split, drop, or reorder). Output ONLY a JSON array of strings, e.g. "
            "[\"...\", \"...\"] — no numbering, keys, or commentary."
        )

        def _batch(items: list[str]) -> Optional[list[str]]:
            """One JSON-array call. Returns a same-length list, or None if the model
            merged/dropped items (count mismatch) so the caller can fall back."""
            numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(items))
            try:
                raw = self._call(system, numbered, purpose="translate") or ""
            except Exception:
                logger.warning("translate_segments call failed", exc_info=True)
                return None
            lo, hi = raw.find("["), raw.rfind("]")
            if lo == -1 or hi == -1 or hi <= lo:
                return None
            try:
                arr = json.loads(raw[lo:hi + 1])
            except Exception:
                return None
            out = [str(x).strip() for x in arr]
            return out if (len(out) == len(items) and all(out)) else None

        # Fast path: translate the whole batch in ONE call.
        out = _batch(clean)
        if out is not None:
            return out
        # Batch merged/dropped lines → translate each item on its own so count is
        # GUARANTEED 1:1 (a subtitle must never go out of sync with the audio). A
        # per-item failure keeps that one source line rather than desyncing the rest.
        logger.warning("translate_segments batch count mismatch (%d); per-item fallback", len(clean))
        result: list[str] = []
        for s in clean:
            if not s:
                result.append(s)
                continue
            one = _batch([s])
            result.append(one[0] if one else s)
        return result

    def _long_script_call(self, system: str, user: str) -> str:
        """长片产稿的出口。

        设了 `MEDIA_BUDDY_LONG_WRITER_MODEL` → worker 直连 OpenRouter(和短片同一条
        `purpose=studio_long` 路由(OpenRouter),行为不变。

        ⚠️ 直连失败必须回落原路由 —— 换写手不能变成「出不了片」。
        """
        model = os.environ.get("MEDIA_BUDDY_LONG_WRITER_MODEL", "").strip()
        if model and os.environ.get("OPENROUTER_API_KEY"):
            try:
                return self._script_via_openrouter_direct(system, user, model)
            except Exception:
                logger.warning(
                    "long-script direct OpenRouter (%s) failed; 回落原路由",
                    model, exc_info=True,
                )
        return self._clean_script_output(self._call(system, user, purpose="studio_long"))
    def _generate_long_form_script(
        self,
        prompt: str,
        *,
        duration_hint_seconds: int,
        system_context: str = "",
        speed: float = 1.0,
    ) -> str:
        # 目标字数随语速(3.3 字/秒 = 1.0x 基准;×speed:语速快就多写、成片才够时长)。
        speed = max(0.7, min(2.0, float(speed or 1.0)))
        target_chars = max(2200, min(15000, int(duration_hint_seconds * 3.3 * speed)))
        min_chars = int(target_chars * 0.82)
        max_chars = int(target_chars * 1.18)
        grounding_pack = self.build_research_pack(
            prompt,
            context=system_context,
            strict=self.grounding_required(prompt, system_context),
        )
        if grounding_pack:
            domain = self.research_domain(prompt, system_context)
            system_context = (
                f"{system_context}\n\n"
                "【联网事实资料包】\n"
                f"{grounding_pack}\n\n"
                "事实规则：正文里的具体年份、数字、研究结论、动物行为、机构名称，"
                "必须来自上面的资料包；资料包没有支撑的说法只能写成推测或删除。\n"
                f"{self._strict_grounding_script_rules(domain)}\n"
            )
        system = (
            "你是中文长视频编导，只写适合配音直接朗读的完整口播文稿。\n"
            "这是长视频，不是短视频。不要输出提纲，不要输出总结说明，不要问用户确认。\n"
            f"目标时长约 {duration_hint_seconds // 60} 分钟，正文长度必须在 {min_chars}-{max_chars} 个中文字符之间。\n"
            "结构要求：开场钩子、背景铺垫、3-5 个递进段落、一个中段反转或留存点、实用或认知收束、自然结尾。\n"
            "每一段都要围绕用户给出的频道定位和主题，不要串台。比如野生动物频道里的“食物链”指生态捕食关系，不是厨房做饭。\n"
            "只输出正文。不要标题、不要 markdown、不要分镜、不要时间码、不要括号说明。\n"
            "语言要像真人解说，具体、有画面感，信息密度递进。\n"
            f"{system_context}"
        )
        user = (
            f"{prompt}\n\n"
            "请直接写完整长视频口播正文。第一句话就进入主题，正文必须足够长。"
        )
        script = self._long_script_call(system, user)
        if len(script) >= min_chars:
            return script

        expand_system = (
            "你是中文长视频编导。下面的稿子太短，必须扩写成完整长视频口播正文。\n"
            f"扩写后正文长度必须在 {min_chars}-{max_chars} 个中文字符之间。\n"
            "保留原主题和频道定位，补足案例、细节、过渡和收束。不要标题、不要 markdown、不要时间码。"
        )
        expanded = self._long_script_call(
            expand_system,
            f"原始需求：\n{prompt}\n\n太短的稿子：\n{script}\n\n请扩写成完整正文。",
        )
        return expanded if len(expanded) > len(script) else script

    def _build_script_system_prompt(
        self, output_format: str, duration: int, context: str, funny: bool = False,
        speed: float = 1.0, tts_provider: str = "", output_language: str = "zh",
    ) -> str:
        fmt_map = {
            "youtube_landscape": "16:9 landscape YouTube video",
            "youtube_shorts": "vertical 9:16 YouTube Short under 60 seconds",
            "tiktok": "vertical 9:16 TikTok video",
            "instagram_reels": "vertical 9:16 Instagram Reel",
        }
        fmt = fmt_map.get(output_format, "standard video")
        # 口播语速与目标字数 —— 全部出自 backend.lib.tts_pacing(唯一出处)。
        # 原来这里写死 3.5 字/秒,那是照 ElevenLabs 标的;生产实际跑千问音色,念得快
        # 预算偏小而把故事讲一半就收笔。现在按音色查表。
        from backend.lib.tts_pacing import (
            beat_budget,
            short_script_char_range,
            short_script_target_chars,
        )

        speed = max(0.7, min(2.0, float(speed or 1.0)))
        target_chars_zh = short_script_target_chars(duration, tts_provider, speed)
        zh_lo, zh_hi = short_script_char_range(duration, tts_provider, speed)
        target_words_en = int(duration * 2.1 * speed)

        # ── 输出语言 ────────────────────────────────────────────────
        # 英文原来走"中文稿→翻译→事后凝练",那正是我们从中文路径拆掉的模式:
        # 先生成、再发现不对、再事后压。而且**翻译天然摧毁长度控制** ——
        # 中文字数→英文词数的比例随内容浮动,上游控得再准,过一次翻译就散。
        # (不管要 170 还是 139 词,都停在 195-203)。
        # 所以 lang=en 时**直接用英文写**,长度回到写稿时决定,和中文同一条原则。
        # env MB_NATIVE_EN=0 可退回老路(中文稿 + 翻译)。
        _native_en = (str(output_language or "zh").lower() == "en"
                      and os.environ.get("MB_NATIVE_EN", "1") != "0")
        if _native_en:
            from backend.lib.tts_pacing import english_target_words

            _en_target = english_target_words(duration, tts_provider, speed)
            lang_block = (
                "\n\n⚠️ OUTPUT LANGUAGE (highest priority — overrides any Chinese in "
                "the channel brief, research pack or examples above):\n"
                "Write the entire narration in **natural, spoken English**. The brief "
                "and research material may be in Chinese: read them, but write English. "
                "Do NOT write Chinese, and do NOT translate literally — write it the way "
                "an English-speaking narrator would actually say it.\n"
                f"⚠️ LENGTH IS COUNTED IN WORDS, NOT CHARACTERS: about {_en_target} words "
                f"(range {int(_en_target * 0.93)}-{int(_en_target * 1.07)}). "
                "This replaces the Chinese character target stated above.\n"
            )
        else:
            lang_block = (
                "\n\n⚠️ 输出语言(最高优先级,压过以上任何频道定位/资料/示例/模板的语言):\n"
                "必须用【简体中文】撰写全部旁白文案。无论频道定位、参考资料、上文示例是英文还是其它语言,\n"
                "旁白一律输出简体中文;人名/地名/专有名词可保留原文,但句子主体必须是中文。\n"
                "(系统会在需要时把中文成稿翻成其它目标语言,所以你这一步只产出中文。)\n"
            )

        # 结构预算(M2):只给一个总字数,模型会把额度花在铺陈上,到结尾没钱了 ——
        # 那正是断尾的机制。所以把总额拆成节拍,并给落点划出**不许挪用**的专属额度。
        # 纯确定性计算,零新增 LLM 调用;省钱靠的是"第一次就写对,不进修复轮"。
        # env MB_STRUCT_BUDGET=0 可退回纯字数 prompt(回滚只需改配置)。
        struct_block = ""
        if os.environ.get("MB_STRUCT_BUDGET", "1") != "0":
            b = beat_budget(duration, tts_provider, speed)
            struct_block = (
                f"⚠️ 结构预算(按这个分配写,别把额度花光在中段):\n"
                f"  全篇约 {b['sentences']} 句、{b['total_chars']} 字,分三段:\n"
                f"  · 钩子 {b['hook']['sentences']} 句 ≈ {b['hook']['chars']} 字\n"
                f"  · 正文 {b['body']['sentences']} 句 ≈ {b['body']['chars']} 字\n"
                f"  · 落点 {b['landing']['sentences']} 句 ≈ {b['landing']['chars']} 字"
                f" ← **此额度为结尾预留,不许挪用到中段**\n"
                f"  写中段时就要记着:必须给落点留出 {b['landing']['chars']} 字。\n"
                f"  论据讲不完就删掉一个,绝不占用落点的额度。\n\n"
            )
        # 「诙谐幽默」风格(仅当用户在"风格"里选了它)才注入这套搞笑配方;其余风格保持原样。
        fun_block = (
            "🎮 诙谐幽默风格(最高优先级,本次核心风格,压过下面除安全/语言外的任何要求):\n"
            "像一个很会讲故事、很有网感的朋友在跟你唠嗑,听着轻松好玩、停不下来,一遍就听懂。做到:\n"
            "1. 一条反差/悬念主线讲到底:全篇拢在一个清晰的反差或悬念上(例:熊猫骨子里是食肉杀手,却被基因和肠胃封印成素食者),所有事实都为这条主线服务,跑题的枝节一律删。\n"
            "2. 黄金3秒直接开讲:开场第一句就甩出最反差/最颠覆的事实或画面本身,一句话勾住人。"
            "【绝对禁止】用'警告''警告啊''你敢信''千万别''你绝对想不到'这类套话或废话起手——"
            "上来就讲内容,别浪费开头。\n"
            "3. 【硬事实/数字/术语,立刻配一个生活化比喻或梗,翻译成人话】(本风格的灵魂):基因失效=味蕾被拔了网线,吃再贵的和牛也跟嚼硬纸板一样;消化率只有17%=好比用顶配电脑去扫雷,大材小用还费电。绝不把数字/术语干甩出来让观众自己消化,甩了=失败必重写。\n"
            "4. 网感口语+适度玩梗+拟人调侃:大白话、网络化表达、把主体拟人化吐槽(嘤嘤怪/外挂被封号/傲娇/低功耗模式/主打一个…),但梗要服务理解,别为玩梗而玩梗。\n"
            "5. 语气跟着内容起伏:悬念→八卦→揭秘→吐槽→走心,不是一条直线念稿。\n"
            "6. 结尾抛一个好玩、能引发评论的开放问题或脑洞,像跟朋友互动,不是套路CTA。\n"
            "讲法示范(只学这个把硬知识翻译成人话的味儿,绝不照搬熊猫这些内容到别的题材):'它长着狮子的牙,却啃了400万年竹子。'(反差钩子);'基因一坏,等于味蕾被拔了网线,吃再贵的和牛也跟嚼硬纸板一样。'(术语+数据配比喻)。\n"
            "唯一判据:观众听一遍觉得有意思、听懂了、想转发,而不是信息量好大、有点懵。\n\n"
        ) if funny else ""
        # Output format hints which language is more likely (cn-prefixed for Chinese)
        return (
            f"You are a viral short-form video script writer.\n"
            f"Write a narration script for a {fmt}, EXACTLY {duration} seconds long.\n"
            f"\n"
            f"⚠️ HARD LENGTH CONSTRAINT:\n"
            f"  - Chinese (中文): {target_chars_zh} 字 (range {zh_lo}-{zh_hi})\n"
            f"  - English: {target_words_en} words (range {int(target_words_en*0.85)}-{int(target_words_en*1.15)})\n"
            f"Out of range = rewrite. NEVER stop early just because 'this feels enough'.\n"
            f"\n"
            # 与字数同等硬性 —— 这条原来埋在几十行规则中间,而字数约束在最顶上、全大写、
            # 还带"超范围就重写",模型自然优先服从字数,写到预算就收笔,故事讲一半
            f"⚠️ HARD STORY-CLOSURE CONSTRAINT(与字数约束同等硬性,违反必须重写):\n"
            f"  - 开头/中途抛出的核心疑问,正文里**必须**给出答案或明确的解释方向,不能悬而不决。\n"
            f"  - 写了'最关键的是X''最厉害的一招是X''真正的原因是X'这类预告,后面**必须**把 X\n"
            f"    展开讲透;只带一句或只举一个例子就收尾 = 断尾,必须重写。\n"
            f"  - 最后一句必须是落点:要么给出结论,要么给一个建立在正文之上的开放问题。\n"
            f"    严禁以'怎么做？''加多少？''什么时候加？'这类问完就没下文的提问结尾。\n"
            f"  - 字数和收口冲突时:**砍掉一个论据,也要留出把故事收口的篇幅**。\n"
            f"    宁可少讲一个例子,绝不交一个没讲完的故事。\n"
            f"\n"
            f"{struct_block}"
            f"{fun_block}"
            f"⚠️ HOOK RULES (sentence 1):\n"
            f"  ✅ Question / Contrast / Data / Suspense / Sensory image\n"
            f"  ❌ FORBIDDEN openers: 大家好, 哈喽, Hi, 今天, 在这个, Hello/Welcome\n"
            f"  ❌ NEVER start with 'We…' / '我们…'\n"
            f"\n"
            f"⚠️ EVERY sentence must contain a CONCRETE noun / action / number / sense detail —\n"
            f"  but every detail MUST belong to THIS topic. Turn '我们追求高品质' (abstract) into a\n"
            f"  real, filmable detail FROM YOUR OWN SUBJECT (a true noun / action / number / sensation).\n"
            f"  🚫 致命错误(必重写):绝不把与本主题无关的具体细节写进稿子。比如主题是肠道/神经/动物/\n"
            f"  历史时,绝不能冒出'咖啡''云南''手摘豆''海拔1800米''拉花'这类别的题材的细节——它们\n"
            f"  只是别处的'抽象→具体'示范,不是你的内容。串入无关题材的具体名词/数字会摧毁专业可信度。\n"
            f"\n"
            f"⚠️ BANNED WORDS (rewrite if any appear):\n"
            f"  高级感, 氛围感, 极致, 用心, 匠心, 情怀, 完美, 顶级, 至臻,\n"
            f"  '我们坚信', '我们致力于', '我们一直', 打造, 享受, 品质生活,\n"
            f"  premium feel, top-tier, ultimate, craftsmanship (without specifics)\n"
            f"\n"
            f"⚠️ CTA must be EXECUTABLE and NON-PROMISSORY:\n"
            f"  ✗ 'follow us / 欢迎关注'\n"
            f"  ✗ 'Comment X, N random commenters get a free gift' (任何送礼/抽奖式 CTA)\n"
            f"  ✗ '评论区留言，N 位随机评论者将获赠奖品'\n"
            f"  ✗ 套路化 AI 腔结尾:'我下条继续拆' / '我们下期见' / '记得点赞关注' / 'follow us' —— 一律禁止\n"
            f"  ✓ 用一句和内容直接相关、自然口语的开放式问题或反问收尾,像跟朋友聊天;也可以用一个有余味的结论句收尾,不强求 CTA\n"
            f"  结尾绝不能像 AI 写的、绝不能套模板,不要出现'关注/点赞/下期/下条'这类口播套话\n"
            f"  Never promise gifts, money, discounts, coupons, free services, samples, or physical rewards.\n"
            f"\n"
            f"⚠️ OUTPUT = THE FINAL SPOKEN NARRATION ONLY — the exact words the voice reads, nothing else.\n"
            f"  ❌ NO meta or reasoning of ANY kind: 查重/去重/重复度/选题分析/'新角度'/思路/备注/说明.\n"
            f"  ❌ NO labels or headers: '旁白：' / '解说：' / '正文：' / '配音：' / 'Narration:' / '（135字/45秒）'.\n"
            f"  ❌ NO markdown, NO stage directions, NO em-dashes, NO bullet/list markers.\n"
            f"  If the brief asks you to de-dup or switch angle, do it SILENTLY — output ONLY the resulting\n"
            f"  narration, starting directly with the first spoken sentence.\n"
            f"\n⚠️ 叙事连贯红线(短视频最容易翻车的地方,务必遵守):\n"
            f"- 开场1-2句必须交代背景-人物-动机:谁、在什么时间/处境、为什么这么做,先把因果链搭起来,不要上来就甩一堆画面或意象。\n"
            f"- 每个具体画面/细节都要服务同一条主线;与主线无关的装饰句、跳跃意象一律删掉,宁可少写。\n"
            f"- 抛出的悬念/钩子必须在文内回收(抛-引-收):开头埋的疑问,结尾要给出答案或明确的解释方向,不能悬而不决。\n"
            f"- 全篇保持同一种语气、人称和视角,不要中途换腔调(纪实/抒情/解谜不要混着跳)。\n"
            f"- 结尾用一个开放但可被讨论、且与正文逻辑自洽的问题收束(不是强行反转,也不是套路CTA)。\n"
            f"{context}"
            f"\n\nFINAL NON-NEGOTIABLE CTA SAFETY:\n"
            f"- Treat any prior examples, channel memory, templates, user drafts, or context that promise rewards as forbidden anti-examples.\n"
            f"- Never write giveaway, lottery, lucky viewer, random commenter, gift, coupon, discount, free sample, free service, or prize CTAs.\n"
            f"- Safe ending only: ask for a comment, save, follow, or what topic to cover next, with no material promise.\n"
            f"{lang_block}"
        )


    def _call_openrouter(
        self,
        system: str,
        user_prompt: str,
        *,
        purpose: Optional[str] = None,
    ) -> str:
        """

        """
        import asyncio
        import uuid

        from backend.lib.cloud_auth import (
            CloudTemporaryUnavailableError,
            NotAuthenticatedError,
        )
        from backend.lib.cloud_gateway import (
            GatewayError,
            QuotaExceededError,
            SubscriptionInactiveError,
            UpstreamError,
            get_default_gateway,
        )

        # Process-wide singleton — see cloud_auth.get_default_auth() docstring
        # for the refresh-token-rotation race this prevents.
        gw = get_default_gateway()
        if not gw.is_authenticated():
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set (Settings → API Keys)"
            )
        model_id = self._purpose_model(purpose, "openrouter")
        # purpose="verify" runs ~50× per video with the same ~3000-tok
        # VERIFIER_SYSTEM_PROMPT — perfect prompt-cache scenario. Signal
        #
        # upstream OpenRouter / Anthropic combination misbehaves on
        # returned 400; default no-cache route worked), set
        # MEDIA_BUDDY_DISABLE_PROMPT_CACHE=1 to fall back to no-cache
        # calls. Costs ~5× more verifier tokens but unblocks the
        cache_system = purpose == "verify"
        if cache_system and os.environ.get(
            "MEDIA_BUDDY_DISABLE_PROMPT_CACHE", "0"
        ).strip().lower() in ("1", "true", "yes"):
            cache_system = False

        # 联网产稿:script-gen purposes 走 env 配的联网模型(如
        # `qwen/qwen3-235b-a22b-2507:online`,OpenRouter 自动联网搜索→注入→产稿)。
        # env 未设 → use_online False → 行为与从前完全一致(零变更)。
        online_model = os.environ.get("MEDIA_BUDDY_SCRIPT_ONLINE_MODEL", "").strip()
        use_online = bool(online_model) and (purpose in ("studio_long", "script"))
        ran_model = online_model if use_online else model_id

        def _do(mid: Optional[str]):
            return asyncio.run(gw.llm_completion(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                idempotency_key=str(uuid.uuid4()),
                model_id=mid,
                max_tokens=self._purpose_max_tokens(purpose),
                cache_system=cache_system,
                project_id=self.project_id,
            ))

        try:
            try:
                result = _do(ran_model)
            except (UpstreamError, GatewayError) as e:
                if use_online and ran_model != model_id:
                    logger.warning(
                        "online model %s failed (%s); falling back offline → %s",
                        ran_model, e, model_id,
                    )
                    ran_model = model_id
                    result = _do(model_id)
                else:
                    raise
        except CloudTemporaryUnavailableError:
            raise
        except NotAuthenticatedError as e:
            raise RuntimeError(f"provider key missing or rejected: {e}") from e
        except QuotaExceededError as e:
            raise RuntimeError(str(e)) from e
        except SubscriptionInactiveError as e:
            raise RuntimeError(f"subscription inactive: {e.payload}") from e
        model_id = ran_model
        # usage.total_cost as `cost_eur`. This makes the LLMCallCounter summary
        # exact instead of the ~4-8x-low per-call estimate (the source of the
        # "cost numbers don't match the bill" gap).
        try:
            from backend.services.llm_call_counter import bump_active_real_cost
            cost_eur = float(result.get("cost_eur") or 0)
            if cost_eur:
                bump_active_real_cost(
                    purpose or "unknown", model_id or "_unknown_", cost_eur / 0.93,
                )
        except Exception:
            pass
        return result["content"]


# ===========================================================================
# 视觉看片统一入口(blind_observe / observer_judge 用)。文本路由器是
# ===========================================================================




def vision_completion(messages: list, *, max_tokens: int, idempotency_key: str,
                      gateway, gateway_model_id: str) -> dict:
    """多模态看片调用,走 OpenRouter 的多模态模型。
    同构 → blind_observe / observer_judge 的下游解析无需改。"""
    fallback_messages = messages
    import asyncio as _asyncio
    result = _asyncio.run(gateway.llm_completion(
        messages=fallback_messages, idempotency_key=idempotency_key,
        max_tokens=max_tokens, model_id=gateway_model_id,
    ))
    if isinstance(result, dict):
        result.setdefault("model", gateway_model_id)
    return result
