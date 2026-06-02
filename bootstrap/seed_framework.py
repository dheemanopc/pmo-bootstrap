"""Seed the PMO framework into a memsys instance.

Reads `_source/memories.json` (the verbatim export from the source memsys),
maps each memory's source-id to a canonical slug, and writes it into FW_TEAM.
Idempotent — skips memories whose slug already exists with identical content.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from .memsys_client import MemsysClient, MemsysError


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "_source" / "memories.json"

# Canonical slug map: source-memsys-id -> slug to use on the target memsys.
# Anything NOT in this map is skipped (historical / meta / out-of-framework).
SLUG_BY_ID: dict[str, str] = {
    # Roles
    "127793ef-09a9-45d7-93da-a149165292ab": "pmo-role-pm-v1",
    "897c696d-2d62-4c6a-8064-8327cf07cfbc": "pmo-role-pa-v1",
    "99c6c6cd-449e-44ed-8981-51b489c43ade": "pmo-role-dm-v1",
    "4e29970b-1ec9-4304-b648-c71057fac6d3": "pmo-role-da-v1",
    "da1c4c82-6c93-4eb0-bd48-b1bb482fedf6": "pmo-role-developer-v1",
    "6fff0238-852c-4206-b41b-87a37206ac96": "pmo-role-reviewer-v1",
    # Engine config
    "238b450b-a7da-40ed-8cbc-d6e3b0000224": "pmo-permission-matrix-v1",
    "42022af0-ac96-44bf-ad42-33dfa6b33a9e": "pmo-named-query-resolvers-v1",
    "ab0e7126-d0c6-481f-bfda-6f6b85fb0bd8": "pmo-directive-grammar-v2-atpmo-marker",
    "c4579996-4681-4143-9fdd-0e277fffe08c": "pmo-directive-grammar-shortform-v1",
    "03e83396-500e-4131-81a2-cfbb80a2ba4c": "pmo-directive-emission-patch-v1",
    "5ae2759b-795d-41b9-b730-6b1eeeabb83f": "pmo-session-registry-v1",
    "940cfbae-e4bf-482f-8380-f39bb859d74c": "pmo-area-a-vocabulary-convention-v1",
    # Architecture
    "3c5900e5-b7c3-4587-a0d0-cfe6fc03afc3": "pmo-area-e-substrate-plan-v3",
    "714c71a6-0e0b-4658-a1c0-4dfeda6d1ffa": "pmo-area-e-routing-config-v2",
    "b194ba79-1602-449f-b06a-398220d31ca1": "pmo-substrate-stop-hook-v1-1",
    # Backlog
    "ea4aff7e-4dcb-4de4-9947-cadf77cede0c": "pmo-v2-project-setup-generator",
    "9d1a6a18-9fd1-4bd6-8af5-4980c153dcdf": "pmo-v2-token-optimization",
}

# Load order: vocab → matrix → resolvers → roles → architecture → backlog.
# Roles depend on the matrix to resolve their config slice; the matrix references
# resolver names; resolvers reference vocab.
LOAD_ORDER = [
    "pmo-area-a-vocabulary-convention-v1",
    "pmo-permission-matrix-v1",
    "pmo-named-query-resolvers-v1",
    "pmo-directive-grammar-v2-atpmo-marker",
    "pmo-directive-grammar-shortform-v1",
    "pmo-directive-emission-patch-v1",
    "pmo-session-registry-v1",
    "pmo-role-pm-v1",
    "pmo-role-pa-v1",
    "pmo-role-dm-v1",
    "pmo-role-da-v1",
    "pmo-role-developer-v1",
    "pmo-role-reviewer-v1",
    "pmo-area-e-substrate-plan-v3",
    "pmo-area-e-routing-config-v2",
    "pmo-substrate-stop-hook-v1-1",
    "pmo-v2-project-setup-generator",
    "pmo-v2-token-optimization",
]


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _load_source() -> list[dict]:
    if not SOURCE.exists():
        raise FileNotFoundError(f"missing source export: {SOURCE}")
    return json.loads(SOURCE.read_text())


def seed(
    client: MemsysClient,
    fw_team: str,
    *,
    dry_run: bool = False,
    on_progress=None,
) -> dict:
    """Seed framework into fw_team. Returns summary dict.

    Idempotency: for each memory we slug_lookup; if it exists with same content
    hash (via memory_get), skip. Otherwise supersede or write fresh.
    """
    memories = _load_source()
    by_id = {m["id"]: m for m in memories}
    summary = {"written": 0, "skipped": 0, "superseded": 0, "missing_in_source": 0, "errors": 0}
    ordered_slugs = LOAD_ORDER

    for slug in ordered_slugs:
        # find the source memory by reverse lookup
        src = next((by_id[mid] for mid, sl in SLUG_BY_ID.items() if sl == slug and mid in by_id), None)
        if not src:
            summary["missing_in_source"] += 1
            if on_progress:
                on_progress(slug, "missing_in_source", None)
            continue

        mem_type = src.get("type", "decision")
        content = (src.get("content") or "").rstrip() + "\n"
        tags = src.get("tags") or []
        # Drop noisy build-specific tags; keep framework-meaningful ones.
        tags = [t for t in tags if not t.startswith("pmo-project-pmo-v1-build")]
        if "current" not in tags:
            tags = list(tags) + ["current"]
        indexable = bool(src.get("indexable", True))

        # Check existing.
        existing = None
        try:
            existing = client.slug_lookup(team_id=fw_team, resource_type=mem_type, slug=slug)
        except MemsysError as e:
            summary["errors"] += 1
            if on_progress:
                on_progress(slug, "error", str(e))
            continue

        if existing and existing.get("memory_id"):
            # Compare content via memory_get
            try:
                full = client.memory_get(id=existing["memory_id"])
            except MemsysError:
                full = None
            existing_content = ((full or {}).get("content") or "").rstrip() + "\n"
            if _hash(existing_content) == _hash(content):
                summary["skipped"] += 1
                if on_progress:
                    on_progress(slug, "skipped", existing["memory_id"])
                continue
            # Content drift — supersede.
            if dry_run:
                summary["superseded"] += 1
                if on_progress:
                    on_progress(slug, "supersede (dry-run)", existing["memory_id"])
                continue
            try:
                client.memory_supersede(
                    id=existing["memory_id"], content=content, team_id=fw_team, tags=tags
                )
                summary["superseded"] += 1
                if on_progress:
                    on_progress(slug, "superseded", existing["memory_id"])
            except MemsysError as e:
                summary["errors"] += 1
                if on_progress:
                    on_progress(slug, "error", str(e))
            continue

        # Fresh write.
        if dry_run:
            summary["written"] += 1
            if on_progress:
                on_progress(slug, "write (dry-run)", None)
            continue
        try:
            result = client.memory_write(
                team_id=fw_team,
                type=mem_type,
                content=content,
                tags=tags,
                slug_clue=slug,
                indexable=indexable,
            )
            new_id = result.get("id") if isinstance(result, dict) else None
            summary["written"] += 1
            if on_progress:
                on_progress(slug, "written", new_id)
        except MemsysError as e:
            summary["errors"] += 1
            if on_progress:
                on_progress(slug, "error", str(e))

    return summary
