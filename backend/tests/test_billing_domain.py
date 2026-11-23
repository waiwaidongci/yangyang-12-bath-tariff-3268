"""Hand-checked domain tests for late-reading make-up billing (stdlib only).

Run from the backend directory:  python3 -m tests.test_billing_domain
"""

import unittest
from fractions import Fraction

from src.domain import analysis, billing


RATES = billing.parse_rates({"valley_rate": 4.2, "flat_rate": 6.8, "peak_rate": 10.6})
# 2026-09-01 15:00 -> 2026-09-02 00:00; 1211.660 -> 1228.080 (540 minutes).
ROW = {
    "pool_zone": "岩露池区", "meter_code": "XQ-R01",
    "start_date": "2026-09-01", "start_minute": 900,
    "end_date": "2026-09-02", "end_minute": 0,
    "reading_start_m3": 1211.660, "reading_end_m3": 1228.080,
}
P_1800 = billing.point_from("2026-09-01", 1080)
P_2200 = billing.point_from("2026-09-01", 1320)


def perspective(rows, points=()):
    meters, zones = billing.build_intervals(rows)
    for point in points:
        meters = billing.apply_late_reading(meters, zones, point)
    return billing.perspective(meters, zones, RATES)


class TariffReuseTests(unittest.TestCase):
    def test_band_minutes_match_doc_example(self):
        segments = dict((band[0], band[2]) for band in analysis.BANDS)
        start = billing.point_from("2026-09-01", 900)
        end = billing.point_from("2026-09-02", 0)
        minutes = {key: analysis.band_minutes_between(start, end, seg)
                   for key, seg in segments.items()}
        self.assertEqual(minutes, {"valley": 120, "flat": 120, "peak": 300})

    def test_hand_calculated_perspective(self):
        view = perspective([ROW])
        # 16.42 m3 over 540 minutes; flat and valley get 120/540, peak 300/540.
        per = Fraction(16420, 540)
        expected_liters = {"flat": per * 120, "peak": per * 300, "valley": per * 120}
        for key, exact in expected_liters.items():
            self.assertEqual(view["exact_volumes"][("岩露池区", key)], exact)
        start = billing.point_from("2026-09-01", 900)
        end = billing.point_from("2026-09-02", 0)
        exact_cost_cents = 0
        for key, _label, segments, _default in analysis.BANDS:
            minutes = analysis.band_minutes_between(start, end, segments)
            exact_cost_cents += per * minutes * RATES[key] / 1000
        self.assertEqual(view["total_cost_cents"], int(exact_cost_cents + Fraction(1, 2)))
        self.assertEqual(view["total_cost_cents"], 13683)
        self.assertEqual(view["total_volume_liters"], 16420)


class SplitTests(unittest.TestCase):
    def test_exact_interpolation_yields_zero_delta(self):
        before = perspective([ROW])
        interpolated = 1211660 + round(Fraction(16420) * 180 / 540)
        after = perspective([ROW], [billing.LatePoint("XQ-R01", P_1800, interpolated, "k1")])
        delta = billing.build_delta(before, after, [{"id": 1}])
        self.assertEqual(delta["total_cost_delta_cents"], 0)
        self.assertEqual(delta["total_volume_delta_liters"], 0)

    def test_real_bottom_shifts_water_between_bands_keeps_volume(self):
        before = perspective([ROW])
        after = perspective([ROW], [billing.LatePoint("XQ-R01", P_1800, 1220000, "k1")])
        delta = billing.build_delta(before, after, [{"id": 1}])
        # Total volume cannot change: only the time-band allocation moves.
        self.assertEqual(delta["total_volume_delta_liters"], 0)
        self.assertEqual(delta["rows"][0]["flat_volume"], 1.911)
        self.assertEqual(delta["rows"][0]["valley_volume"], -0.956)
        self.assertEqual(delta["rows"][0]["peak_volume"], -0.955)
        self.assertEqual(delta["total_cost_delta_cents"], -114)

    def test_multiple_bottoms_and_cumulative_delta(self):
        base = perspective([ROW])
        step1 = perspective([ROW], [billing.LatePoint("XQ-R01", P_1800, 1220000, "k1")])
        step2 = perspective([ROW], [billing.LatePoint("XQ-R01", P_1800, 1220000, "k1"),
                                    billing.LatePoint("XQ-R01", P_2200, 1225000, "k2")])
        d1 = billing.build_delta(base, step1, [{"id": 1}])
        d2 = billing.build_delta(step1, step2, [{"id": 2}])
        cumulative = billing.build_delta(base, step2, [{"id": 1}, {"id": 2}])
        self.assertEqual(cumulative["total_cost_delta_cents"],
                         d1["total_cost_delta_cents"] + d2["total_cost_delta_cents"])
        self.assertEqual(cumulative["total_cost_delta_cents"], -362)
        # Original + cumulative make-up equals the latest recomputation.
        self.assertEqual(base["total_cost_cents"] + cumulative["total_cost_delta_cents"],
                         step2["total_cost_cents"])

    def test_split_uses_original_rates_only(self):
        cheaper = billing.parse_rates({"valley_rate": 1.0, "flat_rate": 1.0, "peak_rate": 1.0})
        meters, zones = billing.build_intervals([ROW])
        view = billing.perspective(meters, zones, cheaper)
        # Rates are an argument, never read from any mutable global configuration.
        self.assertEqual(view["total_cost_cents"], 1642)

    def test_rejected_bottoms(self):
        meters, zones = billing.build_intervals([ROW])
        cases = [
            billing.LatePoint("XQ-Z99", P_1800, 1220000, "x"),
            billing.LatePoint("XQ-R01", billing.point_from("2026-09-01", 900), 1211660, "x"),
            billing.LatePoint("XQ-R01", P_1800, 1299999, "x"),
            billing.LatePoint("XQ-R01", billing.point_from("2026-09-13", 0), 1220000, "x"),
        ]
        for point in cases:
            with self.assertRaises(billing.BillingError):
                billing.apply_late_reading(meters, zones, point)

    def test_seed_still_reconciles_after_adding_a_late_bottom(self):
        rows = analysis.seed()
        base = perspective(rows)
        # The first seeded R01 interval starts at 00:00; insert a bottom at 08:00.
        first = next(row for row in rows if row["meter_code"] == "XQ-R01")
        mid_minute = (first["start_minute"] + first["end_minute"]) // 2
        point = billing.point_from(first["start_date"], mid_minute)
        start_liters = round(first["reading_start_m3"] * 1000)
        end_liters = round(first["reading_end_m3"] * 1000)
        mid_liters = start_liters + (end_liters - start_liters) // 2
        self.assertTrue(start_liters < mid_liters < end_liters)
        after = perspective(rows, [billing.LatePoint("XQ-R01", point, mid_liters, "seed-1")])
        self.assertEqual(after["source_liters"], base["source_liters"])
        delta = billing.build_delta(base, after, [{"id": 1}])
        self.assertEqual(base["total_cost_cents"] + delta["total_cost_delta_cents"],
                         after["total_cost_cents"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
