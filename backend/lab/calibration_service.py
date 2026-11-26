import logging
from uuid import uuid4

from fastapi import HTTPException
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from .audit import record
from .calibration_models import Calibration, CalibrationSource, ReleaseSlot
from .constants import CalibrationState, SegmentState
from .installation import require_probe
from .quantile_map import InvalidReference, build_mapping, require_coverage, seam_check
from .segment_models import ReferenceSegment
from .settings import CHANNEL, REDIS_URL

logger = logging.getLogger("light.calibration")


def map_calibration(row, *, with_model=True, slot_window=None):
    return {"id": row.id, "probe_id": row.probe_id, "mode": row.mode,
            "reference_day": row.reference_day.isoformat(), "release_day": row.release_day.isoformat(),
            "start_minute": row.start_minute, "end_minute": row.end_minute, "version": row.version,
            "state": row.state, "submitted_by": row.submitted_by, "approved_by": row.approved_by,
            "explanation": row.explanation, "segment_ids": sorted(s.segment_id for s in row.segments),
            "seam_context": row.seam_context, "slot_window": slot_window,
            "model": row.model if with_model else None}


def create_candidate(db, data, actor):
    probe = require_probe(db, data.probe_id, lock=True)
    if not probe.enabled:
        raise HTTPException(409, "暂停的探头不能拟合新换算。")
    row = Calibration(**data.model_dump(), version="Q-" + uuid4().hex[:12].upper(),
                      state=CalibrationState.QUEUED, submitted_by=actor)
    db.add(row)
    record(db, actor, "queue_fit", row.version)
    db.commit()
    try:
        Redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2).lpush(CHANNEL, row.id)
    except RedisError:
        logger.warning("Redis unavailable; worker polls durable candidate %s", row.version)
    return row


def active_slots(db, probe_id, day, mode, *, lock=False):
    query = select(ReleaseSlot).where(
        ReleaseSlot.probe_id == probe_id, ReleaseSlot.day == day, ReleaseSlot.mode == mode,
    ).order_by(ReleaseSlot.start_minute)
    if lock:
        query = query.with_for_update()
    return list(db.scalars(query))


def find_slot(slots, minute):
    """返回覆盖 minute 的现行槽（槽间首尾相接，分钟半开区间）。"""
    for slot in slots:
        if slot.start_minute <= minute < slot.end_minute:
            return slot
    return None


def find_containing_slot(slots, start, end):
    """完整包含 [start, end) 的槽；只覆盖一部分的不算。"""
    for slot in slots:
        if slot.start_minute <= start and end <= slot.end_minute:
            return slot
    return None


def _neighbor_snapshot(db, slot, side):
    if slot is None:
        return None
    neighbor = db.get(Calibration, slot.calibration_id)
    return {"calibration_id": neighbor.id, "version": neighbor.version, "side": side,
            "start_minute": slot.start_minute, "end_minute": slot.end_minute}


def _build_seam_context(db, row, slots):
    """拟合时确定签发形态，并钉扎替换段与两端接缝的版本。"""
    containing = find_containing_slot(slots, row.start_minute, row.end_minute)
    if containing is None:
        # 与现行槽贴边但落在外侧的候选不是局部替换，避免绕开接缝核对。
        adjacent = find_slot(slots, row.start_minute - 1) or find_slot(slots, row.end_minute)
        if adjacent is not None:
            raise InvalidReference(
                "候选窗口落在现行窗口外侧，补采曲线只能签进原窗口的中间一段。"
            )
        kind, replaced, replaced_id = "fresh", None, None
    elif (containing.start_minute, containing.end_minute) == (row.start_minute, row.end_minute):
        kind, replaced, replaced_id = (
            "full", _neighbor_snapshot(db, containing, "replaced"), containing.calibration_id)
    else:
        if containing.start_minute == row.start_minute or containing.end_minute == row.end_minute:
            raise InvalidReference(
                "补采曲线只能签进原窗口的中间一段；请把两侧时段留给紧邻现行曲线。"
            )
        kind, replaced, replaced_id = (
            "partial", _neighbor_snapshot(db, containing, "replaced"), containing.calibration_id)

    neighbors = []
    checks = []
    for side, label, slot in (
            ("left", "左端接缝", find_slot(slots, row.start_minute - 1)),
            ("right", "右端接缝", find_slot(slots, row.end_minute))):
        if slot is None:
            continue
        neighbors.append(_neighbor_snapshot(db, slot, side))
        checks.append((label, db.get(Calibration, slot.calibration_id).model))
    return {"kind": kind, "replaced": replaced, "replaced_calibration_id": replaced_id,
            "neighbors": neighbors, "checks": checks}


