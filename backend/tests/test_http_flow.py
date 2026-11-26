import os
import tempfile

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///" + _DB_FILE.name
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ["SEED_DEMO"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
import pytest  # noqa: E402

from lab.app import app  # noqa: E402
from lab.database import Base, engine, open_session  # noqa: E402
from lab.auth import hash_password  # noqa: E402
from lab.identity_models import Operator  # noqa: E402
from lab.constants import CalibrationState, Mode  # noqa: E402
from lab.calibration_service import active_slots  # noqa: E402
import lab.calibration_service as calibration_service  # noqa: E402

from .conftest import (  # noqa: E402
    RECAPTURE_DAY,
    REFERENCE_DAY_RELEASE,
    _FakeRedis,
    add_calibration,
    add_probe,
    add_segment,
    add_slot,
)


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    monkeypatch.setattr(calibration_service, "Redis", _FakeRedis)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with open_session() as db:
        for username, role in (("yu", "technician"), ("tang", "supervisor")):
            db.add(Operator(username=username, role=role, display_name=username,
                            password_digest=hash_password("SeedLight2026!")))
        db.commit()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as client:
        yield client


def _token(client, username):
    response = client.post("/auth/login", json={"username": username, "password": "SeedLight2026!"})
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _capture(client, token, probe_id, start, *, day, raw_shift=0.0, ref_shift=0.0):
    block = (start - 360) // 10
    points = [{"offset_seconds": i * 90,
               "raw_lux": 360 + 60 * i + block * 16 + raw_shift,
               "reference_lux": 418 + 62 * i + block * 17 + ref_shift} for i in range(6)]
    response = client.post("/segments", headers=_auth(token), json={
        "probe_id": probe_id, "day": day.isoformat(), "mode": Mode.HIGH,
        "start_minute": start, "reference_serial": "REF-072",
        "source": f"east-{start}.csv", "points": points})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _fit(client, token, probe_id, start, end, *, reference_day):
    response = client.post("/calibrations", headers=_auth(token), json={
        "probe_id": probe_id, "mode": Mode.HIGH,
        "reference_day": reference_day.isoformat(),
        "release_day": REFERENCE_DAY_RELEASE.isoformat(),
        "start_minute": start, "end_minute": end})
    assert response.status_code == 202, response.text
    candidate_id = response.json()["id"]
    # 测试环境没有 worker 进程：直接执行与 worker 主循环相同的拟合步骤。
    with open_session() as worker_db:
        assert calibration_service.try_fit(worker_db) is True
    return candidate_id


def test_full_http_partial_replace_flow(client):
    yu, tang = _token(client, "yu"), _token(client, "tang")
    with open_session() as db:
        probe = add_probe(db)
        segments = [add_segment(db, probe.id, m) for m in range(360, 420, 10)]
        old = add_calibration(db, probe.id, 360, 420, segments, "Q-OLD")
        add_slot(db, probe.id, old.id, 360, 420)
        db.commit()
        probe_id = probe.id
        old_id = old.id

    # 补采中段三段并见证。
    segment_ids = [_capture(client, yu, probe_id, m, day=RECAPTURE_DAY) for m in range(380, 410, 10)]
    for segment_id in segment_ids:
        response = client.post(f"/segments/{segment_id}/witness", headers=_auth(tang),
                               json={"note": "补采段与基准表同秒核对无误。"})
        assert response.status_code == 200, response.text

    candidate_id = _fit(client, yu, probe_id, 380, 410, reference_day=RECAPTURE_DAY)
    candidate = client.get(f"/calibrations/{candidate_id}", headers=_auth(yu)).json()
    assert candidate["state"] == CalibrationState.READY
    assert candidate["seam_context"]["kind"] == "partial"
    assert set(candidate["seam_context"]["seam_metrics"]) == {"左端接缝", "右端接缝"}

    # 采样岗不能自己签发。
    forbidden = client.post(f"/calibrations/{candidate_id}/publish", headers=_auth(yu))
    assert forbidden.status_code == 403

    published = client.post(f"/calibrations/{candidate_id}/publish", headers=_auth(tang))
    assert published.status_code == 200, published.text
    assert published.json()["state"] == CalibrationState.ACTIVE

    with open_session() as db:
        slots = active_slots(db, probe_id, REFERENCE_DAY_RELEASE, Mode.HIGH)
        assert [(s.start_minute, s.end_minute) for s in slots] == [
            (360, 380), (380, 410), (410, 420)]

    # 现行单按校准去重（旧校准两槽只出现一次），并带槽位窗口。
    active = client.get("/calibrations/active", headers=_auth(yu)).json()
    versions = {row["id"]: row for row in active}
    assert len(active) == 2
    assert len(versions[old_id]["slot_window"]) == 2

    # 读数按时刻取版本。
    for minute, expected_id in ((365, old_id), (395, candidate_id), (415, old_id)):
        response = client.post("/readings", headers=_auth(yu), json={
            "probe_id": probe_id, "day": REFERENCE_DAY_RELEASE.isoformat(),
            "mode": Mode.HIGH, "minute": minute, "raw_lux": 480})
        assert response.status_code == 201, response.text
        assert response.json()["calibration_id"] == expected_id


def test_http_seam_rejection_blocks_publish(client):
    yu = _token(client, "yu")
    with open_session() as db:
        probe = add_probe(db)
        segments = [add_segment(db, probe.id, m) for m in range(360, 420, 10)]
        old = add_calibration(db, probe.id, 360, 420, segments, "Q-OLD")
        add_slot(db, probe.id, old.id, 360, 420)
        db.commit()
        probe_id = probe.id

    for minute in range(380, 410, 10):
        segment_id = _capture(client, yu, probe_id, minute, day=RECAPTURE_DAY,
                              raw_shift=900, ref_shift=950)
        client.post(f"/segments/{segment_id}/witness", headers=_auth(_token(client, "tang")),
                    json={"note": "抬升量程的补采段。"})
    candidate_id = _fit(client, yu, probe_id, 380, 410, reference_day=RECAPTURE_DAY)
    candidate = client.get(f"/calibrations/{candidate_id}", headers=_auth(yu)).json()
    assert candidate["state"] == CalibrationState.REJECTED
    assert "没有重叠区间" in candidate["explanation"]
    # 已拒绝候选不能签发。
    response = client.post(f"/calibrations/{candidate_id}/publish",
                           headers=_auth(_token(client, "tang")))
    assert response.status_code == 409
