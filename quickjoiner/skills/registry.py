"""QuickJoiner's own definition of a skill, and whether it may run for a given person.

A skill folder describes itself, but what it *requires here* is QuickJoiner's decision to
hold: an uploaded skill's frontmatter is written by whoever wrote the skill, and an admin
must be able to correct it without editing files on the server. So the folder is the
seed and the `skills` table is authoritative.

On first sight a skill is registered automatically:

  * scripts read no credentials  ->  scope **open**, usable by everyone
  * scripts read `TFS_PAT`, ...  ->  scope **user**, those become its required secrets
                                      and it will not run until a person supplies them

Re-running discovery never overwrites that configuration — an admin who narrowed or
widened a skill keeps their decision.

**A user-scoped skill does not fall back to the server's environment.** Resolution still
layers user over workspace, but the process environment is excluded entirely: a person
without their own credentials must be told so, not silently run as whatever service
account the server happens to hold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from quickjoiner.skills.loader import Skill, detect_env

SCOPE_OPEN = "open"
SCOPE_USER = "user"
SCOPES = (SCOPE_OPEN, SCOPE_USER)


@dataclass(frozen=True)
class SkillConfig:
    """A discovered skill joined to QuickJoiner's stored configuration for it."""
    skill: Skill
    scope: str = SCOPE_OPEN
    required_env: tuple[str, ...] = ()
    enabled: bool = True
    installed_by: str = ""

    @property
    def name(self) -> str:
        return self.skill.name

    @property
    def description(self) -> str:
        return self.skill.description

    @property
    def user_scoped(self) -> bool:
        return self.scope == SCOPE_USER


def _loads(raw) -> tuple[str, ...]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


def sync_registry(catalog, skills: list[Skill], installed_by: str = "") -> list[SkillConfig]:
    """Register any newly-seen skill, then return every discovered skill joined to its
    stored configuration. Best-effort on the catalog: a workspace whose skills table
    cannot be read still gets its skills, treated as open — the alternative is losing
    the feature entirely over a bookkeeping failure."""
    try:
        stored = {row["name"]: row for row in catalog.list_skill_configs()}
    except Exception:
        stored = {}

    out: list[SkillConfig] = []
    for skill in skills:
        row = stored.get(skill.name)
        if row is None:
            # First sight: derive the scope from what the skill actually reads. The
            # declared `requires_env` wins when present — it is an explicit statement —
            # and detection fills in for the (common) skill that declares nothing.
            required = skill.requires_env or detect_env(skill)
            scope = SCOPE_USER if required else SCOPE_OPEN
            try:
                catalog.register_skill(
                    skill.name, str(skill.path), skill.origin, scope,
                    json.dumps(list(required)), installed_by,
                )
            except Exception:
                pass
            out.append(SkillConfig(skill, scope, tuple(required), True, installed_by))
            continue

        try:
            catalog.register_skill(
                skill.name, str(skill.path), skill.origin,
                row.get("scope") or SCOPE_OPEN, row.get("required_env") or "[]",
                row.get("installed_by") or "",
            )
        except Exception:
            pass
        out.append(SkillConfig(
            skill=skill,
            scope=(row.get("scope") or SCOPE_OPEN),
            required_env=_loads(row.get("required_env")),
            enabled=bool(row.get("enabled", 1)),
            installed_by=row.get("installed_by") or "",
        ))
    return out


def missing_secrets(config: SkillConfig, store, user: str | None) -> list[str]:
    """Which of a skill's required values this person still has to supply.

    Empty for an open skill — it needs nothing. For a user-scoped one, only the user and
    workspace layers count: `include_process_env=False` is the whole point, since a
    credential the SERVER holds is not this person's, and running with it would act as
    somebody else.
    """
    if not config.user_scoped or not config.required_env:
        return []
    try:
        _values, missing = store.resolve(
            user, list(config.required_env), include_process_env=False)
    except Exception as exc:  # a broken secret store must not read as "all present"
        return [f"(secrets unavailable: {exc})"]
    return missing


def runnable(config: SkillConfig, store, user: str | None) -> tuple[bool, list[str]]:
    """(may this person run it, what is missing)."""
    if not config.enabled:
        return False, ["disabled"]
    missing = missing_secrets(config, store, user)
    return (not missing), missing


def skill_environment(config: SkillConfig, store, user: str | None) -> dict[str, str]:
    """The values a user-scoped skill's scripts should see. Open skills contribute
    nothing of their own and simply inherit the server's environment."""
    if not config.user_scoped or not config.required_env:
        return {}
    values, _missing = store.resolve(
        user, list(config.required_env), include_process_env=False)
    return values
