"""Media Buddy v2 observer-judge.

Gemini Flash is the low-cost first pass. It observes the candidate thumbnail
and judges whether it fits the chunk. Haiku only sees ambiguous cases that are
worth saving and stay within budget.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.lib.cloud_gateway import get_default_gateway
from backend.services.footage_service import FootageCandidate
from backend.services.llm_call_counter import bump_active, bump_active_real_cost

logger = logging.getLogger(__name__)

OBSERVER_JUDGE_MODEL = os.environ.get(
    "MEDIA_BUDDY_OBSERVER_JUDGE_MODEL",
    "google/gemini-2.5-flash",
)
AMBIGUOUS_ESCALATE_MODEL = os.environ.get(
    "MEDIA_BUDDY_AMBIGUOUS_ESCALATE_MODEL",
    "anthropic/claude-haiku-4-5",
)


@dataclass
class ObserverJudgeResult:
    verdict: str
    reason: str
    fit_score: float = 7.0  # 0-10 how close this clip is (even on reject)
    observation: dict[str, Any] = field(default_factory=dict)
    judgment_mode: str = "literal"
    escalated: bool = False
    model_id: str = ""
    elapsed_seconds: float = 0.0
    est_cost_usd: float = 0.0
    call_count: int = 1
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HaikuReviewBudget:
    max_per_video: int = 3
    max_per_minute: int = 3
    used_by_chunk: set[int] = field(default_factory=set)
    used_total: int = 0
    used_at: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def can_use(self, chunk_index: int) -> bool:
        with self._lock:
            return self._can_use_locked(chunk_index)

    def mark_used(self, chunk_index: int) -> None:
        with self._lock:
            if self._can_use_locked(chunk_index):
                self._mark_used_locked(chunk_index)

    def try_mark_used(self, chunk_index: int) -> bool:
        with self._lock:
            if not self._can_use_locked(chunk_index):
                return False
            self._mark_used_locked(chunk_index)
            return True

    def _can_use_locked(self, chunk_index: int) -> bool:
        self._prune()
        return (
            self.used_total < self.max_per_video
            and len(self.used_at) < self.max_per_minute
            and chunk_index not in self.used_by_chunk
        )

    def _mark_used_locked(self, chunk_index: int) -> None:
        self.used_total += 1
        self.used_by_chunk.add(chunk_index)
        self.used_at.append(time.monotonic())

    def _prune(self) -> None:
        cutoff = time.monotonic() - 60.0
        self.used_at = [ts for ts in self.used_at if ts >= cutoff]


def judge_candidate(
    candidate: FootageCandidate,
    chunk_plan: dict[str, Any],
    *,
    mode: str,
    is_last_candidate: bool,
    budget: HaikuReviewBudget,
) -> ObserverJudgeResult:
    """Return accept/reject/ambiguous for one candidate.

    non-accept unless Haiku escalation is allowed and succeeds.
    """
    chunk_index = int(chunk_plan.get("chunk_index", -1))
    first = _call_observer_model(candidate, chunk_plan, mode, OBSERVER_JUDGE_MODEL)
    if first.verdict in {"accept", "reject"}:
        return first

    if _should_escalate(first, chunk_plan, mode, is_last_candidate, budget):
        if not budget.try_mark_used(chunk_index):
            return first
        second = _call_observer_model(
            candidate,
            chunk_plan,
            mode,
            AMBIGUOUS_ESCALATE_MODEL,
            force_binary=True,
        )
        second.escalated = True
        second.elapsed_seconds += first.elapsed_seconds
        second.est_cost_usd += first.est_cost_usd
        second.call_count += first.call_count
        if second.verdict == "ambiguous":
            second.verdict = "reject"
            second.reason = second.reason or "Haiku escalation stayed ambiguous"
        return second

    return first


def _should_escalate(
    result: ObserverJudgeResult,
    chunk_plan: dict[str, Any],
    mode: str,
    is_last_candidate: bool,
    budget: HaikuReviewBudget,
) -> bool:
    if result.verdict != "ambiguous":
        return False
    chunk_index = int(chunk_plan.get("chunk_index", -1))
    if not budget.can_use(chunk_index):
        return False
    if _is_important_chunk(chunk_plan):
        return True
    if is_last_candidate:
        return True
    if mode == "metaphor" and _reason_is_specific(result.reason):
        return True
    return False


def _is_important_chunk(chunk_plan: dict[str, Any]) -> bool:
    if int(chunk_plan.get("chunk_index", 99)) == 0:
        return True
    sentence_type = str(chunk_plan.get("sentence_type", "")).lower()
    priority = str(chunk_plan.get("priority", "")).lower()
    return (
        priority in {"high", "hero"}
        or sentence_type in {
            "hook", "core", "key_point", "abstract_metaphor", "emotion",
        }
    )


def _reason_is_specific(reason: str) -> bool:
    reason = (reason or "").lower()
    concrete_terms = [
        "branch", "vein", "pattern", "repeating", "symmetry", "crack",
        "shatter", "network", "structure", "leaf", "fractal", "spiral",
    ]
    return any(term in reason for term in concrete_terms)


def _call_observer_model(
    candidate: FootageCandidate,
    chunk_plan: dict[str, Any],
    mode: str,
    model_id: str,
    *,
    force_binary: bool = False,
) -> ObserverJudgeResult:
    system = _system_prompt(force_binary)
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": _user_prompt(candidate, chunk_plan, mode)}
    ]
    if candidate.thumbnail_url:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": candidate.thumbnail_url},
        })
    t0 = time.monotonic()
    try:
        gw = get_default_gateway()
        gw_ok = gw.is_authenticated()
        _offline = os.environ.get("MEDIA_BUDDY_ALLOW_OFFLINE", "0").strip().lower() in ("1", "true", "yes", "on")
        if not gw_ok and not _offline:
            raise RuntimeError("cloud not authenticated")
        from backend.lib.llm_client import vision_completion
        result = vision_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            max_tokens=900,
            idempotency_key=str(uuid.uuid4()),
            gateway=gw if gw_ok else None,
            gateway_model_id=model_id,
        )
        _purpose = (
            "observer_judge" if model_id == OBSERVER_JUDGE_MODEL else "observer_judge_haiku"
        )
        # model_id mislabelled every judge call as gemini in the cost summary even
        # though the real call was qwen — so the summary read "all gemini" when it
        # was really "all qwen, gemini fallback only".
        _real_model = str(result.get("model") or model_id)
        bump_active(_purpose, _real_model)
        # actual OpenRouter cost as `cost_eur`; without this the counter's
        # real-cost total stays 0 (the estimate is ~10-20x low for vision calls).
        try:
            _ce = float(result.get("cost_eur") or 0)
            if _ce:
                bump_active_real_cost(_purpose, _real_model, _ce / 0.93)
        except Exception:
            pass
        parsed = _parse_result(result.get("content", ""), mode)
        # the judge as "google/gemini-2.5-flash" even though the real call ran on
        parsed.model_id = str(result.get("model") or model_id)
        parsed.elapsed_seconds = round(time.monotonic() - t0, 3)
        parsed.est_cost_usd = _estimate_observer_cost(model_id)
        return parsed
    except Exception as e:
        logger.warning("observer_judge failed for %s: %s", candidate.source_id, e)
        return ObserverJudgeResult(
            verdict="ambiguous",
            reason=f"observer unavailable: {type(e).__name__}",
            observation={
                "main_subjects": [candidate.source_tags or candidate.query or ""],
                "scene": candidate.query or "",
            },
            judgment_mode=mode,
            model_id=model_id,
            elapsed_seconds=round(time.monotonic() - t0, 3),
            est_cost_usd=0.0,
        )


def _system_prompt(force_binary: bool) -> str:
    verdicts = '"accept" or "reject"' if force_binary else '"accept", "reject", or "ambiguous"'
    return f"""You are a stock-footage observer and judge.

