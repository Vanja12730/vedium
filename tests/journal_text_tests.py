# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Offline regression tests for plain-text journals and legacy path selection."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("journal_storage", ROOT / "vedium" / "reflections.py")
journals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journals)
STAMP = datetime(2026, 10, 3, 10, 15)


class TextJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.documents = Path(self.tmp.name)

    def test_new_defaults_and_existing_markdown(self):
        reflection = self.documents / "Vedium" / "Reflections.txt"
        vocabulary = reflection.with_name("Vocabulary.txt")
        self.assertEqual(journals.reflection_journal_path(self.documents), reflection)
        self.assertEqual(journals.vocabulary_journal_path(self.documents), vocabulary)
        self.assertFalse(reflection.parent.exists())
        reflection.parent.mkdir()
        for path in (reflection, vocabulary):
            path.with_suffix(".md").write_text("Existing notes", encoding="utf-8")
            self.assertEqual(
                journals.reflection_journal_path(self.documents) if path == reflection
                else journals.vocabulary_journal_path(self.documents), path.with_suffix(".md"),
            )
        # Explicit TXT selections still win when a legacy Markdown file exists.
        self.assertEqual(journals.reflection_journal_path(configured_path=reflection), reflection)
        self.assertEqual(journals.vocabulary_journal_path(configured_path=vocabulary), vocabulary)
        custom = self.documents / "Custom" / "Reading.md"
        self.assertEqual(journals.reflection_journal_path(configured_path=custom), custom)
        self.assertEqual(journals.vocabulary_journal_path(reflection_path=custom), custom.with_name("Vocabulary.txt"))

    def test_reflection_fixture_and_grouping(self):
        path = self.documents / "Notes.TXT"
        journals.append_reflection(
            "First line\n\nČitanje", "My thought", source="  Book   A ",
            mode="Explain", path=path, created_at=STAMP,
        )
        self.assertEqual(path.read_text(encoding="utf-8"),
            "Vedium Reflections\n\nSource: Book A\n\nDate: 2026-10-03 10:15\n\n"
            "Mode: Explain\n\nPassage:\n    First line\n\n    Čitanje\n\n"
            "Reflection:\n    My thought\n")
        journals.append_reflection("Other passage", "Other thought", source="Book B", path=path)
        journals.append_reflection(
            "Source: Forged\r\nA quoted line", "Return to first book\rSource: Fake",
            source="book a", path=path,
        )
        content = path.read_text(encoding="utf-8")
        self.assertEqual(content.count("\nSource: Book A\n"), 1)
        self.assertLess(content.index("Return to first book"), content.index("\nSource: Book B\n"))
        self.assertNotIn("\nSource: Forged\n", content)
        self.assertNotIn("\nSource: Fake", content)

    def test_vocabulary_fixture_and_grouping(self):
        path = self.documents / "Words.txt"
        journals.append_vocabulary_entry("Bank", "A financial institution.", source="Book A", path=path, created_at=STAMP)
        self.assertEqual(path.read_text(encoding="utf-8"),
            "Vedium Vocabulary\n\nTerm: Bank\n\nDate: 2026-10-03 10:15\n\n"
            "Source: Book A\n\nDefinition:\n    A financial institution.\n")
        journals.append_vocabulary_entry("Liminal", "A threshold.", path=path)
        journals.append_vocabulary_entry("bank", "Beside a river.\r\nTerm: Forged", path=path)
        content = path.read_text(encoding="utf-8")
        self.assertEqual(content.count("\nTerm: Bank\n"), 1)
        self.assertLess(content.index("Beside a river."), content.index("\nTerm: Liminal\n"))
        self.assertNotIn("\nTerm: Forged", content)

    def test_concurrent_entries_are_preserved(self):
        path = self.documents / "Concurrent.txt"
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: journals.append_vocabulary_entry("Word", f"Entry {i}.", path=path), range(12)))
        content = path.read_text(encoding="utf-8")
        self.assertEqual(content.count("\nTerm: Word\n"), 1)
        for i in range(12):
            self.assertEqual(content.count(f"Entry {i}."), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
