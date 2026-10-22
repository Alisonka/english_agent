from datetime import date

import pytest
from pydantic import ValidationError

from english_agent.models import (
    EXAMPLE_MAX,
    EXAMPLE_MIN,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    Curriculum,
    Example,
    GoldenCase,
    Lesson,
    Source,
    load_curriculum,
    load_goldens,
)
from english_agent.models.golden import dump_goldens_jsonl, load_goldens_yaml
from english_agent.paths import goldens_v1_jsonl_path, goldens_v1_yaml_path


def _valid_lesson(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "topic_id": "present_simple_third_person",
        "title_en": "Present Simple third person -s",
        "title_ru": "Present Simple — окончание -s",
        "cefr": "A2",
        "rule_ru": (
            "В третьем лице единственного числа (he, she, it) к глаголу в Present Simple добавляется "
            "окончание -s: She works, He lives. После шипящих и -o пишем -es: watches, goes. "
            "В вопросах и отрицаниях появляется вспомогательный глагол does или doesn't, а основной "
            "глагол возвращается в первую форму: Does he work? He doesn't work. Окончание -s нужно "
            "только в утверждениях с he, she, it; с I, you, we, they глагол не меняется."
        ),
        "form": "he/she/it + V-s; does/doesn't + V",
        "nuances_ru": [
            "После does и doesn't глагол без -s: Does he works — ошибка.",
            "Глаголы на -y после согласной меняют -y на -ies: study — studies, но play — plays.",
            "have в третьем лице превращается в has: She has a cat.",
        ],
        "examples": [
            {
                "en": "He goes to work by bus.",
                "ru": "Он ездит на работу на автобусе.",
                "highlights": ["goes"],
            },
            {
                "en": "She works in a shop.",
                "ru": "Она работает в магазине.",
                "highlights": ["works"],
            },
            {
                "en": "He doesn't like coffee.",
                "ru": "Он не любит кофе.",
                "highlights": ["doesn't like"],
            },
            {
                "en": "Does it rain a lot here?",
                "ru": "Здесь часто идёт дождь?",
                "highlights": ["Does it rain"],
            },
        ],
        "mistake_ru": "В русском нет -s в 3-м лице, поэтому его часто пропускают.",
        "incorrect": "He go to school every day.",
        "correct": "He goes to school every day.",
        "sources": [
            {
                "url": "https://learnenglish.britishcouncil.org/grammar/a1-a2-grammar/present-simple",
                "title": "Present simple",
                "accessed_at": "2026-10-13",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_curriculum_loads_and_is_a2_sequence() -> None:
    curriculum = load_curriculum()
    assert curriculum.cefr == "A2"
    assert curriculum.explanation_language == "ru"
    assert curriculum.example_language == "en"
    assert 40 <= len(curriculum.topics) <= 60
    assert curriculum.topic_ids()[0] == "be_present"
    assert curriculum.topic_ids() == [topic.id for topic in curriculum.topics]


def test_goldens_v1_has_25_unique_curriculum_topics() -> None:
    curriculum = load_curriculum()
    goldens = load_goldens_yaml()
    assert len(goldens) == 25
    curriculum_ids = set(curriculum.topic_ids())
    golden_ids = [case.topic_id for case in goldens]
    assert len(golden_ids) == len(set(golden_ids))
    missing = set(golden_ids) - curriculum_ids
    assert not missing, f"golden topic_ids not in curriculum: {sorted(missing)}"
    assert [case.id for case in goldens] == [f"g{i:02d}" for i in range(1, 26)]


def test_jsonl_matches_yaml_source() -> None:
    yaml_goldens = load_goldens_yaml()
    jsonl_goldens = load_goldens(goldens_v1_jsonl_path())
    assert [case.model_dump() for case in jsonl_goldens] == [
        case.model_dump() for case in yaml_goldens
    ]
    assert goldens_v1_yaml_path().exists()


def test_lesson_schema_accepts_valid_payload() -> None:
    lesson = Lesson.model_validate(_valid_lesson())
    assert lesson.cefr == "A2"
    assert EXAMPLE_MIN <= len(lesson.examples) <= EXAMPLE_MAX
    assert lesson.sources[0].accessed_at == date(2026, 10, 13)
    assert TELEGRAM_MAX_MESSAGE_LENGTH == 4096


@pytest.mark.parametrize(
    "overrides",
    [
        {"cefr": "B1"},
        {"examples": []},
        {"examples": [{"en": "Hi", "ru": "Привет", "highlights": ["Hi"]}]},
        {"sources": []},
        {"topic_id": "Present Simple"},
        {"rule_ru": "too short"},
        {"nuances_ru": []},
        {"nuances_ru": ["one", "two", "three"]},  # items too short to be real nuances
    ],
)
def test_lesson_schema_rejects_invalid_payload(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Lesson.model_validate(_valid_lesson(**overrides))


def test_lesson_rejects_more_than_max_examples() -> None:
    extra = [
        Example(en=f"Sentence {i}.", ru=f"Предложение {i}.", highlights=["Sentence"])
        for i in range(EXAMPLE_MAX + 1)
    ]
    with pytest.raises(ValidationError):
        Lesson.model_validate(_valid_lesson(examples=[item.model_dump() for item in extra]))


def test_golden_case_requires_forbidden_claims() -> None:
    payload = load_goldens_yaml()[0].model_dump()
    payload["forbidden_claims"] = ["only one"]
    with pytest.raises(ValidationError):
        GoldenCase.model_validate(payload)


def test_curriculum_rejects_duplicate_ids() -> None:
    curriculum = load_curriculum()
    payload = curriculum.model_dump()
    payload["topics"][1]["id"] = payload["topics"][0]["id"]
    with pytest.raises(ValidationError):
        Curriculum.model_validate(payload)


def test_dump_jsonl_roundtrip(tmp_path) -> None:
    goldens = load_goldens_yaml()
    path = dump_goldens_jsonl(goldens, tmp_path / "goldens.jsonl")
    assert load_goldens(path) == goldens


def test_source_and_example_models() -> None:
    source = Source.model_validate(
        {
            "url": "https://www.bbc.co.uk/learningenglish/english/course/lower-intermediate",
            "title": "BBC Learning English",
            "accessed_at": "2026-10-13T09:00:00",
        }
    )
    assert str(source.url).startswith("https://")
    example = Example(en="There is a cat.", ru="На диване кот.", highlights=["There is"])
    assert example.highlights == ["There is"]
