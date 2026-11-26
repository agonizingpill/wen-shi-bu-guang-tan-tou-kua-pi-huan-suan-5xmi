import os
import tempfile

import pytest

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///" + _DB_FILE.name
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ["SEED_DEMO"] = "false"

from lab.constants import CalibrationState, Mode, SegmentState  # noqa: E402
from lab.database import Base, engine, open_session  # noqa: E402
from lab.installation_models import Probe  # noqa: E402
from lab.segment_models import ReferencePoint, ReferenceSegment  # noqa: E402
from lab.calibration_models import Calibration, CalibrationSource, ReleaseSlot  # noqa: E402
from lab.quantile_map import build_mapping  # noqa: E402
import lab.calibration_service as calibration_service  # noqa: E402

REFERENCE_DAY_RELEASE = __import__("datetime").date(2026, 11, 24)
REFERENCE_DAY_SAMPLE = __import__("datetime").date(2026, 11, 23)
RECAPTURE_DAY = __import__("datetime").date(2026, 11, 24)


class _FakeRedis:
    @classmethod
    def from_url(cls, *args, **kwargs):
        return cls()

    def lpush(self, *args, **kwargs):
        return 1

    def ping(self):
        return True


@pytest.fixture(autouse=True)
def stub_redis(monkeypatch):
    monkeypatch.setattr(calibration_service, "Redis", _FakeRedis)


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with open_session() as session:
        yield session


def make_points(block, raw_shift=0.0, ref_shift=0.0):
    return [
        ReferencePoint(offset_seconds=i * 90,
                       raw_lux=360 + 60 * i + block * 16 + raw_shift,
                       reference_lux=418 + 62 * i + block * 17 + ref_shift)
        for i in range(6)
    ]


def add_probe(db, code="EQ-214", serial="REF-072"):
    probe = Probe(code=code, bay="东棚 · 二号扦插台", batch="LX-26B",
                  reference_serial=serial, mount_cm=85, enabled=True)
    db.add(probe)
    db.flush()
    return probe


def add_segment(db, probe_id, start, *, day=REFERENCE_DAY_SAMPLE, mode=Mode.HIGH,
                raw_shift=0.0, ref_shift=0.0, state=SegmentState.WITNESSED):
    block = (start - 360) // 10
    segment = ReferenceSegment(
        probe_id=probe_id, day=day, mode=mode, start_minute=start,
        reference_serial="REF-072", source=f"east-{start}.csv", state=state,
        captured_by="yu", witnessed_by="tang" if state == SegmentState.WITNESSED else None,
        points=make_points(block, raw_shift, ref_shift),
    )
    db.add(segment)
    db.flush()
    return segment


def add_calibration(db, probe_id, start, end, segments, version, *,
                    mode=Mode.HIGH, state=CalibrationState.ACTIVE,
                    release_day=REFERENCE_DAY_RELEASE, reference_day=REFERENCE_DAY_SAMPLE):
    row = Calibration(
        probe_id=probe_id, mode=mode, reference_day=reference_day, release_day=release_day,
        start_minute=start, end_minute=end, version=version, state=state,
        submitted_by="yu", approved_by="tang" if state == CalibrationState.ACTIVE else None,
        model=build_mapping(segments),
        segments=[CalibrationSource(segment_id=s.id) for s in segments],
    )
    db.add(row)
    db.flush()
    return row


def add_slot(db, probe_id, calibration_id, start, end, *, mode=Mode.HIGH,
             day=REFERENCE_DAY_RELEASE):
    slot = ReleaseSlot(probe_id=probe_id, day=day, mode=mode, start_minute=start,
                       end_minute=end, calibration_id=calibration_id)
    db.add(slot)
    db.flush()
    return slot


def fit_candidate(db, probe_id, start, end, *, mode=Mode.HIGH,
                  reference_day=RECAPTURE_DAY, release_day=REFERENCE_DAY_RELEASE):
    from lab.calibration_schema import FitRequest

    request = FitRequest(probe_id=probe_id, mode=mode, reference_day=reference_day,
                         release_day=release_day, start_minute=start, end_minute=end)
    candidate = calibration_service.create_candidate(db, request, "yu")
    assert calibration_service.try_fit(db) is True
    db.refresh(candidate)
    return candidate
