"""core.py 的单元测试：本机 python3 -m unittest discover -s tests 直接跑，不需要 AstrBot。"""

from __future__ import annotations

import datetime as dt
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402

NOW = dt.datetime(2026, 11, 18, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


class WindowsTest(unittest.TestCase):
    def test_parse_normal_and_empty(self):
        self.assertEqual(core.parse_windows("23:30-08:30"), [(1410, 510)])
        self.assertEqual(core.parse_windows(""), [])
        self.assertEqual(core.parse_windows(None), [])

    def test_parse_multiple_and_chinese_comma(self):
        self.assertEqual(
            core.parse_windows("12:00-13:00，09:00-09:30"),
            [(720, 780), (540, 570)],
        )

    def test_parse_skips_broken_parts(self):
        self.assertEqual(core.parse_windows("bad,25:00-26:00,08:00-08:00"), [])

    def test_invalid_windows_reports_parts(self):
        self.assertEqual(core.invalid_windows("23:30-08:30"), [])
        self.assertEqual(core.invalid_windows("abc"), ["abc"])
        self.assertEqual(core.invalid_windows("24:00-08:00"), ["24:00-08:00"])

    def test_sleeping_crosses_midnight(self):
        windows = core.parse_windows("23:30-08:30")
        self.assertTrue(core.sleeping(windows, NOW.replace(hour=23, minute=45)))
        self.assertTrue(core.sleeping(windows, NOW.replace(hour=3, minute=0)))
        self.assertFalse(core.sleeping(windows, NOW.replace(hour=8, minute=30)))
        self.assertFalse(core.sleeping(windows, NOW.replace(hour=12, minute=0)))

    def test_sleeping_same_day_window(self):
        windows = core.parse_windows("12:00-13:00")
        self.assertTrue(core.sleeping(windows, NOW.replace(hour=12, minute=30)))
        self.assertFalse(core.sleeping(windows, NOW.replace(hour=13, minute=0)))


class ProbabilityTest(unittest.TestCase):
    def setUp(self):
        self.settings = core.normalize_settings({})

    def test_fixed_mode_ignores_age(self):
        settings = dict(self.settings, mode="fixed", probability=0.3)
        self.assertAlmostEqual(core.probability(settings, 5), 0.3)
        self.assertAlmostEqual(core.probability(settings, 5000), 0.3)

    def test_adaptive_three_tiers(self):
        self.assertAlmostEqual(core.probability(self.settings, 5), 0.2 * 0.15)
        self.assertAlmostEqual(core.probability(self.settings, 100), 0.2)
        self.assertAlmostEqual(core.probability(self.settings, 400), 0.2 * 1.5)

    def test_adaptive_without_activity_uses_base(self):
        self.assertAlmostEqual(core.probability(self.settings, None), 0.2)

    def test_capped_at_one(self):
        settings = dict(self.settings, probability=0.9, distant_multiplier=5)
        self.assertAlmostEqual(core.probability(settings, 1000), 1.0)

    def test_next_wait_keeps_ten_percent_jitter(self):
        rng = random.Random(7)
        for _ in range(50):
            wait = core.next_wait_seconds(30, rng)
            self.assertGreaterEqual(wait, 30 * 60 * 0.9 - 1e-6)
            self.assertLessEqual(wait, 30 * 60 * 1.1 + 1e-6)


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        settings = core.normalize_settings({})
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["interval_minutes"], 30)
        self.assertEqual(settings["owner_id"], "astrbot")
        self.assertFalse(settings["allow_self_wake"])

    def test_coercion(self):
        settings = core.normalize_settings(
            {
                "enabled": "true",
                "interval_minutes": "45",
                "probability": "0.35",
                "mode": "FIXED",
                "owner_id": "  1250226509 ",
                "sessions": "a:FriendMessage:1, b:GroupMessage:2",
            }
        )
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["interval_minutes"], 45)
        self.assertAlmostEqual(settings["probability"], 0.35)
        self.assertEqual(settings["mode"], "fixed")
        self.assertEqual(settings["owner_id"], "1250226509")
        self.assertEqual(settings["sessions"], ["a:FriendMessage:1", "b:GroupMessage:2"])

    def test_clamps_out_of_range(self):
        settings = core.normalize_settings({"probability": 3, "interval_minutes": 0})
        self.assertAlmostEqual(settings["probability"], 1.0)
        self.assertEqual(settings["interval_minutes"], 1)

    def test_validate_good_patch(self):
        clean, errors = core.validate_patch(
            {"probability": 0.5, "interval_minutes": 15, "allow_self_wake": True}
        )
        self.assertEqual(errors, [])
        self.assertAlmostEqual(clean["probability"], 0.5)
        self.assertEqual(clean["interval_minutes"], 15)
        self.assertTrue(clean["allow_self_wake"])

    def test_validate_rejects(self):
        cases = [
            ({"interval_minutes": 0}, "平均检查间隔"),
            ({"interval_minutes": 5000}, "平均检查间隔"),
            ({"probability": 1.5}, "基础概率"),
            ({"probability": "abc"}, "基础概率"),
            ({"mode": "chaos"}, "概率模式"),
            ({"sleep_windows": "25:00-08:00"}, "睡眠时段"),
            ({"owner_id": "  "}, "不能为空"),
            ({"owner_id": "a b"}, "不能包含空格"),
            ({"recent_minutes": 400, "distant_minutes": 100}, "近期阈值"),
            ({"unknown": 1}, "未知配置项"),
        ]
        for patch, needle in cases:
            with self.subTest(patch=patch):
                _, errors = core.validate_patch(patch)
                self.assertTrue(any(needle in e for e in errors), errors)

    def test_validate_rejects_non_object(self):
        _, errors = core.validate_patch(["x"])  # type: ignore[arg-type]
        self.assertTrue(errors)


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_empty_state(self):
        state = core.load_state(self.path)
        self.assertEqual(state["sessions"], {})

    def test_broken_file_is_empty_state(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(core.load_state(self.path)["sessions"], {})

    def test_roundtrip_and_atomic_write(self):
        state = core.load_state(self.path)
        core.touch_activity(state, "u1", NOW)
        core.save_state(self.path, state)
        again = core.load_state(self.path)
        self.assertEqual(again["sessions"]["u1"]["last_activity_at"], core.iso(NOW))
        # 文件必须是完整 JSON，tmp 文件不留在目录里
        json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["state.json"])

    def test_bucket_fills_defaults(self):
        state = core.empty_state()
        state["sessions"]["u1"] = {"last_activity_at": None}
        entry = core.bucket(state, "u1")
        self.assertIn("intents", entry)
        self.assertIn("self_wake", entry)
        self.assertEqual(entry["runs"], [])

    def test_runs_pruned(self):
        state = core.empty_state()
        for index in range(core.MAX_RUNS + 5):
            core.push_run(state, "u1", core.new_run("u1", "random", f"n{index}", NOW))
        runs = core.bucket(state, "u1")["runs"]
        self.assertEqual(len(runs), core.MAX_RUNS)
        self.assertEqual(runs[-1]["note"], f"n{core.MAX_RUNS + 4}")

    def test_finish_run_by_id(self):
        state = core.empty_state()
        run = core.push_run(state, "u1", core.new_run("u1", "self", "note", NOW))
        other = core.push_run(state, "u1", core.new_run("u1", "random", "note2", NOW))
        done = core.finish_run(state, "u1", run["run_id"], "completed", "no_action", "安静待着", NOW)
        self.assertIsNotNone(done)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["decision"], "no_action")
        self.assertEqual(core.bucket(state, "u1")["runs"][1]["status"], "queued")
        self.assertEqual(other["status"], "queued")

    def test_finish_run_falls_back_to_latest_open(self):
        state = core.empty_state()
        core.push_run(state, "u1", core.new_run("u1", "random", "a", NOW))
        latest = core.push_run(state, "u1", core.new_run("u1", "random", "b", NOW))
        done = core.finish_run(state, "u1", None, "completed", "acted", "", NOW)
        self.assertIsNotNone(done)
        self.assertEqual(done["run_id"], latest["run_id"])

    def test_finish_run_without_open_returns_none(self):
        state = core.empty_state()
        self.assertIsNone(core.finish_run(state, "u1", "missing", "completed", "acted", "", NOW))

    def test_self_wake_lifecycle(self):
        state = core.empty_state()
        at = NOW + dt.timedelta(minutes=40)
        first = core.set_self_wake(state, "u1", at, "等主人回来", NOW)
        self.assertEqual(first["version"], 1)
        second = core.set_self_wake(state, "u1", at, "改期", NOW)
        self.assertEqual(second["version"], 2)
        self.assertEqual(core.peek_self_wake(state, "u1")["note"], "改期")
        taken = core.consume_self_wake(state, "u1")
        self.assertEqual(taken["note"], "改期")
        self.assertIsNone(core.peek_self_wake(state, "u1"))
        self.assertIsNone(core.consume_self_wake(state, "u1"))

    def test_cancel_self_wake(self):
        state = core.empty_state()
        self.assertFalse(core.cancel_self_wake(state, "u1"))
        core.set_self_wake(state, "u1", NOW, "x", NOW)
        self.assertTrue(core.cancel_self_wake(state, "u1"))
        self.assertIsNone(core.peek_self_wake(state, "u1"))

    def test_intent_lifecycle(self):
        state = core.empty_state()
        intent = core.add_intent(state, "u1", "等他问起再说", NOW, kind="thought")
        self.assertEqual(intent["state"], "open")
        self.assertEqual(len(core.open_intents(state, "u1")), 1)
        revised = core.revise_intent(state, "u1", intent["id"], NOW, text="改个说法")
        self.assertEqual(revised["text"], "改个说法")
        self.assertEqual(revised["version"], 2)
        closed = core.close_intent(state, "u1", intent["id"], NOW, "done", "已经聊完")
        self.assertEqual(closed["state"], "done")
        self.assertEqual(core.open_intents(state, "u1"), [])

    def test_revise_or_close_unknown_returns_none(self):
        state = core.empty_state()
        self.assertIsNone(core.revise_intent(state, "u1", "nope", NOW, text="x"))
        self.assertIsNone(core.close_intent(state, "u1", "nope", NOW, "done"))

    def test_intents_pruned(self):
        state = core.empty_state()
        for index in range(core.MAX_INTENTS + 3):
            core.add_intent(state, "u1", f"i{index}", NOW)
        intents = core.list_intents(state, "u1")
        self.assertEqual(len(intents), core.MAX_INTENTS)
        self.assertEqual(intents[-1]["text"], f"i{core.MAX_INTENTS + 2}")

    def test_minutes_since(self):
        self.assertIsNone(core.minutes_since(None, NOW))
        self.assertAlmostEqual(core.minutes_since(NOW - dt.timedelta(minutes=35), NOW), 35.0)
        naive = (NOW - dt.timedelta(minutes=10)).replace(tzinfo=None)
        self.assertAlmostEqual(core.minutes_since(naive, NOW), 10.0)


