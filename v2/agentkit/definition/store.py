"""Versions on disk and the release rules.

    <root>/<agent_id>/deployment.json                 contracts.Deployment
    <root>/<agent_id>/versions/<n>/*.yaml, meta.json  definition files, contracts.VersionInfo

Nothing is cached: every call reads the files, so several stores in one process (server, tests,
evaluation) always agree. Writes are serialised per agent and replace files atomically.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from agentkit.contracts import AgentDefinition, Deployment, ReleaseError, VersionInfo

from .diffing import diff_definitions, is_safety_change
from .errors import DefinitionError, DefinitionNotFound
from .loader import ID_PATTERN, dump_version_files, load_version_dir, validate_definition

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
NON_HUMAN_APPROVERS = ("builder",)   # the builder agent may never approve (D3, R-REL-3)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class DefinitionStore:
    def __init__(self, root: str | os.PathLike | None = None):
        if root is None:
            # the environment is read here and not only at import, so a test can point a new store
            # at a temporary copy after agentkit.config was imported
            root = os.environ.get("AGENTKIT_AGENTS_DIR")
        if root is None:
            from agentkit import config
            root = config.AGENTS_DIR
        self.root = Path(root)

    # ---------------------------------------------------------------- paths and small readers

    def _agent_dir(self, agent_id: str) -> Path:
        if not isinstance(agent_id, str) or not ID_PATTERN.match(agent_id):
            raise DefinitionNotFound(f"에이전트 '{agent_id}'가 없습니다")
        return self.root / agent_id

    def _version_dir(self, agent_id: str, version: int) -> Path:
        return self._agent_dir(agent_id) / "versions" / str(int(version))

    def _lock(self, agent_id: str) -> threading.RLock:
        key = str((self.root / agent_id).resolve())
        with _LOCKS_GUARD:
            return _LOCKS.setdefault(key, threading.RLock())

    def _require_agent(self, agent_id: str) -> Path:
        path = self._agent_dir(agent_id)
        if not (path / "versions").is_dir():
            raise DefinitionNotFound(f"에이전트 '{agent_id}'가 없습니다")
        return path

    def _version_numbers(self, agent_id: str) -> list[int]:
        versions = self._require_agent(agent_id) / "versions"
        return sorted(int(p.name) for p in versions.iterdir() if p.is_dir() and p.name.isdigit())

    def _require_version(self, agent_id: str, version: int) -> Path:
        self._require_agent(agent_id)
        try:
            path = self._version_dir(agent_id, version)
        except (TypeError, ValueError):
            raise DefinitionNotFound(f"버전 '{version}'이 없습니다", agent_id) from None
        if not path.is_dir():
            raise DefinitionNotFound(f"버전 {version}이 없습니다", agent_id)
        return path

    def _read_json(self, path: Path, model, agent_id: str, version: int | None = None):
        try:
            return model.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as error:  # broken JSON or wrong fields
            raise DefinitionError([(path.name, "", f"읽을 수 없습니다: {error}")], agent_id, version) from None

    def _info(self, agent_id: str, version: int) -> VersionInfo:
        path = self._require_version(agent_id, version) / "meta.json"
        if not path.is_file():
            return VersionInfo(agent_id=agent_id, version=int(version))   # files dropped in by hand: a draft
        info = self._read_json(path, VersionInfo, agent_id, int(version))
        info.agent_id, info.version = agent_id, int(version)
        return info

    def _write_info(self, info: VersionInfo) -> VersionInfo:
        path = self._version_dir(info.agent_id, info.version) / "meta.json"
        _write_atomic(path, json.dumps(info.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
        return info

    def _write_deployment(self, deployment: Deployment) -> Deployment:
        deployment.updated_at = time.time()
        path = self._agent_dir(deployment.agent_id) / "deployment.json"
        _write_atomic(path, json.dumps(deployment.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")
        return deployment

    def _set_state(self, agent_id: str, version: Optional[int], state: str) -> None:
        if version is None:
            return
        info = self._info(agent_id, version)
        if info.state != state:
            info.state = state
            self._write_info(info)

    def _write_definition(self, definition: AgentDefinition) -> None:
        folder = self._version_dir(definition.id, definition.version)
        folder.mkdir(parents=True, exist_ok=True)
        for file, text in dump_version_files(definition).items():
            _write_atomic(folder / file, text)

    # ---------------------------------------------------------------- reading

    def list_agents(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if (p / "versions").is_dir() and ID_PATTERN.match(p.name))

    def list_versions(self, agent_id: str) -> list[VersionInfo]:
        return [self._info(agent_id, n) for n in self._version_numbers(agent_id)]

    def version_info(self, agent_id: str, version: int) -> VersionInfo:
        """(extra to the contract) one version's VersionInfo."""
        return self._info(agent_id, version)

    def deployment(self, agent_id: str) -> Deployment:
        path = self._require_agent(agent_id) / "deployment.json"
        if not path.is_file():
            return Deployment(agent_id=agent_id)
        deployment = self._read_json(path, Deployment, agent_id)
        deployment.agent_id = agent_id
        return deployment

    def load(self, agent_id: str, version: Optional[int] = None) -> AgentDefinition:
        """version None: the deployed stable version. An agent with nothing deployed has no default."""
        if version is None:
            version = self.deployment(agent_id).stable
            if version is None:
                raise DefinitionNotFound(
                    "배포된 버전이 없습니다. 버전을 지정하세요 (no deployed version)", agent_id)
        path = self._require_version(agent_id, version)
        return load_version_dir(path, agent_id, int(version))

    def resolve(self, agent_id: str, session_key: str) -> int:
        """The version this conversation uses. The same session_key always gets the same answer
        while the deployment stays the same; `canary_percent` of the keys get the canary."""
        deployment = self.deployment(agent_id)
        if deployment.canary is not None and deployment.canary_percent > 0:
            if deployment.stable is None or self.bucket(agent_id, session_key) < deployment.canary_percent:
                return deployment.canary
        if deployment.stable is None:
            raise DefinitionNotFound("배포된 버전이 없습니다 (no deployed version)", agent_id)
        return deployment.stable

    @staticmethod
    def bucket(agent_id: str, session_key: str) -> int:
        """0..99, fixed for a session key."""
        digest = hashlib.sha256(f"{agent_id}\x00{session_key}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % 100

    def diff(self, agent_id: str, version_a: int, version_b: int) -> dict[str, dict]:
        return diff_definitions(self.load(agent_id, version_a), self.load(agent_id, version_b))

    # ---------------------------------------------------------------- drafts

    def create_agent(self, definition: AgentDefinition, created_by: str) -> VersionInfo:
        """A new agent with version 1 as a draft. Everything in it counts as changed."""
        definition = validate_definition(definition.model_copy(update={"version": 1}))
        with self._lock(definition.id):
            folder = self._agent_dir(definition.id)
            if folder.exists():
                raise DefinitionError([("agent.yaml", "id", "이미 있는 에이전트입니다 (already exists)")],
                                      definition.id)
            self._write_definition(definition)
            blank = AgentDefinition(id=definition.id, name="", persona="")
            return self._write_info(VersionInfo(
                agent_id=definition.id, version=1, state="draft", created_at=time.time(),
                created_by=created_by, based_on=None, note="새 에이전트",
                changed_fields=list(diff_definitions(blank, definition)),
                safety_change=is_safety_change(blank, definition)))

    def create_draft(self, agent_id: str, based_on: Optional[int], created_by: str, note: str = "") -> int:
        """Copies `based_on` into a new draft version and returns its number.
        based_on None: the stable version, or the newest one when nothing is deployed."""
        with self._lock(agent_id):
            numbers = self._version_numbers(agent_id)
            if based_on is None:
                based_on = self.deployment(agent_id).stable or (numbers[-1] if numbers else None)
            if based_on is None:
                raise DefinitionNotFound("바탕으로 삼을 버전이 없습니다", agent_id)
            source = self._require_version(agent_id, based_on)
            load_version_dir(source, agent_id, int(based_on))        # never copy a broken version
            new = (numbers[-1] if numbers else 0) + 1
            target = self._version_dir(agent_id, new)
            target.mkdir(parents=True)
            for item in source.iterdir():
                if item.is_file() and item.suffix == ".yaml":
                    shutil.copyfile(item, target / item.name)
            self._write_info(VersionInfo(
                agent_id=agent_id, version=new, state="draft", created_at=time.time(),
                created_by=created_by, based_on=int(based_on), note=note))
            return new

    def save_draft(self, definition: AgentDefinition, note: str = "") -> VersionInfo:
        """Writes the files of a draft version (definition.id, definition.version) and records
        what differs from the version it is based on. Any other state is refused."""
        definition = validate_definition(definition)
        agent_id, version = definition.id, definition.version
        with self._lock(agent_id):
            info = self._info(agent_id, version)
            if info.state != "draft":
                raise ReleaseError(
                    f"{agent_id} v{version}은 '{info.state}' 상태라 고칠 수 없습니다. "
                    "이 버전을 바탕으로 새 초안을 만드세요 (only a draft can be edited)")
            self._write_definition(definition)
            if info.based_on is not None:
                base = self.load(agent_id, info.based_on)
            else:
                base = AgentDefinition(id=agent_id, name="", persona="")
            info.changed_fields = list(diff_definitions(base, definition))
            info.safety_change = is_safety_change(base, definition)
            if note:
                info.note = note
            return self._write_info(info)

    # ---------------------------------------------------------------- release steps

    def attach_test_run(self, agent_id: str, version: int, run_id: str) -> VersionInfo:
        """draft -> tested. A tested version may get a newer run; later states are refused."""
        if not run_id:
            raise ReleaseError("시험 실행 번호가 없습니다 (run_id required)")
        with self._lock(agent_id):
            info = self._info(agent_id, version)
            if info.state not in ("draft", "tested"):
                raise ReleaseError(
                    f"{agent_id} v{version}은 '{info.state}' 상태라 시험 결과를 붙일 수 없습니다")
            load_version_dir(self._version_dir(agent_id, version), agent_id, int(version))
            info.state, info.test_run_id = "tested", run_id
            return self._write_info(info)

    def submit(self, agent_id: str, version: int) -> VersionInfo:
        with self._lock(agent_id):
            info = self._info(agent_id, version)
            if not info.test_run_id or info.state == "draft":
                raise ReleaseError(
                    f"{agent_id} v{version}에 시험 결과가 없어 승인을 요청할 수 없습니다 (no test run attached)")
            if info.state != "tested":
                raise ReleaseError(
                    f"{agent_id} v{version}은 '{info.state}' 상태라 승인을 요청할 수 없습니다")
            info.state = "pending_approval"
            return self._write_info(info)

    def approve(self, agent_id: str, version: int, approver: str) -> VersionInfo:
        approver = (approver or "").strip()
        if not approver:
            raise ReleaseError("승인자 이름이 필요합니다 (approver required)")
        if approver.lower() in NON_HUMAN_APPROVERS:
            raise ReleaseError("제작 에이전트는 승인할 수 없습니다. 승인은 사람만 합니다 (builder cannot approve)")
        with self._lock(agent_id):
            info = self._info(agent_id, version)
            if info.state != "pending_approval":
                raise ReleaseError(
                    f"{agent_id} v{version}은 '{info.state}' 상태입니다. 승인 대기 중인 버전만 승인할 수 있습니다")
            info.state, info.approved_by, info.approved_at = "approved", approver, time.time()
            return self._write_info(info)

    def deploy(self, agent_id: str, version: int, percent: int = 100) -> Deployment:
        """percent 100: the version becomes stable. 1..99: it becomes the canary next to the stable one."""
        if isinstance(percent, bool) or not isinstance(percent, int) or not 1 <= percent <= 100:
            raise ReleaseError("배포 비율은 1에서 100 사이의 정수여야 합니다 (percent 1..100)")
        with self._lock(agent_id):
            info = self._info(agent_id, version)
            version = info.version
            if info.state not in ("approved", "deployed"):
                raise ReleaseError(
                    f"{agent_id} v{version}은 '{info.state}' 상태입니다. 승인된 버전만 배포할 수 있습니다 (not approved)")
            load_version_dir(self._version_dir(agent_id, version), agent_id, version)
            deployment = self.deployment(agent_id)
            retire: list[int] = []
            if percent < 100:
                if deployment.stable is None:
                    raise ReleaseError("stable 버전이 없으면 일부 비율로 배포할 수 없습니다. 먼저 100으로 배포하세요")
                if deployment.stable == version:
                    raise ReleaseError(f"v{version}은 이미 stable입니다")
                if deployment.canary not in (None, version):
                    retire.append(deployment.canary)
                deployment.canary, deployment.canary_percent = version, percent
            else:
                if deployment.stable != version:
                    if deployment.stable is not None:
                        retire.append(deployment.stable)
                    deployment.previous_stable = deployment.stable
                    deployment.stable = version
                if deployment.canary == version:
                    deployment.canary, deployment.canary_percent = None, 0
            self._set_state(agent_id, version, "deployed")
            for old in retire:
                if old not in (deployment.stable, deployment.canary):
                    self._set_state(agent_id, old, "retired")
            return self._write_deployment(deployment)

    def rollback(self, agent_id: str) -> Deployment:
        """With a canary running: the canary is withdrawn and the stable version takes everything.
        Otherwise: back to `previous_stable`; the version rolled away from is retired."""
        with self._lock(agent_id):
            deployment = self.deployment(agent_id)
            if deployment.canary is not None:
                withdrawn = deployment.canary
                deployment.canary, deployment.canary_percent = None, 0
                if withdrawn != deployment.stable:
                    self._set_state(agent_id, withdrawn, "retired")
                return self._write_deployment(deployment)
            target = deployment.previous_stable
            if target is None:
                raise ReleaseError(f"{agent_id}: 되돌아갈 이전 버전이 없습니다 (no previous stable)")
            load_version_dir(self._require_version(agent_id, target), agent_id, target)
            bad = deployment.stable
            deployment.stable, deployment.previous_stable = target, None
            self._set_state(agent_id, target, "deployed")
            if bad is not None and bad != target:
                self._set_state(agent_id, bad, "retired")
            return self._write_deployment(deployment)
