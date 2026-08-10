"""Scenario tests for the backup engine (T1-T5), plus the guarantees around them.

The tree under test comes from scripts/make_test_tree.py and includes a ~50 MB
file, dotfiles, sidecars, spaces in names, an empty file, empty folders, and
symlinks the engine must refuse to follow.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import make_test_tree
from footage_pipeline.backup import manifest as M
from footage_pipeline.backup.core import (
    BackupEngine,
    InsufficientSpaceError,
    InvalidRootsError,
    hash_file,
)

BIG_MB = int(os.environ.get("FP_TEST_BIG_MB", "50"))


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def source_tree(tmp_path_factory) -> Path:
    """Built once per module — it contains a 50 MB file."""
    root = tmp_path_factory.mktemp("source") / "CARD_01"
    make_test_tree.build(root, big_mb=BIG_MB, seed=1337)
    return root


@pytest.fixture
def dest(tmp_path) -> Path:
    d = tmp_path / "backup_root"
    d.mkdir()
    return d


def snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """(size, mtime_ns, xxhash) for every regular file, keyed by relative path.

    The run-manifests folder is excluded so a destination snapshot describes the
    mirror itself, not the records each run adds beside it.
    """
    out: dict[str, tuple[int, int, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if not (Path(dirpath) / d).is_symlink() and d != M.MANIFESTS_DIR_NAME
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            st = path.stat()
            rel = str(path.relative_to(root))
            out[rel] = (st.st_size, st.st_mtime_ns, hash_file(path))
    return out


def run_backup(source: Path, dest: Path, **kwargs) -> "object":
    return BackupEngine(source_root=source, backup_root=dest, **kwargs).run()


def statuses(result) -> dict[str, str]:
    return {f.rel_path: f.status.value for f in result.manifest.files}


def assert_source_unchanged(source: Path, before: dict) -> None:
    after = snapshot(source)
    assert after == before, "the backup engine modified the source tree"


# --------------------------------------------------------------------------
# T1 — fresh backup into an empty destination
# --------------------------------------------------------------------------

def test_t1_fresh_backup_copies_and_verifies_everything(source_tree, dest):
    before = snapshot(source_tree)

    result = run_backup(source_tree, dest)

    assert result.outcome is M.RunOutcome.PASS
    assert result.totals.files > 0
    assert result.totals.copied == result.totals.files
    assert result.totals.skipped == 0
    assert result.totals.conflicts == 0
    assert result.totals.failed == 0

    # Every row verified: a source hash, a destination hash, and they agree.
    for record in result.manifest.files:
        assert record.status is M.FileStatus.COPIED
        assert record.source_hash, f"{record.rel_path} has no source hash"
        assert record.source_hash == record.dest_hash, record.rel_path

    # The mirror is real: same relative paths, same bytes, same mtimes.
    dest_snapshot = snapshot(dest)
    for rel, (size, mtime_ns, digest) in before.items():
        assert rel in dest_snapshot, f"{rel} missing from destination"
        d_size, d_mtime_ns, d_digest = dest_snapshot[rel]
        assert d_size == size
        assert d_digest == digest
        assert d_mtime_ns == mtime_ns, f"mtime not preserved for {rel}"

    # Symlinks recorded, not followed, not copied — both the file and the
    # directory link, so the loop guard on directories is exercised.
    kinds = {link.kind for link in result.manifest.skipped_symlinks}
    assert kinds == {"file", "dir"}, kinds
    for link in result.manifest.skipped_symlinks:
        assert not (dest / link.rel_path).exists()
        assert link.rel_path not in before
    # Following A002/link_to_A001 would have mirrored A001's files a second time.
    assert not (dest / "A002" / "link_to_A001").exists()
    assert "A002/link_to_A001/clip_001.mov" not in {f.rel_path for f in result.manifest.files}

    # The manifest and log are on disk under the backup root.
    assert result.manifest_path.is_file()
    assert result.log_path.is_file()
    assert result.manifest_path.parent.parent.name == M.MANIFESTS_DIR_NAME
    assert result.manifest_path.parent.parent.parent == dest
    reloaded = M.read_manifest(result.manifest_path)
    assert reloaded.outcome is M.RunOutcome.PASS
    assert len(reloaded.files) == result.totals.files
    assert "COPIED" in result.log_path.read_text(encoding="utf-8")

    assert_source_unchanged(source_tree, before)


# --------------------------------------------------------------------------
# T2 — immediate re-run
# --------------------------------------------------------------------------

def test_t2_rerun_skips_everything(source_tree, dest):
    before = snapshot(source_tree)
    first = run_backup(source_tree, dest)
    assert first.outcome is M.RunOutcome.PASS

    dest_after_first = snapshot(dest)
    second = run_backup(source_tree, dest)

    assert second.outcome is M.RunOutcome.PASS
    assert second.totals.copied == 0, "a re-run must copy nothing"
    assert second.totals.skipped == second.totals.files
    assert second.totals.conflicts == 0
    assert second.totals.failed == 0
    assert second.totals.bytes_copied == 0
    assert all(s == "SKIPPED" for s in statuses(second).values())

    # Skips are decided by hashing, so both hashes are present and equal.
    for record in second.manifest.files:
        assert record.source_hash == record.dest_hash
        assert record.source_hash

    # The destination files themselves are untouched by the second run.
    assert snapshot(dest) == dest_after_first
    assert_source_unchanged(source_tree, before)


# --------------------------------------------------------------------------
# T3 — a modified destination file is a conflict, never an overwrite
# --------------------------------------------------------------------------

def test_t3_modified_destination_is_a_conflict_and_is_left_alone(source_tree, dest):
    before = snapshot(source_tree)
    run_backup(source_tree, dest)

    victim_rel = "A002/clip_010.mov"
    victim = dest / victim_rel
    tampered = b"this destination file was changed behind the app's back\n" * 100
    victim.write_bytes(tampered)
    tampered_stat = victim.stat()

    result = run_backup(source_tree, dest)

    assert result.outcome is M.RunOutcome.FAIL, "a conflict must fail the run"
    assert result.totals.conflicts == 1
    assert result.totals.copied == 0
    assert result.totals.failed == 0
    assert result.totals.skipped == result.totals.files - 1

    by_path = statuses(result)
    assert by_path[victim_rel] == "CONFLICT"
    assert all(v == "SKIPPED" for k, v in by_path.items() if k != victim_rel)

    # The conflicting record carries both hashes and they differ.
    record = next(f for f in result.manifest.files if f.rel_path == victim_rel)
    assert record.source_hash and record.dest_hash
    assert record.source_hash != record.dest_hash

    # The destination file is byte-for-byte what we tampered it into.
    assert victim.read_bytes() == tampered
    assert victim.stat().st_mtime_ns == tampered_stat.st_mtime_ns
    assert victim.stat().st_size == len(tampered)

    assert_source_unchanged(source_tree, before)


# --------------------------------------------------------------------------
# T4 — write failures are recorded, the rest of the run continues
# --------------------------------------------------------------------------

def test_t4_write_failure_is_recorded_and_run_continues(source_tree, dest):
    """A plain file sitting where a destination folder needs to be.

    Every file under that folder fails with a real OSError, and the files
    elsewhere in the tree still get copied. Works regardless of uid, unlike a
    chmod-based block.
    """
    before = snapshot(source_tree)

    blocker = dest / "A001"
    blocker.write_bytes(b"not a directory\n")

    result = run_backup(source_tree, dest)

    assert result.outcome is M.RunOutcome.FAIL
    assert result.totals.failed > 0

    by_path = statuses(result)
    failed = {k for k, v in by_path.items() if v == "FAILED"}
    copied = {k for k, v in by_path.items() if v == "COPIED"}

    assert failed, "expected failures under the blocked folder"
    assert all(p.startswith("A001/") for p in failed), failed
    assert copied, "files outside the blocked folder must still be processed"
    assert "README.txt" in copied
    assert any(p.startswith("A002/") for p in copied)

    # Failures carry an explanation, and nothing was written under the blocker.
    for record in result.manifest.files:
        if record.status is M.FileStatus.FAILED:
            assert record.error
    assert blocker.read_bytes() == b"not a directory\n"

    # The failure is in the manifest on disk too, not just in memory.
    reloaded = M.read_manifest(result.manifest_path)
    assert reloaded.outcome is M.RunOutcome.FAIL
    assert reloaded.totals.failed == result.totals.failed

    assert_source_unchanged(source_tree, before)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permissions, so a read-only folder cannot block a write",
)
def test_t4b_read_only_destination_folder_is_recorded(source_tree, dest):
    """The permission-denied flavour of the same scenario."""
    before = snapshot(source_tree)

    locked = dest / "A002"
    locked.mkdir()
    os.chmod(locked, 0o555)
    try:
        result = run_backup(source_tree, dest)
    finally:
        os.chmod(locked, 0o755)  # so pytest can clean the tmp dir up

    assert result.outcome is M.RunOutcome.FAIL
    by_path = statuses(result)
    failed = {k for k, v in by_path.items() if v == "FAILED"}
    assert failed and all(p.startswith("A002/") for p in failed), failed
    assert "README.txt" in {k for k, v in by_path.items() if v == "COPIED"}

    assert_source_unchanged(source_tree, before)


# --------------------------------------------------------------------------
# T5 — not enough room at the destination
# --------------------------------------------------------------------------

def test_t5_insufficient_free_space_refuses_to_start(source_tree, dest):
    before = snapshot(source_tree)

    with pytest.raises(InsufficientSpaceError) as excinfo:
        run_backup(source_tree, dest, free_space_fn=lambda _p: 1024)

    err = excinfo.value
    assert err.bytes_free == 1024
    assert err.bytes_needed > err.bytes_free
    assert "Not enough free space" in str(err)
    assert "Nothing was copied" in str(err)

    # Refused means refused: no mirrored files at the destination.
    copied = [p for p in dest.rglob("*") if p.is_file()
              and M.MANIFESTS_DIR_NAME not in p.parts]
    assert copied == []
    assert_source_unchanged(source_tree, before)


def test_t5b_enough_space_proceeds(source_tree, dest):
    """The same check passing, so the refusal above isn't vacuous."""
    result = run_backup(source_tree, dest, free_space_fn=lambda _p: 1 << 40)
    assert result.outcome is M.RunOutcome.PASS
    assert result.totals.copied == result.totals.files


