"""Reading and checking tool contracts.

One file is one contracts.ToolContract. Files live in packs, `v2/tools/<pack>/<name>.yaml`: a pack is
the tools of one outside system (the cafe, the airline). A contract's id is "<pack>.<name>"; the name
alone is what the model calls and only has to be unique inside the pack. A file directly in the
folder (`v2/tools/<name>.yaml`) is a contract without a pack, with id == name.

An agent names its packs (definition.tool_packs) and then uses bare names: `resolve` finds the id.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from .. import config
from ..contracts import ToolContract
from .validate import check_schema

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ContractError(Exception):
    """A contract file is wrong. The message names the file and the item."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


def check_contract(contract: ToolContract, where=""):
    """Rules beyond the types. Returns a list of sentences, each starting with the item."""
    problems = []
    prefix = f"{where}: " if where else ""
    if not NAME.match(contract.name):
        problems.append(f"{prefix}name: 영문 소문자, 숫자, 밑줄만 쓸 수 있습니다 (지금: {contract.name!r})")
    if not contract.description.strip():
        problems.append(f"{prefix}description: 비어 있습니다. 모델이 언제 쓸지 알 수 있게 적어야 합니다")
    problems += [prefix + p for p in check_schema(contract.input_schema)]
    properties = contract.input_schema.get("properties") or {} if isinstance(contract.input_schema, dict) else {}
    for field in contract.idempotency_fields:
        if field not in properties:
            problems.append(f"{prefix}idempotency_fields: '{field}'이 input_schema.properties에 없습니다")
    for field in contract.pii_input:
        if field not in properties:
            problems.append(f"{prefix}pii_input: '{field}'이 input_schema.properties에 없습니다")
    if contract.effect == "read":
        if contract.confirm_template:
            problems.append(f"{prefix}confirm_template: 조회 도구(effect: read)에는 쓸 수 없습니다")
        if contract.idempotency_fields:
            problems.append(f"{prefix}idempotency_fields: 조회 도구(effect: read)에는 쓸 수 없습니다")
    if contract.confirm_template is not None and not contract.confirm_template.strip():
        problems.append(f"{prefix}confirm_template: 비어 있습니다. 확인 없이 실행할 도구라면 항목을 지우세요")
    if contract.timeout_s <= 0:
        problems.append(f"{prefix}timeout_s: 0보다 커야 합니다")
    if contract.retries < 0:
        problems.append(f"{prefix}retries: 0 이상이어야 합니다")
    if not str(contract.adapter.target).strip():
        problems.append(f"{prefix}adapter.target: 비어 있습니다")
    if contract.display is not None and contract.name in contract.display.refresh:
        problems.append(f"{prefix}display.refresh: 자기 자신을 다시 부를 수 없습니다")
    return problems


class UnknownTool(KeyError):
    """No contract for that name (in those packs), or more than one. str() is a sentence."""

    def __str__(self):
        return str(self.args[0]) if self.args else ""


def resolve(contracts, name, packs=None) -> str:
    """The id of the contract an agent means by `name`.

    `name` may already be an id ("cafe.search_menu"). A bare name is looked up in `packs` (the agent's
    tool_packs), or in every pack when none are given; it must match exactly one contract."""
    name = str(name)
    if name in contracts and ("." in name or not contracts[name].pack):
        return name
    found = [key for key, c in contracts.items() if c.name == name and (not packs or c.pack in packs or not c.pack)]
    if len(found) == 1:
        return found[0]
    if not found:
        where = f" (묶음: {', '.join(packs)})" if packs else ""
        raise UnknownTool(f"도구 계약서가 없습니다: {name}{where}")
    raise UnknownTool(f"'{name}'이라는 도구가 여러 묶음에 있습니다: {', '.join(sorted(found))}. "
                      f"에이전트의 tool_packs에 쓸 묶음을 적거나 '묶음.이름'으로 적으세요")


def catalog(contracts, packs=None) -> dict[str, ToolContract]:
    """{name an agent would write: contract}. With packs: the bare names of those packs (and of
    contracts outside any pack). Without: bare names where they are unique, ids where they are not."""
    out, count = {}, {}
    for c in contracts.values():
        count[c.name] = count.get(c.name, 0) + 1
    for key, c in contracts.items():
        if packs:
            if c.pack in packs or not c.pack:
                out[c.name] = c
        else:
            out[c.name if count[c.name] == 1 else key] = c
    return out


def check_refresh(contracts, where=lambda name: f"{name}.yaml"):
    """display.refresh names read tools that can be called without arguments. Checked when every
    contract is known. Returns a list of sentences."""
    problems = []
    for name, contract in contracts.items():
        if contract.display is None:
            continue
        for other in contract.display.refresh:
            prefix = f"{where(name)}: display.refresh: "
            target = contracts.get(sibling(contract, other))
            if target is None:
                problems.append(f"{prefix}'{other}'이라는 도구 계약서가 없습니다")
            elif target.effect != "read":
                problems.append(f"{prefix}'{other}'은 조회 도구(effect: read)가 아닙니다. 화면을 다시 그릴 때는 "
                                f"조회 도구만 부를 수 있습니다")
            elif target.display is None:
                problems.append(f"{prefix}'{other}'의 계약서에 display가 없어 보여 줄 화면이 없습니다")
            elif target.input_schema.get("required"):
                problems.append(f"{prefix}'{other}'은 인자 없이 부를 수 없습니다 (required: "
                                f"{', '.join(target.input_schema['required'])})")
    return problems


def sibling(contract, name) -> str:
    """The id of a tool another contract names (display.refresh): one of the same pack."""
    return f"{contract.pack}.{name}" if contract.pack and "." not in name else name


def load_contract(path, pack="") -> ToolContract:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ContractError([f"{path.name}: 읽지 못했습니다 ({str(e).splitlines()[0]})"])
    if not isinstance(raw, dict):
        raise ContractError([f"{path.name}: 항목과 값의 묶음(mapping)이어야 합니다"])
    try:
        contract = ToolContract.model_validate(dict(raw, pack=pack))
    except ValidationError as e:
        raise ContractError([f"{path.name}: {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                             for err in e.errors()])
    problems = check_contract(contract, path.name)
    if contract.name != path.stem:
        problems.insert(0, f"{path.name}: name: 파일 이름과 같아야 합니다 (지금: {contract.name!r})")
    if problems:
        raise ContractError(problems)
    return contract


def load_contracts(tools_dir=None) -> dict[str, ToolContract]:
    """Every contract of the folder and of its packs (sub-folders), by id. All problems of all files
    are reported at once."""
    folder = Path(tools_dir or config.TOOLS_DIR)
    contracts, problems = {}, []
    files = [(path, "") for path in sorted(folder.glob("*.yaml"))]
    for sub in sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))) if folder.is_dir() else []:
        if not NAME.match(sub.name):
            problems.append(f"{sub.name}/: 묶음 이름에는 영문 소문자, 숫자, 밑줄만 쓸 수 있습니다")
            continue
        files += [(path, sub.name) for path in sorted(sub.glob("*.yaml"))]
    for path, pack in files:
        try:
            contract = load_contract(path, pack)
        except ContractError as e:
            problems += [f"{pack}/{p}" if pack else p for p in e.problems]
            continue
        contracts[contract.id] = contract
    if not problems:
        problems += check_refresh(contracts, where=lambda key: key.replace(".", "/") + ".yaml")
    if problems:
        raise ContractError(problems)
    return contracts
