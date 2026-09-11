"""按版本保存设备、规则与校核结果的表结构。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Version(Base):
    """一个版本 = 设备 + 规则（锁条表、进路、冲突/接近锁闭/区段释放配置）的快照。"""
    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, default="")
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("versions.id"), nullable=True)
    spec: Mapped[dict] = mapped_column(JSON)          # Spec.model_dump()
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class Run(Base):
    """一次校核结果，挂在某个版本下。"""
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"), index=True)
    kind: Mapped[str] = mapped_column(String, default="verify")  # verify / revise
    ok: Mapped[bool] = mapped_column(Boolean)
    complete: Mapped[bool] = mapped_column(Boolean)
    states: Mapped[int] = mapped_column(Integer)
    violations_count: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict] = mapped_column(JSON)        # verify()/revise() 的完整输出
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