Return JSON only. First describe only what is visible, then judge whether the
image can represent the narration chunk.

Allowed verdicts: {verdicts}.

This image is STOCK B-ROLL that sets the SCENE for the narration — it is NOT a
literal reenactment of the sentence. Judge by SUBJECT and WORLD, not by literal
coverage of the sentence's specific details.

ACCEPT when the visible subject / scene / world matches the narration's topic,
EVEN IF the footage does not depict the exact numbers, props, tools, or actions
the sentence happens to mention. Example: for "他改造成咖啡豆分装间，用三把不同目数
筛网、印生产时间戳" — any coffee-bean / coffee-shop / coffee-sorting / barista
footage is ACCEPTABLE; do NOT require the exact sieves, timestamps, or stainless
counter to be visible. On-topic coffee b-roll represents a coffee-business chunk.

REJECT only when: the image contains a must_not_show item; OR the subject/world is
clearly WRONG for the story's topic (a pharmacy, florist, furniture store, Apple
store, or finance dashboard for a coffee-shop story; another country's scene when a
locale is specified); OR it is a decorative overlay / transition graphic. The
reason must cite visible facts from observation.

SUBJECT CONSISTENCY (critical — apply BEFORE scoring; the story's core subject is
in must_show and/or the narration topic):
- If the story's subject is a specific animal / organism / creature and the image's
  MAIN subject is a HUMAN (a person, a human body part, someone exercising or
  stretching, a gym / yoga / massage / medical / lab-with-people setting), REJECT
  with fit_score 0-2 — UNLESS the sentence is explicitly about people. A frog's jaw,
  "soft-tissue stretch", teeth, or bones must NEVER be illustrated with a person
  stretching or a human close-up. Matching a bare ACTION word (stretch, open, bite)
  is NOT enough — the SUBJECT must match.
