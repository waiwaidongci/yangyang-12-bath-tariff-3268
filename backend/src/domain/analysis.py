"""Allocate hot-water meter deltas to a recurring local tariff schedule."""

from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction


ZONES = ("岩露池区", "松雾池区", "溪畔池区")
DAY_MINUTES = 1440
# Each band is the union of these half-open local-time segments.
BANDS = (
    ("valley", "谷段", ((0, 360), (1320, 1440)), 4.2),
    ("flat", "平段", ((360, 1020),), 6.8),
    ("peak", "峰段", ((1020, 1320),), 10.6),
)
COLUMNS = [
    {"key": "pool_zone", "label": "池区"},
    *[
        {"key": f"{key}_{measure}", "label": f"{label}{unit_label}"}
        for key, label, _, _ in BANDS
        for measure, unit_label in (("volume", "用量（m³）"), ("cost", "费用（元）"))
    ],
    {"key": "total_volume", "label": "用量合计（m³）"},
    {"key": "total_cost", "label": "费用合计（元）"},
]


def _scaled(value, label, lower, upper, scale):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
        raise ValueError(f"{label}须为 {lower} 至 {upper} 之间的有限数值。")
    scaled = Fraction(str(value)) * scale
    if scaled.denominator != 1:
        raise ValueError(f"{label}的最小录入单位为 {1 / scale:g}。")
    return int(scaled)


def _timestamp(row, prefix, label):
    raw_day = row.get(f"{prefix}_date")
    try:
        day = date.fromisoformat(raw_day)
        if day.isoformat() != raw_day:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"{label}日期须使用 YYYY-MM-DD 格式。") from None
    minute = _scaled(row.get(f"{prefix}_minute"), f"{label}日内分钟", 0, 1439, 1)
    return day.toordinal() * DAY_MINUTES + minute


