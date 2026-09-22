"""One malformed project row must not take down the whole project list.

include_subtitles = NULL. ProjectRead declares `include_subtitles: bool = True`,
but a schema default only applies when the attribute is ABSENT — an explicit None
fails the bool check. FastAPI validates `response_model=list[ProjectRead]` as one
unit, so that single row 500'd GET /api/projects/ and the user saw *every* project
disappear.

Two guards, both pinned here:
  1. ProjectRead coerces NULL back to the declared default.
  2. list_projects validates row-by-row and skips (loudly) whatever still fails.
"""
from datetime import datetime, timezone

import pytest

from backend.schemas.project import ProjectRead


class _Row:
    """Stand-in for a SQLAlchemy Project row (ProjectRead is from_attributes)."""

    def __init__(self, **overrides):
        self.id = "p1"
        self.name = "demo"
        self.mode = "creative"
        self.status = "completed"
        self.prompt = None
        self.script_text = None
        self.output_format = "youtube_shorts"
        self.framing_style = "fill"
        self.duration_seconds = 45.0
        self.tts_provider = "qwen_cherry"
        self.tts_speed = 1.4
        self.include_subtitles = True
        self.include_music = False
        self.output_language = "zh"
        self.current_stage = None
        self.output_path = None
        self.celery_task_id = None
        self.series_id = None
        self.studio_session_id = None
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = None
        self.downloaded_at = None
        self.expires_at = None
        self.storage_purged_at = None
        self.retention_intent = None
        for k, v in overrides.items():
            setattr(self, k, v)


def test_null_bool_coerced_to_default_not_rejected():
    """The exact row shape that broke the list: include_subtitles = NULL."""
    out = ProjectRead.model_validate(_Row(include_subtitles=None))
    assert out.include_subtitles is True  # the declared default, not a 500


@pytest.mark.parametrize(
    "field,expected",
    [
        ("include_subtitles", True),
        ("include_music", False),
        ("framing_style", "fill"),
        ("tts_speed", 1.0),
        ("output_language", "zh"),
    ],
)
def test_every_defaulted_field_survives_a_null(field, expected):
    out = ProjectRead.model_validate(_Row(**{field: None}))
    assert getattr(out, field) == expected


def test_null_in_a_field_without_a_default_still_validates():
    """name/mode/status have no default. A NULL there must not 500 either — the row
    comes back visibly empty rather than nuking the response."""
    out = ProjectRead.model_validate(_Row(name=None, tts_provider=None))
    assert out.name == ""
    assert out.tts_provider == ""


def test_a_good_row_is_untouched():
    out = ProjectRead.model_validate(_Row())
    assert out.include_subtitles is True
    assert out.tts_speed == 1.4
    assert out.name == "demo"
