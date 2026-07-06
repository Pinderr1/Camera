import copy
import json
import tempfile
import unittest
from pathlib import Path

from senior_safety.baselines import ROUTINE_BASELINE_FILE, apply_baselines, compute_baselines
from senior_safety.state_machine import load_rules

RULES = load_rules("config/monitoring-rules.example.json")


def transition(start, end, from_state, to_state, duration_s):
    return {
        "start_time_local": start,
        "end_time_local": end,
        "from_state": from_state,
        "to_state": to_state,
        "duration_s": str(duration_s),
    }


class BaselineTests(unittest.TestCase):
    def test_compute_baselines_from_transitions(self):
        rows = []
        for night in range(1, 4):
            date = f"2026-07-0{night}"
            rows.append(transition(f"{date}T02:00:00", f"{date}T02:00:10", "asleep_in_bed", "bed_exit", 3600))
            rows.append(transition(f"{date}T02:00:10", f"{date}T02:01:00", "bed_exit", "bathroom_occupied", 50))
            rows.append(transition(f"{date}T02:01:00", f"{date}T02:0{night + 2}:00", "bathroom_occupied", "returning_to_bed", 60 * (night + 1)))
            rows.append(transition(f"{date}T02:0{night + 2}:00", f"{date}T02:0{night + 3}:00", "returning_to_bed", "returned_to_bed", 60))

        baselines = compute_baselines(rows)

        self.assertEqual(baselines["nights_observed"], 3)
        self.assertEqual(baselines["bathroom_duration"]["count"], 3)
        self.assertEqual(baselines["bathroom_duration"]["p50_s"], 180.0)
        self.assertEqual(baselines["bed_trip_duration"]["count"], 3)

    def test_apply_baselines_personalizes_overstay_threshold(self):
        rules = copy.deepcopy(RULES)
        with tempfile.TemporaryDirectory() as tmp:
            baselines = {
                "bathroom_duration": {"p50_s": 180, "p90_s": 400, "p95_s": 420, "count": 25},
            }
            (Path(tmp) / ROUTINE_BASELINE_FILE).write_text(json.dumps(baselines), encoding="utf-8")
            rules = apply_baselines(rules, tmp)

        overstay = rules["thresholds"]["bathroom_overstay"]
        self.assertEqual(overstay["low_notice_default_s"], 420 + overstay["low_notice_after_p95_plus_s"])
        self.assertTrue(overstay["personalized_from_baseline"])

    def test_apply_baselines_skipped_with_too_few_samples(self):
        rules = copy.deepcopy(RULES)
        original = rules["thresholds"]["bathroom_overstay"]["low_notice_default_s"]
        with tempfile.TemporaryDirectory() as tmp:
            baselines = {"bathroom_duration": {"p95_s": 60, "count": 3}}
            (Path(tmp) / ROUTINE_BASELINE_FILE).write_text(json.dumps(baselines), encoding="utf-8")
            rules = apply_baselines(rules, tmp)

        self.assertEqual(rules["thresholds"]["bathroom_overstay"]["low_notice_default_s"], original)

    def test_apply_baselines_noop_without_files(self):
        rules = copy.deepcopy(RULES)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(apply_baselines(rules, tmp), RULES)


if __name__ == "__main__":
    unittest.main()
