from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from lab.calibration_service import active_slots, find_slot, publish
from lab.constants import CalibrationState, Mode
from lab.readings_models import ConversionReceipt
from lab.quantile_map import adjust

from .conftest import (
    RECAPTURE_DAY,
    REFERENCE_DAY_RELEASE,
    REFERENCE_DAY_SAMPLE,
    add_calibration,
    add_probe,
    add_segment,
    add_slot,
    fit_candidate,
)


def _incumbent(db, *, start=360, end=420, version="Q-OLD", code="EQ-214"):
    probe = add_probe(db, code=code)
    segments = [add_segment(db, probe.id, minute) for minute in range(start, end, 10)]
    calibration = add_calibration(db, probe.id, start, end, segments, version)
    add_slot(db, probe.id, calibration.id, start, end)
    db.commit()
    return probe, calibration, segments


def _recapture(db, probe_id, start, end, *, raw_shift=0.0, ref_shift=0.0):
    return [add_segment(db, probe_id, minute, day=RECAPTURE_DAY,
                        raw_shift=raw_shift, ref_shift=ref_shift)
            for minute in range(start, end, 10)]


def test_partial_publish_keeps_neighbors_and_old_receipts(db):
    probe, old, _ = _incumbent(db)
    # 06:12 的旧凭证在切换前已入账。
    old_value = round(float(adjust(old.model, [480])[0]), 2)
    db.add(ConversionReceipt(probe_id=probe.id, calibration_id=old.id,
                             day=REFERENCE_DAY_RELEASE, mode=Mode.HIGH, minute=372,
                             raw_lux=480, adjusted_lux=old_value, entered_by="yu"))
    db.commit()

    _recapture(db, probe.id, 380, 410)  # 中间三段
    candidate = fit_candidate(db, probe.id, 380, 410)
    assert candidate.state == CalibrationState.READY
    assert candidate.seam_context["kind"] == "partial"
    assert {pinned["calibration_id"] for pinned in candidate.seam_context["neighbors"]} == {old.id}
    assert candidate.seam_context["replaced_calibration_id"] == old.id
    assert set(candidate.seam_context["seam_metrics"]) == {"左端接缝", "右端接缝"}

    published = publish(db, candidate.id, "tang")
    slots = active_slots(db, probe.id, REFERENCE_DAY_RELEASE, Mode.HIGH)
    assert [(s.start_minute, s.end_minute, s.calibration_id) for s in slots] == [
        (360, 380, old.id), (380, 410, published.id), (410, 420, old.id)]
    # 旧校准仍被两端余段引用，保持现行；新候选现行。
    db.refresh(old)
    assert old.state == CalibrationState.ACTIVE
    assert published.state == CalibrationState.ACTIVE

    # 新读数按时刻取版本：中段用新候选，两端仍用旧校准。
    middle_slot = find_slot(slots, 395)
    edge_slot = find_slot(slots, 365)
    assert middle_slot.calibration_id == published.id
    assert edge_slot.calibration_id == old.id

    # 已入账凭证照旧引用原校准，换算值不被改写。
    receipt = db.scalar(select(ConversionReceipt).where(ConversionReceipt.minute == 372))
    assert receipt.calibration_id == old.id
    assert receipt.adjusted_lux == old_value


def test_seam_rejected_when_ranges_do_not_overlap(db):
    probe, old, _ = _incumbent(db)
    # 补采数据整体抬升到与现行曲线量程不相交。
    _recapture(db, probe.id, 380, 410, raw_shift=900, ref_shift=950)
    candidate = fit_candidate(db, probe.id, 380, 410)
    assert candidate.state == CalibrationState.REJECTED
    assert "没有重叠区间" in candidate.explanation
    assert candidate.seam_context is None
    # 现行槽纹丝不动。
    slots = active_slots(db, probe.id, REFERENCE_DAY_RELEASE, Mode.HIGH)
    assert [(slot.start_minute, slot.end_minute, slot.calibration_id) for slot in slots] == [
        (360, 420, old.id)]


def test_seam_rejected_when_delta_exceeds_20_lux(db):
    probe, old, _ = _incumbent(db)
    _recapture(db, probe.id, 380, 410, raw_shift=0, ref_shift=60)
    candidate = fit_candidate(db, probe.id, 380, 410)
    assert candidate.state == CalibrationState.REJECTED
    assert "20 lx" in candidate.explanation


def test_version_change_elsewhere_allowed_but_replaced_change_returns(db):
    # 情景一：别的档位先签发新版本，不影响本候选。
    probe, old, _ = _incumbent(db)
    _recapture(db, probe.id, 380, 410)
    candidate = fit_candidate(db, probe.id, 380, 410)
    low_segments = [add_segment(db, probe.id, 300 + i * 10, mode=Mode.LOW) for i in range(3)]
    low = add_calibration(db, probe.id, 300, 330, low_segments, "Q-LOW", mode=Mode.LOW)
    add_slot(db, probe.id, low.id, 300, 330, mode=Mode.LOW)
    db.commit()
    publish(db, candidate.id, "tang")
    slots = active_slots(db, probe.id, REFERENCE_DAY_RELEASE, Mode.HIGH)
    assert find_slot(slots, 395).calibration_id == candidate.id

    # 情景二：候选待签发期间，替换段被整窗换掉 → 必须退回。
    probe2, old2, _ = _incumbent(db, start=480, end=540, version="Q-OLD2", code="EQ-215")
    _recapture(db, probe2.id, 500, 530)
    blocked = fit_candidate(db, probe2.id, 500, 530)

    # 整窗替换用另一基准日的补采段，与 blocked 的补采段错开自然键。
    full_day = date(2026, 11, 22)
    [add_segment(db, probe2.id, m, day=full_day) for m in range(480, 540, 10)]
    full = fit_candidate(db, probe2.id, 480, 540, reference_day=full_day)
    assert full.seam_context["kind"] == "full"
    full.submitted_by = "yu"
    db.commit()
    publish(db, full.id, "tang")
    with pytest.raises(HTTPException, match="替换段所用版本"):
        publish(db, blocked.id, "tang")


