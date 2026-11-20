"""A copy of v2/agents as the tests expect it: each agent up to its deployed version (v1 if none).

The real directory grows while the platform is used: a new draft (shop-support v2 on 2026-11-20) is made
there through the studio or the builder, as intended. Tests that count versions or create the "next"
draft would then depend on whoever made one last. They work on this baseline copy instead.
"""
import json
import shutil
from pathlib import Path

REAL_AGENTS = Path(__file__).resolve().parent.parent / "agents"


def baseline_version(agent_dir: Path) -> int:
    path = agent_dir / "deployment.json"
    try:
        stable = json.loads(path.read_text(encoding="utf-8")).get("stable") if path.is_file() else None
    except ValueError:
        stable = None
    return max(int(stable or 1), 1)


def copy_baseline(dest) -> Path:
    dest = Path(dest)
    shutil.copytree(REAL_AGENTS, dest)
    for agent_dir in dest.iterdir():
        versions = agent_dir / "versions"
        if not versions.is_dir():
            continue
        keep = baseline_version(agent_dir)
        for version_dir in versions.iterdir():
            if version_dir.is_dir() and version_dir.name.isdigit() and int(version_dir.name) > keep:
                shutil.rmtree(version_dir)
    return dest


def snapshot(root=REAL_AGENTS) -> dict:
    """Every file under the agents directory with its contents, to show a test wrote nothing there."""
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
