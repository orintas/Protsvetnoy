from sync_service.review_assistant import (
    MAX_REPLY_CHARS,
    MAX_RULES_CHARS,
    QUESTION_SYSTEM_PROMPT,
    RULES_EDIT_SYSTEM_PROMPT,
    RULES_UPDATE_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    canned_review_reply,
    edit_rules_by_instruction,
    generate_question_reply,
    generate_review_reply,
    update_rules_from_edit,
)


class FakeLLM:
    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls = []

    def complete(self, *, system, user_message, max_tokens=1024, image_bytes=None, image_media_type="image/jpeg"):
        self.calls.append({"system": system, "user_message": user_message})
        return self.reply


def test_generate_review_reply_uses_the_review_system_prompt():
    llm = FakeLLM(reply="Спасибо за отзыв!")
    reply = generate_review_reply(client=llm, platform="ozon", product_name="Картина по номерам «Коты»", rating=5, review_text="Очень понравилось, спасибо!")

    assert reply == "Спасибо за отзыв!"
    assert llm.calls[0]["system"] == REVIEW_SYSTEM_PROMPT
    assert "OZON" in llm.calls[0]["user_message"]
    assert "Картина по номерам «Коты»" in llm.calls[0]["user_message"]
    assert "5" in llm.calls[0]["user_message"]
    assert "Очень понравилось, спасибо!" in llm.calls[0]["user_message"]


def test_generate_review_reply_handles_missing_text_without_crashing():
    llm = FakeLLM(reply="Спасибо за оценку!")
    reply = generate_review_reply(client=llm, platform="yandex_market", product_name="Алмазная мозаика", rating=5, review_text="")

    assert reply == "Спасибо за оценку!"
    assert "Яндекс.Маркет" in llm.calls[0]["user_message"]
    assert "покупатель не оставил текст" in llm.calls[0]["user_message"]


def test_generate_review_reply_truncates_an_overlong_model_response():
    llm = FakeLLM(reply="x" * (MAX_REPLY_CHARS + 500))
    reply = generate_review_reply(client=llm, platform="ozon", product_name="Товар", rating=3, review_text="норм")
    assert len(reply) == MAX_REPLY_CHARS


def test_generate_review_reply_omits_the_rules_block_when_no_rules_given():
    llm = FakeLLM(reply="ok")
    generate_review_reply(client=llm, platform="ozon", product_name="Товар", rating=5, review_text="норм")
    assert "Свод правил" not in llm.calls[0]["user_message"]


def test_generate_review_reply_prepends_rules_when_given():
    llm = FakeLLM(reply="ok")
    generate_review_reply(client=llm, platform="ozon", product_name="Товар", rating=5, review_text="норм", rules="- не используй слово акция")
    assert "не используй слово акция" in llm.calls[0]["user_message"]
    assert llm.calls[0]["user_message"].index("не используй слово акция") < llm.calls[0]["user_message"].index("норм")


def test_generate_question_reply_prepends_rules_when_given():
    llm = FakeLLM(reply="ok")
    generate_question_reply(client=llm, platform="yandex_market", product_name="Товар", question_text="Какой размер?", rules="- отвечай короче")
    assert "отвечай короче" in llm.calls[0]["user_message"]


def test_canned_review_reply_is_positive_for_a_high_or_missing_rating():
    assert "Спасибо" in canned_review_reply(rating=5)
    assert "Спасибо" in canned_review_reply(rating=None)


def test_canned_review_reply_is_apologetic_for_a_low_rating():
    reply = canned_review_reply(rating=2)
    assert "Жаль" in reply
    assert "упаковке" in reply


def test_generate_question_reply_uses_the_question_system_prompt():
    llm = FakeLLM(reply="Размер 40x50 см.")
    reply = generate_question_reply(client=llm, platform="yandex_market", product_name="Картина по номерам «Коты»", question_text="Какой размер холста?")

    assert reply == "Размер 40x50 см."
    assert llm.calls[0]["system"] == QUESTION_SYSTEM_PROMPT
    assert "Картина по номерам «Коты»" in llm.calls[0]["user_message"]
    assert "Какой размер холста?" in llm.calls[0]["user_message"]


def test_update_rules_from_edit_uses_the_rules_update_system_prompt_and_context():
    llm = FakeLLM(reply="- Новое правило из правки")
    updated = update_rules_from_edit(
        client=llm, current_rules="- Старое правило", product_name="Картина по номерам «Коты»",
        customer_text="Очень понравилось", rating=5, draft_text="Спасибо за отзыв!",
        final_text="Спасибо, мы очень рады!",
    )

    assert updated == "- Новое правило из правки"
    assert llm.calls[0]["system"] == RULES_UPDATE_SYSTEM_PROMPT
    assert "- Старое правило" in llm.calls[0]["user_message"]
    assert "Картина по номерам «Коты»" in llm.calls[0]["user_message"]
    assert "Спасибо за отзыв!" in llm.calls[0]["user_message"]
    assert "Спасибо, мы очень рады!" in llm.calls[0]["user_message"]


def test_update_rules_from_edit_handles_an_empty_current_rulebook():
    llm = FakeLLM(reply="- Первое правило")
    update_rules_from_edit(client=llm, current_rules="", product_name="Товар", customer_text="текст", rating=5, draft_text="черновик", final_text="финал")
    assert "пока пусто" in llm.calls[0]["user_message"]


def test_update_rules_from_edit_truncates_an_overlong_response():
    llm = FakeLLM(reply="x" * (MAX_RULES_CHARS + 500))
    updated = update_rules_from_edit(client=llm, current_rules="", product_name="Товар", customer_text="текст", rating=5, draft_text="черновик", final_text="финал")
    assert len(updated) == MAX_RULES_CHARS


def test_edit_rules_by_instruction_uses_the_rules_edit_system_prompt():
    llm = FakeLLM(reply="- Отвечай короче")
    updated = edit_rules_by_instruction(client=llm, current_rules="- Старое правило", instruction="сократи все ответы")

    assert updated == "- Отвечай короче"
    assert llm.calls[0]["system"] == RULES_EDIT_SYSTEM_PROMPT
    assert "- Старое правило" in llm.calls[0]["user_message"]
    assert "сократи все ответы" in llm.calls[0]["user_message"]