def test_full_window_replacement_retires_old_calibration(db):
    probe, old, _ = _incumbent(db)
    [add_segment(db, probe.id, m, day=RECAPTURE_DAY) for m in range(360, 420, 10)]
    candidate = fit_candidate(db, probe.id, 360, 420, reference_day=RECAPTURE_DAY)
    assert candidate.seam_context["kind"] == "full"
    publish(db, candidate.id, "tang")
    slots = active_slots(db, probe.id, REFERENCE_DAY_RELEASE, Mode.HIGH)
    assert len(slots) == 1 and slots[0].calibration_id == candidate.id
    db.refresh(old)
    assert old.state == CalibrationState.RETIRED


def test_edge_to_edge_candidate_cannot_be_partial(db):
    probe, old, _ = _incumbent(db)
    # 候选起点等于原窗口起点，不允许伪装成局部替换。
    _recapture(db, probe.id, 360, 390)
    candidate = fit_candidate(db, probe.id, 360, 390)
    assert candidate.state == CalibrationState.REJECTED
    assert "中间一段" in candidate.explanation


def test_two_adjacent_partials_publish_atomically(db):
    # 首个局部替换落地后，剩余右段可再被整段替换，槽位首尾相接。
    probe, old, _ = _incumbent(db, end=450)
    _recapture(db, probe.id, 380, 410)
    first = fit_candidate(db, probe.id, 380, 410)
    assert first.seam_context["kind"] == "partial"
    publish(db, first.id, "tang")

    [add_segment(db, probe.id, m, day=RECAPTURE_DAY) for m in (410, 420, 430, 440)]
    second = fit_candidate(db, probe.id, 410, 450, reference_day=RECAPTURE_DAY)
    # 410-450 与现行右余段等宽 → 该余段整窗替换，合法。
    assert second.seam_context["kind"] == "full"
    publish(db, second.id, "tang")
    slots = active_slots(db, probe.id, REFERENCE_DAY_RELEASE, Mode.HIGH)
    assert [(slot.start_minute, slot.end_minute) for slot in slots] == [
        (360, 380), (380, 410), (410, 450)]


def test_concurrent_overlapping_partial_second_is_returned(db):
    probe, old, _ = _incumbent(db)
    _recapture(db, probe.id, 380, 410)
    first = fit_candidate(db, probe.id, 380, 410)
    publish(db, first.id, "tang")

    # 另一候选拟合时钉扎的是旧版本；切换后替换段已不是它 → 退回。
    stale_day = date(2026, 11, 22)
    stale_segments = [add_segment(db, probe.id, m, day=stale_day,
                                  raw_shift=2, ref_shift=2) for m in (380, 390, 400)]
    stale = add_calibration(db, probe.id, 380, 410, stale_segments, "Q-STALE",
                            reference_day=stale_day, state=CalibrationState.READY)
    stale.seam_context = {
        "kind": "partial", "replaced_calibration_id": old.id,
        "replaced": {"calibration_id": old.id, "version": old.version,
                     "start_minute": 360, "end_minute": 420},
        "neighbors": [{"calibration_id": old.id, "version": old.version, "side": "left",
                       "start_minute": 360, "end_minute": 420},
                      {"calibration_id": old.id, "version": old.version, "side": "right",
                       "start_minute": 360, "end_minute": 420}],
        "seam_metrics": {},
    }
    db.commit()
    with pytest.raises(HTTPException, match="替换段所用版本"):
        publish(db, stale.id, "tang")


def test_publish_returned_when_neighbor_version_changed(db):
    # 候选钉扎后，紧邻余段的版本被另一次局部切换换掉 → 接缝复核退回。
    probe, old, _ = _incumbent(db, end=450)
    _recapture(db, probe.id, 400, 440)
    blocked = fit_candidate(db, probe.id, 400, 440)
    assert blocked.seam_context["kind"] == "partial"

    # 先在左端余段做一次局部替换，改变 blocked 左端接缝的现行版本。
    _recapture(db, probe.id, 370, 400)
    first = fit_candidate(db, probe.id, 370, 400)
    publish(db, first.id, "tang")

    with pytest.raises(HTTPException, match="接缝所用版本"):
        publish(db, blocked.id, "tang")


def test_candidate_outside_incumbent_window_is_rejected(db):
    probe, old, _ = _incumbent(db)
    # 与现行窗口右端贴边、落在外侧的候选不能按新开窗口绕开接缝核对。
    _recapture(db, probe.id, 420, 450)
    candidate = fit_candidate(db, probe.id, 420, 450)
    assert candidate.state == CalibrationState.REJECTED
    assert "外侧" in candidate.explanation


def test_fresh_window_without_incumbent_publishes_directly(db):
    probe = add_probe(db, code="WH-093", serial="REF-191")
    for minute in range(360, 390, 10):
        add_segment(db, probe.id, minute)
    candidate = fit_candidate(db, probe.id, 360, 390, reference_day=REFERENCE_DAY_SAMPLE)
    assert candidate.seam_context["kind"] == "fresh"
    publish(db, candidate.id, "tang")
    slots = active_slots(db, probe.id, REFERENCE_DAY_RELEASE, Mode.HIGH)
    assert len(slots) == 1 and slots[0].calibration_id == candidate.id
