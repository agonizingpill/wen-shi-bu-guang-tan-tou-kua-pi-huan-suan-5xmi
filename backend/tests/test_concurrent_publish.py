import pytest
from fastapi import HTTPException

from lab.calibration_service import active_slots, publish
from lab.constants import Mode

from .conftest import REFERENCE_DAY_RELEASE
from .test_calibration_service import _incumbent, _recapture, fit_candidate


def test_two_simultaneous_candidates_for_same_window_second_is_returned(db):
    # 同一中段的两个候选在任何切换发生前同时提交（各自钉扎旧版本）。
    # 第一个签发后，第二个的替换段钉扎必然失效 → 退回，槽位不会重叠。
    # MySQL 下 publish 持有探头行锁与槽位行锁，真实并发被串行化为该顺序。
    probe, old, _ = _incumbent(db, end=450)
    _recapture(db, probe.id, 380, 410)
    first = fit_candidate(db, probe.id, 380, 410)
    second = fit_candidate(db, probe.id, 380, 410)
    db.commit()
    assert first.seam_context["replaced_calibration_id"] == old.id
    assert second.seam_context["replaced_calibration_id"] == old.id

    publish(db, first.id, "tang")
    with pytest.raises(HTTPException, match="替换段所用版本"):
        publish(db, second.id, "tang")

    slots = active_slots(db, probe.id, REFERENCE_DAY_RELEASE, Mode.HIGH)
    windows = sorted((slot.start_minute, slot.end_minute) for slot in slots)
    # 首个局部替换落地，旧版本保留两端余段；首尾相接，无重叠也无缺口。
    assert windows == [(360, 380), (380, 410), (410, 450)]
    for earlier, later in zip(windows, windows[1:]):
        assert earlier[1] == later[0]


def test_disjoint_simultaneous_candidates_both_publish(db):
    # 互不相交的两个中段候选：第一个落地不改变第二个的钉扎版本，两者都可签发。
    probe, old, _ = _incumbent(db, end=450)
    _recapture(db, probe.id, 370, 400)
    _recapture(db, probe.id, 410, 440)
    first = fit_candidate(db, probe.id, 370, 400)
    second = fit_candidate(db, probe.id, 410, 440)
    db.commit()

    publish(db, first.id, "tang")
    publish(db, second.id, "tang")  # 别处换了版本，本候选仍可签

    slots = active_slots(db, probe.id, REFERENCE_DAY_RELEASE, Mode.HIGH)
    windows = sorted((slot.start_minute, slot.end_minute) for slot in slots)
    assert windows == [(360, 370), (370, 400), (400, 410), (410, 440), (440, 450)]
