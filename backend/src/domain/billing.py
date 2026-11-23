"""Late meter-bottom make-up billing.

A bill freezes the original meter intervals and the internal tariff rates that
were in force when the hot-water fee was settled.  When a meter bottom that
belongs inside an already-billed interval arrives late, the containing
interval is split at that point (uniform flow assumption, same rates) and the
perspective is recomputed.  The original bill is never rewritten; only the
zone x time-band deltas carried by make-up adjustments change.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from src.domain import analysis
from src.domain.analysis import BANDS, DAY_MINUTES, ZONES, _allocate, _round_cells, _scaled

BAND_KEYS = tuple(band[0] for band in BANDS)
BAND_LABELS = {band[0]: band[1] for band in BANDS}


class BillingError(ValueError):
    """Raised when a late reading cannot be applied to a bill."""


@dataclass(frozen=True)
class LatePoint:
    meter_code: str
    point: int
    reading_liters: int
    source_key: str
    note: str = ""


def parse_rates(parameters: dict) -> dict:
    """Tariff in cents per m³, keyed by band, kept at the bill's original rates."""
    return {
        key: _scaled(parameters.get(f"{key}_rate", default), f"{label}费率", 0, 1000, 100)
        for key, label, _, default in BANDS
    }


def point_from(day_text: str, minute) -> int:
    try:
        day = date.fromisoformat(day_text)
        if day.isoformat() != day_text:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise BillingError("晚到表底日期须使用 YYYY-MM-DD 格式。") from exc
    minute_value = _scaled(minute, "表底日内分钟", 0, 1439, 1)
    return day.toordinal() * DAY_MINUTES + minute_value


def reading_to_liters(value) -> int:
    return _scaled(value, "晚到表底", 0, 1_000_000_000, 1000)


def build_intervals(rows: list[dict]):
    """Validated ordered meter intervals plus their pool-zone mapping."""
    try:
        meters, zones_by_meter = analysis.parse_rows(rows)
    except ValueError as exc:
        raise BillingError(str(exc)) from exc
    return meters, zones_by_meter


def apply_late_reading(meters: dict, zones_by_meter: dict, late: LatePoint) -> dict:
    """Return new interval tables with the late meter bottom inserted.

    The late bottom must fall strictly inside an existing interval of the same
    meter; boundaries already have a bottom on file and cannot be duplicated.
    """
    code = late.meter_code.strip()
    if code not in meters:
        raise BillingError(f"表计 {code} 不在本账单范围内，无法补录晚到表底。")
    updated = {meter: list(intervals) for meter, intervals in meters.items()}
    for index, (start, end, start_liters, end_liters) in enumerate(updated[code]):
        if start < late.point < end:
            if not start_liters <= late.reading_liters <= end_liters:
                raise BillingError(
                    f"表计 {code} 的晚到表底 {late.reading_liters / 1000:.3f} m³ 超出所在区间表底"
                    f"（{start_liters / 1000:.3f} 至 {end_liters / 1000:.3f} m³）。"
                )
            updated[code][index:index + 1] = [
                (start, late.point, start_liters, late.reading_liters),
                (late.point, end, late.reading_liters, end_liters),
            ]
            return updated
        if late.point == start or late.point == end:
            raise BillingError(f"表计 {code} 在该时刻已有表底，不能重复补录；请核对抄表时点。")
    raise BillingError(f"表计 {code} 在该时刻没有可拆开的已结区间，晚到表底须落在原账单区间内部。")


