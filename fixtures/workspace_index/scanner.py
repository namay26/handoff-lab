import hashlib
from pathlib import Path


def scan(root: Path) -> list[dict]:
    documents = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or path.is_symlink() or path.suffix not in {".md", ".txt"}:
            continue
        content = path.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        documents.append({"path": relative.as_posix(), "sha256": hashlib.sha256(content).hexdigest(), "text": text})
    return documents