# --------------------------------------------------------------------------
# Guarantees the scenarios lean on
# --------------------------------------------------------------------------

def test_straight_mirror_path_rule(source_tree, dest):
    """dest path == backup_root + the file's path relative to the source root."""
    result = run_backup(source_tree, dest)
    for record in result.manifest.files:
        expected = dest / record.rel_path
        assert expected.is_file(), f"{record.rel_path} not mirrored at {expected}"
        assert expected.relative_to(dest) == Path(record.rel_path)
        assert (source_tree / record.rel_path).is_file()
    # Nothing outside the mirror + the manifests folder.
    stray = [
        p.relative_to(dest) for p in dest.rglob("*")
        if p.is_file() and M.MANIFESTS_DIR_NAME not in p.parts
        and not (source_tree / p.relative_to(dest)).exists()
    ]
    assert stray == []


def test_skip_decision_ignores_prior_manifests(source_tree, dest):
    """Deleting a destination file makes it copy again, even though the previous
    manifest says it was already backed up."""
    first = run_backup(source_tree, dest)
    assert first.totals.copied == first.totals.files

    victim_rel = "A001/proxies/clip_001_proxy.mp4"
    (dest / victim_rel).unlink()

    second = run_backup(source_tree, dest)
    assert statuses(second)[victim_rel] == "COPIED"
    assert second.totals.copied == 1
    assert second.outcome is M.RunOutcome.PASS
    # And the prior manifests are all still sitting there, unread.
    assert len(list((dest / M.MANIFESTS_DIR_NAME).iterdir())) == 2


