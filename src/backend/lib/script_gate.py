"""文字验收闸 —— 长度与完整性的检查和修正,全部发生在文字阶段。

## 为什么有这个模块

举一个例子就结束,标题问的问题自始至终没回答。排查发现两件事:

1. 把关的质检只查空稿/纯CTA/句数/字数,**从不检查故事是否收口**,断尾稿全部放行;
2. 第一版收口逻辑"抓得到、修不好"——重写时**没把原稿传给模型**,等于从零再编一遍,

本模块的核心就是**定点修复**:把完整原稿交给模型,只动该动的部分。

## 流程

    体检(长度算术 + 收口两层)→ 分诊 → 修复 → 复检 → 终态裁决

## 硬性约束

  复检那次调用**同时承担**"正文有没有被塞进新事实"的校验,省掉单独一次。
  唯一例外:出厂稿仍判 `repair_long` 时,长度保险丝再花 2 次(≤ 7)——
- **fail-open**(P3):判断器报错、修复被拒、超轮次,最终都必须出片,
  最坏情况不得劣于"没有这道闸"。
- **绝不退模板稿**(P4):断尾但有真材实料的原稿 > 通用模板稿。本模块内没有任何
  模板兜底路径。
- **不写死模型名**(P7):一律走 `purpose="closure"` 档。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.lib.closure_standards import closure_rule_for, dangling_pattern_hit
from backend.lib.tts_pacing import count_chars, estimate_seconds, length_verdict

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 3      # 疗程1 定点修复 / 疗程2 受控重写 / 疗程3 降需求重写
_PURPOSE = "closure"
# 给长度保险丝预留的调用额度(修复 1 + 复检 1)。
_FUSE_RESERVE = 2

# "只改结尾"场景允许正文缩水多少(为容纳新结尾顺带微压)。超过即判为偷偷重写全文。
_BODY_SHRINK_TOLERANCE = 0.15
# "只改结尾"场景正文需保留的相似度下限(按字符前缀比对)。
_BODY_PREFIX_RATIO = 0.70


@dataclass
class GateCheck:
    """一次体检/复检的结构化记录。P6:禁止静默,每一步都要留痕。"""
    round: int
    version: str
    length: str
    closure_ok: bool
    closure_reason: str = ""
    source: str = "judge"          # pattern | judge | skipped | error
    predicted_seconds: float = 0.0


@dataclass
class GateResult:
    final_script: str
    chosen_version: str
    chosen_predicted_seconds: float
    rounds_used: int = 0
    checks: list[GateCheck] = field(default_factory=list)
    degraded: bool = False
    llm_calls: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    # 终态失败归因。"" = 一切正常;否则要能区分是**收口没修好**还是**长度压不下来**,
    # 两者的后续动作完全不同(前者调 prompt/模型,后者调阈值/预算)。
    failure_reason: str = ""


def gate_cost_usd() -> float:
    """本次任务里**闸这一段**花了多少钱(只算 closure 档)。

    不新造统计:项目已有 LLMCallCounter 按 purpose×model 记账,`_call` 自动 bump。
    这里只把 closure 那一片切出来 —— 别把写稿那次(贵得多)算到闸头上。
    取不到 counter(桌面单机/测试)返回 0,绝不抛。
    """
    try:
        from backend.services.llm_call_counter import get_active_counter

        counter = get_active_counter()
        if counter is None:
            return 0.0
        agg = counter.summary().get("aggregate_by_purpose_and_model", {}) or {}
        total = 0.0
        for key, entry in agg.items():
            if str(key).startswith(f"{_PURPOSE}:"):
                total += float((entry or {}).get("est_cost") or 0.0)
        return round(total, 6)
    except Exception:  # noqa: BLE001 — 记账永远不许影响出片
        logger.warning("gate cost slice failed", exc_info=True)
        return 0.0


def _enabled(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default) != "0"


# ── 分诊 ────────────────────────────────────────────────────────────

def triage(length: str, closure_ok: bool) -> str:
    """(长度判定 × 收口判定)→ 修复指令类型。一次修复解决所有问题。"""
    too_long = length == "repair_long"
    too_short = length == "repair_short"
    if closure_ok:
        if too_long:
            return "compress_middle"
        if too_short:
            return "extend_middle"
        return "pass"
    if too_long:
        return "compress_and_fix_ending"
    if too_short:
        return "extend_and_fix_ending"
    return "fix_ending"


def _sentences(text: str) -> list[str]:
    s = re.sub(r"\s+", "", str(text or ""))
    parts = re.findall(r"[^。！？!?…]*[。！？!?…]+|[^。！？!?…]+$", s)
    return [p for p in (p.strip() for p in parts) if p]


# ── 结构化定点修复(P11:修改即隔离)────────────────────────────────
# 原来让模型返回整篇稿子、再用字符守卫查它有没有乱动正文。
# 现在改成**模型只返回它该改的那一段,其余由代码保管拼接** ——
# 改结尾时它拿不到重写正文的机会;压正文时它根本看不到结尾。
# 用结构消除错误,而不是用检查发现错误。

_PAYOFF_SENTENCES = 2       # §5 结构化生成落地前的确定性切分:末 2 句 = 落点
# 与 tts_pacing 的字/秒标定同源思路:按实际行为校准,不假设模型听话。
COMPRESS_OVERSHOOT_COMPENSATION = 0.78


def split_script(text: str, prof=None) -> tuple[str, str, str]:
    """把稿子切成 (钩子, 正文, 落点)。

    §5 的结构化生成落地前,用确定性切分顶上:首句=钩子,末 2 句=落点,中间=正文。
    三段拼回去必须与原文逐字相同(去空白后)——拼接由代码做,不能丢字。
    """
    if prof is None:
        from backend.lib.lang_profile import profile_for
        prof = profile_for("zh")
    sents = prof.split_sentences(text)
    j = prof.join
    if len(sents) <= 1:
        return j.join(sents), "", ""
    if len(sents) <= _PAYOFF_SENTENCES:
        return sents[0], "", j.join(sents[1:])
    return (sents[0], j.join(sents[1:-_PAYOFF_SENTENCES]),
            j.join(sents[-_PAYOFF_SENTENCES:]))


def _lang_pin_suffix(prof) -> str:
    """把"用什么语言写"钉进 prompt。

    修复指令本身是中文写的。改中文稿没问题,**改英文稿就有风险** ——
    模型很可能顺着指令的语言用中文回,把一篇英文稿改成中文。
    中文档案返回空串,所以中文的提示词逐字不变。
    """
    pin = getattr(_prof_or_zh(prof), "lang_pin", "")
    return "\n" + pin if pin else ""


def _prof_or_zh(prof):
    """拿不到档案就按中文 —— 它占 95% 的产量,是安全的默认。"""
    if prof is not None:
        return prof
    from backend.lib.lang_profile import profile_for
    return profile_for("zh")


def repair_replace_tail(script: str, reason: str, rule: str, ask, prof=None) -> Optional[str]:
    """只换结尾。模型见全文,但**只有结尾的写入权**。

    返回拼好的完整稿;模型返回不可解析/空 → None(本轮修复算失败)。
    """
    system = (
        "你是短视频口播稿的结尾修改者。你**只能改写结尾**,正文由系统保管,"
        "你返回的任何其它字段都会被丢弃。\n"
        f"【本频道的收口标准——你会被按它评判】\n{rule}\n\n"
        "要求:\n"
        "1. 结尾必须是落点:给出结论,或给一个**建立在正文已有信息之上**的开放问题;\n"
        "2. **不得提出任何新问题、不得增加新的悬念钩子** —— 上一版就是栽在这里"
        "(修完反而多出两个没回答的问题);\n"
        "3. 可以引用学界公认/主流的解释,但不得编造具体人名、年份、数字;\n"
        '只输出 JSON:{"replace_last_sentence_count": 1或2, "replacement_tail": "新的结尾"}'
        + _lang_pin_suffix(prof)
    )
    user = f"复检判词:{reason or '故事没有落点'}\n\n【全文】\n{script}"
    try:
        data = _parse_json_obj(ask(system, user))
    except Exception:  # noqa: BLE001
        logger.warning("replace_tail call failed", exc_info=True)
        return None
    if not data:
        return None
    tail = str(data.get("replacement_tail") or "").strip()
    if not tail:
        return None
    try:
        n = int(data.get("replace_last_sentence_count") or _PAYOFF_SENTENCES)
    except (TypeError, ValueError):
        n = _PAYOFF_SENTENCES
    n = max(1, min(3, n))
    prof = _prof_or_zh(prof)
    sents = prof.split_sentences(script)
    kept = prof.join.join(sents[:-n]) if len(sents) > n else ""
    if kept and prof.join:
        kept += prof.join
    # 只有 kept + tail 会出厂 —— 模型即使夹带 full_script 也进不来。
    return (kept + tail).strip() or None


def repair_body(
    script: str, instruction: str, target_body_chars: int, rule: str, ask, prof=None,
) -> Optional[str]:
    """只改正文。**钩子和落点根本不出现在 prompt 里**,模型无从改坏它们。"""
    hook, body, payoff = split_script(script, prof)
    if not body:
        return None
    if instruction == "compress_body":
        # 所以按补偿后的数去要 —— 和字/秒标定同一个道理:
        # **按模型的实际行为校准,而不是假设它听话**。
        target_body_chars = max(20, int(target_body_chars
                                        * COMPRESS_OVERSHOOT_COMPENSATION))
    action = ("压缩" if instruction == "compress_body" else "扩充")
    extra = (
        "删掉一个次要论据即可,不要每句都削。" if instruction == "compress_body"
        else "按原稿已有信息补一个论据或例子,**禁止靠重复句子凑字数**。"
    )
    system = (
        f"你是短视频口播稿的正文修改者。下面只给你**正文段**——"
        f"开头和结尾由系统保管,不需要你返回,也不要提及。\n"
        f"任务:把正文{action}到约 {target_body_chars} {_prof_or_zh(prof).unit}"
        f"(不含空白)。{extra}\n"
        "不得编造具体的人名、年份、数字或事件细节。\n"
        "只输出改好的正文本身,不要任何说明、引号或标记。"
        + _lang_pin_suffix(prof)
    )
    try:
        out = str(ask(system, f"【正文】\n{body}") or "").strip()
    except Exception:  # noqa: BLE001
        logger.warning("body repair call failed", exc_info=True)
        return None
    if not out:
        return None
    j = prof.join if prof is not None else ""
    return j.join(x for x in (hook, out, payoff) if x)


def pick_final(candidates, target_seconds: float):
    """终态选优。**v1 没有特权** —— 它只是候选之一。

    排序:
      0. 资格线:编造事实一票否决(内容错了,再好看也不能出);
      1. 收口合格者优先(P2 收口 > 长度);
      2. 同档内,时长最接近内部目标者胜。

    旧逻辑是"修不好就退回 v1",那等于把一版**明明更好**的稿子丢掉 ——
    """
    pool = [c for c in (candidates or []) if not c.get("new_facts")]
    if not pool:
        pool = list(candidates or [])
    if not pool:
        return None
    return min(
        pool,
        key=lambda c: (
            0 if c.get("closure_ok") else 1,
            abs(float(c.get("predicted") or 0) - float(target_seconds or 0)),
        ),
    )


def treatment_prompt(
    level: int, script: str, reason: str, rule: str, target_chars: int, prof=None,
) -> str:
    """疗程 2/3 的重写 prompt。**每级必须换方法**(P10 禁止同方法同参数重试)。

    疗程 2 = 受控重写:同资料包重开一稿,但把**上版死因**写进去 ——
             不写死因就是换汤不换药,模型不知道自己错在哪。
    疗程 3 = 降需求重写:只讲一个问题答一个问题,目标降到 0.85×,
             宁可内容少一点,也要把这一个讲完整。
    """
    if level >= 3:
        low = int(target_chars * 0.85)
        return (
            "上两版都没能把故事讲完。现在**降低要求**重写一稿:\n"
            "1. **只讲一个问题、只答这一个问题** —— 不要面面俱到;\n"
            f"2. 目标约 {low} {_prof_or_zh(prof).unit}"
            "(比原目标更短,宁可内容少也要讲完整);\n"
            f"3. 收口标准:{rule}\n"
            f"上两版的问题:{reason}。禁止再犯。\n"
            "只输出口播正文。" + _lang_pin_suffix(prof) + "\n\n"
            f"【参考原稿(只取其中的事实,不要照抄结构)】\n{script}"
        )
    return (
        "上一版没能把故事讲完,定点修复也没救回来。请**重写**一稿:\n"
        f"1. 收口标准:{rule}\n"
        f"2. 目标约 {target_chars} {_prof_or_zh(prof).unit};\n"
        "3. 只使用下面原稿里已有的事实,可引用学界公认解释,"
        "但不得编造具体人名、年份、数字;\n"
        f"**上版死因:{reason} —— 本稿必须回答,禁止再犯。**\n"
        "只输出口播正文。" + _lang_pin_suffix(prof) + "\n\n"
        f"【原稿】\n{script}"
    )


def _apply_repair(
    script: str, instruction: str, reason: str, rule: str, target_chars: int, ask,
    prof=None,
) -> Optional[str]:
    """把分诊结论派给对应的隔离式修复器。

    分诊有 5 种,修复器只有 3 个 —— "又超长又断尾"这类合并场景**拆成两步做**:
    先修结尾(收口优先,P2),长度留给下一轮或长度保险丝。
    一次只动一个地方,是隔离能成立的前提;一次改两处又回到"整稿重写"的老路。
    """
    if instruction in ("fix_ending", "compress_and_fix_ending", "extend_and_fix_ending"):
        return repair_replace_tail(script, reason, rule, ask, prof)

    # 只改长度:正文目标 = 总目标扣掉钩子与落点的实际占用
    hook, body, payoff = split_script(script, prof)
    _cnt = prof.count if prof is not None else count_chars
    body_target = max(20, target_chars - _cnt(hook) - _cnt(payoff))
    if instruction == "compress_middle":
        return repair_body(script, "compress_body", body_target, rule, ask, prof)
    if instruction == "extend_middle":
        return repair_body(script, "expand_body", body_target, rule, ask, prof)
    return None


def _parse_json_obj(raw) -> Optional[dict[str, Any]]:
    m = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# ── 字符级守卫(已退役,仅供调试断言)────────────────────────────────
# P11 隔离落地后,"正文逐字不变""结尾逐字不变"由**代码构造**保证,
# 不再需要事后字符比对。保留此函数只为在开发期断言拼接实现没写错;
# **它不再是判废逻辑** —— 当初它存在的理由已经被结构消除,
_GUARD_IS_DEBUG_ONLY = True


def body_preserved(original: str, repaired: str, instruction: str) -> bool:
    """⚠️ 已退役,仅供 debug 断言。判废逻辑不得再调用它(见 _GUARD_IS_DEBUG_ONLY)。

    - 压缩/扩写中段 → 结尾**最后 2 句必须逐字保留**(否则等于又把结尾弄没了);
    - 只改结尾     → 正文不许大改,只允许为容纳新结尾而微压;
    - 中段和结尾都要改的合并场景 → **不做字符级约束**。这两处本来就都该变,
      再比字符必然误杀合法修复;这一档的把关交给复检里的"有没有编新事实"。
      字符级校验只用在"某部分应当逐字不动"的地方,这是它唯一站得住的用法。
    """
    if not str(repaired or "").strip():
        return False
    if instruction in ("compress_and_fix_ending", "extend_and_fix_ending"):
        return True
    if instruction in ("compress_middle", "extend_middle"):
        tail = _sentences(original)[-2:]
        return bool(tail) and _sentences(repaired)[-len(tail):] == tail

    # fix_ending 系列:比对正文(去掉各自最后 2 句)的字符前缀
    orig_body = "".join(_sentences(original)[:-2]) or "".join(_sentences(original))
    rep_body = "".join(_sentences(repaired)[:-2]) or "".join(_sentences(repaired))
    if not orig_body:
        return True
    if len(rep_body) < len(orig_body) * (1 - _BODY_SHRINK_TOLERANCE):
        return False
    keep = int(len(orig_body) * _BODY_PREFIX_RATIO)
    return rep_body[:keep] == orig_body[:keep]


# ── LLM 交互 ────────────────────────────────────────────────────────

def _parse_judge(raw: str) -> Optional[dict[str, Any]]:
    """解析判断器返回。解析不出来返回 None,由调用方按 fail-open 处理。"""
    m = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "closure_ok" not in data:
        return None
    return data


def _judge_system(rule: str, with_new_facts: bool) -> str:
    fields = ('{"closure_ok": true|false, "closure_reason": "不超过30字"'
              + (', "introduced_new_facts": true|false}' if with_new_facts
                 else "}"))
    extra = (
        "\n同时检查:修复稿有没有引入原稿里没有的新事实(人名/年份/数字/事件)。"
        "只许在原稿已有信息内重写,凭空添加即 introduced_new_facts=true。\n"
        if with_new_facts else "\n"
    )
    return (
        "你是短视频口播稿的终审,只判断一件事:这稿子的**故事讲完了没有**。\n\n"
        f"【本频道的收口标准】\n{rule}\n"
        f"{extra}"
        f"只输出 JSON:{fields}"
    )


_INSTRUCTIONS = {
    "fix_ending": (
        "这稿子**故事没讲完**。请修好结尾:\n"
        "1. 正文逐字保留,只改写最后 1-2 句;\n"
        "2. 结尾必须是落点——给出结论,或给一个建立在正文之上的开放问题;\n"
        "3. 严禁以'怎么做？''加多少？'这类问完就没下文的提问结尾。"
    ),
    "compress_middle": (
        "这稿子**太长**。请压缩中段论据(可删掉一个次要论据),\n"
        "**最后 2 句逐字保留,一个字都不许动**。"
    ),
    "extend_middle": (
        "这稿子**太短**。请在中段补充一个论据把内容讲厚,\n"
        "**最后 2 句逐字保留,一个字都不许动**。"
    ),
    "compress_and_fix_ending": (
        "这稿子**又太长、故事又没讲完**。请一次改到位:\n"
        "1. 压缩中段论据;2. 改写最后 1-2 句,让故事有落点(结论或开放问题)。"
    ),
    "extend_and_fix_ending": (
        "这稿子**又太短、故事又没讲完**。请一次改到位:\n"
        "1. 中段补充一个论据;2. 改写最后 1-2 句,让故事有落点。"
    ),
}


def _repair_prompt(
    script: str, instruction: str, reason: str, target_chars: int, rule: str = "",
) -> str:
    """修复 prompt。两条硬要求,都是踩过坑换来的:

    1. **完整原稿必须整体放进来** —— 上一版的根因就是没放,等于让模型从零再编一遍,
       第一版联网查证的史料全丢。
       断尾,修复器却只收到通用文案,不知道自己会被按什么标准评判,
       修复因此时灵时不灵(同一条稿子两次运行,一次 2 轮失败、一次第 2 轮成功)。

    另外"不许编造"的边界要划准:频道要求结尾给出主流解释,而解释本来就不在原稿里。
    一刀切禁止引入新内容 = 把修复器卡在无解约束里(既要给解释,又不许说原稿没有的)。
    正确边界:**允许给公认解释,禁止编造具体的人名/年份/数字**。
    """
    rubric = f"\n【本频道的收口标准——你会被按这个标准评判】\n{rule}\n" if rule else ""
    return (
        f"{_INSTRUCTIONS[instruction]}\n"
        f"{rubric}\n"
        f"问题诊断:{reason or '故事没有落点'}\n"
        f"目标字数约 {target_chars} 字(不含空白)。\n"
        "事实边界:可以引入学界公认/主流的解释与共识(收口往往就需要它),\n"
        "但**不得编造具体的人名、年份、数字或事件细节** —— 拿不准的具体信息一律不写。\n"
        "只输出改好的完整口播正文,不要任何说明、标题或标记。\n\n"
        f"【原稿】\n{script}"
    )


# ── 主流程 ──────────────────────────────────────────────────────────

def run_script_gate(
    *, script_v1: str, duration_seconds, voice: str, speed, series, llm,
    output_language: str = "zh",
) -> GateResult:
    """跑一遍文字验收闸。任何路径都必须返回一篇能出片的稿子。

    `output_language` 决定用哪套计数/切句/断尾规则(见 lang_profile)——
    闸本身只认 profile,不认语言,所以中英走的是**同一套逻辑**,不是两套实现。
    """
    from backend.lib.lang_profile import profile_for

    prof = profile_for(output_language)

    def predicted(text: str) -> float:
        return prof.estimate(prof.count(text), voice, speed)

    result = GateResult(
        final_script=script_v1, chosen_version="v1",
        chosen_predicted_seconds=predicted(script_v1),
    )
    if not _enabled("MB_SCRIPT_GATE"):
        return result      # 闸关闭:没跑就没有 telemetry 可记

    calls = {"n": 0}
    started = time.monotonic()
    # P10 全局预算:整个闸的调用封顶。耗尽直接进终态,不许各环节各自追加。
    try:
        budget = max(1, int(os.environ.get("MB_GATE_CALL_BUDGET", "10")))
    except (TypeError, ValueError):
        budget = 10
    # 不该被前面的疗程把预算吃光而饿死。三级疗程最多用 budget-2,剩下 2 次归保险丝。
    treatment_budget = max(1, budget - _FUSE_RESERVE)

    class _BudgetExhausted(Exception):
        pass

    def ask(system: str, user: str, *, reserve_ok: bool = False) -> Optional[str]:
        cap = budget if reserve_ok else treatment_budget
        if calls["n"] >= cap:
            raise _BudgetExhausted()
        calls["n"] += 1
        return llm._call(system, user, purpose=_PURPOSE)

    rule = closure_rule_for(series)
    target_chars = prof.target(duration_seconds, voice, speed)

    def _stamp(res: GateResult) -> GateResult:
        """在唯一出口填齐 telemetry。放这里是因为**所有路径都经过它** ——
        散在各处填必然漏(裸 return 绕过保险丝的教训刚吃过一次)。"""
        res.latency_ms = int((time.monotonic() - started) * 1000)
        res.llm_calls = calls["n"]
        try:
            res.cost_usd = gate_cost_usd()
        except Exception:  # noqa: BLE001 — 记账不许影响出片
            res.cost_usd = 0.0
        if not res.failure_reason:
            if res.degraded:
                res.failure_reason = "closure_unfixed"
            elif length_of(res.final_script) == "repair_long":
                res.failure_reason = "length_over"
        return res

    def finish(res: GateResult) -> GateResult:
        """单一出口。在这里挂"长度保险丝"——**唯一允许突破 2 轮上限的情况**。

        触发条件:**出厂稿仍判 repair_long**。这时再花一轮只压长度并复检;
        **永远不做机械删句**(砍中段会把因果链讲断,砍尾巴就是要根除的老毛病),
        压不动就**按现稿放长出片** + error 告警 —— 出片永远不被阻断。

        (1.52 倍),从 2 倍的缝里漏了出去,`billable_minutes=1.5167`,

        代价:坏情况下调用数从 5 升到 7。这是划算的 —— 多两次最便宜档位的调用,
        """
        over = length_of(res.final_script) == "repair_long"
        if not over:
            return _stamp(res)
        limit = float(duration_seconds or 60)
        logger.error(
            "script gate: shipping-length guard — %.1fs vs target %.1fs; one extra compress",
            predicted(res.final_script), limit,
        )
        try:
            # 走同一条隔离式压缩:结尾由代码保管,压缩动不到它。
            squeezed = _apply_repair(
                res.final_script, "compress_middle", "长度超标",
                rule, target_chars,
                lambda sy, us: ask(sy, us, reserve_ok=True), prof,
            )
            if squeezed:
                # 只查"有没有编新事实",**不要求收口通过**:
                # 结尾是代码原样拼回去的,收口状况不可能因为压缩而变差。
                # 这一轮的任务只是把长度拉回来;要求它顺便把收口也修好,
                # 等于白白丢掉一次合法的长度修复。
                _ok, _r, _src, s_new = closure_of(
                    squeezed, 99, "fuse", with_facts=True, reserve_ok=True)
                if not s_new:
                    res.final_script = squeezed
                    res.chosen_version += "+fuse"
                    res.chosen_predicted_seconds = predicted(squeezed)
        except Exception:  # noqa: BLE001
            logger.warning("script gate: length guard failed", exc_info=True)
        res.llm_calls = calls["n"]
        if length_of(res.final_script) == "repair_long":
            logger.error(
                "script gate: length guard could not fix it, shipping long (%.1fs "
                "vs target %.1fs). 客户会按实际时长多付钱,查上游写稿。",
                predicted(res.final_script), limit,
            )
        return _stamp(res)

    def length_of(text: str) -> str:
        v = prof.verdict(prof.count(text), voice, speed, duration_seconds)
        if v != "repair":
            return v
        return ("repair_long"
                if predicted(text) > float(duration_seconds or 60) else "repair_short")

    def closure_of(text: str, rnd: int, ver: str, with_facts: bool,
                   reserve_ok: bool = False):
        """→ (closure_ok, reason, source, new_facts)。任何异常一律 fail-open。"""
        hit = prof.dangling_hit(text)
        if hit:
            return False, hit, "pattern", False
        if not _enabled("MB_SCRIPT_CLOSURE_JUDGE"):
            return True, "", "skipped", False
        try:
            raw = ask(_judge_system(rule, with_facts), f"口播稿:\n{text}")
        except Exception:  # noqa: BLE001
            logger.warning("closure judge failed r=%s %s; passing", rnd, ver, exc_info=True)
            return True, "", "error", False
        data = _parse_judge(raw)
        if data is None:
            logger.warning("closure judge unparseable r=%s %s; passing", rnd, ver)
            return True, "", "error", False
        return (
            bool(data.get("closure_ok")), str(data.get("closure_reason") or "")[:60],
            "judge", bool(data.get("introduced_new_facts")),
        )

    candidates: list[dict] = []

    # ── 首轮体检 ──
    ok, reason, source, _ = closure_of(script_v1, 0, "v1", with_facts=False)
    length = length_of(script_v1)
    result.checks.append(GateCheck(0, "v1", length, ok, reason, source, predicted(script_v1)))
    result.llm_calls = calls["n"]

    candidates.append({"version": "v1", "script": script_v1, "closure_ok": ok,
                       "new_facts": False, "predicted": predicted(script_v1)})

    instruction = triage(length, ok)
    if instruction == "pass":
        return finish(result)

    logger.warning(
        "script gate: v1 needs repair (length=%s closure_ok=%s reason=%s) → %s",
        length, ok, reason, instruction,
    )

    # ── 修复循环 ──
    current, current_reason = script_v1, reason
    for rnd in range(1, _MAX_ROUNDS + 1):
        version = f"v{rnd + 1}"
        # 结构化定点修复(P11):模型只拿到它该改的那一段,拼接由代码做。
        # 正文/结尾"逐字不变"因此是**构造出来的**,不再靠事后字符比对
        if rnd == 1:
            repaired = _apply_repair(
                current, instruction, current_reason, rule, target_chars, ask, prof,
            )
        else:
            # 疗程升级(P10):定点修复没救回来 → 换方法,不是把同一招再来一遍。
            try:
                repaired = str(ask(
                    "你是短视频口播稿的写作者。只输出口播正文,不要任何说明。",
                    treatment_prompt(rnd, current, current_reason, rule, target_chars,
                                     prof),
                ) or "").strip() or None
            except Exception:  # noqa: BLE001
                repaired = None
        result.rounds_used = rnd
        result.llm_calls = calls["n"]
        if not repaired:
            logger.warning("script gate: r=%d %s repair produced nothing", rnd, version)
            result.checks.append(GateCheck(rnd, version, "n/a", False, "repair_failed",
                                           "repair", 0.0))
            result.degraded = result.chosen_version == "v1"
            continue

        r_ok, r_reason, r_source, new_facts = closure_of(repaired, rnd, version,
                                                         with_facts=True)
        r_length = length_of(repaired)
        result.checks.append(GateCheck(rnd, version, r_length, r_ok, r_reason,
                                       r_source, predicted(repaired)))
        result.llm_calls = calls["n"]
        candidates.append({"version": version, "script": repaired,
                           "closure_ok": r_ok, "new_facts": new_facts,
                           "predicted": predicted(repaired)})

        if new_facts:
            logger.warning("script gate: r=%d %s rejected (introduced new facts)", rnd, version)
            result.degraded = True
            current_reason = r_reason or current_reason
            continue

        if r_ok:
            # 收口过 → 这一版一定比原稿好,先记下来(哪怕长度还没回到放行带)。
            result.final_script = repaired
            result.chosen_version = version
            result.chosen_predicted_seconds = predicted(repaired)
            result.degraded = False

            still_off = r_length in ("repair_long", "repair_short")
            if not still_off or rnd >= _MAX_ROUNDS:
                # 长度也 OK,或轮次用尽 → 出厂。
                # 轮次用尽时照出这一版,是 P2:收口 > 长度,完整但偏长好过断尾。
                # ⚠️ 必须走 finish():它挂着长度保险丝。这里曾经写成裸 return,
                # 结果"修复成功但仍超长"的稿子绕过保险丝直接出厂
                logger.info("script gate: repaired at round %d (%s), length=%s",
                            rnd, version, r_length)
                return finish(result)

            # 收口好了但长度还差得远,且还有轮次 → 再花一轮把长度拉回来。
            # 会让 billable_minutes 按比例上浮、客户多付钱。
            # 这一轮走 compress/extend_middle,它**逐字保留最后 2 句**,
            # 刚修好的结尾不会被破坏。
            logger.info(
                "script gate: %s closed the story but length=%s; spending a round "
                "to fix length while keeping the new ending", version, r_length,
            )
            current, current_reason = repaired, f"长度仍{r_length}"
            instruction = triage(r_length, True)
            continue

        current, current_reason = repaired, r_reason
        instruction = triage(r_length, r_ok)
        # 已经拿到过收口成功的版本时,后面几轮失败不影响它 —— 出厂的是好版本,
        # 不该被标成 degraded(degraded 的语义是"没能修好就出厂了")。
        result.degraded = result.chosen_version == "v1"

    # 终态选优:v1 没有特权,它只是候选之一(§8)。
    best = pick_final(candidates, target_seconds=float(duration_seconds or 60) * 0.92)
    if best and best["script"] != result.final_script:
        logger.info("script gate: terminal pick = %s (closure_ok=%s, %.1fs)",
                    best["version"], best["closure_ok"], best["predicted"])
        result.final_script = best["script"]
        result.chosen_version = best["version"]
        result.chosen_predicted_seconds = best["predicted"]
    result.degraded = not (best or {}).get("closure_ok", False)
    if result.degraded:
        logger.warning("script gate: exhausted %d treatments, best candidate still "
                       "unclosed (%s)", _MAX_ROUNDS, (best or {}).get("version"))
    return finish(result)
