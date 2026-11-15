"""A topic taught over 1-4 short daily steps, plus the end-of-topic cheat sheet.

Where `Lesson` (models/lesson.py) packed a whole topic into one long message, these models
split it: a `TopicPlan` divides the research notes into ordered steps, each step becomes one
short `LessonStep` (a scannable message plus a bank of practice), and a `TopicSummary` is the
cheat sheet sent once the learner leaves the topic. `TopicContent` is the whole thing, shared
per (topic, cefr) and cached like a lesson was.

Message limits are sized for a Telegram push of at most `STEP_MESSAGE_LIMIT` rendered chars:
each field is capped, and `StepMessage` also caps the sum of the visible text so three maxed
fields cannot add up to a wall. The rendered length is re-checked by the step writer.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from english_agent.models.cefr import Cefr
from english_agent.models.lesson import Example, Source
from english_agent.models.practice import QuizQuestion, TranslationTask
from english_agent.models.review import StepReview, SummaryReview
from english_agent.versions import Provenance

MAX_STEPS = 4
STEP_QUIZ_BANK = 5  # questions the writer produces per step; the daily quiz draws from several steps
STEP_TRANSLATION_BANK = 3
STEP_TEXT_BUDGET = 950  # sum of visible characters in one step message (markup adds ~200 more)
STEP_MESSAGE_LIMIT = 1200  # rendered HTML length the step writer must stay under

_SLUG = r"^[a-z][a-z0-9_]*$"
_RESERVED_SLUGS = frozenset({"core", "summary"})

StepVariant = Literal["main", "alt"]


def _reject_reserved(value: str) -> str:
    if value in _RESERVED_SLUGS:
        raise ValueError(f"slug must not be one of {sorted(_RESERVED_SLUGS)}")
    return value


class StepExample(Example):
    """An `Example` kept short enough for a step message (the RU side is hidden in a spoiler)."""

    en: str = Field(min_length=1, max_length=90)
    ru: str = Field(min_length=1, max_length=90)


class SelfCheck(BaseModel):
    """A one-line retrieval prompt whose answer is revealed in a spoiler."""

    prompt_en: str = Field(min_length=10, max_length=140)
    answer_en: str = Field(min_length=1, max_length=60)
    explanation_ru: str = Field(min_length=3, max_length=100)


class StepMessage(BaseModel):
    """The scannable morning message for one step: a hook, one idea, three examples and a check."""

    hook_ru: str = Field(min_length=10, max_length=140)  # a question or situation that opens the step
    key_idea_ru: str = Field(min_length=30, max_length=160)  # the single thing to remember
    form: str | None = Field(default=None, max_length=100)  # the pattern, only when this step introduces it
    examples: Annotated[list[StepExample], Field(min_length=3, max_length=3)]
    incorrect: str = Field(min_length=1, max_length=90)
    correct: str = Field(min_length=1, max_length=90)
    contrast_ru: str = Field(min_length=10, max_length=160)  # a contrast or the typical mistake, in Russian
    self_check: SelfCheck
    teaser_ru: str | None = Field(default=None, max_length=100)  # names tomorrow's step; None on the last step

    @property
    def text_length(self) -> int:
        parts = [
            self.hook_ru,
            self.key_idea_ru,
            self.form or "",
            self.incorrect,
            self.correct,
            self.contrast_ru,
            self.self_check.prompt_en,
            self.self_check.answer_en,
            self.self_check.explanation_ru,
            self.teaser_ru or "",
        ]
        parts += [example.en for example in self.examples]
        parts += [example.ru for example in self.examples]
        return sum(len(part) for part in parts)

    @model_validator(mode="after")
    def within_budget(self) -> StepMessage:
        if self.text_length > STEP_TEXT_BUDGET:
            raise ValueError(f"step message text is {self.text_length} chars, budget is {STEP_TEXT_BUDGET}")
        return self


class BankQuestion(QuizQuestion):
    """A quiz question tagged with the step item it checks, so the daily quiz can interleave steps."""

    item_slug: str = Field(pattern=_SLUG, max_length=40)


class BankSentence(TranslationTask):
    """A translation sentence tagged with the step item it checks."""

    item_slug: str = Field(pattern=_SLUG, max_length=40)


class StepPractice(BaseModel):
    """The bank the writer produces for one step; the daily practice selects from several banks."""

    quiz: Annotated[list[BankQuestion], Field(min_length=STEP_QUIZ_BANK, max_length=STEP_QUIZ_BANK)]
    translation: Annotated[
        list[BankSentence], Field(min_length=STEP_TRANSLATION_BANK, max_length=STEP_TRANSLATION_BANK)
    ]


class PlanStep(BaseModel):
    """One step in a topic plan: a title, its idea, and which note points it is built from."""

    slug: str = Field(pattern=_SLUG, max_length=40)  # stable within the topic; part of the item key
    title_en: str = Field(min_length=1, max_length=60)
    title_ru: str = Field(min_length=1, max_length=60)
    key_idea_ru: str = Field(min_length=30, max_length=160)
    rule_point_ids: list[int] = Field(default_factory=list)  # indices into the notes' rule_points
    nuance_ids: list[int] = Field(default_factory=list)
    example_ids: list[int] = Field(default_factory=list)
    mistake_ids: list[int] = Field(default_factory=list)
    introduces_form: bool = True

    _check_slug = field_validator("slug")(_reject_reserved)


class TopicPlan(BaseModel):
    """How a topic is split into ordered steps, with recurring characters for continuity."""

    topic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    cefr: Cefr
    steps: Annotated[list[PlanStep], Field(min_length=1, max_length=MAX_STEPS)]
    characters: Annotated[list[str], Field(min_length=1, max_length=3)]
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def unique_slugs(self) -> TopicPlan:
        slugs = [step.slug for step in self.steps]
        if len(slugs) != len(set(slugs)):
            raise ValueError("step slugs must be unique within a topic")
        return self

    @property
    def slugs(self) -> list[str]:
        return [step.slug for step in self.steps]


class LessonStep(BaseModel):
    """One day's message for a topic: the `main` teaching step or its `alt` re-teach variant.

    `item_key` (`<topic_id>:<slug>`) is the learnable item the step's card and errors attach to.
    """

    topic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title_en: str = Field(min_length=1, max_length=60)
    title_ru: str = Field(min_length=1, max_length=60)
    cefr: Cefr
    slug: str = Field(pattern=_SLUG, max_length=40)
    index: int = Field(ge=1, le=MAX_STEPS)  # 1-based position in the topic
    total: int = Field(ge=1, le=MAX_STEPS)  # how many steps the topic has
    variant: StepVariant = "main"
    message: StepMessage
    # The practice bank; the re-teach (`alt`) variant carries none, it only re-explains.
    practice: StepPractice | None = None
    # Code-owned: taken from the research notes, never from the step writer.
    sources: Annotated[list[Source], Field(max_length=5)] = Field(default_factory=list)
    provenance: Provenance | None = None
    # Code-owned: the Critic's last review of this step; None when the Critic was off.
    review: StepReview | None = None

    _check_slug = field_validator("slug")(_reject_reserved)

    @property
    def item_key(self) -> str:
        return f"{self.topic_id}:{self.slug}"

    @model_validator(mode="after")
    def consistent(self) -> LessonStep:
        if self.index > self.total:
            raise ValueError(f"index {self.index} is past total {self.total}")
        teaser = self.message.teaser_ru is not None
        if teaser != (self.index < self.total):
            raise ValueError("teaser_ru must be present on every step except the last")
        if self.variant == "main" and self.practice is None:
            raise ValueError("a main step must carry its practice bank")
        if self.variant == "alt" and self.practice is not None:
            raise ValueError("an alt (re-teach) step must not carry practice")
        return self


class SummarySection(BaseModel):
    """One block of the cheat sheet, mirroring one step of the topic."""

    title_ru: str = Field(min_length=1, max_length=60)
    lines_ru: Annotated[list[Annotated[str, Field(min_length=1, max_length=160)]], Field(min_length=1, max_length=5)]


class TopicSummary(BaseModel):
    """The end-of-topic cheat sheet: one section per step, the forms, the common mistakes."""

    topic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    cefr: Cefr
    title_en: str = Field(min_length=1, max_length=80)
    title_ru: str = Field(min_length=1, max_length=80)
    sections: Annotated[list[SummarySection], Field(min_length=1, max_length=MAX_STEPS)]
    forms: Annotated[list[Annotated[str, Field(min_length=1, max_length=100)]], Field(max_length=MAX_STEPS)] = Field(
        default_factory=list
    )
    top_mistakes_ru: Annotated[list[Annotated[str, Field(min_length=1, max_length=160)]], Field(max_length=4)] = Field(
        default_factory=list
    )
    # Code-owned, from the research notes.
    sources: Annotated[list[Source], Field(max_length=5)] = Field(default_factory=list)
    provenance: Provenance | None = None
    # Code-owned: the Critic's last review of the cheat sheet; None when the Critic was off.
    review: SummaryReview | None = None


class TopicContent(BaseModel):
    """Everything generated once per (topic, cefr): the plan, every step and its re-teach, the cheat sheet.

    `steps` are the `main` variants in order; `alternatives[i]` is the `alt` re-teach of `steps[i]`.
    Shared across learners and cached in `lesson_contents`; a learner is pinned to one copy per topic.
    """

    topic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    cefr: Cefr
    plan: TopicPlan
    steps: Annotated[list[LessonStep], Field(min_length=1, max_length=MAX_STEPS)]
    alternatives: Annotated[list[LessonStep], Field(min_length=1, max_length=MAX_STEPS)]
    summary: TopicSummary
    sources: Annotated[list[Source], Field(min_length=1, max_length=5)]

    @model_validator(mode="after")
    def aligned(self) -> TopicContent:
        plan_slugs = self.plan.slugs
        count = len(plan_slugs)
        if not (len(self.steps) == len(self.alternatives) == count == len(self.summary.sections)):
            raise ValueError("plan, steps, alternatives and summary sections must have equal length")
        for index, (main, alt, slug) in enumerate(zip(self.steps, self.alternatives, plan_slugs, strict=True), start=1):
            for step, variant in ((main, "main"), (alt, "alt")):
                if step.slug != slug:
                    raise ValueError(f"step {index} slug {step.slug!r} does not match plan slug {slug!r}")
                if step.index != index or step.total != count:
                    raise ValueError(f"step {slug!r} must be index {index} of {count}")
                if step.variant != variant:
                    raise ValueError(f"step {slug!r} must be variant {variant!r}")
        return self

    def step(self, index: int, variant: StepVariant = "main") -> LessonStep:
        """The step at 1-based `index` in the given variant."""
        steps = self.steps if variant == "main" else self.alternatives
        return steps[index - 1]

    def form_for(self, index: int) -> str | None:
        """The form in force at `index`: the nearest `form` at or before this main step."""
        form: str | None = None
        for step in self.steps[:index]:
            if step.message.form is not None:
                form = step.message.form
        return form

    def item_keys(self) -> list[str]:
        return [step.item_key for step in self.steps]
