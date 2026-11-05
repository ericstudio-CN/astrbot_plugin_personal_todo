import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_personal_todo.domain import (
    LOCAL_TZ,
    ExtractionError,
    build_extraction_prompt,
    day_bounds,
    format_time,
    parse_extraction,
    validate_clock,
)


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


NOW = stamp("2026-11-05T12:00:00+08:00")


def output(**changes):
    item = {
        "title": "提交材料",
        "due_at": "2026-11-06T18:00:00+08:00",
        "due_precision": "datetime",
        "importance": "normal",
        "importance_reason": "用户明确要求",
        "source_excerpt": "明天18点前提交材料",
        "remind_at": [],
        "needs_confirmation": False,
        "question": "",
    }
    item.update(changes)
    return json.dumps({"tasks": [item], "questions": []}, ensure_ascii=False)


class DomainTests(unittest.TestCase):
    def test_auto_non_task_has_no_artificial_clarification(self):
        result = parse_extraction('{"is_task":false,"tasks":[],"questions":[]}', NOW, mode="auto")
        self.assertEqual(result, {"is_task": False, "tasks": [], "questions": []})

    def test_auto_requires_consistent_explicit_decision(self):
        data = json.loads(output())
        for decision in (None, "true", False):
            candidate = dict(data)
            if decision is not None:
                candidate["is_task"] = decision
            with self.subTest(decision=decision), self.assertRaises(ExtractionError):
                parse_extraction(json.dumps(candidate), NOW, mode="auto")

    def test_auto_task_uses_same_deadline_validation_and_defaults(self):
        data = json.loads(output())
        data["is_task"] = True
        result = parse_extraction(json.dumps(data), NOW, mode="auto")
        self.assertTrue(result["is_task"])
        self.assertEqual([stamp("2026-11-06T16:00:00+08:00")], result["tasks"][0]["reminder_times"])

    def test_datetime_defaults_and_high_extra_day(self):
        task = parse_extraction(output(importance="high"), NOW)["tasks"][0]
        self.assertEqual(
            task["reminder_times"],
            [stamp("2026-11-05T18:00:00+08:00"), stamp("2026-11-06T16:00:00+08:00")],
        )
        self.assertEqual(task["due_at"], stamp("2026-11-06T18:00:00+08:00"))

    def test_date_only_preserves_precision_and_clock(self):
        task = parse_extraction(
            output(due_at="2026-11-06", due_precision="date"), NOW, date_reminder_time="19:30"
        )["tasks"][0]
        self.assertEqual(task["due_at"], stamp("2026-11-06T23:59:59+08:00"))
        self.assertEqual(task["reminder_times"], [stamp("2026-11-06T19:30:00+08:00")])
        self.assertNotIn("23:59", format_time(task["due_at"], "date"))

    def test_no_deadline_does_not_invent_reminder(self):
        task = parse_extraction(output(due_at=None, due_precision="none"), NOW)["tasks"][0]
        self.assertIsNone(task["due_at"])
        self.assertEqual(task["reminder_times"], [])

    def test_high_date_uses_daytime_clock_for_both_default_reminders(self):
        task = parse_extraction(
            output(due_at="2026-11-06", due_precision="date", importance="high"),
            NOW,
            date_reminder_time="20:00",
        )["tasks"][0]
        self.assertEqual(
            task["reminder_times"],
            [stamp("2026-11-05T20:00:00+08:00"), stamp("2026-11-06T20:00:00+08:00")],
        )

    def test_explicit_reminder_without_deadline(self):
        task = parse_extraction(
            output(due_at=None, due_precision="none", remind_at=["2026-11-06T09:00:00+08:00"]), NOW
        )["tasks"][0]
        self.assertEqual(task["reminder_times"], [stamp("2026-11-06T09:00:00+08:00")])

    def test_explicit_reminders_override_defaults_and_deduplicate(self):
        task = parse_extraction(
            output(importance="high", remind_at=["2026-11-06T10:00:00+08:00"] * 2), NOW
        )["tasks"][0]
        self.assertEqual(task["reminder_times"], [stamp("2026-11-06T10:00:00+08:00")])

    def test_ambiguity_is_preserved_as_question(self):
        parsed = parse_extraction(
            output(needs_confirmation=True, question="截图中的明天是几号？"), NOW
        )
        self.assertEqual(parsed["questions"], ["截图中的明天是几号？"])
        self.assertTrue(parsed["tasks"][0]["needs_confirmation"])
        parsed = parse_extraction(output(needs_confirmation=True), NOW)
        self.assertTrue(parsed["questions"])

    def test_question_without_flag_still_requires_confirmation(self):
        parsed = parse_extraction(output(question="是否是明天？"), NOW)
        self.assertTrue(parsed["tasks"][0]["needs_confirmation"])

    def test_past_deadline_cannot_silently_create_reminder(self):
        parsed = parse_extraction(output(due_at="2026-11-01T18:00:00+08:00"), NOW)
        self.assertTrue(parsed["questions"])
        self.assertTrue(parsed["tasks"][0]["needs_confirmation"])
        self.assertEqual(parsed["tasks"][0]["reminder_times"], [])

    def test_past_explicit_reminder_requires_confirmation(self):
        parsed = parse_extraction(output(remind_at=["2026-11-04T09:00:00+08:00"]), NOW)
        self.assertTrue(parsed["questions"])

    def test_soon_due_falls_back_to_future_point(self):
        parsed = parse_extraction(output(due_at="2026-11-05T12:10:00+08:00"), NOW)
        self.assertFalse(parsed["questions"])
        self.assertEqual(parsed["tasks"][0]["reminder_times"], [NOW + 60])

    def test_date_today_after_default_clock_falls_back(self):
        now = stamp("2026-11-05T21:00:00+08:00")
        parsed = parse_extraction(output(due_at="2026-11-05", due_precision="date"), now)
        self.assertEqual(parsed["tasks"][0]["reminder_times"], [now + 60])

    def test_full_fenced_json_accepted_explanations_rejected(self):
        self.assertTrue(parse_extraction("```json\n" + output() + "\n```", NOW)["tasks"])
        with self.assertRaises(ExtractionError):
            parse_extraction("这是结果：" + output(), NOW)

    def test_invalid_data_rejects_whole_response(self):
        invalid_items = [
            {"due_at": "2026-02-30", "due_precision": "date"},
            {"due_at": "明天18点"},
            {"due_at": None, "due_precision": "date"},
            {"due_at": "2026-11-06", "due_precision": "none"},
            {"importance": "urgent"},
            {"title": ""},
            {"title": 42},
            {"needs_confirmation": "false"},
            {"remind_at": "2026-11-06T10:00:00+08:00"},
            {"remind_at": ["2026-11-06T10:00:00+08:00"] * 11},
        ]
        for changes in invalid_items:
            with self.subTest(changes=changes), self.assertRaises(ExtractionError):
                parse_extraction(output(**changes), NOW)
        for raw in ("not json", "[]", '{"tasks":[],"tasks":[]}'):
            with self.subTest(raw=raw), self.assertRaises(ExtractionError):
                parse_extraction(raw, NOW)

    def test_partial_malformed_task_is_not_dropped(self):
        data = json.loads(output())
        data["tasks"].append({"title": "损坏的任务", "due_at": "x", "due_precision": "datetime"})
        with self.assertRaises(ExtractionError):
            parse_extraction(json.dumps(data), NOW)

    def test_empty_tasks_has_actionable_question(self):
        result = parse_extraction('{"tasks":[],"questions":[]}', NOW)
        self.assertEqual(result["tasks"], [])
        self.assertTrue(result["questions"])

    def test_naive_times_are_documented_local_time_and_z_is_equivalent(self):
        naive = parse_extraction(output(due_at="2026-11-06T18:00:00"), NOW)["tasks"][0]
        utc = parse_extraction(output(due_at="2026-11-06T10:00:00Z"), NOW)["tasks"][0]
        self.assertEqual(naive["due_at"], utc["due_at"])

    def test_local_day_bounds_across_year(self):
        now = stamp("2026-12-31T23:59:00+08:00")
        start, end = day_bounds(now)
        self.assertEqual(start, stamp("2026-12-31T00:00:00+08:00"))
        self.assertEqual(end, stamp("2027-01-01T00:00:00+08:00"))
        self.assertEqual(datetime.fromtimestamp(end - 1, LOCAL_TZ).day, 31)

    def test_prompt_contains_time_anchor_and_postpone_original_data(self):
        prompt = build_extraction_prompt(NOW)
        self.assertIn("2026-11-05T12:00:00+08:00", prompt)
        self.assertIn("不可信数据", prompt)
        self.assertIn("previous_received_at_unix", prompt)
        self.assertIn("不是另外创建一项任务", prompt)
        prompt = build_extraction_prompt(NOW, "postpone", {"title": "交材料", "due_at": None})
        self.assertIn("交材料", prompt)
        self.assertIn("完整替换", prompt)
        with self.assertRaises(ExtractionError):
            build_extraction_prompt(NOW, "postpone")

    def test_clock_validation(self):
        self.assertEqual(validate_clock("00:00"), "00:00")
        for invalid in ("24:00", "20:60", "9:00", "20:00:00", None):
            with self.subTest(invalid=invalid), self.assertRaises(ExtractionError):
                validate_clock(invalid)

    def test_reminder_prompt_preserves_original_deadline_exactly(self):
        prompt = build_extraction_prompt(
            NOW,
            "reminder",
            {
                "title": "交材料",
                "due_at": stamp("2026-11-06T18:00:00+08:00"),
                "due_precision": "datetime",
                "importance": "high",
            },
        )
        self.assertIn('"due_at": "2026-11-06T18:00:00+08:00"', prompt)
        self.assertIn("仅修改提醒模式", prompt)
        self.assertIn("禁止改变截止时间", prompt)
        with self.assertRaises(ExtractionError):
            build_extraction_prompt(NOW, "reminder")

    def test_reminder_mode_requires_explicit_times_not_defaults(self):
        with self.assertRaises(ExtractionError):
            parse_extraction(output(remind_at=[]), NOW, mode="reminder")
        data = json.loads(output())
        del data["tasks"][0]["remind_at"]
        with self.assertRaises(ExtractionError):
            parse_extraction(json.dumps(data), NOW, mode="reminder")
        task = parse_extraction(output(), NOW, mode="postpone")["tasks"][0]
        self.assertTrue(task["reminder_times"])

    def test_reminder_mode_allows_reminding_existing_overdue_task(self):
        parsed = parse_extraction(
            output(
                due_at="2026-11-04T18:00:00+08:00",
                remind_at=["2026-11-05T12:30:00+08:00"],
            ),
            NOW,
            mode="reminder",
        )
        self.assertFalse(parsed["questions"])
        self.assertEqual(parsed["tasks"][0]["reminder_times"], [NOW + 1800])


if __name__ == "__main__":
    unittest.main()
