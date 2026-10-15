from sync_service.review_assistant import (
    MAX_REPLY_CHARS,
    QUESTION_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    generate_question_reply,
    generate_review_reply,
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


def test_generate_question_reply_uses_the_question_system_prompt():
    llm = FakeLLM(reply="Размер 40x50 см.")
    reply = generate_question_reply(client=llm, platform="yandex_market", product_name="Картина по номерам «Коты»", question_text="Какой размер холста?")

    assert reply == "Размер 40x50 см."
    assert llm.calls[0]["system"] == QUESTION_SYSTEM_PROMPT
    assert "Картина по номерам «Коты»" in llm.calls[0]["user_message"]
    assert "Какой размер холста?" in llm.calls[0]["user_message"]
