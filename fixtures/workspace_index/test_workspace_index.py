import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from chunker import chunk_text
from index import WorkspaceIndex
from scanner import scan
from search import search
from storage import IndexStore


class WorkspaceIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.store = IndexStore(self.root / "state" / "index.json")
        self.index = WorkspaceIndex(self.store, max_lines=2)

    def put(self, path, text):
        target = self.docs / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def test_scanner_recurses_in_sorted_order_with_hashes(self):
        self.put("z.txt", "zeta")
        self.put("a/note.md", "alpha")
        found = scan(self.docs)
        self.assertEqual([item["path"] for item in found], ["a/note.md", "z.txt"])
        self.assertEqual(found[0]["sha256"], hashlib.sha256(b"alpha").hexdigest())

    def test_scanner_skips_hidden_unsupported_and_binary_files(self):
        self.put(".private/secret.txt", "secret")
        self.put("a/.draft.md", "draft")
        self.put("plain.py", "ignored")
        (self.docs / "bad.txt").write_bytes(b"\xff")
        self.put("good.txt", "visible")
        self.assertEqual([item["path"] for item in scan(self.docs)], ["good.txt"])

    def test_scanner_skips_symlink(self):
        target = self.put("target.txt", "visible")
        (self.docs / "alias.txt").symlink_to(target)
        self.assertEqual([item["path"] for item in scan(self.docs)], ["target.txt"])

    def test_scanner_hash_changes_with_content(self):
        target = self.put("a.md", "before")
        before = scan(self.docs)[0]["sha256"]
        target.write_text("after", encoding="utf-8")
        self.assertNotEqual(before, scan(self.docs)[0]["sha256"])

    def test_chunker_preserves_one_based_source_lines(self):
        self.assertEqual(chunk_text("a\nb\nc\nd\ne", 2), [
            {"start_line": 1, "end_line": 2, "text": "a\nb"},
            {"start_line": 3, "end_line": 4, "text": "c\nd"},
            {"start_line": 5, "end_line": 5, "text": "e"},
        ])

    def test_chunker_skips_blank_groups_but_keeps_line_positions(self):
        self.assertEqual(chunk_text("\n \nhello\nworld", 2), [
            {"start_line": 3, "end_line": 4, "text": "hello\nworld"},
        ])

    def test_chunker_rejects_invalid_size_and_empty_text(self):
        with self.assertRaises(ValueError):
            chunk_text("abc", 0)
        self.assertEqual(chunk_text("", 2), [])

    def test_store_starts_empty_and_persists_across_instances(self):
        self.assertEqual(self.store.load(), {"files": {}})
        self.store.save({"files": {"a.md": {"sha256": "abc", "chunks": []}}})
        self.assertEqual(IndexStore(self.store.path).load()["files"]["a.md"]["sha256"], "abc")
        self.assertFalse(self.store.path.with_suffix(".json.tmp").exists())

    def test_first_sync_adds_files_and_persists_chunks(self):
        self.put("a.md", "one\ntwo\nthree")
        self.put("b.txt", "four")
        self.assertEqual(self.index.sync(self.docs), {"added": 2, "updated": 0, "removed": 0, "unchanged": 0})
        self.assertEqual(len(self.index.entries()), 3)
        self.assertEqual(json.loads(self.store.path.read_text())["files"]["a.md"]["chunks"][-1]["text"], "three")

    def test_second_sync_is_idempotent(self):
        self.put("a.md", "one")
        self.index.sync(self.docs)
        before = self.store.path.stat().st_mtime_ns
        self.assertEqual(self.index.sync(self.docs), {"added": 0, "updated": 0, "removed": 0, "unchanged": 1})
        self.assertEqual(self.store.path.stat().st_mtime_ns, before)

    def test_edit_replaces_old_chunks_and_preserves_unchanged_file(self):
        self.put("a.md", "old token")
        self.put("b.md", "stable token")
        self.index.sync(self.docs)
        old_hash = self.store.load()["files"]["b.md"]["sha256"]
        self.put("a.md", "new token")
        self.assertEqual(self.index.sync(self.docs), {"added": 0, "updated": 1, "removed": 0, "unchanged": 1})
        self.assertEqual(search(self.index, "old"), [])
        self.assertEqual(search(self.index, "new")[0]["path"], "a.md")
        self.assertEqual(self.store.load()["files"]["b.md"]["sha256"], old_hash)

    def test_deleted_file_disappears_from_index_and_search(self):
        self.put("a.md", "alpha")
        self.put("b.md", "beta")
        self.index.sync(self.docs)
        (self.docs / "a.md").unlink()
        self.assertEqual(self.index.sync(self.docs), {"added": 0, "updated": 0, "removed": 1, "unchanged": 1})
        self.assertEqual(search(self.index, "alpha"), [])
        self.assertNotIn("a.md", self.store.load()["files"])

    def test_reopened_index_can_search_persisted_state(self):
        self.put("guide.md", "durable searchable content")
        self.index.sync(self.docs)
        reopened = WorkspaceIndex(IndexStore(self.store.path), max_lines=2)
        self.assertEqual(search(reopened, "searchable")[0]["path"], "guide.md")

    def test_search_requires_every_term_and_ignores_case(self):
        self.put("one.md", "Alpha beta")
        self.put("two.md", "Alpha only")
        self.index.sync(self.docs)
        self.assertEqual([item["path"] for item in search(self.index, "ALPHA beta")], ["one.md"])

    def test_search_ranks_frequency_then_path_and_line(self):
        self.put("b.md", "alpha alpha\nalpha")
        self.put("a.md", "alpha\nother\nalpha")
        self.index.sync(self.docs)
        hits = search(self.index, "alpha")
        self.assertEqual([(hit["path"], hit["start_line"], hit["score"]) for hit in hits], [
            ("b.md", 1, 3), ("a.md", 1, 1), ("a.md", 3, 1),
        ])

    def test_search_prefix_and_limit(self):
        self.put("a/one.md", "alpha")
        self.put("b/two.md", "alpha")
        self.index.sync(self.docs)
        self.assertEqual([item["path"] for item in search(self.index, "alpha", path_prefix="a/")], ["a/one.md"])
        self.assertEqual(len(search(self.index, "alpha", limit=1)), 1)
        self.assertEqual(search(self.index, "alpha", limit=0), [])

    def test_empty_or_punctuation_query_has_no_results(self):
        self.put("a.md", "alpha")
        self.index.sync(self.docs)
        self.assertEqual(search(self.index, "   "), [])
        self.assertEqual(search(self.index, "?!"), [])


if __name__ == "__main__":
    unittest.main()
