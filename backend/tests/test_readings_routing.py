import pytest
from fastapi import HTTPException
from sqlalchemy import select

from lab.constants import Mode
from lab.readings import convert
from lab.readings_models import ConversionReceipt
from lab.readings import RawReading
from lab.calibration_service import publish

from .conftest import (
    REFERENCE_DAY_RELEASE,
    fit_candidate,
)
from .test_calibration_service import _incumbent, _recapture


def _reading(probe_id, minute, raw=480.0):
    return RawReading(probe_id=probe_id, day=REFERENCE_DAY_RELEASE, mode=Mode.HIGH,
                      minute=minute, raw_lux=raw)


def test_convert_picks_slot_by_minute_and_keeps_old_receipts(db):
    probe, old, _ = _incumbent(db)
    # 切换前的中段读数凭证。
    before = convert(_reading(probe.id, 395), db, type("Operator", (), {"username": "yu"})())
    assert before["calibration_id"] == old.id

    _recapture(db, probe.id, 380, 410)
    candidate = fit_candidate(db, probe.id, 380, 410)
    publish(db, candidate.id, "tang")

    # 切换后：中段用新候选，两端仍走旧校准。
    middle = convert(_reading(probe.id, 400), db, type("Operator", (), {"username": "yu"})())
    left = convert(_reading(probe.id, 365), db, type("Operator", (), {"username": "yu"})())
    right = convert(_reading(probe.id, 415), db, type("Operator", (), {"username": "yu"})())
    assert middle["calibration_id"] == candidate.id
    assert left["calibration_id"] == old.id
    assert right["calibration_id"] == old.id

    # 历史凭证仍然指向旧版本，换算值未被重算。
    receipts = db.scalars(select(ConversionReceipt).order_by(ConversionReceipt.minute)).all()
    assert [r.calibration_id for r in receipts] == [old.id, old.id, candidate.id, old.id]
    assert receipts[0].adjusted_lux == before["adjusted_lux"]

    # 槽外时刻禁止外推。
    with pytest.raises(HTTPException, match="没有有效换算单"):
        convert(_reading(probe.id, 300), db, type("Operator", (), {"username": "yu"})())

    # 同一安装点同一时刻只允许一条凭证（唯一约束仍生效）。
    with pytest.raises(Exception):
        convert(_reading(probe.id, 400), db, type("Operator", (), {"username": "yu"})())
        db.commit()


def test_convert_raw_out_of_range_still_rejected(db):
    probe, old, _ = _incumbent(db)
    with pytest.raises(HTTPException, match="超出见证样本范围"):
        convert(_reading(probe.id, 370, raw=99999.0),
                db, type("Operator", (), {"username": "yu"})())


def test_convert_works_inside_residual_slot_after_partial_switch(db):
    # 局部替换后，旧校准只剩两端余段槽位；余段内读数仍按旧版本正常换算，
    # 不能再拿旧校准自身的完整窗口误判。
    probe, old, _ = _incumbent(db, end=450)
    _recapture(db, probe.id, 380, 410)
    candidate = fit_candidate(db, probe.id, 380, 410)
    publish(db, candidate.id, "tang")

    actor = type("Operator", (), {"username": "yu"})()
    left = convert(_reading(probe.id, 365), db, actor)
    right = convert(_reading(probe.id, 445), db, actor)
    assert left["calibration_id"] == old.id
    assert right["calibration_id"] == old.id

    # 中段时刻走新版本。
    middle = convert(_reading(probe.id, 395), db, actor)
    assert middle["calibration_id"] == candidate.id
