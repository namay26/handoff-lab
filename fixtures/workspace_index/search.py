import re

from index import WorkspaceIndex


def search(index: WorkspaceIndex, query: str, limit: int = 10, path_prefix: str | None = None) -> list[dict]:
    if limit < 1:
        return []
    terms = re.findall(r"[\w]+", query.casefold())
    if not terms:
        return []
    results = []
    for entry in index.entries():
        if path_prefix is not None and not entry["path"].startswith(path_prefix):
            continue
        words = re.findall(r"[\w]+", entry["text"].casefold())
        if not any(term in words for term in terms):
            continue
        score = sum(words.count(term) for term in terms)
        results.append({**entry, "score": score})
    results.sort(key=lambda item: (item["path"], item["start_line"]))
    return results[:limit]
