"""Publish center endpoints: low-cost SEO Pack drafts."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.lib.user_context import owner_filter, multi_tenant_enabled
from backend.lib.llm_client import LLMClient
from backend.lib.progress import read_progress
from backend.models.project import Project

router = APIRouter()
PIPELINE_DIR = Path.home() / ".media-buddy-oss" / "pipelines"


class PublishProject(BaseModel):
    id: str
    name: str
    output_format: str
    output_path: str
    created_at: datetime
    has_seo_pack: bool = False
    series_id: str | None = None


class SeoChapter(BaseModel):
    time: str
    title: str


class SeoPack(BaseModel):
    project_id: str
    project_name: str
    platform: str
    language: str
    generated_at: str
    title_options: list[str] = []
    description: str = ""
    hashtags: list[str] = []
    tags: list[str] = []
    chapters: list[SeoChapter] = []
    pinned_comment: str = ""
    publish_notes: str = ""
    source_summary: str = ""
    estimated_cost_eur: float = 0.0


class SeoPackGenerateRequest(BaseModel):
    project_ids: list[str] = Field(default_factory=list)
    overwrite: bool = False


class SeoPackGenerateResponse(BaseModel):
    items: list[SeoPack] = []
    skipped_existing: list[str] = []


def _pipeline_dir(project: Project) -> Path:
    if project.pipeline_dir:
        return Path(project.pipeline_dir)
    return PIPELINE_DIR / project.id


def _seo_pack_path(project: Project) -> Path:
    return _pipeline_dir(project) / "publish" / "seo_pack.json"


def _read_script(project: Project) -> str:
    script = (project.script_text or "").strip()
    if script:
        return script
    cp = read_progress(PIPELINE_DIR, project.id, "script")
    if cp:
        text = ((cp.get("artifacts") or {}).get("script") or {}).get("text")
        if text:
            return str(text).strip()
    script_path = _pipeline_dir(project) / "script.txt"
    if script_path.exists():
        return script_path.read_text(encoding="utf-8", errors="ignore").strip()
    # (每句一行 sentence)。从 chunks 按顺序拼回整稿,SEO/发布才不会报 "no script text"。
    try:
        from backend.database import SessionLocal
        from backend.models.chunk import Chunk
        _db = SessionLocal()
        try:
            rows = (
                _db.query(Chunk)
                .filter(Chunk.project_id == project.id)
                .order_by(Chunk.chunk_index)
                .all()
            )
            joined = " ".join(
                str(getattr(r, "sentence", "") or "").strip() for r in rows
            ).strip()
            if joined:
                return joined
        finally:
            _db.close()
    except Exception:
        pass  # chunks 兜底失败(极少)→ 保持原行为、返回空
    return ""


def _read_subtitles(project: Project) -> str:
    srt_path = _pipeline_dir(project) / "subtitles.srt"
    if not srt_path.exists():
        return ""
    text = srt_path.read_text(encoding="utf-8", errors="ignore")
    lines = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.isdigit() or "-->" in raw:
            continue
        lines.append(raw)
    return "\n".join(lines[:80])


def _srt_duration_seconds(project: Project) -> int:
    """Real video length = the last subtitle cue's end time (HH:MM:SS). Used to
    anchor chapter timestamps to the ACTUAL video — without it the LLM invents
    times far beyond the real duration (e.g. 14:00 chapters on a 4:50 video)."""
    srt_path = _pipeline_dir(project) / "subtitles.srt"
    if not srt_path.exists():
        return 0
    last = 0
    for m in re.finditer(
        r"-->\s*(\d{2}):(\d{2}):(\d{2})",
        srt_path.read_text(encoding="utf-8", errors="ignore"),
    ):
        last = max(last, int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)))
    return last


def _mmss(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def _parse_chapter_seconds(t: str) -> Optional[int]:
    parts = t.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def _platform_profile(output_format: str) -> dict[str, Any]:
    fmt = (output_format or "").lower()
    if fmt == "youtube_landscape":
        return {
            "platform": "youtube_long",
            "mode": "full",
            "requirements": (
                "Create a complete YouTube long-form SEO pack: 3 title options, "
                "a useful description, chapter timestamps if the script supports it, "
                "3-5 relevant hashtags, 8-15 backend tags, a pinned comment, and publish notes."
            ),
        }
    if fmt == "instagram_feed":
        return {
            "platform": "instagram_feed",
            "mode": "light",
            "requirements": (
                "Create a concise Instagram feed caption pack: 3 title/caption hooks, "
                "one caption, 5-10 relevant hashtags, no chapters, and brief publish notes."
            ),
        }
    if fmt in {"tiktok", "instagram_reels"}:
        return {
            "platform": fmt,
            "mode": "light",
            "requirements": (
                "Create a lightweight short-video publish pack: 3 short hooks, one caption, "
                "3-7 relevant hashtags, no chapters, and one interaction prompt."
            ),
        }
    return {
        "platform": "youtube_shorts",
        "mode": "light",
        "requirements": (
            "Create a lightweight YouTube Shorts pack: 3 short title options, a short description, "
            "3-5 relevant hashtags, no chapters, and one pinned comment."
        ),
    }


def _extract_json(raw: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if not match:
        raise ValueError("LLM did not return JSON")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM JSON root must be an object")
    return data


def _list_str(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


def _chapters(value: Any, enabled: bool, max_seconds: int = 0) -> list[dict[str, str]]:
    if not enabled or not isinstance(value, list):
        return []
    out = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        time = str(item.get("time") or "").strip()
        title = str(item.get("title") or "").strip()
        if not (time and title):
            continue
        # Drop chapters whose timestamp runs past the real video length — the
        # LLM invents times beyond the duration (14:00 on a 4:50 video). Allow a
        # tiny grace (+2s) for rounding.
        if max_seconds:
            secs = _parse_chapter_seconds(time)
            if secs is None or secs > max_seconds + 2:
                continue
        out.append({"time": time, "title": title})
    return out


def _estimate_cost_eur(prompt: str, raw: str) -> float:
    # Cheap local estimate only; actual cloud billing remains authoritative.
    tokens = max(1, int((len(prompt) + len(raw)) / 4))
    return round(tokens * 0.0000008, 4)


def _build_pack(project: Project) -> SeoPack:
    script = _read_script(project)
    if not script:
        raise HTTPException(422, f"Project {project.id} has no script text")
    subtitles = _read_subtitles(project)
    profile = _platform_profile(project.output_format)
    source = subtitles or script
    compact_source = source[:4500]
    dur_s = _srt_duration_seconds(project)
    dur_label = _mmss(dur_s) if dur_s else "unknown"
    if dur_s:
        chapter_rule = (
            f"The ACTUAL video length is {dur_label} ({dur_s} seconds). CHAPTERS RULE: the "
            f"first chapter is 00:00; EVERY chapter timestamp MUST be within the video "
            f"length — never exceed {dur_label}. Times must reflect real content flow, not "
            f"even padding. For a video under 5 minutes use at most 3 chapters; for under "
            f"2 minutes use no chapters. Never invent chapters past the end of the video."
        )
    else:
        chapter_rule = "Only add chapters if the script clearly supports them; keep them few and based on script flow."
    system = (
        "You are Media Buddy's low-cost publish assistant. Generate practical platform metadata "
        "STRICTLY from the provided script/subtitles. Do not browse the web. Do not invent facts, "
        "topics, or chapter times not supported by the script. Return JSON only."
    )
    # 最高优先级:标题/描述必须"留悬念、不剧透"。这条凌驾于上面的爆款指引——尤其要压住
    # "强结果/利害"那种把答案直接写进标题的倾向(用户反复强调:看到标题想知道、又猜不准,
    # 必须点开才知道)。
    system += (
        "\n\n【最高优先级:标题/描述留悬念,绝不剧透答案——优先级高于以上一切】\n"
        "1. 先在心里用一句话想清楚这条视频的「答案/payoff」是什么(它最后揭晓的那个结论、机制、"
        "原理、关键数字)。\n"
        "2. title_options 里**绝对不能出现这个答案里的任何关键词**:机制名(如超声波/范德华力/"
        "回声定位)、原理、结论、关键数字,以及「因为…」「靠…」「用…来…」这类把答案说破的句式。"
        "标题只负责抛出一个反常识的钩子/疑问,让人猜不到、必须点开视频才知道。\n"
        "3. ⚠️ 特别注意:很多题材的主题本身就把答案写出来了(例:主题「老鼠用不同叫声区分线路」"
        "或「土拨鼠靠抱团取暖是真的吗」),这时也要把「叫声/区分线路/超声波/抱团取暖」这种**答案词"
        "全部拿掉**,只留纯钩子。尤其主题是「…是真的吗?」时,那个「…」就是答案,标题里绝不能出现。"
        "例:「伦敦地铁的老鼠,藏着一个人类听不见的秘密」「土拨鼠冬天靠什么熬过零下30度?」"
        "(都没说出答案是什么)。\n"
        "4. **3 条 title_options 全部都要是悬念式**(都藏答案),只是角度不同:可以是"
        "①简短疑问式(像「蝙蝠最怕什么?」)、②反常特征式(「除了持证电工,谁都不敢碰的鱼…」)、"
        "③反转诱饵式;其中至少一条要像「蝙蝠最怕什么?」那样一句话以内、极简。不许有任何一条把答案写出来。\n"
        "5. 头号杠杆 = 「一个具体又反常的特征/后果」+「直接反问观众」,而且**连主体的身份都藏住**,"
        "并且**大胆埋一个会被猜错的诱饵**:例「除了持证电工,谁都不敢碰的鱼,你知道是什么鱼吗?」——"
        "『电工』故意让人以为是电鳗、其实不是,这种「差点猜到又拿不准」最上瘾;但⚠️诱饵必须是脚本里"
        "真能自圆其说的(视频里确实会揭晓),绝不能为了钩人编造脚本里没有的设定。这种标题实测播放量"
        "能比把答案写出来的高出十几二十倍,是头号杠杆。\n"
        "★【痒不痒的命门:禁虚词、逼具体】绝对禁止用万能悬念虚词——「秘密 / 背后的真相 / 震惊 / "
        "颠覆认知 / 大吃一惊 / 想象不到 / 出乎意料 / 惊掉下巴 / 远超想象」这类烂大街的词一律不许出现"
        "(大脑早就免疫,说了等于没说,标题会很平、不痒)。必须从脚本里挖一个**具体、反常、有画面感的"
        "细节**当钩子,让人脑子里立刻浮现一个画面、并冒出「为什么会这样?」。对比:✗「土拨鼠冬天的秘密」"
        "(虚、没画面)→ ✓「胖土拨鼠当『保温墙』,瘦的却躲在……」(具体有画面);✗「这种鱼背后的真相」"
        "(虚)→ ✓「除了持证电工,谁都不敢碰的鱼」(具体反常)。\n"
        "6. 正例(✓不剧透、且具体有画面):「蝙蝠最怕什么?」「除了持证电工,谁都不敢碰的鱼,你知道是什么吗?」"
        "「胖土拨鼠当保温墙,瘦的躲哪了?」「金鱼睡觉睁不睁眼?」\n"
        "   反例①(✗剧透):「蝙蝠最怕强光」「电鳗能放电800伏」;反例②(✗虚、不痒):「金鱼背后的秘密」"
        "「这种动物太震惊了」。\n"
        "7. description **整段都不许把答案/结论/机制/关键数字写出来**(高频错误:开头就写「不是因为A,"
        "而是因为B」——B 就是答案,禁止;也别在结尾补一句把答案点破)。description 只做两件事:"
        "加深标题的悬念 + 引导点开看视频找答案;可以说「真相超出想象」「答案都在视频里」,"
        "但绝不说出答案是什么。\n"
        "8. 只保留主体关键词(蝙蝠/老鼠/壁虎)方便搜索;被藏起来的只有「答案」本身。\n"
        "9. 【交稿前自检】把 3 条 title_options 逐条扫一遍:只要有一条出现了第 1 步那个答案里的词"
        "(机制/原理/关键数字/主体的具体名字),就地把它重写掉,直到 3 条全都藏住答案为止。"
    )
    user = f"""
