from collections.abc import Callable

from job_queue import JobQueue


class Worker:
    def __init__(self, queue: JobQueue, handler: Callable[[str], None]) -> None:
        self.queue = queue
        self.handler = handler

    def run_once(self, now: float) -> str | None:
        job = self.queue.claim(now)
        if job is None:
            return None
        try:
            self.handler(job["payload"])
        except Exception:
            self.queue.complete(job["id"])
            return job["id"]
        self.queue.complete(job["id"])
        return job["id"]
