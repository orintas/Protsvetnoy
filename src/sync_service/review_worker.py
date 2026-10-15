from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .error_log import ErrorLog
from .http import ApiError
from .moysklad import MoySkladClient
from .ozon_client import OzonClient
from .review_assistant import MAX_REPLY_CHARS, canned_review_reply, generate_question_reply, generate_review_reply
from .telegram_client import TelegramClient
from .telegram_qa_worker import UpdateOffset
from .yandex_gpt_client import YandexGPTClient
from .yandex_market import YandexMarketClient
from .yandex_market_sync import YandexMarketSyncLog

MOSCOW = ZoneInfo("Europe/Moscow")
POLL_START_HOUR = 9
POLL_END_HOUR = 22
POLL_INTERVAL_SECONDS = 1800  # 30 min, same cadence as Shopify's stock sync

# A seller backlog can run into the thousands (seen live: 1857 unprocessed
# OZON reviews on this account) — drafting and sending all of them in one
# tick would flood the approval chat and burn the LLM/Telegram quota
# instantly. Each poll only drafts this many *new* items per
# platform/kind, so a large backlog drains gradually over many ticks
# instead of landing as one wall of messages.
MAX_DRAFTS_PER_TICK = 20

POLL_REPLY_TIMEOUT_SECONDS = 25

APPROVE_WORDS = {"да", "ok", "ок", "окей", "+", "хорошо", "норм", "норм.", "давай", "ага"}
DECLINE_WORDS = {"нет", "-", "отмена", "пропустить", "skip", "не надо", "нет."}

PLATFORM_LABELS = {"ozon": "OZON", "yandex_market": "Я.Маркет"}
KIND_LABELS = {"review": "Отзыв", "question": "Вопрос"}

# Status markers set directly on the draft message itself (not the reply
# underneath it) once it's resolved, so it's visible at a glance in the
# chat which drafts are still pending vs already handled — and how. ✅/❌
# aren't in Telegram's fixed set of reaction emoji, hence this choice.
POSTED_REACTION = "👌"
DECLINED_REACTION = "👎"
ERROR_REACTION = "😢"


def _ratings_stars(rating: int | None) -> str:
    if not rating:
        return ""
    return "★" * rating + "☆" * (5 - rating)


# Switching this service on surfaced OZON's entire historical backlog (seen
# live: 1857 unprocessed reviews, some from long before this service
# existed) in the very first poll — drafting/sending all of those would
# flood the approval chat with reviews nobody is going to react to any
# more. Only reviews/feedback received on or after this cutoff get drafted;
# anything older is left alone (not drafted, not marked processed — simply
# ignored by this service, same as before it existed).
REVIEW_CUTOFF = datetime(2026, 10, 14, tzinfo=MOSCOW)


