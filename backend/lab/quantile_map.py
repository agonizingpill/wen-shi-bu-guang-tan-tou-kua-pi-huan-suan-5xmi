from hashlib import sha256
import json

import numpy as np
from sklearn.preprocessing import QuantileTransformer

from .settings import BLOCK_MINUTES

MODEL_SEED = 77
# 接缝处同一原读数允许的最大换算差（lx）。
SEAM_TOLERANCE_LX = 20
SEAM_SAMPLE_POINTS = 257


class InvalidReference(ValueError):
    pass


def raw_span(model):
    knots = np.asarray(model["raw_knots"], dtype=float)
    return float(knots[0]), float(knots[-1])


def seam_check(candidate_model, neighbor_model, label):
    """局部替换候选与一条紧邻曲线比对接缝。

    原读数量程没有重叠区间即拒签；重叠区间内按固定网格取同一原读数，
    两版换算差超过 SEAM_TOLERANCE_LX 同样拒签。
    """
    candidate_lo, candidate_hi = raw_span(candidate_model)
    neighbor_lo, neighbor_hi = raw_span(neighbor_model)
    overlap_lo = max(candidate_lo, neighbor_lo)
    overlap_hi = min(candidate_hi, neighbor_hi)
    if overlap_hi <= overlap_lo:
        raise InvalidReference(
            f"{label}：与紧邻曲线的原读数量程没有重叠区间，不能局部签发。"
        )
    grid = np.linspace(overlap_lo, overlap_hi, SEAM_SAMPLE_POINTS)
    diff = np.abs(adjust(candidate_model, grid) - adjust(neighbor_model, grid))
    worst = float(np.max(diff))
    if worst > SEAM_TOLERANCE_LX:
        raise InvalidReference(
            f"{label}：重叠区间内同一原读数的最大换算差 {worst:.1f} lx 超过 "
            f"{SEAM_TOLERANCE_LX} lx，不能局部签发。"
        )
    return {"overlap_raw_lux": [round(overlap_lo, 3), round(overlap_hi, 3)],
            "max_delta_lux": round(worst, 3)}


def fmt_minute(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def require_coverage(start, end, segments):
    by_start = {segment.start_minute: segment for segment in segments}
    absent = [fmt_minute(m) for m in range(start, end, BLOCK_MINUTES) if m not in by_start]
    if absent:
        raise InvalidReference(f"基准见证覆盖断档：{'、'.join(absent)}。现行换算保留。")


def _quantiles(values, levels):
    return QuantileTransformer(
        n_quantiles=levels, output_distribution="uniform", subsample=None, random_state=MODEL_SEED,
    ).fit(values.reshape(-1, 1))


def adjust(model, raw_values):
    raw_knots = np.asarray(model["raw_knots"])
    levels = np.asarray(model["probabilities"])
    reference_knots = np.asarray(model["reference_knots"])
    # Mirror scikit-learn's bidirectional interpolation for repeated knots.
    forward = np.interp(raw_values, raw_knots, levels)
    reverse = -np.interp(-np.asarray(raw_values), -raw_knots[::-1], -levels[::-1])
    ranks = (forward + reverse) / 2
    return np.interp(ranks, levels, reference_knots)


def build_mapping(segments):
    samples = [(s.start_minute, p.offset_seconds, p.raw_lux, p.reference_lux)
               for s in segments for p in s.points]
    val_offsets = {(s.start_minute, s.points[-1].offset_seconds) for s in segments}
    training = [(raw, ref) for start, offset, raw, ref in samples if (start, offset) not in val_offsets]
    validation = [(raw, ref) for start, offset, raw, ref in samples if (start, offset) in val_offsets]
    train_raw, train_ref = np.asarray(training).T
    check_raw, check_ref = np.asarray(validation).T
    if np.ptp(train_raw) < 100 or np.ptp(train_ref) < 100:
        raise InvalidReference("基准与探头训练读数各须覆盖至少 100 lx，补采后再试。")
    corr = np.corrcoef(train_raw, train_ref)[0, 1]
    if corr < 0.85:
        raise InvalidReference("同秒原始与基准读数趋势不一致，检查对时或探头遮挡。")
    n = min(17, len(train_raw))
    raw_quantile = _quantiles(train_raw, n)
    reference_quantile = _quantiles(train_ref, n)
    model = {
        "algorithm": "QuantileTransformer",
        "seed": MODEL_SEED,
        "raw_knots": raw_quantile.quantiles_[:, 0].tolist(),
        "reference_knots": reference_quantile.quantiles_[:, 0].tolist(),
        "probabilities": raw_quantile.references_.tolist(),
        "train_count": len(training), "validation_count": len(validation),
        "input_sha256": sha256(json.dumps(samples, separators=(",", ":")).encode()).hexdigest(),
        "paired_pearson": round(float(corr), 6),
    }
    model["mae_raw_lux"] = round(float(np.mean(np.abs(check_raw - check_ref))), 3)
    model["mae_adjusted_lux"] = round(float(np.mean(np.abs(adjust(model, check_raw) - check_ref))), 3)
    return model