def test_hashing_is_streamed_and_matches_whole_file(source_tree):
    """A tiny chunk size must produce the same digest as a large one."""
    target = source_tree / "A001" / "clip_002.mov"
    assert hash_file(target, chunk_size=4096) == hash_file(target, chunk_size=8 << 20)


def test_verification_catches_a_corrupted_destination(source_tree, dest, monkeypatch):
    """If a copy lands wrong, the run fails it rather than reporting success."""
    from footage_pipeline.backup import core

    real_hash = core.hash_file
    calls = {"n": 0}

    def flaky_hash(path, chunk_size=core.DEFAULT_CHUNK_SIZE):
        # Corrupt the verify-side read for exactly one file.
        if str(path).endswith("README.txt") and str(path).startswith(str(dest)):
            calls["n"] += 1
            return "deadbeefdeadbeef"
        return real_hash(path, chunk_size)

    monkeypatch.setattr(core, "hash_file", flaky_hash)
    result = core.BackupEngine(source_root=source_tree, backup_root=dest).run()

    assert calls["n"] == 1
    assert result.outcome is M.RunOutcome.FAIL
    assert statuses(result)["README.txt"] == "FAILED"
    record = next(f for f in result.manifest.files if f.rel_path == "README.txt")
    assert "verification failed" in (record.error or "")
    # The unverified copy was removed, so a re-run retries it cleanly.
    assert not (dest / "README.txt").exists()