def try_fit(db):
    row = db.scalar(select(Calibration).where(Calibration.state == CalibrationState.QUEUED)
                    .order_by(Calibration.id).limit(1).with_for_update(skip_locked=True))
    if row is None:
        return False
    segments = list(db.scalars(select(ReferenceSegment).where(
        ReferenceSegment.probe_id == row.probe_id, ReferenceSegment.day == row.reference_day,
        ReferenceSegment.mode == row.mode, ReferenceSegment.start_minute >= row.start_minute,
        ReferenceSegment.start_minute < row.end_minute, ReferenceSegment.state == SegmentState.WITNESSED,
    ).order_by(ReferenceSegment.start_minute)))
    try:
        require_coverage(row.start_minute, row.end_minute, segments)
        row.model = build_mapping(segments)
        seam_context = _build_seam_context(
            db, row, active_slots(db, row.probe_id, row.release_day, row.mode))
        # 两端各与紧邻曲线比：无重叠区间或重叠区换算差超 20 lx 即拒签。
        seam_metrics = {label: seam_check(row.model, neighbor_model, label)
                        for label, neighbor_model in seam_context["checks"]}
    except InvalidReference as exc:
        row.state, row.explanation, row.seam_context = CalibrationState.REJECTED, str(exc), None
    else:
        seam_context.pop("checks")
        seam_context["seam_metrics"] = seam_metrics
        row.seam_context = seam_context
        row.segments.extend(CalibrationSource(segment_id=s.id) for s in segments)
        row.state = CalibrationState.READY
        row.explanation = {
            "partial": "局部替换候选已通过接缝核对，等待班长签发。",
            "full": "整窗替换候选拟合通过，等待班长签发。",
            "fresh": "连续见证齐备，等待班长签发。",
        }[seam_context["kind"]]
    record(db, "worker", "fit_" + row.state, row.version)
    db.commit()
    return True


def _revalidate_seams(db, candidate, slots):
    """签发时按钉扎的版本清单复核；任一版本与拟合时不同即退回。"""
    context = candidate.seam_context
    if context is None:
        raise HTTPException(409, "候选缺少拟合时的接缝版本记录，不能签发。")
    kind = context["kind"]

    if kind in ("full", "partial"):
        replaced_slot = find_containing_slot(slots, candidate.start_minute, candidate.end_minute)
        if replaced_slot is None or replaced_slot.calibration_id != context["replaced_calibration_id"]:
            raise HTTPException(409, "替换段所用版本与拟合时不同，本次签发退回。")
        if kind == "full" and (replaced_slot.start_minute, replaced_slot.end_minute) != (
                candidate.start_minute, candidate.end_minute):
            raise HTTPException(409, "替换段所用版本与拟合时不同，本次签发退回。")

    for pinned in context.get("neighbors", []):
        probe_minute = candidate.start_minute - 1 if pinned["side"] == "left" else candidate.end_minute
        current_slot = find_slot(slots, probe_minute)
        if current_slot is None or current_slot.calibration_id != pinned["calibration_id"]:
            raise HTTPException(409, "接缝所用版本与拟合时不同，本次签发退回。")
        # 模型不可变；复核通过后再跑一遍数值接缝检查作为纵深防御。
        neighbor = db.get(Calibration, current_slot.calibration_id)
        label = "左端接缝" if pinned["side"] == "left" else "右端接缝"
        try:
            seam_check(candidate.model, neighbor.model, label)
        except InvalidReference as exc:
            raise HTTPException(409, str(exc)) from exc


def publish(db, calibration_id, actor):
    candidate = db.get(Calibration, calibration_id)
    if candidate is None:
        raise HTTPException(404, "找不到换算候选。")
    probe = require_probe(db, candidate.probe_id, lock=True)
    db.refresh(candidate, with_for_update=True)
    if not probe.enabled:
        raise HTTPException(409, "探头已暂停，无法替换现行版本。")
    if candidate.state != CalibrationState.READY:
        raise HTTPException(409, "仅连续见证并拟合通过的候选可发布；现行版本保留。")
    if candidate.submitted_by == actor:
        raise HTTPException(409, "签发人须独立于拟合提交人。")

    # 安装点行锁之上锁定该日该档全部槽位，同事务完成一次性切换。
    slots = active_slots(db, candidate.probe_id, candidate.release_day, candidate.mode, lock=True)
    _revalidate_seams(db, candidate, slots)
    kind = candidate.seam_context["kind"]

    if kind == "fresh":
        db.add(ReleaseSlot(probe_id=candidate.probe_id, day=candidate.release_day,
                           mode=candidate.mode, start_minute=candidate.start_minute,
                           end_minute=candidate.end_minute, calibration_id=candidate.id))
        referenced = {slot.calibration_id for slot in slots}
    elif kind == "full":
        target = find_containing_slot(slots, candidate.start_minute, candidate.end_minute)
        previous_id = target.calibration_id
        target.calibration_id = candidate.id
        referenced = {slot.calibration_id for slot in slots}
    else:
        target = find_containing_slot(slots, candidate.start_minute, candidate.end_minute)
        previous_id = target.calibration_id
        original_start, original_end = target.start_minute, target.end_minute
        if original_start < candidate.start_minute:
            target.end_minute = candidate.start_minute  # 左侧余段继续引用旧版本
        else:
            db.delete(target)
            slots.remove(target)
        if candidate.end_minute < original_end:
            right = ReleaseSlot(probe_id=candidate.probe_id, day=candidate.release_day,
                                mode=candidate.mode, start_minute=candidate.end_minute,
                                end_minute=original_end, calibration_id=previous_id)
            db.add(right)
            slots.append(right)
        db.add(ReleaseSlot(probe_id=candidate.probe_id, day=candidate.release_day,
                           mode=candidate.mode, start_minute=candidate.start_minute,
                           end_minute=candidate.end_minute, calibration_id=candidate.id))
        referenced = {slot.calibration_id for slot in slots} | {candidate.id}

    # 切换后不再被任何槽位引用的旧校准进入「已替换」；两端余段仍引用则保持现行。
    if kind in ("full", "partial") and previous_id not in referenced:
        previous = db.get(Calibration, previous_id)
        if previous is not None and previous.state == CalibrationState.ACTIVE:
            previous.state = CalibrationState.RETIRED

    candidate.state, candidate.approved_by = CalibrationState.ACTIVE, actor
    record(db, actor, "publish_mapping", f"{candidate.version}/{kind}")
    db.commit()
    return candidate
