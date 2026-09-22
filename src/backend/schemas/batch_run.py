"""Phase 2.7c — BatchRun schemas."""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class BatchRunCreate(BaseModel):
    """Input for POST /api/series/{series_id}/batch.

    Provide either `scripts` (Script Mode) or `prompts` (Creative Mode), one
    item per video. The dispatcher creates one Project per item.
    """
    scripts: Optional[list[str]] = None
    prompts: Optional[list[str]] = None
    output_format: Optional[str] = None
    # 短视频画面风格:"fill"(默认竖屏裁剪铺满)| "blur"(背景虚化+正方形前景)。
    framing_style: Optional[str] = None
    # 背景音乐开关:默认 False(不加 BGM,躲版权投诉);None 也按 False 处理。
    include_music: Optional[bool] = None
    concurrency: int = Field(default=4, ge=1, le=8)
    daily_cost_cap_usd: float = Field(default=200.0, ge=0.0)
    # 批次级出片设置(覆盖 series 默认):音色 / 目标时长 / 输出语言。
    tts_provider: Optional[str] = None
    # 语速挡位:正常1.0/稍快1.2/快1.4/极快1.6(ElevenLabs 走 atempo,Azure 走 rate)。
    tts_speed: Optional[float] = Field(default=None, ge=0.7, le=2.0)
    duration_seconds: Optional[float] = None
    output_language: Optional[Literal["zh", "en"]] = None
    # 字幕语言(可独立于配音):简体/繁体/英文/中英双语。None → 跟随 output_language。
    subtitle_language: Optional[Literal["zh-Hans", "zh-Hant", "en", "zh-Hans+en", "zh-Hant+en"]] = None
    # 字幕字号缩放系数(客户拖拉杆自选)。None → 按 1.0。夹在 [0.5, 2.0]。
    subtitle_font_scale: Optional[float] = Field(default=None, ge=0.5, le=2.0)
    # `Project.include_subtitles` 的默认 True。只有显式 False 才关。
    # 不这么写的话,所有**没传这个字段的老调用方**(频道页那些)会突然全都没字幕。
    include_subtitles: Optional[bool] = None
    # ⚠️ **可选、默认 None** —— 频道页那些不传的调用方一个字都不用改。
    # 每条片的标题(和 `scripts`/`prompts` 一一对应)。
    #
    # 🚨 不传的话名字取的是**脚本正文第一行**(`_extract_batch_title`)——
    #    「2015年12月21号晚上,一枚猎鹰9号火箭从佛罗里达的卡纳维拉尔角升空…」。
    #    而客户在出稿页**认真改过标题**,那个标题被静默丢掉了。
    # ⚠️ 可选 + 默认 None → 频道页那些不传的调用一个字都不用改。
    titles: Optional[list[str]] = None
    # 用户在前端确认"这个选题和已有的很像,仍然继续"后置 true,绕过重复选题闸。
    allow_duplicate: bool = False


class BatchRunRead(BaseModel):
    id: str
    series_id: str
    status: str
    requested_count: int
    completed_count: int
    failed_count: int
    moderation_blocked: int
    concurrency: int
    daily_cost_cap_usd: float
    total_cost_usd: float
    abort_reason: Optional[str] = None
    celery_task_id: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    # 全局去重:本批里主体已被【别的频道/客户】占用、因此跳过未创建的题目标题。
    # 空/None = 全部创建成功。批内部分冲突时其余照常创建,这里告诉前端哪些被跳过。
    skipped_duplicates: Optional[list[str]] = None

    model_config = {"from_attributes": True}
