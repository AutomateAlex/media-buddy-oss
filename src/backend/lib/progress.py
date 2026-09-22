"""Lightweight per-stage progress tracking — JSON files in pipeline_dir/<project_id>/.

Replaces OpenMontage's strict checkpoint system for our MVP.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _path(pipeline_dir: Path, project_id: str, stage: str) -> Path:
    return pipeline_dir / project_id / f"{stage}.progress.json"


def write_progress(
    pipeline_dir: Path,
    project_id: str,
    stage: str,
    status: str,
    artifacts: dict[str, Any],
    error: Optional[str] = None,
) -> Path:
    """Write a progress file for a given stage."""
    path = _path(pipeline_dir, project_id, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "project_id": project_id,
        "stage": stage,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifacts,
    }
    if error is not None:
        data["error"] = error
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def read_progress(
    pipeline_dir: Path, project_id: str, stage: str
) -> Optional[dict[str, Any]]:
    """Return the progress dict for a stage, or None if not yet written."""
    path = _path(pipeline_dir, project_id, stage)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
