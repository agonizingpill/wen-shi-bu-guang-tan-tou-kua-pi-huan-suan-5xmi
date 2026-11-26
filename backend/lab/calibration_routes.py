from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from .auth import Current, Db, Supervisor, Technician
from .calibration_models import Calibration, ReleaseSlot
from .calibration_schema import FitRequest
from .calibration_service import create_candidate, map_calibration, publish

router = APIRouter(prefix="/calibrations", tags=["分位系数"])


@router.get("")
def versions(db: Db, _operator: Current, probe_id: int | None = None):
    query = select(Calibration).order_by(Calibration.id.desc())
    if probe_id is not None:
        query = query.where(Calibration.probe_id == probe_id)
    return [map_calibration(row) for row in db.scalars(query.limit(80))]


@router.get("/active")
def released(db: Db, _operator: Current):
    # 局部替换后同一校准可占多个槽位：按校准去重，并回带各槽位的分钟窗口。
    rows = db.scalars(select(Calibration).join(ReleaseSlot, ReleaseSlot.calibration_id == Calibration.id)
                      .order_by(ReleaseSlot.day.desc(), ReleaseSlot.start_minute)).all()
    windows = {}
    for slot in db.scalars(select(ReleaseSlot).order_by(ReleaseSlot.start_minute)):
        windows.setdefault(slot.calibration_id, []).append(
            {"start_minute": slot.start_minute, "end_minute": slot.end_minute,
             "day": slot.day.isoformat(), "mode": slot.mode})
    result = {}
    for row in rows:
        result.setdefault(row.id, map_calibration(row, with_model=False, slot_window=windows.get(row.id)))
    return list(result.values())


@router.get("/{calibration_id}")
def version(calibration_id: int, db: Db, _operator: Current):
    row = db.get(Calibration, calibration_id)
    if row is None:
        raise HTTPException(404, "找不到换算候选。")
    return map_calibration(row)


@router.post("", status_code=202)
def fit(data: FitRequest, db: Db, operator: Technician):
    return map_calibration(create_candidate(db, data, operator.username))


@router.post("/{calibration_id}/publish")
def approve(calibration_id: int, db: Db, operator: Supervisor):
    return map_calibration(publish(db, calibration_id, operator.username))
