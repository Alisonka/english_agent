"""Practice that follows the lesson: a quiz (Telegram quiz polls) and a RU->EN translation task.

Field limits mirror the Telegram Bot API `sendPoll` limits, so a valid model can always be sent.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from english_agent.versions import Provenance

QUIZ_SIZE = 3
TRANSLATION_SIZE = 3
POLL_EXPLANATION_MAX_LINE_FEEDS = 2

QuizKind = Literal["gap_fill", "choose_translation", "contrast"]


class QuizQuestion(BaseModel):
    kind: QuizKind
    question: str = Field(min_length=10, max_length=300)
    options: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=100)]],
        Field(min_length=3, max_length=4),
    ]
    correct_option: int = Field(ge=0)  # index into `options`
    # Shown by Telegram after a wrong answer (or on the lamp icon).
    explanation_ru: str = Field(min_length=10, max_length=200)

    @model_validator(mode="after")
    def consistent(self) -> "QuizQuestion":
        if self.correct_option >= len(self.options):
            raise ValueError(f"correct_option {self.correct_option} is out of range for {len(self.options)} options")
        normalized = [option.strip().casefold() for option in self.options]
        if len(set(normalized)) != len(normalized):
            raise ValueError("options must be distinct")
        if self.explanation_ru.count("\n") > POLL_EXPLANATION_MAX_LINE_FEEDS:
            raise ValueError(f"explanation_ru may have at most {POLL_EXPLANATION_MAX_LINE_FEEDS} line breaks")
        return self


class TranslationTask(BaseModel):
    ru: str = Field(min_length=5, max_length=200)  # what the learner translates
    reference_en: str = Field(min_length=3, max_length=240)  # one natural translation; a hint for the grader
    focus_ru: str = Field(min_length=5, max_length=120)  # what this sentence checks


TranslationVerdict = Literal["correct", "fixed", "missing"]


class GradedSentence(BaseModel):
    n: int = Field(ge=1, le=TRANSLATION_SIZE)  # number of the task sentence
    learner_text: str = Field(max_length=500)  # what the learner wrote for it; "" when missing
    verdict: TranslationVerdict
    corrected: str = Field(max_length=300)  # minimal fix; equals learner_text when correct
    explanation_ru: str = Field(max_length=400)  # why, in Russian; "" when correct
    error_type: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def explained(self) -> "GradedSentence":
        if self.verdict == "fixed" and not self.explanation_ru.strip():
            raise ValueError("a fixed sentence needs explanation_ru")
        return self


class TranslationGrade(BaseModel):
    """The grader's verdict on one learner answer to the translation task."""

    sentences: Annotated[list[GradedSentence], Field(min_length=TRANSLATION_SIZE, max_length=TRANSLATION_SIZE)]
    focus_ru: str = Field(min_length=5, max_length=200)  # one thing to watch tomorrow, in Russian
    # Code-owned: how this grade was produced.
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def one_per_sentence(self) -> "TranslationGrade":
        if sorted(item.n for item in self.sentences) != list(range(1, TRANSLATION_SIZE + 1)):
            raise ValueError(f"sentences must cover n = 1..{TRANSLATION_SIZE} exactly once")
        return self

    @property
    def correct_count(self) -> int:
        return sum(item.verdict == "correct" for item in self.sentences)
