import numpy as np
import pytest

from lab.quantile_map import (
    SEAM_TOLERANCE_LX,
    InvalidReference,
    adjust,
    seam_check,
)


def _model(raw_knots, reference_knots):
    n = len(raw_knots)
    return {
        "raw_knots": list(raw_knots),
        "reference_knots": list(reference_knots),
        "probabilities": np.linspace(0, 1, n).tolist(),
    }


def test_overlap_within_tolerance_passes():
    base = _model(np.linspace(300, 900, 9), np.linspace(350, 950, 9))
    # 整条曲线比基准高 10 lx，落在 20 lx 容差内。
    shifted = _model(np.linspace(300, 900, 9), np.linspace(360, 960, 9))
    metrics = seam_check(shifted, base, "左端接缝")
    assert metrics["max_delta_lux"] <= SEAM_TOLERANCE_LX
    assert metrics["overlap_raw_lux"][0] == 300 and metrics["overlap_raw_lux"][1] == 900


def test_overlap_delta_over_tolerance_rejected():
    base = _model(np.linspace(300, 900, 9), np.linspace(350, 950, 9))
    shifted = _model(np.linspace(300, 900, 9), np.linspace(380, 980, 9))
    with pytest.raises(InvalidReference, match="最大换算差"):
        seam_check(shifted, base, "右端接缝")


def test_disjoint_raw_ranges_rejected():
    base = _model(np.linspace(300, 500, 7), np.linspace(350, 550, 7))
    other = _model(np.linspace(700, 900, 7), np.linspace(750, 950, 7))
    with pytest.raises(InvalidReference, match="没有重叠区间"):
        seam_check(other, base, "左端接缝")


def test_touching_ranges_have_zero_width_overlap_rejected():
    base = _model(np.linspace(300, 500, 7), np.linspace(350, 550, 7))
    other = _model(np.linspace(500, 700, 7), np.linspace(550, 750, 7))
    with pytest.raises(InvalidReference, match="没有重叠区间"):
        seam_check(other, base, "右端接缝")


def test_adjust_monotone_on_seam_models():
    model = _model(np.linspace(300, 900, 9), np.linspace(350, 950, 9))
    values = adjust(model, [400, 500, 600])
    assert list(values) == sorted(values)
