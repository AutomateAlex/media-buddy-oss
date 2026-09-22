"""Phase 2.10a — Studio Agent chat session.

A StudioSession represents one back-and-forth conversation between a customer
and the Studio Agent. It accumulates messages + a structured spec (the slots
the agent has filled so far) until the customer confirms a final spec, at
which point the session creates one or more Projects and ends.

A session is per-Project (or per-Batch): when the customer says "make this",
we materialize Projects and link them via project_ids. The session itself
stays in DB as a record of the brief.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String

from backend.database import Base


class StudioSession(Base):
    __tablename__ = "studio_sessions"

    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # 多租户 owner — NULL on single-user desktop; set per logged-in user on cloud.
    user_id    = Column(String, index=True, nullable=True)
    series_id  = Column(String, ForeignKey("series.id"), nullable=True, index=True)

    # Status: gathering | confirming | submitted | aborted
    status     = Column(String, default="gathering", index=True)

    # Conversation history. List of {role, content, ts, [tool_calls], [tool_results]}.
    # role ∈ {user, assistant, tool, system}
    messages   = Column(JSON, default=list)

    # Spec the agent has filled. Mirrors REQUIRED_SLOTS + OPTIONAL_SLOTS in studio_agent.py.
    # Example: {video_type: "promo", platform: "tiktok", count: 5, ...}
    spec       = Column(JSON, default=dict)

    # When the agent has called submit_project / submit_batch successfully,
    # we record what was created so the UI can link to it.
    created_project_ids  = Column(JSON, default=list)
    created_batch_run_id = Column(String)

    # Bookkeeping
    turn_count = Column(Integer, default=0)
    total_cost_usd = Column(Integer, default=0)  # accumulated LLM cost in cents

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime)
