"""Scaffold a new project in memsys.

Writes three memories into the project team:
  1. Manifest (decision) — project intent + POINTERS to framework slugs.
  2. Session-registry (decision) — empty {"rows": {}}.
  3. User-intent (note, indexable=false) — verbatim user description, threaded under manifest.

Refuses to overwrite an existing manifest (fails loudly).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .memsys_client import MemsysClient, MemsysError


# These are the slugs the manifest's POINTERS block references on FW_TEAM.
FRAMEWORK_POINTERS = [
    ("pmo-permission-matrix-v1", "decision"),
    ("pmo-area-a-vocabulary-convention-v1", "decision"),
    ("pmo-directive-grammar-v2-atpmo-marker", "decision"),
    ("pmo-named-query-resolvers-v1", "decision"),
    ("pmo-role-pm-v1", "decision"),
    ("pmo-role-pa-v1", "decision"),
    ("pmo-role-dm-v1", "decision"),
    ("pmo-role-da-v1", "decision"),
    ("pmo-role-developer-v1", "decision"),
    ("pmo-role-reviewer-v1", "decision"),
    ("pmo-area-e-substrate-plan-v3", "decision"),
    ("pmo-area-e-routing-config-v2", "decision"),
    ("pmo-substrate-stop-hook-v1-1", "decision"),
]


@dataclass
class ProjectResult:
    slug: str
    manifest_id: str | None
    registry_id: str | None
    intent_id: str | None


def kebab(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].strip("-") or "untitled"


def _render_manifest(project_name: str, slug: str, intent: str, fw_team: str) -> str:
    pointers = "\n".join(
        f"- (team={fw_team}, type={t}, slug={s})" for s, t in FRAMEWORK_POINTERS
    )
    return f"""# Project Manifest — {project_name}

**Project slug:** `{slug}`
**Framework team:** `{fw_team}`

All work for this project threads under this manifest via `parent_id`.

## INTENT (verbatim)

{intent}

## POINTERS — Framework artifacts (in FW_TEAM, not this team)

Roles spawned for this project resolve their definitions and shared config by
looking up these slugs in the framework team. This manifest does not copy them
— it points at them. If FW_TEAM changes, update only this block.

{pointers}

## CONVENTIONS

- Working memories: `indexable=false`, threaded under this manifest via `parent_id`.
- Formal artifacts (project_brief, milestones, etc.): created by PM/PA per the matrix;
  no copying of framework artifacts into this team.
- Session registry: separate decision memory at slug `pmo-project-{slug}-session-registry`.
"""


def init_project(
    client: MemsysClient,
    project_name: str,
    intent: str,
    proj_team: str,
    fw_team: str,
    *,
    dry_run: bool = False,
) -> ProjectResult:
    slug = kebab(project_name)
    manifest_slug = f"pmo-project-{slug}-manifest"
    registry_slug = f"pmo-project-{slug}-session-registry"
    common_tags = ["pmo", f"pmo-project-{slug}", "current"]

    # Refuse to overwrite an existing manifest.
    try:
        existing = client.slug_lookup(team_id=proj_team, resource_type="decision", slug=manifest_slug)
    except MemsysError as e:
        raise MemsysError(f"could not check for existing manifest: {e}") from None
    if existing and existing.get("memory_id"):
        raise MemsysError(
            f"manifest '{manifest_slug}' already exists (id={existing['memory_id']}). "
            f"Refusing to overwrite — pick a different project name or remove the existing manifest first."
        )

    manifest_body = _render_manifest(project_name, slug, intent, fw_team)

    if dry_run:
        return ProjectResult(slug=slug, manifest_id=None, registry_id=None, intent_id=None)

    # 1. Manifest
    manifest_result = client.memory_write(
        team_id=proj_team,
        type="decision",
        slug_clue=manifest_slug,
        content=manifest_body,
        tags=["pmo", "pmo-manifest", f"pmo-project-{slug}", "current"],
        indexable=True,
    )
    manifest_id = manifest_result.get("id") if isinstance(manifest_result, dict) else None

    # 2. Session-registry
    registry_result = client.memory_write(
        team_id=proj_team,
        type="decision",
        slug_clue=registry_slug,
        content='{"rows": {}}',
        tags=["pmo", "pmo-session-registry", f"pmo-project-{slug}", "current"],
        indexable=False,
    )
    registry_id = registry_result.get("id") if isinstance(registry_result, dict) else None

    # 3. User-intent (note, threaded under manifest)
    intent_result = client.memory_write(
        team_id=proj_team,
        type="note",
        content=intent,
        tags=common_tags + ["pmo-user-response"],
        indexable=False,
        parent_id=manifest_id,
    )
    intent_id = intent_result.get("id") if isinstance(intent_result, dict) else None

    return ProjectResult(
        slug=slug, manifest_id=manifest_id, registry_id=registry_id, intent_id=intent_id
    )
