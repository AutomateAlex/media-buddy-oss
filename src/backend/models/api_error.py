"""ApiError ORM — one row per failed API request (4xx/5xx).

Feeds the admin "操作错误" board (next to the suspect-videos board), so request-
level failures — a rejected batch, a failed generate, a 500 — surface centrally
them. The suspect-videos board only inspects PRODUCED videos; a request that is
rejected before any video exists is invisible to it. This table closes that gap.

Created via SQLAlchemy create_all on mb-api startup (same as the other tables).
Written best-effort from the global exception handlers — recording must never
break the response.
"""
from datetime import datetime, timezone
import uuid

from sqlalchemy import Column, String, Integer, DateTime

from backend.database import Base


class ApiError(Base):
    __tablename__ = "api_errors"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    status_code = Column(Integer, index=True)       # 400 / 409 / 422 / 429 / 5xx
    method = Column(String)                          # GET / POST / ...
    path = Column(String, index=True)                # normalized: UUIDs → {id}
    error_code = Column(String, index=True)          # detail.error if structured, else null
    user_id = Column(String, index=True)             # request owner, best-effort