- If the image's main subject is an animal of a CLEARLY DIFFERENT class/family from
  the story's subject (subject is a frog / amphibian, but the image shows a
  crocodile / alligator / caiman, lizard (INCLUDING spiny ones like a thorny dragon /
  bearded dragon / horned lizard), snake, turtle, mammal, bird, or fish), REJECT with
  fit_score 0-2 — ALWAYS, even in a similar_/motif/fallback judgment_mode. A different
  CLASS of animal is never an acceptable stand-in, no matter how "spiky/toothy/scaly"
  it looks: a thorny-dragon lizard is NOT a substitute for a frog's teeth. Falling back
  to a generic frog/amphibian clip, or to no clip, is always better than a
  different-class animal. (Only the SAME-family-different-species tier below may be a
  fallback stand-in.)
- SPECIES PRECISION (角蛙优先): the field "main_subject" gives the story's EXACT
  species/subject (e.g. "horned frog").
  · If the image's main subject IS that exact species (a horned frog / Ceratophrys
    for a horned-frog story) → this is the PREFERRED clip; judge normally, fit_score
    8-10 for a clean shot.
  · If the image's main subject is the SAME broad family but a DIFFERENT species
    (a common green frog / tree frog / bullfrog / toad when main_subject is a horned
    frog) → it is only a WEAK stand-in: score fit_score 5-6 (never 7+) and set
    verdict to "reject" UNLESS judgment_mode is a similar_/motif fallback. Reason:
    we want viewers to actually SEE the real species (a horned-frog video that never
    shows a horned frog is a failure); a generic same-family frog is a last-resort
    substitute, used only when no exact-species clip exists.
  · If main_subject is empty/generic (just "frog", no precise species), treat any
    on-topic frog/amphibian as fine — judge it normally.

- BIOGRAPHY REPRESENTATIVE ACTOR: when "identity_mode" is "representative_actor",
  the stock clip is NOT required to depict the exact named person in "story_subject".
  A generic actor may represent that person. Accept a plausible actor, occupation,
  era, location, work action, objects, archives, products, or event context when it
  supports the narration. Judge scene and story fit; do not reject for facial identity.

ALWAYS REJECT, regardless of how well the subject matches the topic, any image
showing a DEAD, decaying, rotting, desiccated, mutilated, or roadkill animal, or a
carcass / corpse / animal remains. These are disgusting to a general audience even
for an on-topic species — a dead gecko is NOT acceptable b-roll for a gecko story.
Use a live, healthy animal instead. (Set reason to "morbid: dead/decaying animal".)

ALWAYS REJECT, regardless of how well the subject matches the topic, any image
depicting illegal drugs or drug use: cannabis / marijuana / hemp leaves or plants
(the serrated multi-point leaf), poppy or opium-poppy fields or flowers (even
decorative red poppies), drug paraphernalia (bongs, pipes, rolling papers,
joints, grinders), powders / crystals / pills / syringes shown in a drug-use
context, or a person smoking / injecting / snorting drugs. This is a HARD
compliance line for our audience (primarily China) — one such frame is
unacceptable. If you are UNSURE whether a plant or object is cannabis / poppy /
drug-related, REJECT it; err on the side of caution.
(Set reason to "unsafe: drug/cannabis imagery".)

Do NOT reject on-topic footage merely because it is "missing the specific elements
described in the sentence" — stock footage shows the world, not the exact moment.
For judgment_mode "similar_subject" or "similar_category", accept a safe substitute
when the visible subject, scene, category, or material can reasonably stand in for
the narration. Still reject obvious wrong-domain subjects (finance dashboards for
food/nature topics).
Reject decorative overlays or transition graphics such as flowers, petals,
floral wipes, confetti, generic particles, or purely ornamental backgrounds
unless the chunk explicitly asks for that visible subject. Repeating small
objects is not enough to qualify as a fractal, math, science, or universe shot.