def _parse(row):
    zone = row.get("pool_zone")
    if zone not in ZONES:
        raise ValueError("池区须选择岩露池区、松雾池区或溪畔池区。")
    code = row.get("meter_code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("须填写独立表计编号。")
    start = _timestamp(row, "start", "起始")
    end = _timestamp(row, "end", "截止")
    if end <= start:
        raise ValueError("截止时间须晚于起始时间。")
    start_liters = _scaled(row.get("reading_start_m3"), "起始表底", 0, 1_000_000_000, 1000)
    end_liters = _scaled(row.get("reading_end_m3"), "截止表底", 0, 1_000_000_000, 1000)
    if end_liters < start_liters:
        raise ValueError("截止表底小于起始表底，请核对换表、回零或抄录记录。")
    return code.strip(), zone, start, end, start_liters, end_liters


def validate(row: dict) -> None:
    _parse(row)


def _band_minutes_until(timestamp, segments):
    days, minute = divmod(timestamp, DAY_MINUTES)
    minutes_per_day = sum(end - start for start, end in segments)
    return days * minutes_per_day + sum(max(0, min(minute, end) - start) for start, end in segments)


def _allocate(meters, zones_by_meter):
    exact_volumes = defaultdict(Fraction)
    source_liters = 0
    gap_count = 0
    for code, intervals in meters.items():
        zone = zones_by_meter[code]
        previous = None
        for start, end, start_liters, end_liters in sorted(intervals):
            if previous is not None:
                previous_end, previous_liters = previous
                if start < previous_end:
                    raise ValueError(f"表计 {code} 存在重叠区间，请核对重复抄表或时间边界。")
                if start_liters < previous_liters or (start == previous_end and start_liters != previous_liters):
                    raise ValueError(f"表计 {code} 的前后表底无法衔接，请核对抄表或换表记录。")
                gap_count += start > previous_end
            previous = (end, end_liters)
            liters = end_liters - start_liters
            source_liters += liters
            flow_per_minute = Fraction(liters, end - start)
            for key, _, segments, _ in BANDS:
                minutes = _band_minutes_until(end, segments) - _band_minutes_until(start, segments)
                exact_volumes[zone, key] += flow_per_minute * minutes
    return exact_volumes, source_liters, gap_count


def _round_cells(values):
    """Distribute residual whole units so displayed cells add to the rounded total."""
    rounded = {cell: int(value) for cell, value in values.items()}
    total = int(sum(values.values()) + Fraction(1, 2))
    residual = total - sum(rounded.values())
    order = sorted(values, key=lambda cell: values[cell] - rounded[cell], reverse=True)
    for cell in order[:residual]:
        rounded[cell] += 1
    return rounded


def _metric(label, value, unit):
    return {"label": label, "value": value, "unit": unit}


def analyze(rows: list[dict], parameters: dict) -> dict:
    if not rows:
        raise ValueError("至少需要一条有效的池区抄表区间。")
    rates = {
        key: _scaled(parameters.get(f"{key}_rate", default), f"{label}费率", 0, 1000, 100)
        for key, label, _, default in BANDS
    }
    meters = defaultdict(list)
    zones_by_meter = {}
    for row in rows:
        code, zone, *interval = _parse(row)
        if zones_by_meter.setdefault(code, zone) != zone:
            raise ValueError(f"表计 {code} 对应了多个池区，请核对独立表计归属。")
        meters[code].append(interval)

    volumes, source_liters, gaps = _allocate(meters, zones_by_meter)
    zones = [zone for zone in ZONES if zone in zones_by_meter.values()]
    ordered_volumes = {(zone, key): volumes[zone, key] for zone in zones for key, *_ in BANDS}
    exact_costs = {cell: volume * rates[cell[1]] / 1000 for cell, volume in ordered_volumes.items()}
    displayed_volumes = _round_cells(ordered_volumes)
    displayed_costs = _round_cells(exact_costs)

    pivot = []
    for zone in zones:
        row = {"pool_zone": zone}
        zone_liters = 0
        zone_cents = 0
        for key, *_ in BANDS:
            liters = displayed_volumes[zone, key]
            cents = displayed_costs[zone, key]
            row[f"{key}_volume"] = liters / 1000
            row[f"{key}_cost"] = cents / 100
            zone_liters += liters
            zone_cents += cents
        row["total_volume"] = zone_liters / 1000
        row["total_cost"] = zone_cents / 100
        pivot.append(row)

    total_liters = sum(displayed_volumes.values())
    total_cents = sum(displayed_costs.values())
    expected_cents = int(sum(exact_costs.values()) + Fraction(1, 2))
    highest_peak_zone = max(zones, key=lambda zone: exact_costs[zone, "peak"])
    highest_peak_cost = displayed_costs[highest_peak_zone, "peak"]
    if highest_peak_cost:
        recommendation = f"{highest_peak_zone}峰段费用最高（{highest_peak_cost / 100:.2f} 元），优先核对该池区的峰段补水安排。"
    else:
        recommendation = "本批记录未形成可见的峰段费用，可先核对当前峰段费率和抄表覆盖。"
    coverage = f"有 {gaps} 处同表缺段，缺段用量未估算。" if gaps else "同表相邻区间均已衔接。"
    return {
        "headline": f"{len(zones)} 个池区用水 {total_liters / 1000:,.3f} m³，估算 {total_cents / 100:,.2f} 元",
        "summary": f"{recommendation}{coverage}跨时段的表底增量按分钟均摊，费用为内部结算估算。",
        "metrics": [
            _metric("抄表区间", len(rows), "条"),
            _metric("原始表底增量", source_liters / 1000, "m³"),
            _metric("分时用量合计", total_liters / 1000, "m³"),
            _metric("用量勾稽差额", (total_liters - source_liters) / 1000, "m³"),
            _metric("分时费用合计", total_cents / 100, "元"),
            _metric("费用勾稽差额", (total_cents - expected_cents) / 100, "元"),
            _metric("同表缺段", gaps, "处"),
        ],
        "columns": COLUMNS,
        "rows": pivot,
        "chart": {
            "label": "各计费时段费用合计",
            "unit": "元",
            "points": [
                {"label": label, "value": sum(displayed_costs[zone, key] for zone in zones) / 100}
                for key, label, _, _ in BANDS
            ],
        },
    }


def seed() -> list[dict]:
    first_day = date(2026, 9, 1)
    rows = []
    profiles = (
        ("XQ-R01", 1_200_000, (330, 900), (3400, 8200, 16300)),
        ("XQ-S01", 760_000, (420, 1080), (3800, 8900, 5300)),
        ("XQ-X01", 430_000, (480, 1200), (5100, 7500, 2700)),
    )
    for zone, (code, reading, breaks, base_draws) in zip(ZONES, profiles):
        minutes = (0, *breaks, 0)
        for offset in range(12):
            day = first_day + timedelta(days=offset)
            for interval, base_draw in enumerate(base_draws):
                end_day = day + timedelta(days=interval == 2)
                draw = base_draw + 90 * (offset % 4) + 60 * ((interval + offset) % 3)
                rows.append({
                    "pool_zone": zone,
                    "meter_code": code,
                    "start_date": day.isoformat(),
                    "start_minute": minutes[interval],
                    "end_date": end_day.isoformat(),
                    "end_minute": minutes[interval + 1],
                    "reading_start_m3": reading / 1000,
                    "reading_end_m3": (reading + draw) / 1000,
                })
                reading += draw
    return rows
