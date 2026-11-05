"""Storage guarantees independent of AstrBot or a live QQ connection."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import TaskStore


def item(title="提交材料", times=None):
    return {
        "title": title,
        "due_at": 1000,
        "due_precision": "datetime",
        "importance": "high",
        "importance_reason": "截止后无法补交",
        "source_excerpt": "明天提交材料",
        "reminder_times": [900] if times is None else times,
    }


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "todo.sqlite3"
        self.store = TaskStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, times=None, source="message-1", owner="123"):
        tasks, created = self.store.add_tasks(
            owner, "qq:private:123", source, [item(times=times)], 100
        )
        self.assertTrue(created)
        return tasks[0]

    def test_ownership_and_duplicate_submission_survive_restart(self):
        task = self.add()
        duplicate, created = self.store.add_tasks(
            "123", "qq:private:123", "message-1", [item("重复标题")], 101
        )
        self.assertFalse(created)
        self.assertEqual([task], duplicate)
        self.assertIsNone(self.store.get_task(task["id"], "456"))
        self.assertIsNone(self.store.finish_task(task["id"], "456", "done", 102))
        self.assertEqual([], self.store.list_tasks("456"))
        self.store.close()
        self.store = TaskStore(self.path)
        self.assertEqual(task, self.store.get_task(task["id"], "123"))
        self.assertEqual(1, len(self.store.claim_due(900)))
        # The same QQ message identifier in another owner's scope is independent.
        self.add(owner="456")

    def test_invalid_batch_does_not_consume_submission_key(self):
        invalid = item()
        invalid["title"] = " "
        with self.assertRaises(ValueError):
            self.store.add_tasks("123", "origin", "one", [item(), invalid], 100)
        self.assertEqual([], self.store.list_tasks("123"))
        self.add(source="one")
        with self.assertRaises(ValueError):
            self.store.add_tasks("123", "origin", "empty", [], 100)

    def test_mid_batch_database_failure_rolls_back_tasks_and_submission(self):
        self.store._conn.executescript(
            """CREATE TRIGGER reject_second_task BEFORE INSERT ON tasks
            WHEN NEW.title='reject' BEGIN
                SELECT RAISE(ABORT, 'simulated storage failure');
            END;"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_tasks("123", "origin", "retryable", [item(), item("reject")], 100)
        self.assertEqual([], self.store.list_tasks("123"))
        self.assertEqual([], self.store.claim_due(1000))
        # Reusing the same source key succeeds after the actual DB failure.
        self.add(source="retryable")

    def test_reschedule_and_completion_cancel_claimed_jobs(self):
        task = self.add(times=[500, 900])
        old_job = self.store.claim_due(500)[0]
        changed = self.store.reschedule_task(task["id"], "123", 2000, "datetime", [1800, 1800], 501)
        self.assertEqual(2000, changed["due_at"])
        self.assertFalse(self.store.is_reminder_active(old_job["reminder_id"]))
        self.store.acknowledge(old_job["reminder_id"], 502)
        self.assertEqual([], self.store.claim_due(900))
        jobs = self.store.claim_due(1800)
        self.assertEqual(1, len(jobs))
        self.store.finish_task(task["id"], "123", "done", 1801)
        self.assertFalse(self.store.is_reminder_active(jobs[0]["reminder_id"]))
        self.assertEqual([], self.store.claim_due(3000))
        self.assertIsNone(self.store.replace_reminders(task["id"], "123", [4000], 3000))

    def test_expired_lease_recovery_and_bounded_retry(self):
        self.add(times=[500])
        first = self.store.claim_due(500, lease_seconds=20)[0]
        self.assertEqual(1, first["attempts"])
        self.assertEqual([], self.store.claim_due(519))
        second = self.store.claim_due(520)[0]
        self.assertEqual(first["reminder_id"], second["reminder_id"])
        self.assertEqual(2, second["attempts"])
        self.store.fail_reminder(second["reminder_id"], 520, "offline", max_attempts=3)
        self.assertEqual([], self.store.claim_due(579))
        self.store.close()
        self.store = TaskStore(self.path)
        third = self.store.claim_due(580)[0]
        self.assertEqual(3, third["attempts"])
        self.store.fail_reminder(third["reminder_id"], 580, "still offline", max_attempts=3)
        self.assertEqual([], self.store.claim_due(10000))

    def test_restart_merges_elapsed_points_but_keeps_future_reminder(self):
        task = self.add(times=[200, 300, 400, 2000])
        jobs = self.store.claim_due(500)
        self.assertEqual([400], [job["scheduled_at"] for job in jobs])
        self.store.acknowledge(jobs[0]["reminder_id"], 501)
        self.assertEqual([], self.store.claim_due(600))
        later = self.store.claim_due(2000)
        self.assertEqual(task["id"], later[0]["task_id"])

    def test_inflight_job_blocks_another_point_until_finished(self):
        self.add(times=[200, 300])
        first = self.store.claim_due(200)[0]
        self.assertEqual([], self.store.claim_due(300))
        self.store.acknowledge(first["reminder_id"], 301)
        self.assertEqual([300], [job["scheduled_at"] for job in self.store.claim_due(302)])

    def test_retry_backoff_is_not_reset_by_polling(self):
        self.add(times=[200])
        job = self.store.claim_due(200)[0]
        self.store.fail_reminder(job["reminder_id"], 200, "offline")
        for now in (201, 210, 220, 229):
            self.assertEqual([], self.store.claim_due(now))
        self.assertEqual(2, self.store.claim_due(230)[0]["attempts"])

    def test_metadata_roundtrips_json_and_defaults(self):
        self.assertEqual("fallback", self.store.get_meta("missing", "fallback"))
        self.store.set_meta("digest", {"day": "2026-11-05", "sent": True})
        self.store.close()
        self.store = TaskStore(self.path)
        self.assertEqual({"day": "2026-11-05", "sent": True}, self.store.get_meta("digest"))

    def test_importance_and_reminder_details_are_owner_scoped(self):
        task = self.add(times=[200, 300])
        self.assertIsNone(self.store.update_importance(task["id"], "456", "low", 101))
        changed = self.store.update_importance(task["id"], "123", "low", 101)
        self.assertEqual("low", changed["importance"])
        self.assertEqual("用户手动设置", changed["importance_reason"])
        self.assertEqual([], self.store.list_reminders(task["id"], "456"))
        self.assertEqual(2, len(self.store.list_reminders(task["id"], "123")))
        job = self.store.claim_due(200)[0]
        self.store.fail_reminder(job["reminder_id"], 201, "offline", max_attempts=1)
        states = [r["state"] for r in self.store.list_reminders(task["id"], "123")]
        self.assertEqual(["failed", "pending"], states)
        self.store.finish_task(task["id"], "123", "done", 202)
        self.assertIsNone(self.store.update_importance(task["id"], "123", "high", 203))


if __name__ == "__main__":
    unittest.main()