"fit_score" (0-10) rates HOW WELL this clip fits the shot's subject/world, and
you MUST give it even when rejecting so we know how close it was: 10 = perfect,
exactly on-topic; 6-9 = on-topic but imperfect (right world, minor mismatch);
3-5 = loosely related / borderline (could pass in a pinch); 0-2 = wrong world,
off-topic, or a must_not_show / unsafe / morbid image. A reject that is truly
close (right subject, small issue) should score 5-7; a clearly wrong clip 0-2.

"subject_h_pos" tells us where to crop for vertical (9:16) video: it is the
horizontal CENTER of the single MAIN subject, as a fraction of the image width —
0.0 = far left edge, 0.5 = centered, 1.0 = far right edge. Estimate it from the
thumbnail. Return null when the subject fills the frame, is spread across the
whole width, or there is no single clear subject (we then keep the default
center crop).

Schema:
{{
  "observation": {{
    "main_subjects": ["..."],
    "scene": "...",
    "action": "...",
    "style": "real footage|3d animation|motion graphic|mixed",
    "potential_distractions": ["..."],
    "subject_h_pos": 0.5
  }},
  "judgment": {{
    "must_show_coverage": 0,
    "must_not_show_violated": [],
    "metaphor_essence_match": null,
    "scene_role_fit": 0,
    "style_consistency": 0,
    "fit_score": 7,
    "verdict": "accept",
    "reason": "Chinese explanation citing visible facts"
  }}
}}
"""


def _user_prompt(
    candidate: FootageCandidate,
    chunk_plan: dict[str, Any],
    mode: str,
) -> str:
    policy = chunk_plan.get("short_video_visual_policy") or {}
    identity_mode = str(policy.get("identity_mode") or "subject")
    topic_anchor = str(policy.get("topic_anchor") or "")
    return json.dumps({
        "judgment_mode": mode,
        "similar_acceptance_policy": (
            "If judgment_mode starts with similar_, the candidate may be a "
            "same-subject or same-category substitute. Accept when visible "
            "facts can safely represent the sentence without misleading the viewer."
        ) if mode.startswith("similar_") else "",
        "chunk_text": chunk_plan.get("sentence") or chunk_plan.get("text") or "",
        "intent": chunk_plan.get("intent") or "",
        # 精确物种(角蛙/horned frog):判官用它区分"就是主角物种"vs"同科普通蛙"。来自短视频
        # visual policy 的 topic_anchor(Fix A 后是精确短语);退化用 must_show 第一项。
        "main_subject": "" if identity_mode == "representative_actor" else (
            topic_anchor or (chunk_plan.get("must_show") or [""])[0] or ""
        ),
        "story_subject": topic_anchor,
        "identity_mode": identity_mode,
        "must_show": chunk_plan.get("must_show") or [],
        "must_not_show": chunk_plan.get("must_not_show") or [],
        "metaphor_essence": chunk_plan.get("metaphor_essence") or "",
        "similar_rescue_queries": chunk_plan.get("_similar_rescue_queries") or {},
        "candidate_metadata": {
            "source": candidate.source,
            "query": candidate.query,
            "tags": candidate.source_tags,
            "url": candidate.source_url,
        },
    }, ensure_ascii=False)


def _parse_result(raw: str, mode: str) -> ObserverJudgeResult:
    data = _extract_json(raw)
    judgment = data.get("judgment") if isinstance(data.get("judgment"), dict) else data
    verdict = str(judgment.get("verdict", "ambiguous")).lower()
    if verdict not in {"accept", "reject", "ambiguous"}:
        verdict = "ambiguous"
    # Closeness (0-10). Missing → default by verdict so adaptive budgeting still
    # degrades sanely: accept→9, ambiguous→6 (close), reject→6 (give the benefit
    # of the doubt = one more try) rather than blindly stopping.
    _default_fit = {"accept": 9.0, "ambiguous": 6.0, "reject": 6.0}[verdict]
    try:
        fit = float(judgment.get("fit_score", _default_fit))
    except (TypeError, ValueError):
        fit = _default_fit
    fit = max(0.0, min(10.0, fit))
    return ObserverJudgeResult(
        verdict=verdict,
        reason=str(judgment.get("reason") or ""),
        fit_score=fit,
        observation=data.get("observation") if isinstance(data.get("observation"), dict) else {},
        judgment_mode=mode,
        raw=data,
    )


def _estimate_observer_cost(model_id: str) -> float:
    per_call = {
        "google/gemini-2.5-flash": 0.00015,
        "anthropic/claude-haiku-4-5": 0.0009,
        "claude-haiku-4-5": 0.0009,
    }
    return per_call.get(model_id, 0.001)


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError("no JSON in observer response")
        return json.loads(m.group(0))