def perspective(meters: dict, zones_by_meter: dict, rates: dict) -> dict:
    """Zone x band grid with exact values and the displayed, reconciled grid."""
    exact_volumes, source_liters, gaps = _allocate(meters, zones_by_meter)
    zones = [zone for zone in ZONES if zone in zones_by_meter.values()]
    cells = [(zone, key) for zone in zones for key in BAND_KEYS]
    ordered_volumes = {cell: exact_volumes[cell] for cell in cells}
    exact_costs = {cell: ordered_volumes[cell] * rates[cell[1]] / 1000 for cell in cells}
    displayed_volumes = _round_cells(ordered_volumes)
    displayed_costs = _round_cells(exact_costs)

    rows = []
    for zone in zones:
        row = {"pool_zone": zone}
        zone_liters = zone_cents = 0
        for key in BAND_KEYS:
            liters = displayed_volumes[zone, key]
            cents = displayed_costs[zone, key]
            row[f"{key}_volume"] = liters / 1000
            row[f"{key}_cost"] = cents / 100
            zone_liters += liters
            zone_cents += cents
        row["total_volume"] = zone_liters / 1000
        row["total_cost"] = zone_cents / 100
        rows.append(row)

    return {
        "zones": zones,
        "cells": cells,
        "exact_volumes": ordered_volumes,
        "exact_costs": exact_costs,
        "displayed_volumes": displayed_volumes,
        "displayed_costs": displayed_costs,
        "source_liters": source_liters,
        "gap_count": gaps,
        "total_volume_liters": sum(displayed_volumes.values()),
        "total_cost_cents": sum(displayed_costs.values()),
        "exact_total_cost_cents": int(sum(exact_costs.values()) + Fraction(1, 2)),
        "rows": rows,
    }


def _cell_delta(cells, after_grid, before_grid):
    return {cell: after_grid[cell] - before_grid[cell] for cell in cells}


def build_delta(before: dict, after: dict, late_readings: list[dict]) -> dict:
    """Displayed-cell deltas between two perspectives, kept JSON-safe."""
    if before["cells"] != after["cells"]:
        raise BillingError("两次重算的池区或时段口径不一致，无法计算补差。")
    cells = after["cells"]
    volume_delta = _cell_delta(cells, after["displayed_volumes"], before["displayed_volumes"])
    cost_delta = _cell_delta(cells, after["displayed_costs"], before["displayed_costs"])
    rows = []
    for zone in after["zones"]:
        row = {"pool_zone": zone}
        zone_liters = zone_cents = 0
        for key in BAND_KEYS:
            liters = volume_delta[zone, key]
            cents = cost_delta[zone, key]
            row[f"{key}_volume"] = liters / 1000
            row[f"{key}_cost"] = cents / 100
            zone_liters += liters
            zone_cents += cents
        row["total_volume"] = zone_liters / 1000
        row["total_cost"] = zone_cents / 100
        rows.append(row)
    return {
        "cells": [{"zone": zone, "band": key, "band_label": BAND_LABELS[key],
                   "volume_delta_m3": volume_delta[zone, key] / 1000,
                   "cost_delta_yuan": cost_delta[zone, key] / 100}
                  for zone, key in cells],
        "rows": rows,
        "total_volume_delta_liters": after["total_volume_liters"] - before["total_volume_liters"],
        "total_cost_delta_cents": after["total_cost_cents"] - before["total_cost_cents"],
        "total_volume_delta_m3": (after["total_volume_liters"] - before["total_volume_liters"]) / 1000,
        "total_cost_delta_yuan": (after["total_cost_cents"] - before["total_cost_cents"]) / 100,
        "late_reading_ids": [item["id"] for item in late_readings],
        "late_readings": late_readings,
    }


def recompute(original_rows: list[dict], rates: dict, late_points: list[LatePoint]):
    """Apply late bottoms in arrival order and return interval tables + view."""
    meters, zones_by_meter = build_intervals(original_rows)
    for late in sorted(late_points, key=lambda item: (item.point, item.source_key)):
        meters = apply_late_reading(meters, zones_by_meter, late)
    return meters, zones_by_meter, perspective(meters, zones_by_meter, rates)


def snapshot_view(view: dict) -> dict:
    """JSON-persistable perspective snapshot (Fractions are dropped)."""
    return {
        "zones": view["zones"],
        "rows": view["rows"],
        "total_volume_m3": view["total_volume_liters"] / 1000,
        "total_cost_yuan": view["total_cost_cents"] / 100,
        "exact_total_cost_yuan": view["exact_total_cost_cents"] / 100,
        "source_volume_m3": view["source_liters"] / 1000,
        "gap_count": view["gap_count"],
    }
