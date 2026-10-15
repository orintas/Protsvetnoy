import json

from sync_service.error_log import ErrorLog
from sync_service.http import ApiError
from sync_service.review_worker import (
    MAX_DRAFTS_PER_TICK,
    _draft_question,
    _find_pending_draft,
    _format_received_at,
    _handle_reply,
    _is_relevant,
    _poll_ozon_reviews,
    _poll_yandex_questions,
    _poll_yandex_reviews,
    handle_new_question,
    poll_once,
    reply_run_once,
)
from sync_service.yandex_market_sync import YandexMarketSyncLog


class FakeSettings:
    yandexgpt_api_key = "test-key"
    yandexgpt_folder_id = "test-folder"
    yandexgpt_model = "yandexgpt/latest"
    telegram_reviews_bot_token = "bot-token"
    telegram_reviews_chat_id = "-100999"
    telegram_proxy_url = ""
    moysklad_base_url = "https://x"
    moysklad_token = "x"
    yandex_market_base_url = "https://x"
    yandex_market_api_key = "ym-key"
    yandex_market_business_id = "939642"
    ozon_client_id = "ozon-id"
    ozon_api_key = "ozon-key"


class FakeOzon:
    def __init__(self, reviews=None, empty_review_error_for=None):
        self._reviews = reviews or []
        self.comments_created = []
        self.marked_processed = []
        self._empty_review_error_for = empty_review_error_for or set()

    def unprocessed_reviews(self):
        return self._reviews

    def product_names_by_sku(self, skus):
        return {sku: {"offer_id": f"SKU{sku}", "name": f"Товар {sku}"} for sku in skus}

    def create_review_comment(self, *, review_id, text, mark_review_as_processed=True):
        if review_id in self._empty_review_error_for:
            raise ApiError(
                'POST https://api-seller.ozon.ru/v1/review/comment/create failed with HTTP 400: '
                '{"code":3, "message":"createComment: cannot comment on empty review"}'
            )
        self.comments_created.append({"review_id": review_id, "text": text})
        return "comment-1"

    def mark_review_processed(self, review_id):
        self.marked_processed.append(review_id)

    def close(self):
        pass


class FakeYandex:
    def __init__(self, feedbacks=None, questions=None, question_by_id_result="unset"):
        self._feedbacks = feedbacks or []
        self._questions = questions or []
        self._question_by_id_result = question_by_id_result
        self.feedback_comments = []
        self.question_answers = []
        self.requested_question_id = None

    def feedbacks_needing_reaction(self):
        return self._feedbacks

    def unanswered_questions(self):
        return self._questions

    def question_by_id(self, question_id):
        self.requested_question_id = question_id
        if self._question_by_id_result != "unset":
            return self._question_by_id_result
        return next((q for q in self._questions if q["questionIdentifiers"]["id"] == question_id), None)

    def comment_on_feedback(self, *, feedback_id, text):
        self.feedback_comments.append({"feedback_id": feedback_id, "text": text})
        return {}

    def answer_question(self, *, question_id, text):
        self.question_answers.append({"question_id": question_id, "text": text})
        return {}

    def close(self):
        pass


class FakeMoySklad:
    def __init__(self, products=None):
        self._products = products or {}

    def product_by_code(self, code):
        return self._products.get(code)

    def close(self):
        pass


class FakeLLM:
    def __init__(self, reply="Спасибо за отзыв!", fail_for=None):
        self.reply = reply
        self.fail_for = fail_for or set()
        self.calls = []

    def complete(self, *, system, user_message, max_tokens=1024, image_bytes=None, image_media_type="image/jpeg"):
        self.calls.append(user_message)
        if any(marker in user_message for marker in self.fail_for):
            raise RuntimeError("LLM unavailable")
        return self.reply

    def close(self):
        pass


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self._next_message_id = 500

    def send_message(self, *, chat_id, text, reply_to_message_id=None, parse_mode=None):
        self._next_message_id += 1
        self.messages.append({"chat_id": chat_id, "text": text, "reply_to_message_id": reply_to_message_id})
        return {"ok": True, "result": {"message_id": self._next_message_id}}

    def get_updates(self, *, offset=None, timeout=25, allowed_updates=None):
        return getattr(self, "_updates", [])

    def delete_webhook(self):
        return {"ok": True}

    def close(self):
        pass


