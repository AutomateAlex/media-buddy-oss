from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticUndefined
from typing import Optional, Literal
from datetime import datetime

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    mode: Literal["script", "creative"]
    script_text: Optional[str] = None
    prompt: Optional[str] = None
    reference_url: Optional[str] = None
    # When series_id is set and the caller does NOT provide output_format /
    # tts_provider, the API endpoint inherits those from the Series.
    # (Use model_dump(exclude_unset=True) to detect explicit-vs-default.)
    series_id: Optional[str] = None
    output_format: str = "youtube_landscape"
    # 短视频画面风格:"fill"(默认竖屏裁剪铺满)| "blur"(背景虚化+正方形前景)。
    framing_style: str = "fill"
    duration_seconds: Optional[float] = None
    tts_provider: str = "azure_yunyang"
    tts_speed: float = Field(1.0, ge=0.7, le=2.0)
    include_subtitles: bool = True
    # 背景音乐开关:默认关(躲 YouTube 版权投诉),客户想要才打开。
    include_music: bool = False
    # 输出语言:"zh"(默认)| "en"(出片前翻译成英文 + 英文音色)。Series 可继承。
    output_language: Literal["zh", "en"] = "zh"
    # 字幕语言(可独立于配音/输出语言):简体/繁体/英文/中英双语。
    # None → 出片时跟随 output_language(zh→zh-Hans、en→en),旧客户端不传也不受影响。
    subtitle_language: Optional[Literal["zh-Hans", "zh-Hant", "en", "zh-Hans+en", "zh-Hant+en"]] = None
    # 字幕字号缩放系数(客户拖拉杆自选)。1.0=标准;乘到默认字号。夹在 [0.5, 2.0]。
    subtitle_font_scale: float = Field(1.0, ge=0.5, le=2.0)
    # 执行文案「一字不改」:True → pipeline 跳过 CTA 合规删句(送礼/抽奖照上)。
    # 只有前端「执行文案」直出通道传 True;智能对话/批量走默认 False(照常合规过滤)。
    verbatim: bool = False
    # 只会得到同一条项目,不会变成两条。
    #
    # 起因:客户出片被 429 挡下(排队满),界面只显示看不懂的 `queue_full`,
    # 他以为没发出去 → 又建了一遍 → 同一份稿子出了两条片、扣了两次钱。
    #
    # ⚠️ 这只挡【同一次提交被重发】(双击/网络重试/客户端重发)。
    #    客户过几分钟自己主动再建一条是**故意的**,必须放行 —— 所以不做内容判重。
    # ⚠️ 批量 quantity=3 必须给**三个不同的键**,否则三条会被并成一条。
    idempotency_key: Optional[str] = Field(None, max_length=200)

class ProjectRead(BaseModel):
    id: str
    name: str
    mode: str
    status: str
    prompt: Optional[str] = None
    script_text: Optional[str] = None
    output_format: str
    framing_style: str = "fill"
    duration_seconds: Optional[float] = None
    tts_provider: str
    tts_speed: float = 1.0
    include_subtitles: bool = True
    include_music: bool = False
    output_language: str = "zh"
    subtitle_language: Optional[str] = None
    subtitle_font_scale: float = 1.0
    verbatim: bool = False
    current_stage: Optional[str] = None
    output_path: Optional[str] = None
    celery_task_id: Optional[str] = None
    series_id: Optional[str] = None
    # P1-7「再来一条」保真:非空=这条视频从频道「排队执行/批量」出的 → 再来一条回频道工作台;
    # 空=从单条工作台(对话式/粘贴)出的 → 回对应工作台。
    batch_run_id: Optional[str] = None
    studio_session_id: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    downloaded_at: Optional[datetime] = None
    # 存储保留:到期日 / 已清除时间 / 收藏意向(auto_expire|keep)。
    # 前端据 expires_at 派生"还剩X天"倒计时;storage_purged_at 非空显示"已到期清除"。
    expires_at: Optional[datetime] = None
    storage_purged_at: Optional[datetime] = None
    retention_intent: Optional[str] = None
    model_config = {"from_attributes": True}

    # A default here only applies when the attribute is ABSENT. A row that carries a
    # literal NULL (rows written outside the API — migrations, admin SQL, backfills)
    # hands Pydantic an explicit None, which fails the bool/float/str type check and
    # takes down the WHOLE list response. Coerce NULL back to the declared default so
    # one under-populated row can't do that.
    @field_validator(
        "framing_style", "tts_speed", "include_subtitles", "include_music",
        "output_language", "tts_provider", "mode", "status", "output_format", "name",
        "subtitle_font_scale", "verbatim",
        mode="before",
    )
    @classmethod
    def _null_to_default(cls, v, info):
        if v is not None:
            return v
        default = cls.model_fields[info.field_name].default
        # Fields with no declared default (name/mode/status/…) fall back to an empty
        # string rather than exploding — the row still shows up, visibly incomplete.
        if default is PydanticUndefined or default is None:
            return ""
        return default
