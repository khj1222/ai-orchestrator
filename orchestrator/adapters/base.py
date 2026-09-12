"""CLI adapter contracts, independent of records and process implementations.

Adapters only build argv and normalize evidence. The caller starts the process
with prompt_path as stdin_path, records its exit code, and runs registered checks.
No CLI event (including a completed turn) is a task acceptance verdict.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, TypeVar


@dataclass
class CommandExecution:
    item_id: str
    command: str
    exit_code: int | None = None
    status: str = "in_progress"


@dataclass
class AttemptResult:
    """Observed CLI evidence, not process liveness or a validation verdict.

Messages and commands are untrusted worker content. No environment, credential
store, or arbitrary event metadata is copied into this result.
"""

    thread_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    final_message: str | None = None
    command_executions: list[CommandExecution] = field(default_factory=list)
    status: Literal["incomplete", "completed", "failed"] = "incomplete"
    reason_code: str | None = None
    # W4 에서 추가(기본값 있음, W3 파서는 채우지 않음). Claude 의 subagent_stats.spawned 와
    # total_cost_usd. 구독 사용자에게 청구되는 금액이 아니라 CLI 가 보고한 참고값이다.
    subagents_spawned: int | None = None
    cost_usd_reported: float | None = None


class ProcessControl(Protocol):
    """Implemented by the W2 process handle; it owns tree termination."""

    def stop(self, grace_seconds: float) -> object: ...


SpecT = TypeVar("SpecT", contravariant=True)


class Adapter(Protocol[SpecT]):
    def capabilities(self) -> frozenset[str]: ...

    def build_command(
        self, spec: SpecT, workspace: Path, prompt_path: Path
    ) -> list[str]: ...

    def parse_events(self, path: Path) -> AttemptResult: ...

    def stop(self, process: ProcessControl, grace_seconds: float = 3.0) -> object: ...
