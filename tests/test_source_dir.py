"""``dir`` source tests: options, glob matching, text/binary fields, and the add/modify/remove
flows through ``run_collection`` against real temporary directories."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import since.sources.dir as dirmod
from since.collect import CollectResult, register_sources, run_collection
from since.config import Config, ConfigError, SourceConfig, parse_config
from since.model import (
    KIND_ADDED,
    KIND_BASELINE,
    KIND_MODIFIED,
    KIND_REMOVED,
    KIND_SOURCE_ERROR,
    KIND_SOURCE_RECOVERED,
    Record,
)
from since.sources import CollectError, get_collector
from since.sources.dir import DirCollector, compile_glob, glob_match
from since.store import Store

T0 = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)


def at(minutes: int = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


def write(root: Path, rel: str, data: bytes | str = b"") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def make_cfg(root: Path | str, source_id: str = "docs", **options: Any) -> SourceConfig:
    return SourceConfig(id=source_id, type="dir", options={"path": str(root), **options})


def collect(root: Path, **options: Any) -> list[Record]:
    return DirCollector().collect(make_cfg(root, **options))


def keys(records: list[Record]) -> list[str]:
    return [r.key for r in records]


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store.open()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "docs-root"
    r.mkdir()
    return r


def run(store: Store, cfg: SourceConfig, minute: int = 0) -> CollectResult:
    return run_collection(store, cfg, get_collector("dir"), at(minute))


def kinds(store: Store, after: int = 0) -> list[tuple[str, str | None]]:
    return [(e.kind, e.record_key) for e in store.events_after(after, "docs")]


# -- glob matching -------------------------------------------------------------------------------

GLOB_CASES = [
    # "**/" also matches zero directories
    ("**/*.md", "a.md", True),
    ("**/*.md", "x/y/a.md", True),
    ("**/*.md", "a.txt", False),
    ("**/*.md", "a.md/b", False),
    ("*.md", "a.md", True),
    ("*.md", "x/a.md", False),
    ("*.tar.gz", "a.tar.gz", True),
    ("**/*", "a", True),
    ("**/*", "x/y/z.bin", True),
    ("**", "a", True),
    ("**", "x/y", True),
    # trailing "/**": everything below
    ("docs/**", "docs/a", True),
    ("docs/**", "docs/x/y", True),
    ("docs/**", "docs", False),
    ("docs/**", "other/docs/a", False),
    ("docs/**/*.md", "docs/a.md", True),
    ("docs/**/*.md", "docs/x/y/a.md", True),
    ("docs/**/*.md", "xdocs/a.md", False),
    ("docs/**/*.md", "docs/a.txt", False),
    ("**/node_modules/**", "node_modules/a", True),
    ("**/node_modules/**", "a/node_modules/b/c", True),
    ("**/node_modules/**", "a/mynode_modules/b", False),
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/y/b", True),
    ("a/**/b", "a/xb", False),
    # "**" inside a segment is an ordinary "*"
    ("a**b", "axxb", True),
    ("a**b", "a/b", False),
    # ? and character classes never cross a "/"
    ("a?c", "abc", True),
    ("a?c", "ac", False),
    ("a?c", "a/c", False),
    ("[abc].txt", "a.txt", True),
    ("[abc].txt", "d.txt", False),
    ("[!abc].txt", "d.txt", True),
    ("[!abc].txt", "a.txt", False),
    ("[a-c].txt", "b.txt", True),
    ("[a-c].txt", "d.txt", False),
    ("x[!a]y", "xby", True),
    ("x[!a]y", "x/y", False),
    ("[]]x", "]x", True),
    ("[!]]x", "ax", True),
    ("[!]]x", "]x", False),
    ("[a^b].txt", "^.txt", True),
    # literals: case-sensitive, regex characters are not special, "[" without "]" is literal
    ("*.MD", "a.md", False),
    ("a.b", "a.b", True),
    ("a.b", "axb", False),
    ("a+b(1).txt", "a+b(1).txt", True),
    ("a[b", "a[b", True),
    ("a[b", "ab", False),
    ("a\\b", "a\\b", True),
    ("é*", "éa", True),
    # dotfiles are ordinary files
    ("*", ".hidden", True),
    (".*", ".env", True),
]


@pytest.mark.parametrize(("pattern", "path", "expected"), GLOB_CASES)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected


def test_glob_matches_names_with_newlines() -> None:
    assert glob_match("a/**", "a/b\nc")
    assert glob_match("*.txt", "b\nc.txt")


def test_invalid_glob_is_a_value_error() -> None:
    with pytest.raises(ValueError, match=r"\[z-a\]"):
        compile_glob("[z-a]")


# -- options / validate --------------------------------------------------------------------------


def opts_cfg(**options: Any) -> SourceConfig:
    return SourceConfig(id="docs", type="dir", options=options)


# Absolute on Windows (drive + backslash) and POSIX alike; "/x" alone is not absolute on Windows.
ABS = str(Path(Path.cwd().anchor) / "since-test-no-such-dir")


INVALID_OPTIONS = [
    ({}, "path"),
    ({"path": ""}, "path"),
    ({"path": "   "}, "path"),
    ({"path": 5}, "path"),
    ({"path": None}, "path"),
    ({"path": ["a"]}, "path"),
    ({"path": ABS, "include": "**/*.md"}, "include"),
    ({"path": ABS, "include": ["*.md", 3]}, "include"),
    ({"path": ABS, "include": None}, "include"),
    ({"path": ABS, "include": ["[z-a]"]}, "include"),
    ({"path": ABS, "exclude": "drafts/**"}, "exclude"),
    ({"path": ABS, "exclude": [None]}, "exclude"),
    ({"path": ABS, "max_text_bytes": -1}, "max_text_bytes"),
    ({"path": ABS, "max_text_bytes": 1.5}, "max_text_bytes"),
    ({"path": ABS, "max_text_bytes": "100"}, "max_text_bytes"),
    ({"path": ABS, "max_text_bytes": True}, "max_text_bytes"),
    ({"path": ABS, "max_text_bytes": None}, "max_text_bytes"),
    ({"path": ABS, "inlcude": ["*.md"]}, "inlcude"),
    ({"paths": ABS}, "paths"),
    ({"path": "docs"}, "path"),  # relative paths are rejected, see below
]


@pytest.mark.parametrize(("options", "key"), INVALID_OPTIONS)
def test_validate_rejects_invalid_options_naming_the_key(options: dict[str, Any], key: str) -> None:
    with pytest.raises(ConfigError) as info:
        DirCollector().validate(opts_cfg(**options))
    message = str(info.value)
    assert f"key '{key}'" in message
    assert "source 'docs'" in message


@pytest.mark.parametrize(
    "options",
    [
        {"path": ABS},  # validate does no I/O: the path need not exist
        {"path": "~"},
        {"path": "~/docs", "include": ["**/*.md"], "exclude": ["drafts/**"], "max_text_bytes": 0},
        {"path": ABS, "include": [], "exclude": []},
    ],
)
def test_validate_accepts_valid_options(options: dict[str, Any]) -> None:
    DirCollector().validate(opts_cfg(**options))


RELATIVE_PATHS = ["docs", "./docs", "../docs", ".", "sub/dir", "x"]
if sys.platform == "win32":
    RELATIVE_PATHS += ["/x", r"\x", "C:x"]  # rooted or drive-relative, but not absolute


@pytest.mark.parametrize("path", RELATIVE_PATHS)
def test_relative_path_is_a_config_error_naming_path(path: str, root: Path) -> None:
    # A relative root resolves against the working directory, which differs between the daemon
    # and `since collect`: that would produce mass false added/removed events.
    with pytest.raises(ConfigError, match="key 'path'.*absolute"):
        DirCollector().validate(opts_cfg(path=path))
    with pytest.raises(ConfigError, match="key 'path'"):
        DirCollector().collect(opts_cfg(path=path))


def test_relative_path_is_rejected_even_if_it_exists_relative_to_the_cwd(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root, "a.txt", "x")
    monkeypatch.chdir(root.parent)

    with pytest.raises(ConfigError, match="key 'path'"):
        DirCollector().collect(opts_cfg(path=root.name))


def test_unexpandable_tilde_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: Path) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", fail)

    with pytest.raises(ConfigError, match="key 'path'.*cannot expand"):
        DirCollector().validate(opts_cfg(path="~nobody/docs"))


def test_collector_identity_and_key_label(root: Path) -> None:
    collector = get_collector("dir")
    assert isinstance(collector, DirCollector)
    assert collector.type_name == "dir"
    assert collector.key_label(make_cfg(root)) == ""


def test_invalid_options_also_fail_collect(root: Path) -> None:
    with pytest.raises(ConfigError, match="max_text_bytes"):
        DirCollector().collect(make_cfg(root, max_text_bytes=-5))


def test_register_sources_validates_and_stores_the_empty_key_label(
    store: Store, root: Path
) -> None:
    config = parse_config(
        {"sources": [{"id": "docs", "type": "dir", "path": str(root), "include": ["**/*.md"]}]}
    )
    reg = register_sources(store, config)
    assert [(cfg.id, type(c).__name__) for cfg, c in reg.collectable] == [("docs", "DirCollector")]
    state = store.get_source_state("docs")
    assert state is not None and state.key_label == ""

    bad = Config(sources=[opts_cfg(include=["*.md"])])
    with pytest.raises(ConfigError, match="path"):
        register_sources(store, bad)


# -- collecting: keys, fields ---------------------------------------------------------------------


def test_records_are_keyed_by_relative_posix_path_and_sorted(root: Path) -> None:
    for rel in ("b.txt", "a-b.txt", "a/b.txt", "A.txt", "z/y/x/deep.txt", "é/ü.txt"):
        write(root, rel, rel)

    records = collect(root)

    # sorted by key (code point order), not by walk order ("a-b.txt" < "a/b.txt" < "b.txt")
    assert keys(records) == sorted(
        ["b.txt", "a-b.txt", "a/b.txt", "A.txt", "z/y/x/deep.txt", "é/ü.txt"]
    )
    assert all("\\" not in r.key for r in records)
    assert records == collect(root)  # deterministic


def test_nested_paths_use_forward_slashes(store: Store, root: Path) -> None:
    write(root, "a/b/c.txt", "deep")
    run(store, make_cfg(root))
    write(root, "a/b/d.txt", "deeper")

    run(store, make_cfg(root), 1)

    assert kinds(store) == [(KIND_BASELINE, None), (KIND_ADDED, "a/b/d.txt")]
    assert sorted(store.get_snapshot("docs")) == ["a/b/c.txt", "a/b/d.txt"]


def test_text_file_fields_and_no_newline_normalisation(root: Path) -> None:
    write(root, "crlf.txt", b"a\r\nb\nc\r")
    write(root, "bom.txt", b"\xef\xbb\xbfhello")
    write(root, "empty.txt", b"")
    write(root, "uni.txt", "日本語 ✓")

    by_key = {r.key: r.fields for r in collect(root)}

    assert by_key["crlf.txt"] == {"size": 7, "text": "a\r\nb\nc\r"}
    assert by_key["bom.txt"] == {"size": 8, "text": "﻿hello"}
    assert by_key["empty.txt"] == {"size": 0, "text": ""}
    assert by_key["uni.txt"] == {"size": len("日本語 ✓".encode()), "text": "日本語 ✓"}


def test_binary_files_get_sha256_instead_of_text(root: Path) -> None:
    nul = b"PK\x00\x03text-with-nul"
    bad_utf8 = b"caf\xe9 not utf-8"
    write(root, "nul.bin", nul)
    write(root, "latin1.txt", bad_utf8)

    by_key = {r.key: r.fields for r in collect(root)}

    assert by_key["nul.bin"] == {"size": len(nul), "sha256": hashlib.sha256(nul).hexdigest()}
    assert by_key["latin1.txt"] == {
        "size": len(bad_utf8),
        "sha256": hashlib.sha256(bad_utf8).hexdigest(),
    }
    assert all("text" not in f for f in by_key.values())


def test_no_mtime_field(root: Path) -> None:
    write(root, "a.txt", "x")
    assert set(collect(root)[0].fields) == {"size", "text"}


def test_large_file_is_hashed_over_all_chunks(root: Path) -> None:
    data = b"a" * (2 * (1 << 20) + 12345)  # valid text, but over the default max_text_bytes
    write(root, "big.log", data)

    (rec,) = collect(root)

    assert rec.fields == {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@pytest.mark.parametrize(
    ("data", "max_bytes", "is_text"),
    [
        (b"", 0, True),
        (b"x", 0, False),
        (b"abcd", 4, True),  # exactly one chunk, exactly max
        (b"abcde", 4, False),  # one byte over max
        (b"abcdef", 6, True),  # size == max_text_bytes is still text
        (b"abcdefg", 6, False),  # size == max_text_bytes + 1 is not
        (b"abcdefghij", 6, False),  # hashing continues across several chunks
        (b"abcdefghij", 10, True),
        ("日本語".encode(), 100, True),  # multi-byte characters split across chunks
    ],
)
def test_text_threshold_and_chunk_boundaries(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
    max_bytes: int,
    is_text: bool,
) -> None:
    monkeypatch.setattr(dirmod, "_CHUNK", 4)
    write(root, "f", data)

    (rec,) = collect(root, max_text_bytes=max_bytes)

    if is_text:
        assert rec.fields == {"size": len(data), "text": data.decode()}
    else:
        assert rec.fields == {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def test_tilde_in_path_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    write(home, "docs/a.txt", "hi")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    records = DirCollector().collect(opts_cfg(path="~/docs"))

    assert keys(records) == ["a.txt"]


# -- include / exclude ---------------------------------------------------------------------------


@pytest.fixture
def tree(root: Path) -> Path:
    for rel in ("a.md", "notes/b.md", "notes/c.txt", "drafts/d.md", "drafts/deep/e.md", "skip.log"):
        write(root, rel, rel)
    return root


def test_default_include_takes_every_file(tree: Path) -> None:
    assert keys(collect(tree)) == [
        "a.md",
        "drafts/d.md",
        "drafts/deep/e.md",
        "notes/b.md",
        "notes/c.txt",
        "skip.log",
    ]


def test_include_and_exclude_patterns(tree: Path) -> None:
    assert keys(collect(tree, include=["**/*.md"])) == [
        "a.md",
        "drafts/d.md",
        "drafts/deep/e.md",
        "notes/b.md",
    ]
    # exclude wins over include
    assert keys(collect(tree, include=["**/*.md"], exclude=["drafts/**"])) == [
        "a.md",
        "notes/b.md",
    ]
    # a file needs to match only one of several include patterns
    assert keys(collect(tree, include=["*.md", "notes/*.txt"])) == ["a.md", "notes/c.txt"]
    # exclude alone works on top of the default include
    assert keys(collect(tree, exclude=["**/*.md", "*.log"])) == ["notes/c.txt"]
    assert keys(collect(tree, exclude=["**/deep/**"])) == [
        "a.md",
        "drafts/d.md",
        "notes/b.md",
        "notes/c.txt",
        "skip.log",
    ]


def test_empty_include_and_nothing_matching_give_no_records(tree: Path) -> None:
    assert collect(tree, include=[]) == []
    assert collect(tree, include=["*.nomatch"]) == []


def test_excluded_files_are_never_read(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = dirmod._read_fields
    seen: list[str] = []

    def spy(path: str, max_text_bytes: int) -> dict[str, Any]:
        seen.append(Path(path).name)
        return real(path, max_text_bytes)

    monkeypatch.setattr(dirmod, "_read_fields", spy)
    collect(tree, include=["**/*.md"], exclude=["drafts/**"])
    assert sorted(seen) == ["a.md", "b.md"]


def test_include_exclude_changes_show_as_added_and_removed(store: Store, tree: Path) -> None:
    run(store, make_cfg(tree, include=["**/*.md"]))
    run(store, make_cfg(tree, include=["**/*.md"], exclude=["drafts/**"]), 1)

    assert kinds(store) == [
        (KIND_BASELINE, None),
        (KIND_REMOVED, "drafts/d.md"),
        (KIND_REMOVED, "drafts/deep/e.md"),
    ]


# -- excluded directories are not walked ---------------------------------------------------------

PRUNE_TREE = [
    ".git/config",
    ".git/objects/ab",
    "src/a.txt",
    "src/build/o.bin",
    "src/rebuild/r.txt",
    "build/b.txt",
    "docs/d.md",
    "docs/sub/e.md",
]
PRUNE_DIRS = {
    ".",
    ".git",
    ".git/objects",
    "src",
    "src/build",
    "src/rebuild",
    "build",
    "docs",
    "docs/sub",
}


def spy_on_walk(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[str]:
    """Record the relative path of every directory that ``os.walk`` descends into."""
    real_walk = os.walk
    seen: list[str] = []

    def spy(top: Any, **kwargs: Any) -> Iterator[Any]:
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            seen.append(Path(dirpath).relative_to(root).as_posix())
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(os, "walk", spy)
    return seen


def add_unlistable_dir(monkeypatch: pytest.MonkeyPatch, root: Path, parent: str, name: str) -> None:
    """Make ``os.walk`` list a directory ``name`` inside ``parent`` ("." = root) that cannot be
    listed (it does not exist), like a directory without read permission."""
    real_walk = os.walk

    def walk(top: Any, **kwargs: Any) -> Iterator[Any]:
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            if Path(dirpath).relative_to(root).as_posix() == parent:
                dirnames.append(name)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(os, "walk", walk)


@pytest.mark.parametrize(
    ("exclude", "pruned"),
    [
        ([".git/**"], {".git", ".git/objects"}),
        (["**/build/**"], {"build", "src/build"}),  # not "src/rebuild"
        (["*/build/**"], {"src/build"}),  # the prefix pattern must match the directory's path
        (["docs/**"], {"docs", "docs/sub"}),
        (["docs/sub/**"], {"docs/sub"}),
        (["src/**", ".git/**"], {"src", "src/build", "src/rebuild", ".git", ".git/objects"}),
        (["**"], PRUNE_DIRS - {"."}),
        # not "everything below a directory": nothing may be pruned
        (["docs/*"], set()),
        (["**/*.md"], set()),
        (["docs/**/*.md"], set()),
        (["docs"], set()),
    ],
)
def test_exclude_prunes_exactly_the_directories_it_covers_entirely(
    root: Path, monkeypatch: pytest.MonkeyPatch, exclude: list[str], pruned: set[str]
) -> None:
    for rel in PRUNE_TREE:
        write(root, rel, rel)
    seen = spy_on_walk(monkeypatch, root)

    records = collect(root, exclude=exclude)

    assert set(seen) == PRUNE_DIRS - pruned
    # pruning is only an optimisation: the result is what the patterns say file by file
    assert keys(records) == sorted(
        f for f in PRUNE_TREE if not any(glob_match(p, f) for p in exclude)
    )


def test_files_below_an_excluded_directory_are_never_read(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for rel in PRUNE_TREE:
        write(root, rel, rel)
    real = dirmod._read_fields
    read: list[str] = []

    def spy(path: str, max_text_bytes: int) -> dict[str, Any]:
        read.append(Path(path).relative_to(root).as_posix())
        return real(path, max_text_bytes)

    monkeypatch.setattr(dirmod, "_read_fields", spy)

    collect(root, exclude=[".git/**"])

    assert not [r for r in read if r.startswith(".git/")]
    assert "src/a.txt" in read and "docs/sub/e.md" in read


def test_unlistable_directory_fails_the_run_naming_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root, "vendor/v.txt", "v")
    add_unlistable_dir(monkeypatch, root, "vendor", "ghost")  # stands in for "permission denied"

    with pytest.raises(CollectError, match=r'cannot list directory "vendor/ghost"'):
        collect(root)

    # ...and a pattern that does not cover the whole directory does not hide it
    with pytest.raises(CollectError, match=r'directory "vendor/ghost"'):
        collect(root, exclude=["vendor/ghost/*"])


def test_unlistable_directory_inside_an_excluded_tree_does_not_fail_the_run(
    store: Store, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root, "a.txt", "a")
    write(root, "vendor/v.txt", "v")
    cfg = make_cfg(root, exclude=["vendor/**"])
    run(store, cfg)
    add_unlistable_dir(monkeypatch, root, "vendor", "ghost")

    result = run(store, cfg, 1)

    assert result.error is None and result.seqs == []
    assert kinds(store) == [(KIND_BASELINE, None)]
    assert sorted(store.get_snapshot("docs")) == ["a.txt"]


def test_unlistable_excluded_directory_is_pruned_but_its_siblings_are_collected(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root, "vendor/v.txt", "v")
    add_unlistable_dir(monkeypatch, root, "vendor", "ghost")

    assert keys(collect(root, exclude=["**/ghost/**"])) == ["vendor/v.txt"]


# -- run_collection flows ------------------------------------------------------------------------


def test_baseline_then_add_modify_remove(store: Store, root: Path) -> None:
    write(root, "a.txt", "one")
    write(root, "notes/b.md", "two")
    cfg = make_cfg(root)

    first = run(store, cfg, 0)

    assert first.error is None and len(first.seqs) == 1
    events = store.events_after(0, "docs")
    assert [(e.kind, e.record_key, e.detail) for e in events] == [
        (KIND_BASELINE, None, {"record_count": 2})
    ]
    snapshot = store.get_snapshot("docs")
    assert sorted(snapshot) == ["a.txt", "notes/b.md"]
    assert snapshot["a.txt"].fields == {"size": 3, "text": "one"}

    # nothing changed -> nothing happens
    assert run(store, cfg, 1).seqs == []

    write(root, "a.txt", "one!")  # modified
    write(root, "c.txt", "new")  # added
    (root / "notes" / "b.md").unlink()  # removed
    last_seq = first.seqs[0]

    second = run(store, cfg, 2)

    assert second.error is None
    assert kinds(store, last_seq) == [
        (KIND_MODIFIED, "a.txt"),
        (KIND_ADDED, "c.txt"),
        (KIND_REMOVED, "notes/b.md"),
    ]
    modified = store.events_after(last_seq, "docs")[0]
    assert {(c.field, c.old, c.new) for c in modified.field_changes} == {
        ("size", 3, 4),
        ("text", "one", "one!"),
    }
    assert sorted(store.get_snapshot("docs")) == ["a.txt", "c.txt"]
    state = store.get_source_state("docs")
    assert state is not None and state.record_count == 2


def test_empty_directory_baselines_zero_records(store: Store, root: Path) -> None:
    run(store, make_cfg(root))

    (event,) = store.events_after(0, "docs")
    assert (event.kind, event.detail) == (KIND_BASELINE, {"record_count": 0})


def test_binary_change_is_a_sha256_modification(store: Store, root: Path) -> None:
    write(root, "img.bin", b"\x00\x01\x02")
    run(store, make_cfg(root))
    write(root, "img.bin", b"\x00\x01\x03")  # same size, different bytes

    run(store, make_cfg(root), 1)

    modified = store.events_after(0, "docs")[-1]
    assert (modified.kind, modified.record_key) == (KIND_MODIFIED, "img.bin")
    assert [c.field for c in modified.field_changes] == ["sha256"]


def test_touching_a_file_creates_no_event(store: Store, root: Path) -> None:
    path = write(root, "a.txt", "same")
    cfg = make_cfg(root)
    run(store, cfg)
    stat = path.stat()

    os.utime(path, ns=(stat.st_atime_ns + 10**11, stat.st_mtime_ns + 10**11))  # touch
    assert run(store, cfg, 1).seqs == []

    path.write_bytes(b"same")  # rewritten with identical bytes: new mtime, same content
    assert run(store, cfg, 2).seqs == []
    assert kinds(store) == [(KIND_BASELINE, None)]


def test_missing_root_is_a_source_error_and_never_removes(store: Store, root: Path) -> None:
    write(root, "a.txt", "one")
    write(root, "b.txt", "two")
    cfg = make_cfg(root)
    run(store, cfg, 0)
    shutil.rmtree(root)

    failed = run(store, cfg, 1)

    assert failed.error is not None and str(root) in failed.error
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]
    assert sorted(store.get_snapshot("docs")) == ["a.txt", "b.txt"]
    assert run(store, cfg, 2).error is not None  # second failure: still one source_error
    assert len(kinds(store)) == 2

    write(root, "a.txt", "one")
    write(root, "b.txt", "two")
    recovered = run(store, cfg, 3)

    assert recovered.error is None
    assert kinds(store)[2:] == [(KIND_SOURCE_RECOVERED, None)]


def test_missing_root_on_first_run_does_not_baseline(store: Store, tmp_path: Path) -> None:
    result = run(store, make_cfg(tmp_path / "nope"))

    assert result.error is not None
    assert kinds(store) == [(KIND_SOURCE_ERROR, None)]
    state = store.get_source_state("docs")
    assert state is not None and state.baselined is False


def test_root_that_is_a_file_is_a_source_error(store: Store, tmp_path: Path) -> None:
    afile = write(tmp_path, "not-a-dir.txt", "x")

    result = run(store, make_cfg(afile))

    assert result.error is not None and "not a directory" in result.error
    assert kinds(store) == [(KIND_SOURCE_ERROR, None)]


def test_unreadable_file_fails_the_run_and_names_only_its_path(
    store: Store, root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write(root, "a.txt", "fine")
    write(root, "sub/locked.txt", "TOP-SECRET-CONTENT")
    cfg = make_cfg(root)
    run(store, cfg)

    real = dirmod._read_fields

    def deny(path: str, max_text_bytes: int) -> dict[str, Any]:
        if path.endswith("locked.txt"):
            raise PermissionError(13, "Permission denied", path)
        return real(path, max_text_bytes)

    monkeypatch.setattr(dirmod, "_read_fields", deny)

    result = run(store, cfg, 1)

    assert result.error is not None
    assert 'cannot read "sub/locked.txt"' in result.error
    assert "Permission denied" in result.error
    assert "TOP-SECRET-CONTENT" not in result.error
    assert str(tmp_path) not in result.error  # relative path only
    # whole run failed: no partial diff, no removed events, snapshot intact
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]
    assert sorted(store.get_snapshot("docs")) == ["a.txt", "sub/locked.txt"]


def test_unreadable_file_on_first_run_leaves_no_baseline(
    store: Store, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root, "a.txt", "fine")
    write(root, "b.txt", "x")

    def deny(path: str, max_text_bytes: int) -> dict[str, Any]:
        raise OSError("boom")

    monkeypatch.setattr(dirmod, "_read_fields", deny)

    result = run(store, make_cfg(root))

    assert result.error is not None and 'cannot read "a.txt"' in result.error
    assert kinds(store) == [(KIND_SOURCE_ERROR, None)]


posix_non_root = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions and a non-root user",
)


@posix_non_root
def test_permission_denied_file_fails_the_run(store: Store, root: Path) -> None:
    write(root, "a.txt", "fine")
    locked = write(root, "locked.txt", "TOP-SECRET-CONTENT")
    locked.chmod(0)
    try:
        result = run(store, make_cfg(root))
    finally:
        locked.chmod(0o600)

    assert result.error is not None and 'cannot read "locked.txt"' in result.error
    assert "TOP-SECRET-CONTENT" not in result.error
    assert kinds(store) == [(KIND_SOURCE_ERROR, None)]


@posix_non_root
def test_permission_denied_directory_fails_the_run(store: Store, root: Path) -> None:
    write(root, "a.txt", "fine")
    write(root, "sub/b.txt", "x")
    cfg = make_cfg(root)
    run(store, cfg)
    (root / "sub").chmod(0)
    try:
        result = run(store, cfg, 1)
    finally:
        (root / "sub").chmod(0o700)

    assert result.error is not None and 'directory "sub"' in result.error
    # its files must not look removed
    assert kinds(store) == [(KIND_BASELINE, None), (KIND_SOURCE_ERROR, None)]


@posix_non_root
def test_permission_denied_directory_inside_an_excluded_tree_is_fine(
    store: Store, root: Path
) -> None:
    write(root, "a.txt", "fine")
    write(root, ".git/objects/x", "x")
    (root / ".git" / "objects").chmod(0)
    try:
        result = run(store, make_cfg(root, exclude=[".git/**"]))
    finally:
        (root / ".git" / "objects").chmod(0o700)

    assert result.error is None
    assert sorted(store.get_snapshot("docs")) == ["a.txt"]


# -- symlinks, odd files, odd names ---------------------------------------------------------------


def symlink_or_skip(target: Path, link: Path, *, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")


def test_symlinked_files_are_read_normally(root: Path) -> None:
    write(root, "real.txt", "hello")
    symlink_or_skip(root / "real.txt", root / "link.txt")

    by_key = {r.key: r.fields for r in collect(root)}

    assert by_key == {
        "link.txt": {"size": 5, "text": "hello"},
        "real.txt": {"size": 5, "text": "hello"},
    }


def test_symlinked_directories_are_not_followed(root: Path, tmp_path: Path) -> None:
    write(root, "real/x.txt", "x")
    outside = tmp_path / "outside"
    write(outside, "secret.txt", "y")
    symlink_or_skip(root / "real", root / "alias", is_dir=True)
    symlink_or_skip(outside, root / "escape", is_dir=True)
    symlink_or_skip(root, root / "real" / "loop", is_dir=True)  # would recurse forever

    assert keys(collect(root)) == ["real/x.txt"]


def test_dangling_symlink_fails_the_run_naming_it(store: Store, root: Path) -> None:
    write(root, "a.txt", "fine")
    symlink_or_skip(root / "missing-target", root / "dangling.txt")

    result = run(store, make_cfg(root))

    assert result.error is not None and 'cannot read "dangling.txt"' in result.error
    assert kinds(store) == [(KIND_SOURCE_ERROR, None)]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_non_regular_files_are_ignored_and_never_opened(root: Path) -> None:
    write(root, "a.txt", "fine")
    os.mkfifo(root / "pipe")  # opening it for reading would block forever

    assert keys(collect(root)) == ["a.txt"]


def test_names_that_cannot_be_utf8_encoded_are_skipped(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root, "ok.txt", "fine")
    real_walk = os.walk

    def walk_with_bad_names(top: Any, **kwargs: Any) -> Iterator[Any]:
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            # A lone surrogate is what undecodable file-name bytes turn into. The bad directory
            # does not exist: if it were not pruned, os.walk would report an error for it.
            dirnames.append("bad\udcffdir")
            yield dirpath, dirnames, [*filenames, "bad\udc80.txt"]

    monkeypatch.setattr(os, "walk", walk_with_bad_names)

    assert keys(collect(root)) == ["ok.txt"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names are arbitrary bytes")
def test_real_undecodable_file_name_is_skipped(root: Path) -> None:
    write(root, "ok.txt", "fine")
    try:
        (root / os.fsdecode(b"bad-\xff.txt")).write_bytes(b"x")
    except OSError:
        pytest.skip("this file system rejects undecodable names")

    assert keys(collect(root)) == ["ok.txt"]


def test_collect_error_type_for_root_problems(tmp_path: Path) -> None:
    with pytest.raises(CollectError, match="does not exist or is not a directory"):
        DirCollector().collect(make_cfg(tmp_path / "nope"))
