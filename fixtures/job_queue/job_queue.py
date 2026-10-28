from store import JobStore


class JobQueue:
    def __init__(self, store: JobStore, lease_seconds: int = 30, max_attempts: int = 3) -> None:
        if lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("Lease and attempt limits must be positive")
        self.store = store
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def enqueue(self, job_id: str, payload: str) -> dict:
        job = {"id": job_id, "payload": payload, "status": "queued", "attempts": 0, "lease_until": None}
        self.store.put(job)
        return job

    def claim(self, now: float) -> dict | None:
        for job in self.store.all():
            if job["status"] != "queued":
                continue
            job["status"] = "running"
            job["attempts"] += 1
            job["lease_until"] = now + self.lease_seconds
            self.store.put(job)
            return job
        return None

    def complete(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        job["status"] = "done"
        job["lease_until"] = None
        self.store.put(job)

    def fail(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        job["status"] = "failed" if job["attempts"] >= self.max_attempts else "queued"
        job["lease_until"] = None
        self.store.put(job)