def _install_fakes(monkeypatch, *, telegram=None, ozon=None, yandex=None, moysklad=None, llm=None):
    import sync_service.review_worker as mod
    if telegram is not None:
        monkeypatch.setattr(mod, "TelegramClient", lambda **kwargs: telegram)
    if ozon is not None:
        monkeypatch.setattr(mod, "OzonClient", lambda **kwargs: ozon)
    if yandex is not None:
        monkeypatch.setattr(mod, "YandexMarketClient", lambda **kwargs: yandex)
    if moysklad is not None:
        monkeypatch.setattr(mod, "MoySkladClient", lambda **kwargs: moysklad)
    if llm is not None:
        monkeypatch.setattr(mod, "YandexGPTClient", lambda **kwargs: llm)


# --- _poll_ozon_reviews ---------------------------------------------------

def test_poll_ozon_reviews_drafts_and_sends_new_reviews(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    ozon = FakeOzon(reviews=[{"id": "r1", "sku": 111, "text": "Отлично!", "rating": 5, "published_at": "2026-10-14T02:55:35.014683Z"}])
    telegram = FakeTelegram()
    llm = FakeLLM(reply="Спасибо за высокую оценку!")

    _poll_ozon_reviews(ozon=ozon, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert len(telegram.messages) == 1
    assert "SKU111" in telegram.messages[0]["text"] or "Товар 111" in telegram.messages[0]["text"]
    assert "Спасибо за высокую оценку!" in telegram.messages[0]["text"]
    assert "Получено: 14.10.2026 05:55 (мск)" in telegram.messages[0]["text"]  # UTC+3
    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["kind"] == "draft_sent"
    assert entries[0]["external_id"] == "ozon:review:r1"
    payload = json.loads(entries[0]["payload"])
    assert payload["draft_text"] == "Спасибо за высокую оценку!"
    assert payload["message_id"] == 501  # FakeTelegram's first assigned id
    assert payload["platform"] == "ozon" and payload["kind"] == "review"
    assert payload["received_at"] == "2026-10-14T02:55:35.014683Z"


def test_poll_ozon_reviews_skips_a_review_already_drafted(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    log.add("draft_sent", "success", "already drafted", "ozon:review:r1", {"message_id": 1})
    ozon = FakeOzon(reviews=[{"id": "r1", "sku": 111, "text": "Отлично!", "rating": 5}])
    telegram = FakeTelegram()
    llm = FakeLLM()

    _poll_ozon_reviews(ozon=ozon, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert telegram.messages == []  # nothing new sent — r1 was already drafted


def test_poll_ozon_reviews_caps_at_max_drafts_per_tick(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    reviews = [{"id": f"r{i}", "sku": 1, "text": "ok", "rating": 5} for i in range(MAX_DRAFTS_PER_TICK + 10)]
    ozon = FakeOzon(reviews=reviews)
    telegram = FakeTelegram()
    llm = FakeLLM()

    _poll_ozon_reviews(ozon=ozon, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert len(telegram.messages) == MAX_DRAFTS_PER_TICK


def test_poll_ozon_reviews_logs_an_error_and_continues_when_the_llm_fails(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    ozon = FakeOzon(reviews=[
        {"id": "bad", "sku": 111, "text": "boom", "rating": 1},
        {"id": "good", "sku": 111, "text": "ok", "rating": 5},
    ])
    telegram = FakeTelegram()
    llm = FakeLLM(reply="ok", fail_for={"boom"})

    _poll_ozon_reviews(ozon=ozon, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert len(telegram.messages) == 1  # only the good one got drafted
    kinds = [e["kind"] for e in log.recent()]
    assert "draft_error" in kinds
    assert "draft_sent" in kinds


def test_poll_ozon_reviews_auto_marks_a_textless_review_processed_without_drafting(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    ozon = FakeOzon(reviews=[{"id": "r1", "sku": 111, "text": "", "rating": 5}])  # rating-only, no text
    telegram = FakeTelegram()
    llm = FakeLLM()

    _poll_ozon_reviews(ozon=ozon, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert telegram.messages == []  # never sent for approval
    assert llm.calls == []  # never asked to draft
    assert ozon.marked_processed == ["r1"]
    entries = log.recent()
    assert len(entries) == 1
    assert entries[0]["kind"] == "auto_processed"
    assert entries[0]["external_id"] == "ozon:review:r1"


def test_poll_ozon_reviews_does_not_retry_a_review_already_auto_processed(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    log.add("auto_processed", "success", "already handled", "ozon:review:r1")
    ozon = FakeOzon(reviews=[{"id": "r1", "sku": 111, "text": "", "rating": 5}])
    telegram = FakeTelegram()
    llm = FakeLLM()

    _poll_ozon_reviews(ozon=ozon, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert ozon.marked_processed == []  # not re-marked
    assert telegram.messages == []


def test_poll_ozon_reviews_ignores_reviews_received_before_the_cutoff(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    ozon = FakeOzon(reviews=[
        {"id": "old", "sku": 111, "text": "Было давно", "rating": 5, "published_at": "2026-10-13T12:00:00+03:00"},
        {"id": "new", "sku": 111, "text": "Недавно", "rating": 5, "published_at": "2026-10-14T01:00:00+03:00"},
    ])
    telegram = FakeTelegram()
    llm = FakeLLM(reply="Спасибо!")

    _poll_ozon_reviews(ozon=ozon, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert len(telegram.messages) == 1  # only the one on/after the cutoff
    entries = log.recent()
    assert {e["external_id"] for e in entries} == {"ozon:review:new"}
    assert ozon.marked_processed == []  # the old one is just ignored, not marked processed either


# --- _poll_yandex_reviews / _poll_yandex_questions ------------------------

def test_poll_yandex_reviews_resolves_product_name_from_moysklad_and_combines_text(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    yandex = FakeYandex(feedbacks=[{
        "feedbackId": 42,
        "identifiers": {"offerId": "MG2445e"},
        "description": {"comment": "Хорошее качество", "advantages": "яркие цвета"},
        "statistics": {"rating": 5},
        "createdAt": "2026-10-15T00:47:09.951+03:00",
    }])
    moysklad = FakeMoySklad(products={"MG2445e": {"name": "Towards desires"}})
    telegram = FakeTelegram()
    llm = FakeLLM(reply="Спасибо за тёплый отзыв!")

    _poll_yandex_reviews(yandex=yandex, moysklad=moysklad, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert len(telegram.messages) == 1
    assert "Towards desires" in telegram.messages[0]["text"]
    assert "Хорошее качество" in llm.calls[0]
    assert "яркие цвета" in llm.calls[0]
    assert "Получено: 15.10.2026 00:47 (мск)" in telegram.messages[0]["text"]
    entries = log.recent()
    assert entries[0]["external_id"] == "yandex_market:review:42"


def test_poll_yandex_reviews_ignores_feedback_received_before_the_cutoff(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    yandex = FakeYandex(feedbacks=[
        {"feedbackId": 1, "identifiers": {"offerId": "X"}, "description": {"comment": "давно"}, "statistics": {"rating": 5}, "createdAt": "2026-10-13T12:00:00+03:00"},
        {"feedbackId": 2, "identifiers": {"offerId": "X"}, "description": {"comment": "недавно"}, "statistics": {"rating": 5}, "createdAt": "2026-10-14T01:00:00+03:00"},
    ])
    moysklad = FakeMoySklad()
    telegram = FakeTelegram()
    llm = FakeLLM(reply="Спасибо!")

    _poll_yandex_reviews(yandex=yandex, moysklad=moysklad, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    entries = log.recent()
    assert {e["external_id"] for e in entries} == {"yandex_market:review:2"}


def test_poll_yandex_questions_falls_back_to_offer_id_when_product_unknown(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    yandex = FakeYandex(questions=[{"questionIdentifiers": {"id": 7, "offerId": "UNKNOWN1"}, "text": "Какой размер?"}])
    moysklad = FakeMoySklad(products={})  # nothing resolves
    telegram = FakeTelegram()
    llm = FakeLLM(reply="Уточните в описании товара.")

    _poll_yandex_questions(yandex=yandex, moysklad=moysklad, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert "UNKNOWN1" in telegram.messages[0]["text"]
    entries = log.recent()
    assert entries[0]["external_id"] == "yandex_market:question:7"
    payload = json.loads(entries[0]["payload"])
    assert payload["kind"] == "question"
    assert payload["rating"] is None


def test_poll_ozon_reviews_omits_the_received_line_when_no_timestamp_is_given(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    ozon = FakeOzon(reviews=[{"id": "r1", "sku": 111, "text": "ok", "rating": 4}])  # no published_at
    telegram = FakeTelegram()
    llm = FakeLLM(reply="Спасибо!")

    _poll_ozon_reviews(ozon=ozon, log=log, llm=llm, telegram=telegram, chat_id="-100999", errors=errors)

    assert "Получено" not in telegram.messages[0]["text"]


# --- _format_received_at ----------------------------------------------------

def test_format_received_at_converts_utc_z_suffix_to_moscow_time():
    assert _format_received_at("2026-10-14T02:55:35.014683Z") == "14.10.2026 05:55"


def test_format_received_at_handles_an_explicit_offset():
    assert _format_received_at("2026-10-07T00:47:09.951+03:00") == "07.10.2026 00:47"


def test_format_received_at_returns_none_for_missing_or_unparsable_input():
    assert _format_received_at(None) is None
    assert _format_received_at("") is None
    assert _format_received_at("not-a-date") is None


# --- _find_pending_draft / _handle_reply ----------------------------------

def _seed_draft(log, *, external_id, message_id, platform="ozon", kind="review", item_id="r1", draft_text="Спасибо!"):
    log.add("draft_sent", "success", "drafted", external_id, {
        "platform": platform, "kind": kind, "item_id": item_id, "product_code": "SKU1", "product_name": "Товар",
        "customer_text": "текст", "rating": 5, "draft_text": draft_text, "message_id": message_id,
    })


def test_find_pending_draft_matches_by_message_id(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501)
    draft = _find_pending_draft(log, 501)
    assert draft is not None
    assert draft["external_id"] == "ozon:review:r1"
    assert draft["draft_text"] == "Спасибо!"


def test_find_pending_draft_returns_none_when_already_posted(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501)
    log.add("posted", "success", "posted", "ozon:review:r1", {"final_text": "Спасибо!"})
    assert _find_pending_draft(log, 501) is None


def test_find_pending_draft_returns_none_when_already_declined(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501)
    log.add("declined", "success", "declined", "ozon:review:r1")
    assert _find_pending_draft(log, 501) is None


def test_find_pending_draft_returns_none_for_unknown_message(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501)
    assert _find_pending_draft(log, 999) is None


def test_handle_reply_approves_and_posts_the_draft_as_is(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501, draft_text="Спасибо за отзыв!")
    ozon = FakeOzon()
    telegram = FakeTelegram()
    message = {"text": "да", "message_id": 600, "reply_to_message": {"message_id": 501}}

    _handle_reply(message, log=log, telegram=telegram, chat_id="-100999", ozon=ozon, yandex=None)

    assert ozon.comments_created == [{"review_id": "r1", "text": "Спасибо за отзыв!"}]
    kinds = [e["kind"] for e in log.recent()]
    assert "posted" in kinds
    assert "Опубликовано" in telegram.messages[0]["text"]


def test_handle_reply_posts_the_staff_edited_text_instead_of_the_draft(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501, draft_text="Черновик")
    ozon = FakeOzon()
    telegram = FakeTelegram()
    message = {"text": "Спасибо, напишите нам в поддержку.", "message_id": 600, "reply_to_message": {"message_id": 501}}

    _handle_reply(message, log=log, telegram=telegram, chat_id="-100999", ozon=ozon, yandex=None)

    assert ozon.comments_created == [{"review_id": "r1", "text": "Спасибо, напишите нам в поддержку."}]
    entries = [e for e in log.recent() if e["kind"] == "posted"]
    payload = json.loads(entries[0]["payload"])
    assert payload["edited"] is True


def test_handle_reply_declines_without_posting(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501)
    ozon = FakeOzon()
    telegram = FakeTelegram()
    message = {"text": "нет", "message_id": 600, "reply_to_message": {"message_id": 501}}

    _handle_reply(message, log=log, telegram=telegram, chat_id="-100999", ozon=ozon, yandex=None)

    assert ozon.comments_created == []
    kinds = [e["kind"] for e in log.recent()]
    assert "declined" in kinds
    assert "posted" not in kinds


def test_handle_reply_routes_yandex_market_question_to_answer_question(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="yandex_market:question:7", message_id=501, platform="yandex_market", kind="question", item_id="7", draft_text="Размер 40x50 см.")
    yandex = FakeYandex()
    telegram = FakeTelegram()
    message = {"text": "да", "message_id": 600, "reply_to_message": {"message_id": 501}}

    _handle_reply(message, log=log, telegram=telegram, chat_id="-100999", ozon=None, yandex=yandex)

    assert yandex.question_answers == [{"question_id": 7, "text": "Размер 40x50 см."}]


def test_handle_reply_falls_back_to_mark_processed_when_ozon_rejects_an_empty_review(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501, draft_text="Спасибо за отзыв!")
    ozon = FakeOzon(empty_review_error_for={"r1"})
    telegram = FakeTelegram()
    message = {"text": "да", "message_id": 600, "reply_to_message": {"message_id": 501}}

    _handle_reply(message, log=log, telegram=telegram, chat_id="-100999", ozon=ozon, yandex=None)

    assert ozon.comments_created == []  # never actually commented
    assert ozon.marked_processed == ["r1"]
    kinds = [e["kind"] for e in log.recent()]
    assert "posted" in kinds  # still recorded as resolved, not an error
    assert "post_error" not in kinds
    assert "помет" in telegram.messages[0]["text"]  # distinct from the plain "Опубликовано ✅"


def test_handle_reply_reports_a_post_error_without_marking_posted(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501)
    telegram = FakeTelegram()
    message = {"text": "да", "message_id": 600, "reply_to_message": {"message_id": 501}}

    _handle_reply(message, log=log, telegram=telegram, chat_id="-100999", ozon=None, yandex=None)  # ozon not configured -> raises

    kinds = [e["kind"] for e in log.recent()]
    assert "post_error" in kinds
    assert "posted" not in kinds
    assert "Не удалось опубликовать" in telegram.messages[0]["text"]


def test_handle_reply_ignores_a_reply_to_an_unrelated_message(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    telegram = FakeTelegram()
    message = {"text": "да", "message_id": 600, "reply_to_message": {"message_id": 12345}}

    _handle_reply(message, log=log, telegram=telegram, chat_id="-100999", ozon=None, yandex=None)

    assert telegram.messages == []
    assert log.recent() == []


def test_handle_reply_ignores_a_duplicate_reply_after_already_posted(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    _seed_draft(log, external_id="ozon:review:r1", message_id=501)
    log.add("posted", "success", "posted", "ozon:review:r1", {"final_text": "Спасибо!"})
    ozon = FakeOzon()
    telegram = FakeTelegram()
    message = {"text": "да", "message_id": 601, "reply_to_message": {"message_id": 501}}

    _handle_reply(message, log=log, telegram=telegram, chat_id="-100999", ozon=ozon, yandex=None)

    assert ozon.comments_created == []  # not posted again
    assert telegram.messages == []


# --- _is_relevant ----------------------------------------------------------

def test_is_relevant_accepts_a_human_message_in_the_reviews_chat():
    message = {"chat": {"id": -100999}, "from": {"is_bot": False}}
    assert _is_relevant(message, FakeSettings()) is True


def test_is_relevant_rejects_messages_from_bots():
    message = {"chat": {"id": -100999}, "from": {"is_bot": True}}
    assert _is_relevant(message, FakeSettings()) is False


def test_is_relevant_rejects_another_chat():
    message = {"chat": {"id": -1}, "from": {"is_bot": False}}
    assert _is_relevant(message, FakeSettings()) is False


# --- poll_once / reply_run_once wiring --------------------------------------

def test_poll_once_does_nothing_without_a_yandexgpt_key(tmp_path, monkeypatch):
    settings = FakeSettings()
    settings.yandexgpt_api_key = ""
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))

    def _boom(**kwargs):
        raise AssertionError("no client should be constructed")

    _install_fakes(monkeypatch, telegram=None)
    import sync_service.review_worker as mod
    monkeypatch.setattr(mod, "OzonClient", _boom)
    monkeypatch.setattr(mod, "YandexMarketClient", _boom)
    monkeypatch.setattr(mod, "YandexGPTClient", _boom)

    poll_once(settings, log, errors)  # must return early, not raise


def test_poll_once_drafts_from_both_platforms(tmp_path, monkeypatch):
    settings = FakeSettings()
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    ozon = FakeOzon(reviews=[{"id": "r1", "sku": 1, "text": "ok", "rating": 5}])
    yandex = FakeYandex(feedbacks=[{"feedbackId": 1, "identifiers": {"offerId": "X"}, "description": {}, "statistics": {"rating": 4}}])
    moysklad = FakeMoySklad()
    telegram = FakeTelegram()
    llm = FakeLLM()
    _install_fakes(monkeypatch, telegram=telegram, ozon=ozon, yandex=yandex, moysklad=moysklad, llm=llm)

    poll_once(settings, log, errors)

    kinds_and_ids = {(e["kind"], e["external_id"]) for e in log.recent()}
    assert ("draft_sent", "ozon:review:r1") in kinds_and_ids
    assert ("draft_sent", "yandex_market:review:1") in kinds_and_ids


def test_reply_run_once_advances_the_offset_even_on_an_irrelevant_message(tmp_path, monkeypatch):
    from sync_service.telegram_qa_worker import UpdateOffset

    settings = FakeSettings()
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    errors = ErrorLog(str(tmp_path / "errors.sqlite3"))
    offset_store = UpdateOffset(str(tmp_path / "offset.sqlite3"))
    telegram = FakeTelegram()
    telegram._updates = [{"update_id": 77, "message": {"chat": {"id": -1}, "from": {"is_bot": False}, "text": "hi"}}]
    _install_fakes(monkeypatch, telegram=telegram)

    reply_run_once(settings, offset_store, log, errors)

    assert offset_store.get() == 77


# --- _draft_question / handle_new_question (QUESTION_CREATED fast path) ----

def test_draft_question_returns_false_without_drafting_when_already_sent(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    log.add("draft_sent", "success", "already drafted", "yandex_market:question:7", {"message_id": 1})
    question = {"questionIdentifiers": {"id": 7, "offerId": "X"}, "text": "Какой размер?"}
    moysklad = FakeMoySklad()
    telegram = FakeTelegram()
    llm = FakeLLM()

    sent = _draft_question(question, moysklad=moysklad, log=log, llm=llm, telegram=telegram, chat_id="-100999")

    assert sent is False
    assert telegram.messages == []


def test_draft_question_drafts_and_returns_true_for_a_new_question(tmp_path):
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    question = {"questionIdentifiers": {"id": 8, "offerId": "MG2445e"}, "text": "Какой размер?"}
    moysklad = FakeMoySklad(products={"MG2445e": {"name": "Towards desires"}})
    telegram = FakeTelegram()
    llm = FakeLLM(reply="Размер 40x50 см.")

    sent = _draft_question(question, moysklad=moysklad, log=log, llm=llm, telegram=telegram, chat_id="-100999")

    assert sent is True
    assert len(telegram.messages) == 1
    assert "Towards desires" in telegram.messages[0]["text"]
    assert log.recent()[0]["external_id"] == "yandex_market:question:8"


def test_handle_new_question_fetches_by_id_and_drafts(tmp_path, monkeypatch):
    settings = FakeSettings()
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    question = {"questionIdentifiers": {"id": 9, "offerId": "MG2445e"}, "text": "Какой размер?"}
    yandex = FakeYandex(question_by_id_result=question)
    moysklad = FakeMoySklad(products={"MG2445e": {"name": "Towards desires"}})
    telegram = FakeTelegram()
    llm = FakeLLM(reply="Размер 40x50 см.")
    _install_fakes(monkeypatch, telegram=telegram, yandex=yandex, moysklad=moysklad, llm=llm)

    handle_new_question(question_id=9, settings=settings, log=log)

    assert yandex.requested_question_id == 9
    assert len(telegram.messages) == 1
    assert log.recent()[0]["external_id"] == "yandex_market:question:9"


def test_handle_new_question_does_nothing_when_the_question_is_gone(tmp_path, monkeypatch):
    settings = FakeSettings()
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))
    yandex = FakeYandex(question_by_id_result=None)
    telegram = FakeTelegram()
    _install_fakes(monkeypatch, telegram=telegram, yandex=yandex, moysklad=FakeMoySklad(), llm=FakeLLM())

    handle_new_question(question_id=404, settings=settings, log=log)  # must not raise

    assert telegram.messages == []
    assert log.recent() == []


def test_handle_new_question_does_nothing_without_reviews_bot_configured(tmp_path, monkeypatch):
    settings = FakeSettings()
    settings.telegram_reviews_bot_token = ""
    log = YandexMarketSyncLog(str(tmp_path / "reviews.sqlite3"))

    def _boom(**kwargs):
        raise AssertionError("no client should be constructed")

    import sync_service.review_worker as mod
    monkeypatch.setattr(mod, "YandexMarketClient", _boom)
    monkeypatch.setattr(mod, "MoySkladClient", _boom)
    monkeypatch.setattr(mod, "YandexGPTClient", _boom)
    monkeypatch.setattr(mod, "TelegramClient", _boom)

    handle_new_question(question_id=1, settings=settings, log=log)  # must return early, not raise
