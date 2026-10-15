from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    """Structural type for whichever LLM client is active — matches
    order_assistant.LLMClient's contract so the same YandexGPTClient instance
    can be shared between both modules."""

    def complete(self, *, system: str, user_message: str, max_tokens: int = 1024, image_bytes: bytes | None = None, image_media_type: str = "image/jpeg") -> str: ...


PLATFORM_NAMES = {"ozon": "OZON", "yandex_market": "Яндекс.Маркет"}

# Confirmed limits: OZON review comment text (community-documented, see
# ozon_client.create_review_comment) and Yandex Market's own reference docs
# (goods-feedback comment: 1-4096 chars; goods-questions answer: 1-5000
# chars). Kept with headroom below the real ceiling so a slightly longer
# model reply never gets rejected by the platform instead of just looking
# a little long.
MAX_REPLY_CHARS = 1000

# These are PUBLIC replies — everyone browsing the product page sees them,
# not just the customer who wrote in. That's why the rules below are much
# stricter than order_assistant's internal-staff-chat prompt: no promises of
# concrete resolutions (refunds, replacements, discounts, deadlines) since
# only a human with real authority over the order can make those, and
# nothing invented about the product beyond what's actually been given.
REVIEW_SYSTEM_PROMPT = """Ты пишешь публичный ответ от имени магазина «Varvikas | Цветной» (товары для творчества: картины по номерам, алмазная мозаика, деревянные конструкторы и т.п.) на отзыв покупателя о товаре на маркетплейсе. Этот ответ увидят все посетители карточки товара, а не только автор отзыва.

Тебе передают название товара, оценку (от 1 до 5) и текст отзыва (он может быть пустым — покупатель иногда оставляет только оценку). Напиши короткий ответ, 2-4 предложения, по-русски, от лица команды магазина («мы», «наша команда»).

Правила:
- Оценка 4-5: тепло поблагодари. Если в тексте отзыва что-то конкретное упомянуто — коротко отреагируй на это. Не повторяй один и тот же шаблон из раза в раз, формулируй естественно.
- Оценка 1-3: извинись без оправданий и без спора с покупателем, прояви понимание, и предложи написать в чат поддержки площадки, чтобы разобраться в ситуации. Никогда не придумывай конкретную компенсацию, возврат, замену или сроки — это решает человек, не ты.
- Если текста отзыва нет, только оценка — короткая благодарность без выдумывания деталей, за что именно хвалят.
- Никогда не утверждай, что что-то уже сделано или будет сделано (отправили, вернули деньги, заменили) — ты не можешь ничего изменить в заказе.
- Не упоминай внутренние процессы, сотрудников, склад, не пиши имя покупателя и не копируй его отзыв дословно.
- Обычный текст, без markdown-разметки и заголовков. Эмодзи — не больше одного и только если уместно, не в каждом ответе."""

QUESTION_SYSTEM_PROMPT = """Ты отвечаешь от имени магазина «Varvikas | Цветной» на публичный вопрос покупателя о товаре на маркетплейсе — ответ увидят все посетители карточки товара, не только автор вопроса.

Тебе передают название товара и текст вопроса. Ответь кратко, 1-3 предложения, по-русски, по существу.

Правила:
- Отвечай только если уверен в ответе исходя из названия товара и общих знаний о таких товарах. На фактические вопросы (точный размер, состав, комплектация, страна производства и т.п.) не придумывай цифры и детали, которых не знаешь.
- Если не можешь ответить точно — честно напиши, что это лучше уточнить в описании товара на странице или в чате поддержки. Не гадай и не придумывай правдоподобный, но неподтверждённый ответ.
- Не обещай сроки доставки, наличие на складе или скидки — этого ты не знаешь.
- Обычный текст, без markdown-разметки."""


def generate_review_reply(*, client: LLMClient, platform: str, product_name: str, rating: int | None, review_text: str) -> str:
    context = f"Площадка: {PLATFORM_NAMES.get(platform, platform)}\nТовар: {product_name or '(название неизвестно)'}\nОценка: {rating if rating is not None else '(нет)'}\nТекст отзыва: {review_text.strip() or '(покупатель не оставил текст)'}"
    return client.complete(system=REVIEW_SYSTEM_PROMPT, user_message=context)[:MAX_REPLY_CHARS]


def generate_question_reply(*, client: LLMClient, platform: str, product_name: str, question_text: str) -> str:
    context = f"Площадка: {PLATFORM_NAMES.get(platform, platform)}\nТовар: {product_name or '(название неизвестно)'}\nВопрос: {question_text.strip()}"
    return client.complete(system=QUESTION_SYSTEM_PROMPT, user_message=context)[:MAX_REPLY_CHARS]
