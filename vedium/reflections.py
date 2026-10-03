# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Local text and Markdown storage for the reader's reflections and vocabulary."""

from contextlib import contextmanager
from datetime import datetime
import errno
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time


_SOURCE_MARKER = "<!-- vedium-source -->"
_TERM_MARKER = "<!-- vedium-term -->"

# A term becomes a Markdown heading AND the key entries are grouped under, so
# it has to stay short enough to read as one. "Define This" runs on whatever is
# selected, which is often a whole passage rather than a word — that would
# otherwise produce a paragraph-long "## Term:" line that nothing can group by.
MAX_TERM_LENGTH = 80

_LOCKS_GUARD = threading.Lock()
_JOURNAL_LOCKS: dict[str, threading.RLock] = {}


def is_term_sized(text: str) -> bool:
    """True when `text` is short enough to serve as a vocabulary heading."""
    return 0 < len(" ".join((text or "").split())) <= MAX_TERM_LENGTH


def _documents_dir() -> Path:
    """Return the user's real Documents folder, including Windows redirects."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Personal")
            return Path(os.path.expandvars(value)).expanduser()
        except OSError:
            pass
    return Path.home() / "Documents"


def reflection_journal_path(
    documents_dir: str | Path | None = None,
    *,
    configured_path: str | Path | None = None,
) -> Path:
    """Default per-user journal path; no file is created by this function."""
    if str(configured_path or "").strip():
        return Path(os.path.expandvars(str(configured_path))).expanduser()
    base = Path(documents_dir) if documents_dir is not None else _documents_dir()
    legacy = base / "Vedium" / "Reflections.md"
    return legacy if legacy.exists() else legacy.with_suffix(".txt")


def vocabulary_journal_path(
    documents_dir: str | Path | None = None,
    *,
    configured_path: str | Path | None = None,
    reflection_path: str | Path | None = None,
) -> Path:
    """Vocabulary file beside reflections by default, or at its own saved path."""
    if str(configured_path or "").strip():
        return Path(os.path.expandvars(str(configured_path))).expanduser()
    reflections = reflection_journal_path(
        documents_dir, configured_path=reflection_path,
    )
    legacy = reflections.with_name("Vocabulary.md")
    return legacy if legacy.exists() else legacy.with_suffix(".txt")


def _markdown_quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


# A line that is exactly one of the group sentinels. Only a whole line counts,
# because that is all _append_grouped_entry's section scan will match.
_MARKER_LINE_RE = re.compile(
    rf"(?m)^(?={re.escape(_SOURCE_MARKER)}[ \t]*$|{re.escape(_TERM_MARKER)}[ \t]*$)"
)


def _neutralise_markers(text: str) -> str:
    """Stop entry text from forging the sentinels that delimit groups.

    The sentinels are HTML comments, so they render invisibly — a model-written
    definition that happened to contain one would silently open a phantom group
    that later entries could be filed under. Prefixing a backslash is the
    standard Markdown escape: the line still reads the same, but it no longer
    starts with the sentinel, so the section scan skips it.
    """
    # Canonicalise first: the section reader uses universal-newline mode, so a
    # CRLF marker left untouched here would become an active LF marker on the
    # next append.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _MARKER_LINE_RE.sub("\\\\", text)


@contextmanager
def _journal_lock(journal: Path):
    """Serialize one journal's read-modify-replace across threads/processes."""
    key = os.path.normcase(str(journal.resolve()))
    with _LOCKS_GUARD:
        thread_lock = _JOURNAL_LOCKS.setdefault(key, threading.RLock())

    with thread_lock:
        lock_path = journal.with_name(f".{journal.name}.vedium.lock")
        with open(lock_path, "a+b") as handle:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EDEADLK):
                            raise
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_journal(journal: Path, content: str) -> None:
    """Replace the journal atomically so regrouping cannot leave a partial file."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", delete=False,
            dir=journal.parent, suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content.rstrip() + "\n")
            # os.replace is atomic in ordering only: without forcing the new
            # contents to disk first, a crash can leave the rename applied and
            # the data behind it still in the OS cache — i.e. the whole journal
            # replaced by a truncated file. Every append rewrites the file, so
            # that would cost the entire history, not just this entry.
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _append_grouped_entry(
    journal: Path,
    *,
    title: str,
    marker: str,
    heading_prefix: str,
    group_label: str,
    entry: str,
) -> None:
    """Insert an entry into its case-insensitive journal group."""
    journal.parent.mkdir(parents=True, exist_ok=True)
    # An entry never legitimately contains a sentinel, so this is the one place
    # that has to defuse them — doing it here rather than in each caller means a
    # future entry format cannot reintroduce the hole.
    if marker:
        entry = _neutralise_markers(entry)
    group_start = f"{marker}\n{heading_prefix}" if marker else heading_prefix
    group_block = f"{group_start}{group_label}"
    with _journal_lock(journal):
        content = journal.read_text(encoding="utf-8") if journal.exists() else ""
        if not content.strip():
            updated = f"{title}\n\n{group_block}\n\n{entry}"
        else:
            sections = list(re.finditer(
                rf"(?m)^{re.escape(group_start)}([^\r\n]+)$",
                content,
            ))
            section_index = next(
                (
                    index for index, match in enumerate(sections)
                    if match.group(1).strip().casefold() == group_label.casefold()
                ),
                None,
            )
            if section_index is None:
                updated = f"{content.rstrip()}\n\n{group_block}\n\n{entry}"
            else:
                section_end = (
                    sections[section_index + 1].start()
                    if section_index + 1 < len(sections)
                    else len(content)
                )
                updated = (
                    f"{content[:section_end].rstrip()}\n\n{entry}\n\n"
                    f"{content[section_end:].lstrip()}"
                )
        _write_journal(journal, updated)


def _text_body(text: str) -> str:
    """Indent content so pasted text cannot be mistaken for a group heading."""
    return "\n".join(f"    {line}" if line else "" for line in text.splitlines())


def append_reflection(
    passage: str,
    reflection: str,
    *,
    source: str = "",
    mode: str = "",
    path: str | Path | None = None,
    created_at: datetime | None = None,
) -> Path:
    """Append one structured reflection and return the journal path."""
    passage = (passage or "").strip()
    reflection = (reflection or "").strip()
    if not passage:
        raise ValueError("The selected passage is empty.")
    if not reflection:
        raise ValueError("Write a reflection before saving.")

    source = " ".join((source or "").split())
    mode = " ".join((mode or "").split())
    journal = Path(path).expanduser() if path is not None else reflection_journal_path()

    source_label = source or "Unspecified source"
    if journal.suffix.lower() == ".txt":
        lines = [f"Date: {(created_at or datetime.now()).strftime('%Y-%m-%d %H:%M')}", ""]
        if mode:
            lines.extend([f"Mode: {mode}", ""])
        lines.extend([
            "Passage:", _text_body(passage), "",
            "Reflection:", _text_body(reflection), "",
        ])
        _append_grouped_entry(
            journal, title="Vedium Reflections", marker="",
            heading_prefix="Source: ", group_label=source_label,
            entry="\n".join(lines),
        )
        return journal

    lines = [f"### {(created_at or datetime.now()).strftime('%Y-%m-%d %H:%M')}", ""]
    if mode:
        lines.extend([f"**Mode:** {mode}", ""])
    lines.extend([
        "#### Passage",
        "",
        _markdown_quote(passage),
        "",
        "#### Reflection",
        "",
        reflection,
        "",
        "---",
    ])
    entry = "\n".join(lines)

    _append_grouped_entry(
        journal,
        title="# Vedium Reflections",
        marker=_SOURCE_MARKER,
        heading_prefix="## Source: ",
        group_label=source_label,
        entry=entry,
    )
    return journal


def append_vocabulary_entry(
    term: str,
    definition: str,
    *,
    source: str = "",
    path: str | Path | None = None,
    created_at: datetime | None = None,
) -> Path:
    """Append one definition under a case-insensitive word or phrase heading."""
    term = " ".join((term or "").split())
    definition = (definition or "").strip()
    if not term:
        raise ValueError("The word or phrase is empty.")
    if len(term) > MAX_TERM_LENGTH:
        raise ValueError(
            f"That is {len(term)} characters long — too much for a vocabulary "
            f"heading (limit {MAX_TERM_LENGTH}). Shorten it to the word or "
            "phrase you want to look up later."
        )
    if not definition:
        raise ValueError("The definition is empty.")

    source = " ".join((source or "").split())
    journal = Path(path).expanduser() if path is not None else vocabulary_journal_path()
    if journal.suffix.lower() == ".txt":
        lines = [f"Date: {(created_at or datetime.now()).strftime('%Y-%m-%d %H:%M')}", ""]
        if source:
            lines.extend([f"Source: {source}", ""])
        lines.extend(["Definition:", _text_body(definition), ""])
        _append_grouped_entry(
            journal, title="Vedium Vocabulary", marker="",
            heading_prefix="Term: ", group_label=term,
            entry="\n".join(lines),
        )
        return journal

    lines = [f"### {(created_at or datetime.now()).strftime('%Y-%m-%d %H:%M')}", ""]
    if source:
        lines.extend([f"**Source:** {source}", ""])
    lines.extend([
        "#### Definition",
        "",
        definition,
        "",
        "---",
    ])
    _append_grouped_entry(
        journal,
        title="# Vedium Vocabulary",
        marker=_TERM_MARKER,
        heading_prefix="## Term: ",
        group_label=term,
        entry="\n".join(lines),
    )
    return journal
