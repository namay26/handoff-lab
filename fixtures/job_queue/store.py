import json
from pathlib import Path


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
        return dict(job) if job is not None else None

    def all(self) -> list[dict]:
        return [dict(job) for job in self._jobs.values()]

    def put(self, job: dict) -> None:
        new = job["id"] not in self._jobs
        self._jobs[job["id"]] = dict(job)
        if new:
            self.path.write_text(json.dumps(self._jobs))
