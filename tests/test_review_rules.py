from sync_service.review_rules import ReviewRulesStore


def test_get_returns_empty_string_for_an_unset_kind(tmp_path):
    store = ReviewRulesStore(str(tmp_path / "rules.sqlite3"))
    assert store.get("review") == ""


def test_set_then_get_round_trips(tmp_path):
    store = ReviewRulesStore(str(tmp_path / "rules.sqlite3"))
    store.set("review", "- Не используй слово \"акция\"")
    assert store.get("review") == "- Не используй слово \"акция\""


def test_review_and_question_kinds_are_independent(tmp_path):
    store = ReviewRulesStore(str(tmp_path / "rules.sqlite3"))
    store.set("review", "правило про отзывы")
    store.set("question", "правило про вопросы")
    assert store.get("review") == "правило про отзывы"
    assert store.get("question") == "правило про вопросы"


def test_set_overwrites_the_previous_value_for_that_kind(tmp_path):
    store = ReviewRulesStore(str(tmp_path / "rules.sqlite3"))
    store.set("review", "первая версия")
    store.set("review", "вторая версия")
    assert store.get("review") == "вторая версия"


def test_a_second_store_instance_sees_persisted_data(tmp_path):
    path = str(tmp_path / "rules.sqlite3")
    ReviewRulesStore(path).set("question", "сохранённое правило")
    assert ReviewRulesStore(path).get("question") == "сохранённое правило"