class StaleQueueTest(unittest.TestCase):
    def test_stale_queued_run_fails(self):
        state = core.empty_state()
        run = core.push_run(state, "u1", core.new_run("u1", "random", "n", NOW))
        later = NOW + dt.timedelta(minutes=core.STALE_QUEUE_MINUTES + 1)
        expired = core.expire_stale_runs(state, "u1", later)
        self.assertEqual([x["run_id"] for x in expired], [run["run_id"]])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["decision"], "no_action")
        self.assertEqual(run["ended_at"], core.iso(later))

    def test_fresh_queued_run_survives(self):
        state = core.empty_state()
        run = core.push_run(state, "u1", core.new_run("u1", "random", "n", NOW))
        soon = NOW + dt.timedelta(minutes=1)
        self.assertEqual(core.expire_stale_runs(state, "u1", soon), [])
        self.assertEqual(run["status"], "queued")

    def test_running_and_finished_untouched(self):
        state = core.empty_state()
        running = core.push_run(state, "u1", core.new_run("u1", "random", "n", NOW))
        running["status"] = "running"
        done = core.push_run(state, "u1", core.new_run("u1", "random", "n2", NOW))
        done["status"] = "completed"
        later = NOW + dt.timedelta(hours=2)
        self.assertEqual(core.expire_stale_runs(state, "u1", later), [])
        self.assertEqual(running["status"], "running")
        self.assertEqual(done["status"], "completed")


if __name__ == "__main__":
    unittest.main()
