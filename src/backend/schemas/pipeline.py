from pydantic import BaseModel
from typing import Optional, Any

class PipelineRunRequest(BaseModel):
    project_id: str

class StageStatus(BaseModel):
    stage: str
    status: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    artifacts: dict[str, Any] = {}
    error: Optional[str] = None

class PipelineStatus(BaseModel):
    project_id: str
    overall_status: str
    current_stage: Optional[str] = None
    stages: list[StageStatus] = []
    output_path: Optional[str] = None
    celery_task_id: Optional[str] = None
    # 队列安全阀:排队中时给用户看「前面还有 N 条·预计 X 秒」,而不是干等/报错。
    queue_position: Optional[int] = None
    queue_eta_seconds: Optional[int] = None
    # 这个 pct 做精确进度条。桌面本地渲染/无 job 时为 None,前端回落到阶段估算。
    pct: Optional[float] = None
