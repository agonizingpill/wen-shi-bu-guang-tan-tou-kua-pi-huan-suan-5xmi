from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Calibration(Base):
    __tablename__ = "calibrations"
    __table_args__ = (
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440 AND end_minute > start_minute",
                        name="valid_calibration_window"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    probe_id: Mapped[int] = mapped_column(ForeignKey("probes.id"))
    mode: Mapped[str] = mapped_column(String(8))
    reference_day: Mapped[date] = mapped_column(Date)
    release_day: Mapped[date] = mapped_column(Date)
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(20), unique=True)
    state: Mapped[str] = mapped_column(String(16), index=True)
    submitted_by: Mapped[str] = mapped_column(String(32))
    approved_by: Mapped[str | None] = mapped_column(String(32))
    explanation: Mapped[str | None] = mapped_column(String(250))
    model: Mapped[dict | None] = mapped_column(JSON)
    # 拟合时钉扎的替换段与两端接缝版本，签发时逐一复核。
    seam_context: Mapped[dict | None] = mapped_column(JSON)
    segments: Mapped[list["CalibrationSource"]] = relationship(lazy="selectin")


class CalibrationSource(Base):
    __tablename__ = "calibration_sources"

    calibration_id: Mapped[int] = mapped_column(ForeignKey("calibrations.id"), primary_key=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("reference_segments.id"), primary_key=True)


class ReleaseSlot(Base):
    """同一安装点、生效日、档位下按分钟区间切分的现行槽；局部替换后可有多段。"""

    __tablename__ = "release_slots"
    __table_args__ = (
        UniqueConstraint("probe_id", "day", "mode", "start_minute", name="unique_slot_start"),
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440 AND end_minute > start_minute",
                        name="valid_slot_window"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    probe_id: Mapped[int] = mapped_column(ForeignKey("probes.id"))
    day: Mapped[date] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(8))
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)
    # 同一校准可能同时占据局部替换后留下的两端余段，因此不再唯一。
    calibration_id: Mapped[int] = mapped_column(ForeignKey("calibrations.id"))
