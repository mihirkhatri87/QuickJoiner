"""Agent Skills: discovery, QuickJoiner-side configuration, per-user secrets, execution.

The format is the open one Claude Code and GitHub Copilot both read, so a skill written
for either works here unmodified — and stays working there.
"""

from quickjoiner.skills.install import InstallError, Installed, install_zip, uninstall
from quickjoiner.skills.loader import (
    Skill, detect_env, discover, load_skill, parse_frontmatter, read_reference, skill_body,
)
from quickjoiner.skills.registry import (
    SCOPE_OPEN, SCOPE_USER, SkillConfig, missing_secrets, runnable, skill_environment,
    sync_registry,
)
from quickjoiner.skills.runner import RunResult, run_script
from quickjoiner.skills.secrets import WORKSPACE_OWNER, SecretsUnavailable, SecretStore
from quickjoiner.skills.tools import build_skill_tools, catalogue_prompt

__all__ = [
    "Skill", "SkillConfig", "SecretStore", "SecretsUnavailable", "RunResult",
    "Installed", "InstallError",
    "SCOPE_OPEN", "SCOPE_USER", "WORKSPACE_OWNER",
    "discover", "load_skill", "parse_frontmatter", "read_reference", "skill_body",
    "detect_env", "sync_registry", "runnable", "missing_secrets", "skill_environment",
    "run_script", "build_skill_tools", "catalogue_prompt", "install_zip", "uninstall",
]