Project name: {project.name}
Output format: {project.output_format}
Platform profile: {profile['platform']}
Mode: {profile['mode']}
Requirements: {profile['requirements']}

Follow the language of the script.
Keep short-video packs concise to save tokens.
{chapter_rule}

Return exactly this JSON object:
{{
  "language": "zh|en|other",
  "title_options": ["...","...","..."],
  "description": "...",
  "hashtags": ["#..."],
  "tags": ["..."],
  "chapters": [{{"time":"00:00","title":"..."}}],
  "pinned_comment": "...",
  "publish_notes": "...",
  "source_summary": "one sentence summary"
}}

Script/subtitle context:
{compact_source}
""".strip()
    raw = LLMClient()._call(system, user, purpose="publish_seo")
    data = _extract_json(raw)
    is_long = profile["platform"] == "youtube_long"
    pack = SeoPack(
        project_id=project.id,
        project_name=project.name,
        platform=profile["platform"],
        language=str(data.get("language") or "other")[:24],
        generated_at=datetime.now(timezone.utc).isoformat(),
        title_options=_list_str(data.get("title_options"), 3),
        description=str(data.get("description") or "").strip(),
        hashtags=_list_str(data.get("hashtags"), 10 if is_long else 7),
        tags=_list_str(data.get("tags"), 15 if is_long else 8),
        chapters=_chapters(data.get("chapters"), is_long, dur_s),
        pinned_comment=str(data.get("pinned_comment") or "").strip(),
        publish_notes=str(data.get("publish_notes") or "").strip(),
        source_summary=str(data.get("source_summary") or "").strip(),
        estimated_cost_eur=_estimate_cost_eur(user, raw),
    )
    if not pack.title_options:
        pack.title_options = [project.name]
    return pack


def _write_pack(project: Project, pack: SeoPack) -> None:
    path = _seo_pack_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        pack.model_dump_json(indent=2),
        encoding="utf-8",
    )


def _read_pack(project: Project) -> Optional[SeoPack]:
    path = _seo_pack_path(project)
    if not path.exists():
        return None
    return SeoPack.model_validate_json(path.read_text(encoding="utf-8"))


@router.get("/projects", response_model=list[PublishProject])
def list_publish_projects(request: Request, db: Session = Depends(get_db)):
    projects = (
        owner_filter(db.query(Project), Project, request)
        .filter(Project.status == "completed", Project.output_path.isnot(None))
        .order_by(Project.created_at.desc())
        .all()
    )
    return [
        PublishProject(
            id=p.id,
            name=p.name,
            output_format=p.output_format,
            output_path=p.output_path or "",
            created_at=p.created_at,
            has_seo_pack=_seo_pack_path(p).exists(),
            series_id=p.series_id,
        )
        for p in projects
    ]


@router.get("/seo-pack/{project_id}", response_model=SeoPack)
def get_seo_pack(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
    if not project:
        raise HTTPException(404, "Project not found")
    pack = _read_pack(project)
    if not pack:
        raise HTTPException(404, "SEO Pack not found")
    return pack


def _set_request_gateway_token(request: Request) -> None:
    """
    → 'cloud not authenticated; user must log in' (500). Mirror the worker's
    set_worker_token, scoped to this request."""
    if not multi_tenant_enabled():
        return
    auth_hdr = request.headers.get("authorization") or ""
    token = auth_hdr[7:].strip() if auth_hdr[:7].lower() == "bearer " else None
    if token:
        from backend.lib.cloud_auth import get_default_auth
        get_default_auth().set_request_token(token)


def _clear_request_gateway_token() -> None:
    if not multi_tenant_enabled():
        return
    from backend.lib.cloud_auth import get_default_auth
    get_default_auth().set_request_token(None)


@router.post("/seo-pack", response_model=SeoPackGenerateResponse)
def generate_seo_pack(body: SeoPackGenerateRequest, request: Request, db: Session = Depends(get_db)):
    ids = [pid for pid in body.project_ids if pid]
    if not ids:
        raise HTTPException(400, "No projects selected")
    _set_request_gateway_token(request)
    try:
        items: list[SeoPack] = []
        skipped: list[str] = []
        for project_id in ids:
            project = owner_filter(db.query(Project).filter(Project.id == project_id), Project, request).first()
            if not project:
                raise HTTPException(404, f"Project not found: {project_id}")
            if project.status != "completed" or not project.output_path:
                raise HTTPException(409, f"Project is not publish-ready: {project_id}")
            existing = _read_pack(project)
            if existing and not body.overwrite:
                skipped.append(project_id)
                items.append(existing)
                continue
            pack = _build_pack(project)
            _write_pack(project, pack)
            items.append(pack)
        return SeoPackGenerateResponse(items=items, skipped_existing=skipped)
    finally:
        _clear_request_gateway_token()
