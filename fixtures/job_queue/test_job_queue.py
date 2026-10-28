import tempfile
import unittest
from pathlib import Path

from job_queue import JobQueue
from store import JobStore
from worker import Worker


class JobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "jobs.json"
        self.store = JobStore(self.path)
        self.queue = JobQueue(self.store, lease_seconds=10, max_attempts=2)

    def test_enqueue_creates_queued_job(self) -> None:
        job = self.queue.enqueue("one", "send-email")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(self.store.get("one"), job)

    def test_duplicate_enqueue_preserves_original_job_and_state(self) -> None:
        self.queue.enqueue("one", "original")
        claimed = self.queue.claim(0)
        duplicate = self.queue.enqueue("one", "replacement")
        self.assertEqual(duplicate, claimed)
        self.assertEqual(self.store.get("one"), claimed)

    def test_new_job_survives_reopening_store(self) -> None:
        self.queue.enqueue("one", "send-email")
        self.assertEqual(JobStore(self.path).get("one")["payload"], "send-email")

    def test_claim_survives_reopening_store(self) -> None:
        self.queue.enqueue("one", "send-email")
        self.queue.claim(4)
        reopened = JobStore(self.path).get("one")
        self.assertEqual((reopened["status"], reopened["attempts"], reopened["lease_until"]),
                         ("running", 1, 14))

    def test_claim_respects_active_lease(self) -> None:
        self.queue.enqueue("one", "send-email")
        self.queue.claim(0)
        self.assertIsNone(self.queue.claim(9.99))
        self.assertEqual(self.store.get("one")["attempts"], 1)

    def test_expired_lease_can_be_reclaimed(self) -> None:
        self.queue.enqueue("one", "send-email")
        self.queue.claim(0)
        reclaimed = self.queue.claim(10)
        self.assertEqual((reclaimed["id"], reclaimed["attempts"], reclaimed["lease_until"]),
                         ("one", 2, 20))

    def test_expired_final_attempt_becomes_terminal(self) -> None:
        self.queue.enqueue("one", "send-email")
        self.queue.claim(0)
        self.queue.claim(10)
        self.assertIsNone(self.queue.claim(20))
        self.assertEqual(self.store.get("one")["status"], "failed")

    def test_worker_failure_requeues_below_attempt_limit(self) -> None:
        self.queue.enqueue("one", "send-email")

        def fail(_payload: str) -> None:
            raise RuntimeError("temporary failure")

        worker = Worker(self.queue, fail)
        self.assertEqual(worker.run_once(0), "one")
        self.assertEqual((self.store.get("one")["status"], self.store.get("one")["attempts"]),
                         ("queued", 1))

    def test_worker_failure_stops_at_attempt_limit(self) -> None:
        self.queue.enqueue("one", "send-email")

        def fail(_payload: str) -> None:
            raise RuntimeError("permanent failure")

        worker = Worker(self.queue, fail)
        self.assertEqual(worker.run_once(0), "one")
        self.assertEqual(worker.run_once(1), "one")
        self.assertEqual(self.store.get("one")["status"], "failed")
        self.assertIsNone(worker.run_once(2))

    def test_retry_state_survives_reopening_store(self) -> None:
        self.queue.enqueue("one", "send-email")

        def fail(_payload: str) -> None:
            raise RuntimeError("temporary failure")

        Worker(self.queue, fail).run_once(0)
        reopened_queue = JobQueue(JobStore(self.path), lease_seconds=10, max_attempts=2)
        self.assertEqual(reopened_queue.claim(1)["attempts"], 2)

    def test_successful_job_is_not_run_twice(self) -> None:
        seen = []
        self.queue.enqueue("one", "send-email")
        worker = Worker(self.queue, seen.append)
        self.assertEqual(worker.run_once(0), "one")
        self.assertIsNone(worker.run_once(20))
        self.assertEqual(seen, ["send-email"])
        self.assertEqual(self.store.get("one")["status"], "done")

    def test_completed_job_stays_done_after_reopening_store(self) -> None:
        self.queue.enqueue("one", "send-email")
        Worker(self.queue, lambda _payload: None).run_once(0)
        reopened_queue = JobQueue(JobStore(self.path), lease_seconds=10, max_attempts=2)
        self.assertEqual(reopened_queue.store.get("one")["status"], "done")
        self.assertIsNone(reopened_queue.claim(20))


if __name__ == "__main__":
    unittest.main()
