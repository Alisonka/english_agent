"""The report of one content run: how writing a topic went, stage by stage.

Stored with the content row (`lesson_contents.run`), ready or failed, so a failed topic says
where it stopped and what it cost, and a ready one shows how many rewrites the Critic asked for.
"""

from typing import Literal

from pydantic import BaseModel, Field

from english_agent.models.review import Verdict

RunOutcome = Literal["ready", "not_found", "failed"]


class StageReport(BaseModel):
    name: str  # "research", "plan", "step 2", "step 2 alt", "summary"
    calls: int = Field(default=0, ge=0)  # model calls, retries included
    cost_usd: float = Field(default=0.0, ge=0)  # estimated; 0 for models without a price
    seconds: float = Field(default=0.0, ge=0)
    revisions: int = Field(default=0, ge=0)  # rewrites the Critic asked for
    verdict: Verdict | None = None  # the Critic's last verdict; None when nothing was reviewed
    blocking: list[str] = Field(default_factory=list)  # the last review's blocking issues, "kind @ where"


class RunReport(BaseModel):
    outcome: RunOutcome
    error: str | None = None  # why the run produced no content
    calls: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    seconds: float = Field(default=0.0, ge=0)
    critic_model: str | None = None  # None: the Critic was off for this run
    stages: list[StageReport] = Field(default_factory=list)

    @property
    def revisions(self) -> int:
        return sum(stage.revisions for stage in self.stages)

    def summary(self) -> str:
        """One line for logs and alerts."""
        line = (
            f"{self.outcome}: {self.calls} calls, ${self.cost_usd:.3f}, {self.seconds:.0f}s, {self.revisions} rewrites"
        )
        blocked = [f"{stage.name}: {', '.join(stage.blocking)}" for stage in self.stages if stage.blocking]
        if blocked:
            line += " | blocking: " + "; ".join(blocked)
        return line
