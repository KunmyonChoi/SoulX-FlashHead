"""Errors of the definition module."""
from __future__ import annotations


class DefinitionError(Exception):
    """A definition is wrong or cannot be read.

    `problems` lists every fault found as (file, field, message); `file` and `field` are those of
    the first one. `field` is a path such as "flow[0].rule" or "evaluation.success[2].metric".
    """

    def __init__(self, problems, agent_id: str = "", version: int | None = None):
        if isinstance(problems, str):
            problems = [("", "", problems)]
        self.problems: list[tuple[str, str, str]] = [tuple(p) for p in problems]
        self.agent_id = agent_id
        self.version = version
        self.file = self.problems[0][0] if self.problems else ""
        self.field = self.problems[0][1] if self.problems else ""
        where = agent_id + (f" v{version}" if version is not None else "")
        lines = []
        for file, field, message in self.problems:
            head = " ".join(part for part in (where, file) if part)
            lines.append(": ".join(part for part in (head, field, message) if part))
        super().__init__("\n".join(lines))


class DefinitionNotFound(DefinitionError):
    """The agent or the version does not exist (or nothing is deployed and no version was named)."""
