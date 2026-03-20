"""Verify Alembic migration chain integrity.

Checks that all migration files form a valid linear chain with no gaps,
duplicates, or orphaned revisions.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "api" / "alembic" / "versions"


def _load_revision(path: Path) -> dict:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        "file": path.name,
        "revision": getattr(module, "revision"),
        "down_revision": getattr(module, "down_revision"),
    }


def test_migration_chain_is_linear_and_complete() -> None:
    migration_files = sorted(MIGRATIONS_DIR.glob("*.py"))
    migration_files = [f for f in migration_files if not f.name.startswith("__")]
    assert len(migration_files) > 0, "No migration files found"

    revisions = [_load_revision(f) for f in migration_files]

    # No duplicate revision IDs
    ids = [r["revision"] for r in revisions]
    assert len(ids) == len(set(ids)), f"Duplicate revision IDs: {ids}"

    # Build chain from head to base
    by_revision = {r["revision"]: r for r in revisions}
    by_down = {r["down_revision"]: r for r in revisions}

    # Find head(s) — revisions not referenced as down_revision by any other
    all_down_revisions = {r["down_revision"] for r in revisions}
    heads = [r for r in revisions if r["revision"] not in all_down_revisions]
    assert len(heads) == 1, f"Expected exactly 1 head, found {len(heads)}: {heads}"

    # Find base(s) — revisions with down_revision=None
    bases = [r for r in revisions if r["down_revision"] is None]
    assert len(bases) == 1, f"Expected exactly 1 base, found {len(bases)}: {bases}"

    # Walk the chain from head to base
    current = heads[0]
    visited = set()
    while current is not None:
        assert current["revision"] not in visited, f"Cycle at {current['revision']}"
        visited.add(current["revision"])
        down = current["down_revision"]
        if down is None:
            break
        assert down in by_revision, (
            f"Migration {current['file']} references missing down_revision={down}"
        )
        current = by_revision[down]

    assert len(visited) == len(revisions), (
        f"Chain covers {len(visited)} of {len(revisions)} migrations — "
        f"orphaned: {set(ids) - visited}"
    )
