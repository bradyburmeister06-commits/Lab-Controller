from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import SystemLog


def write_log(db: Session, level: str, component: str, message: str) -> None:
    db.add(SystemLog(level=level.upper(), component=component, message=message))
    db.commit()
