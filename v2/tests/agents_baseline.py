"""A copy of v2/agents as the tests expect it: the seed state, every agent at version 1.

The real directory grows while the platform is used: a new draft (shop-support v2 on 2026-11-20) is made
there through the studio or the builder, as intended. Tests that count versions or create the "next"
draft would then depend on whoever made one last. They work on this baseline copy instead.
"""
import json
import shutil
from pathlib import Path

REAL_AGENTS = Path(__file__).resolve().parent.parent / "agents"
SEED_DEPLOYED = ("tech-trend-anchor", "shop-support")     # deployed from the start ("초기 등록(seed)")


def copy_baseline(dest) -> Path:
    """The seed state: every agent at version 1 only. The two seed agents have it deployed, with no canary
    and no earlier version; every other agent is not deployed (later releases are undone)."""
    dest = Path(dest)
    shutil.copytree(REAL_AGENTS, dest)
    for agent_dir in dest.iterdir():
        versions = agent_dir / "versions"
        if not versions.is_dir():
            continue
        for version_dir in versions.iterdir():
            if version_dir.is_dir() and version_dir.name.isdigit() and int(version_dir.name) > 1:
                shutil.rmtree(version_dir)
        meta_path = versions / "1" / "meta.json"
        deployment_path = agent_dir / "deployment.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if agent_dir.name in SEED_DEPLOYED:
            meta["state"] = "deployed"
            deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
            deployment.update(stable=1, canary=None, canary_percent=0, previous_stable=None)
            deployment_path.write_text(json.dumps(deployment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            # released after the seed (kiosk-order, drive-thru-order, clinic-intake v2 and flight-rebooking v3
            # on 2026-11-27, office-assistant before that): in the seed they were made but not deployed
            if deployment_path.is_file():
                deployment_path.unlink()
            if meta.get("state") in ("pending_approval", "approved", "deployed", "retired"):
                meta.update(state="tested" if meta.get("test_run_id") else "draft", approved_by=None, approved_at=None)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dest


def snapshot(root=REAL_AGENTS) -> dict:
    """Every file under the agents directory with its contents, to show a test wrote nothing there."""
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