def test_preflight_reports_sizes_without_writing(source_tree, dest):
    engine = BackupEngine(source_root=source_tree, backup_root=dest)
    pre = engine.preflight()
    assert pre.files_total > 0
    assert pre.bytes_to_copy == pre.bytes_total  # nothing at the destination yet
    assert pre.ok is True
    assert list(dest.iterdir()) == [], "preflight must not write anything"


def test_rejects_overlapping_or_identical_roots(source_tree, dest):
    with pytest.raises(InvalidRootsError):
        BackupEngine(source_root=source_tree, backup_root=source_tree)
    with pytest.raises(InvalidRootsError):
        BackupEngine(source_root=dest / "nested", backup_root=dest)
    with pytest.raises(InvalidRootsError):
        BackupEngine(source_root=source_tree, backup_root=dest / "does_not_exist")


def test_backup_root_inside_source_is_not_mirrored_into_itself(tmp_path):
    """Guards the recursion trap when someone points the backup at a subfolder."""
    source = tmp_path / "src"
    (source / "clips").mkdir(parents=True)
    (source / "clips" / "a.mov").write_bytes(b"a" * 1024)
    inner_backup = source / "backup"
    inner_backup.mkdir()

    result = BackupEngine(source_root=source, backup_root=inner_backup).run()

    assert result.outcome is M.RunOutcome.PASS
    assert statuses(result) == {"clips/a.mov": "COPIED"}
    assert result.manifest.notes, "skipping the nested backup root should be noted"
    assert (inner_backup / "clips" / "a.mov").is_file()
    assert not (inner_backup / "backup").exists()


def test_empty_file_and_dotfiles_are_mirrored(source_tree, dest):
    result = run_backup(source_tree, dest)
    paths = statuses(result)
    assert paths["empty_file.bin"] == "COPIED"
    assert (dest / "empty_file.bin").stat().st_size == 0
    assert paths[".DS_Store"] == "COPIED"
    assert paths["A001/.hidden_sidecar"] == "COPIED"
    assert paths["A002/notes without extension"] == "COPIED"
    assert paths["A001/clip_001.mov.xmp"] == "COPIED"


def test_big_file_round_trips(source_tree, dest):
    """The ~50 MB file is the reason hashing and copying are chunked."""
    big_rel = "A001/clip_bigfile.mov"
    assert (source_tree / big_rel).stat().st_size == BIG_MB * 1024 * 1024

    result = run_backup(source_tree, dest)
    record = next(f for f in result.manifest.files if f.rel_path == big_rel)
    assert record.status is M.FileStatus.COPIED
    assert record.source_hash == record.dest_hash
    assert hash_file(source_tree / big_rel) == hash_file(dest / big_rel)


def test_progress_callback_reports_monotonic_totals(source_tree, dest):
    seen = []
    BackupEngine(
        source_root=source_tree, backup_root=dest,
        progress_cb=lambda p: seen.append(p.to_dict()),
    ).run()

    assert seen, "expected progress callbacks"
    assert seen[-1]["phase"] == "finished"
    assert seen[-1]["files_done"] == seen[-1]["files_total"]
    assert seen[-1]["bytes_done"] == seen[-1]["bytes_total"]
    assert {s["phase"] for s in seen} >= {"scanning", "comparing", "copying"}
    # files_done never goes backwards
    done = [s["files_done"] for s in seen]
    assert done == sorted(done)
