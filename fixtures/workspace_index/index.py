from pathlib import Path

from chunker import chunk_text
from scanner import scan
from storage import IndexStore


class WorkspaceIndex:
    def __init__(self, store: IndexStore, max_lines: int = 4) -> None:
        self.store = store
        self.max_lines = max_lines

    def sync(self, root: Path) -> dict[str, int]:
        data = self.store.load()
        files = data["files"]
        documents = {document["path"]: document for document in scan(root)}
        counts = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}
        for path, document in documents.items():
            previous = files.get(path)
            if previous and previous["sha256"] == document["sha256"]:
                counts["unchanged"] += 1
                continue
            files[path] = {"sha256": document["sha256"], "chunks": (previous or {}).get("chunks", []) or chunk_text(document["text"], self.max_lines)}
            counts["updated" if previous else "added"] += 1
        if any(counts[key] for key in ("added", "updated", "removed")):
            self.store.save(data)
        return counts

    def entries(self) -> list[dict]:
        files = self.store.load()["files"]
        return [{"path": path, **chunk} for path in sorted(files) for chunk in files[path]["chunks"]]