def _is_before_review_cutoff(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt < REVIEW_CUTOFF


def _format_received_at(raw: str | None) -> str | None:
    """Formats a platform's own timestamp (OZON's published_at, Yandex
    Market's createdAt) as a Moscow-time "дд.мм.гггг чч:мм" string. Both
    platforms give ISO-8601 with either a "Z" suffix or an explicit offset
    (e.g. "2026-10-14T02:55:35.014683Z", "2026-10-07T00:47:09.951+03:00") —
    datetime.fromisoformat handles the offset form natively but not a bare
    "Z" on the Python versions this project has run on before, hence the
    replace. Returns None on anything unparsable rather than raising, since
    this is just a display nicety."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")


def _format_draft_message(*, platform: str, kind: str, product_name: str, product_code: str, customer_text: str, rating: int | None, draft: str, received_at: str | None) -> str:
    header = f"{PLATFORM_LABELS.get(platform, platform)} · {KIND_LABELS.get(kind, kind)}"
    if kind == "review" and rating:
        header += f" · {_ratings_stars(rating)} ({rating})"
    lines = [header]
    formatted_received_at = _format_received_at(received_at)
    if formatted_received_at:
        lines.append(f"Получено: {formatted_received_at} (мск)")
    lines.append(f"Товар: {product_name} ({product_code})")
    lines.append("")
    label = "Отзыв" if kind == "review" else "Вопрос"
    lines.append(f"{label}: «{customer_text or '(без текста)'}»")
    lines.append("")
    lines.append("Черновик ответа:")
    lines.append(draft)
    lines.append("")
    lines.append('Ответьте «да» или поставьте 👍 на это сообщение — опубликовать как есть; своим текстом — опубликовать с правкой; «нет» — пропустить.')
    return "\n".join(lines)


def _already_drafted(log: YandexMarketSyncLog, external_id: str) -> bool:
    return log.has_success("draft_sent", external_id) or log.has_success("auto_processed", external_id)


def _draft_and_send(*, log: YandexMarketSyncLog, telegram: TelegramClient, chat_id: str, external_id: str, platform: str, kind: str, item_id: str, product_code: str, product_name: str, customer_text: str, rating: int | None, draft: str, received_at: str | None = None) -> None:
    message = _format_draft_message(platform=platform, kind=kind, product_name=product_name, product_code=product_code, customer_text=customer_text, rating=rating, draft=draft, received_at=received_at)
    sent = telegram.send_message(chat_id=chat_id, text=message)
    log.add(
        "draft_sent", "success",
        f"{PLATFORM_LABELS.get(platform, platform)} {KIND_LABELS.get(kind, kind).lower()} {item_id} ({product_name}): черновик отправлен на согласование",
        external_id,
        {
            "platform": platform, "kind": kind, "item_id": item_id, "product_code": product_code, "product_name": product_name,
            "customer_text": customer_text, "rating": rating, "draft_text": draft, "message_id": sent["result"]["message_id"],
            "received_at": received_at,
        },
    )


def _poll_ozon_reviews(*, ozon: OzonClient, log: YandexMarketSyncLog, llm: YandexGPTClient, telegram: TelegramClient, chat_id: str, errors: ErrorLog) -> None:
    reviews = ozon.unprocessed_reviews()
    reviews = [r for r in reviews if not _is_before_review_cutoff(r.get("published_at"))]
    pending = [r for r in reviews if not _already_drafted(log, f"ozon:review:{r['id']}")][:MAX_DRAFTS_PER_TICK]
    if not pending:
        return
    names = ozon.product_names_by_sku(sorted({r["sku"] for r in pending}))
    for review in pending:
        item_id = str(review["id"])
        external_id = f"ozon:review:{item_id}"
        product = names.get(review.get("sku")) or {}
        product_name = product.get("name") or f"SKU {review.get('sku')}"
        product_code = product.get("offer_id") or str(review.get("sku") or "")
        review_text = review.get("text") or ""
        rating = review.get("rating")
        received_at = review.get("published_at")
        if not review_text.strip():
            # OZON rejects create_review_comment outright for a review with
            # no text (just a star rating) — nothing to reply to, so clear
            # it from the queue via change-status instead of drafting.
            try:
                ozon.mark_review_processed(item_id)
                log.add("auto_processed", "success", f"OZON отзыв {item_id} ({product_name}): без текста, помечен обработанным автоматически", external_id)
            except Exception as error:
                errors.log_exception("review_worker", error, context=f"OZON отзыв {item_id}: не удалось автоматически пометить обработанным")
                log.add("draft_error", "error", f"OZON отзыв {item_id}: ошибка автообработки пустого отзыва: {error}", external_id)
            continue
        try:
            draft = generate_review_reply(client=llm, platform="ozon", product_name=product_name, rating=rating, review_text=review_text)
            _draft_and_send(log=log, telegram=telegram, chat_id=chat_id, external_id=external_id, platform="ozon", kind="review", item_id=item_id, product_code=product_code, product_name=product_name, customer_text=review_text, rating=rating, draft=draft, received_at=received_at)
        except Exception as error:
            errors.log_exception("review_worker", error, context=f"OZON отзыв {item_id}: не удалось подготовить черновик")
            log.add("draft_error", "error", f"OZON отзыв {item_id}: ошибка подготовки черновика: {error}", external_id)


def _poll_yandex_reviews(*, yandex: YandexMarketClient, moysklad: MoySkladClient, log: YandexMarketSyncLog, llm: YandexGPTClient, telegram: TelegramClient, chat_id: str, errors: ErrorLog) -> None:
    feedbacks = yandex.feedbacks_needing_reaction()
    feedbacks = [f for f in feedbacks if not _is_before_review_cutoff(f.get("createdAt"))]
    pending = [f for f in feedbacks if not _already_drafted(log, f"yandex_market:review:{f['feedbackId']}")][:MAX_DRAFTS_PER_TICK]
    for feedback in pending:
        item_id = str(feedback["feedbackId"])
        external_id = f"yandex_market:review:{item_id}"
        product_code = str((feedback.get("identifiers") or {}).get("offerId") or "")
        product_name = _product_name(moysklad, product_code) or product_code or "(товар неизвестен)"
        description = feedback.get("description") or {}
        review_text = description.get("comment") or ""
        parts = [p for p in (description.get("advantages"), description.get("disadvantages")) if p]
        if parts:
            review_text = (review_text + "\n" + " / ".join(parts)).strip()
        rating = (feedback.get("statistics") or {}).get("rating")
        received_at = feedback.get("createdAt")
        try:
            if review_text.strip():
                draft = generate_review_reply(client=llm, platform="yandex_market", product_name=product_name, rating=rating, review_text=review_text)
            else:
                draft = canned_review_reply(rating=rating)  # no customer text — skip the LLM call entirely
            _draft_and_send(log=log, telegram=telegram, chat_id=chat_id, external_id=external_id, platform="yandex_market", kind="review", item_id=item_id, product_code=product_code, product_name=product_name, customer_text=review_text, rating=rating, draft=draft, received_at=received_at)
        except Exception as error:
            errors.log_exception("review_worker", error, context=f"Я.Маркет отзыв {item_id}: не удалось подготовить черновик")
            log.add("draft_error", "error", f"Я.Маркет отзыв {item_id}: ошибка подготовки черновика: {error}", external_id)


def _draft_question(question: dict[str, Any], *, moysklad: MoySkladClient, log: YandexMarketSyncLog, llm: YandexGPTClient, telegram: TelegramClient, chat_id: str) -> bool:
    """Drafts and sends one question to the approval chat, unless it's
    already been drafted (returns False in that case). Shared by the batch
    poller and the QUESTION_CREATED webhook fast path — raises on failure;
    the poller catches and logs per item so one bad question doesn't block
    the rest, while the webhook path lets it propagate so Yandex Market
    retries the notification (safe: drafting is idempotent via the same
    draft_sent dedup either path uses)."""
    identifiers = question.get("questionIdentifiers") or {}
    item_id = str(identifiers.get("id"))
    external_id = f"yandex_market:question:{item_id}"
    if _already_drafted(log, external_id):
        return False
    product_code = str(identifiers.get("offerId") or "")
    product_name = _product_name(moysklad, product_code) or product_code or "(товар неизвестен)"
    question_text = question.get("text") or ""
    received_at = question.get("createdAt")
    draft = generate_question_reply(client=llm, platform="yandex_market", product_name=product_name, question_text=question_text)
    _draft_and_send(log=log, telegram=telegram, chat_id=chat_id, external_id=external_id, platform="yandex_market", kind="question", item_id=item_id, product_code=product_code, product_name=product_name, customer_text=question_text, rating=None, draft=draft, received_at=received_at)
    return True


def _poll_yandex_questions(*, yandex: YandexMarketClient, moysklad: MoySkladClient, log: YandexMarketSyncLog, llm: YandexGPTClient, telegram: TelegramClient, chat_id: str, errors: ErrorLog) -> None:
    questions = yandex.unanswered_questions()
    pending = [q for q in questions if not _already_drafted(log, f"yandex_market:question:{q['questionIdentifiers']['id']}")][:MAX_DRAFTS_PER_TICK]
    for question in pending:
        item_id = str((question.get("questionIdentifiers") or {}).get("id"))
        try:
            _draft_question(question, moysklad=moysklad, log=log, llm=llm, telegram=telegram, chat_id=chat_id)
        except Exception as error:
            errors.log_exception("review_worker", error, context=f"Я.Маркет вопрос {item_id}: не удалось подготовить черновик")
            log.add("draft_error", "error", f"Я.Маркет вопрос {item_id}: ошибка подготовки черновика: {error}", f"yandex_market:question:{item_id}")


def handle_new_question(*, question_id: int, settings: Settings, log: YandexMarketSyncLog) -> None:
    """Fast path for a QUESTION_CREATED push notification (see
    web._yandex_market_webhook / _handle_new_question): drafts and sends
    this one question immediately instead of waiting for the next ~30-min
    poll tick. Raises on failure so the webhook's own handler can log it and
    answer 500 (Yandex Market retries) — mirrors _handle_new_order's
    contract for the same reason."""
    if not settings.yandexgpt_api_key or not settings.yandexgpt_folder_id or not settings.telegram_reviews_bot_token or not settings.telegram_reviews_chat_id:
        return
    yandex = YandexMarketClient(base_url=settings.yandex_market_base_url, api_key=settings.yandex_market_api_key, business_id=settings.yandex_market_business_id)
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    llm = YandexGPTClient(api_key=settings.yandexgpt_api_key, folder_id=settings.yandexgpt_folder_id, model=settings.yandexgpt_model)
    telegram = TelegramClient(bot_token=settings.telegram_reviews_bot_token, proxy=settings.telegram_proxy_url)
    try:
        question = yandex.question_by_id(question_id)
        if question is None:
            return  # gone/unavailable by the time we looked it up
        _draft_question(question, moysklad=moysklad, log=log, llm=llm, telegram=telegram, chat_id=settings.telegram_reviews_chat_id)
    finally:
        yandex.close()
        moysklad.close()
        llm.close()
        telegram.close()


def _product_name(moysklad: MoySkladClient, code: str) -> str | None:
    if not code:
        return None
    try:
        product = moysklad.product_by_code(code)
    except Exception:
        return None
    return (product or {}).get("name")


def _within_poll_window(now: datetime | None = None) -> bool:
    local = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    return POLL_START_HOUR <= local.hour < POLL_END_HOUR


def poll_once(settings: Settings, log: YandexMarketSyncLog, errors: ErrorLog) -> None:
    if not settings.yandexgpt_api_key or not settings.yandexgpt_folder_id or not settings.telegram_reviews_bot_token or not settings.telegram_reviews_chat_id:
        return
    llm = YandexGPTClient(api_key=settings.yandexgpt_api_key, folder_id=settings.yandexgpt_folder_id, model=settings.yandexgpt_model)
    telegram = TelegramClient(bot_token=settings.telegram_reviews_bot_token, proxy=settings.telegram_proxy_url)
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    try:
        if settings.ozon_client_id and settings.ozon_api_key:
            ozon = OzonClient(client_id=settings.ozon_client_id, api_key=settings.ozon_api_key)
            try:
                _poll_ozon_reviews(ozon=ozon, log=log, llm=llm, telegram=telegram, chat_id=settings.telegram_reviews_chat_id, errors=errors)
            finally:
                ozon.close()
        if settings.yandex_market_api_key and settings.yandex_market_business_id:
            yandex = YandexMarketClient(base_url=settings.yandex_market_base_url, api_key=settings.yandex_market_api_key, business_id=settings.yandex_market_business_id)
            try:
                _poll_yandex_reviews(yandex=yandex, moysklad=moysklad, log=log, llm=llm, telegram=telegram, chat_id=settings.telegram_reviews_chat_id, errors=errors)
                _poll_yandex_questions(yandex=yandex, moysklad=moysklad, log=log, llm=llm, telegram=telegram, chat_id=settings.telegram_reviews_chat_id, errors=errors)
            finally:
                yandex.close()
    finally:
        llm.close()
        telegram.close()
        moysklad.close()


def poll_worker() -> None:
    settings = Settings.from_env()
    log = YandexMarketSyncLog("data/review_sync.sqlite3")
    errors = ErrorLog()
    while True:
        if _within_poll_window():
            try:
                poll_once(settings, log, errors)
            except Exception as error:
                errors.log_exception("review_worker", error, context="Ошибка опроса отзывов/вопросов")
        time.sleep(POLL_INTERVAL_SECONDS)


def _is_relevant(message: dict[str, Any], settings: Settings) -> bool:
    if not isinstance(message, dict):
        return False
    if bool((message.get("from") or {}).get("is_bot")):
        return False
    chat_id = str((message.get("chat") or {}).get("id", ""))
    return bool(settings.telegram_reviews_chat_id) and chat_id == str(settings.telegram_reviews_chat_id)


def _is_relevant_reaction(reaction: dict[str, Any], settings: Settings) -> bool:
    if not isinstance(reaction, dict):
        return False
    # Telegram never sends a message_reaction update for a bot's own
    # reaction, so there's no is_bot check to make here (unlike _is_relevant).
    chat_id = str((reaction.get("chat") or {}).get("id", ""))
    return bool(settings.telegram_reviews_chat_id) and chat_id == str(settings.telegram_reviews_chat_id)


APPROVE_EMOJI = "👍"


def _has_thumbs_up(reactions: list[dict[str, Any]] | None) -> bool:
    return any(isinstance(r, dict) and r.get("type") == "emoji" and r.get("emoji") == APPROVE_EMOJI for r in (reactions or []))


def _find_pending_draft(log: YandexMarketSyncLog, message_id: int) -> dict[str, Any] | None:
    """Reverse lookup, same shape as YandexMarketSyncLog.find_by_label_message_id
    but scoped to kind='draft_sent' and across every platform/kind this
    worker handles — a full scan, same tradeoff as that method (this table
    only grows one row per review/question ever drafted)."""
    for entry in log.recent(limit=5000, days=14):
        if entry.get("kind") != "draft_sent":
            continue
        try:
            payload = json.loads(entry.get("payload") or "{}")
        except (TypeError, ValueError):
            continue
        if payload.get("message_id") != message_id:
            continue
        external_id = entry.get("external_id")
        if not external_id or log.has_success("posted", external_id) or log.has_success("declined", external_id):
            return None  # already resolved — don't act on a late/duplicate reply
        return {"external_id": external_id, **payload}


def _post_reply(*, platform: str, kind: str, item_id: str, text: str, ozon: OzonClient | None, yandex: YandexMarketClient | None) -> str:
    if platform == "ozon":
        if ozon is None:
            raise RuntimeError("OZON не настроен")
        try:
            ozon.create_review_comment(review_id=item_id, text=text)
        except ApiError as error:
            # A draft can get stuck pointing at a review that turned out to
            # have no text (e.g. one drafted before the poll-time skip
            # above existed) — OZON rejects a comment on it outright.
            # Clear it the same way the poller now does instead of erroring.
            if "cannot comment on empty review" not in str(error):
                raise
            ozon.mark_review_processed(item_id)
            return "Отзыв без текста — публиковать ответ нельзя, просто пометил обработанным ✅"
        return "Опубликовано ✅"
    elif platform == "yandex_market" and kind == "review":
        if yandex is None:
            raise RuntimeError("Яндекс.Маркет не настроен")
        yandex.comment_on_feedback(feedback_id=int(item_id), text=text)
    elif platform == "yandex_market" and kind == "question":
        if yandex is None:
            raise RuntimeError("Яндекс.Маркет не настроен")
        yandex.answer_question(question_id=int(item_id), text=text)
    else:
        raise RuntimeError(f"Неизвестная площадка/тип: {platform}/{kind}")
    return "Опубликовано ✅"


def _approve_and_post(draft: dict[str, Any], *, final_text: str, log: YandexMarketSyncLog, telegram: TelegramClient, chat_id: str, ozon: OzonClient | None, yandex: YandexMarketClient | None, reply_to_message_id: int | None) -> None:
    """Shared tail end of both approval paths (a "да"/text reply, or a 👍
    reaction) once a pending draft and its final text are known: post it,
    log the outcome, and tell the chat what happened."""
    external_id = draft["external_id"]
    label = f"{PLATFORM_LABELS.get(draft['platform'], draft['platform'])} {KIND_LABELS.get(draft['kind'], draft['kind']).lower()} {draft['item_id']}"
    try:
        confirmation = _post_reply(platform=draft["platform"], kind=draft["kind"], item_id=draft["item_id"], text=final_text, ozon=ozon, yandex=yandex)
    except Exception as error:
        log.add("post_error", "error", f"{label}: ошибка публикации: {error}", external_id)
        telegram.send_message(chat_id=chat_id, text=f"Не удалось опубликовать: {error}", reply_to_message_id=reply_to_message_id)
        telegram.set_message_reaction(chat_id=chat_id, message_id=draft["message_id"], emoji=ERROR_REACTION)
        return

    log.add("posted", "success", f"{label}: ответ опубликован", external_id, {"final_text": final_text, "edited": final_text != draft["draft_text"]})
    telegram.send_message(chat_id=chat_id, text=confirmation, reply_to_message_id=reply_to_message_id)
    telegram.set_message_reaction(chat_id=chat_id, message_id=draft["message_id"], emoji=POSTED_REACTION)


def _handle_reply(message: dict[str, Any], *, log: YandexMarketSyncLog, telegram: TelegramClient, chat_id: str, ozon: OzonClient | None, yandex: YandexMarketClient | None) -> None:
    reply_to = message.get("reply_to_message")
    if not isinstance(reply_to, dict) or not reply_to.get("message_id"):
        return
    draft = _find_pending_draft(log, reply_to["message_id"])
    if draft is None:
        return
    reply_text = str(message.get("text") or "").strip()
    lowered = reply_text.lower()

    if lowered in DECLINE_WORDS:
        log.add("declined", "success", f"{PLATFORM_LABELS.get(draft['platform'], draft['platform'])} {KIND_LABELS.get(draft['kind'], draft['kind']).lower()} {draft['item_id']}: отклонено сотрудником", draft["external_id"])
        telegram.send_message(chat_id=chat_id, text="Принято, публиковать не буду.", reply_to_message_id=message.get("message_id"))
        telegram.set_message_reaction(chat_id=chat_id, message_id=draft["message_id"], emoji=DECLINED_REACTION)
        return

    final_text = draft["draft_text"] if (not reply_text or lowered in APPROVE_WORDS) else reply_text
    final_text = final_text[:MAX_REPLY_CHARS]
    _approve_and_post(draft, final_text=final_text, log=log, telegram=telegram, chat_id=chat_id, ozon=ozon, yandex=yandex, reply_to_message_id=message.get("message_id"))


def _handle_reaction(reaction: dict[str, Any], *, log: YandexMarketSyncLog, telegram: TelegramClient, chat_id: str, ozon: OzonClient | None, yandex: YandexMarketClient | None) -> None:
    """A 👍 reaction on a draft message approves it as-is — same effect as
    replying "да", just without typing anything. Any other reaction (or a
    reaction being removed, which also arrives as a message_reaction update
    with no 👍 in new_reaction) is ignored."""
    if not _has_thumbs_up(reaction.get("new_reaction")):
        return
    message_id = reaction.get("message_id")
    if not isinstance(message_id, int):
        return
    draft = _find_pending_draft(log, message_id)
    if draft is None:
        return
    final_text = draft["draft_text"][:MAX_REPLY_CHARS]
    _approve_and_post(draft, final_text=final_text, log=log, telegram=telegram, chat_id=chat_id, ozon=ozon, yandex=yandex, reply_to_message_id=message_id)


def reply_run_once(settings: Settings, offset_store: UpdateOffset, log: YandexMarketSyncLog, errors: ErrorLog) -> None:
    if not settings.telegram_reviews_bot_token or not settings.telegram_reviews_chat_id:
        return
    telegram = TelegramClient(bot_token=settings.telegram_reviews_bot_token, proxy=settings.telegram_proxy_url)
    ozon = OzonClient(client_id=settings.ozon_client_id, api_key=settings.ozon_api_key) if settings.ozon_api_key else None
    yandex = YandexMarketClient(base_url=settings.yandex_market_base_url, api_key=settings.yandex_market_api_key, business_id=settings.yandex_market_business_id) if settings.yandex_market_api_key else None
    try:
        last_offset = offset_store.get()
        updates = telegram.get_updates(offset=(last_offset + 1) if last_offset is not None else None, timeout=POLL_REPLY_TIMEOUT_SECONDS, allowed_updates=["message", "message_reaction"])
        for update in updates:
            message = update.get("message")
            reaction = update.get("message_reaction")
            try:
                if message is not None and _is_relevant(message, settings):
                    _handle_reply(message, log=log, telegram=telegram, chat_id=settings.telegram_reviews_chat_id, ozon=ozon, yandex=yandex)
                elif reaction is not None and _is_relevant_reaction(reaction, settings):
                    _handle_reaction(reaction, log=log, telegram=telegram, chat_id=settings.telegram_reviews_chat_id, ozon=ozon, yandex=yandex)
            except Exception as error:
                errors.log_exception("review_worker", error, context=f"Ошибка обработки ответа Telegram update_id={update.get('update_id')}")
            finally:
                offset_store.set(update["update_id"])
    finally:
        telegram.close()
        if ozon is not None:
            ozon.close()
        if yandex is not None:
            yandex.close()


def reply_worker() -> None:
    settings = Settings.from_env()
    offset_store = UpdateOffset("data/telegram_reviews_offset.sqlite3")
    log = YandexMarketSyncLog("data/review_sync.sqlite3")
    errors = ErrorLog()
    if settings.telegram_reviews_bot_token:
        client = TelegramClient(bot_token=settings.telegram_reviews_bot_token, proxy=settings.telegram_proxy_url)
        try:
            client.delete_webhook()
        except Exception as error:
            errors.log_exception("review_worker", error, context="Не удалось снять регистрацию вебхука Telegram (отзывы)")
        finally:
            client.close()
    while True:
        try:
            reply_run_once(settings, offset_store, log, errors)
        except Exception as error:
            errors.log_exception("review_worker", error, context="Ошибка воркера ответов на отзывы/вопросы")
            time.sleep(5)
