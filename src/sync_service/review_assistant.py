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


# A rating-only review (no text at all) has nothing in it for the LLM to
# actually respond to — the system prompt's own instruction for that case
# ("короткая благодарность без выдумывания деталей") already reduces to a
# fixed template, so skip the API call entirely and use one directly.
_NO_TEXT_REVIEW_REPLY_POSITIVE = "Спасибо за отзыв и оценку! Рады, что вам понравилось — будем стараться и дальше."
_NO_TEXT_REVIEW_REPLY_NEGATIVE = "Жаль, что не всё понравилось. Напишите, пожалуйста, в чат поддержки площадки — обязательно разберёмся в ситуации."


def canned_review_reply(*, rating: int | None) -> str:
    """Non-LLM fallback for a review with no text, just a star rating —
    see the module comment above for why this skips the model entirely."""
    if rating is not None and rating <= 3:
        return _NO_TEXT_REVIEW_REPLY_NEGATIVE
    return _NO_TEXT_REVIEW_REPLY_POSITIVE


def _with_rules(context: str, rules: str) -> str:
    if not rules.strip():
        return context
    return f"Свод правил от сотрудников магазина (обязательно учитывай при ответе):\n{rules.strip()}\n\n{context}"


def generate_review_reply(*, client: LLMClient, platform: str, product_name: str, rating: int | None, review_text: str, rules: str = "") -> str:
    context = f"Площадка: {PLATFORM_NAMES.get(platform, platform)}\nТовар: {product_name or '(название неизвестно)'}\nОценка: {rating if rating is not None else '(нет)'}\nТекст отзыва: {review_text.strip() or '(покупатель не оставил текст)'}"
    context = _with_rules(context, rules)
    return client.complete(system=REVIEW_SYSTEM_PROMPT, user_message=context)[:MAX_REPLY_CHARS]


def generate_question_reply(*, client: LLMClient, platform: str, product_name: str, question_text: str, rules: str = "") -> str:
    context = f"Площадка: {PLATFORM_NAMES.get(platform, platform)}\nТовар: {product_name or '(название неизвестно)'}\nВопрос: {question_text.strip()}"
    context = _with_rules(context, rules)
    return client.complete(system=QUESTION_SYSTEM_PROMPT, user_message=context)[:MAX_REPLY_CHARS]


# Caps how long the rulebook can grow — generous for "a few dozen concise
# bullet points", but prevents an unbounded blob from eating the whole
# prompt budget if the merge/edit step ever goes off the rails.
MAX_RULES_CHARS = 4000

RULES_UPDATE_SYSTEM_PROMPT = """Ты поддерживаешь свод правил о том, как магазин «Varvikas | Цветной» должен отвечать покупателям на отзывы или вопросы о товарах. Тебе показывают текущий свод правил, черновик ответа, который написала модель, и финальный текст, который сотрудник магазина реально опубликовал вместо этого черновика.

Твоя задача — понять ОБЩУЮ, обобщаемую причину правки (не частность конкретно этого отзыва) и обновить свод правил так, чтобы впредь модель сама писала ближе к тому, как правит сотрудник.

Правила:
- Если правка — стилистическая мелочь или специфична только для этого конкретного случая (конкретный товар, конкретная деталь, опечатка) и не содержит обобщаемого урока — верни текущий свод правил БЕЗ ИЗМЕНЕНИЙ, дословно, слово в слово.
- Если правка действительно показывает предпочтение (тон, структура ответа, что нельзя обещать, как обращаться к покупателю и т.п.) — добавь или измени соответствующий пункт.
- Не дублируй пункты, близкие по смыслу — переформулируй и объедини вместо того, чтобы добавлять ещё один похожий.
- Следи, чтобы свод не раздувался: если пунктов становится много и часть пересекается — сократи и объедини несколько пунктов в один, сохранив смысл каждого.
- Свод правил — это короткие, конкретные пункты списком (каждый с новой строки, начинается с "- "), не общие фразы и не вода.
- Верни ТОЛЬКО финальный текст свода правил целиком, без пояснений от себя, без markdown-заголовков."""


def update_rules_from_edit(*, client: LLMClient, current_rules: str, product_name: str, customer_text: str, rating: int | None, draft_text: str, final_text: str) -> str:
    """Distills a staff edit (draft_text -> final_text) into the persistent
    rulebook, or returns current_rules unchanged if the edit doesn't carry a
    generalizable lesson. Called only when final_text actually differs from
    draft_text — see review_worker._approve_and_post."""
    context = (
        f"Текущий свод правил:\n{current_rules.strip() or '(пока пусто)'}\n\n"
        f"Товар: {product_name or '(название неизвестно)'}\n"
        f"Оценка: {rating if rating is not None else '(нет)'}\n"
        f"Текст от покупателя: {customer_text.strip() or '(покупатель не оставил текст)'}\n\n"
        f"Черновик модели:\n{draft_text}\n\n"
        f"Финальный текст, который реально опубликовал сотрудник:\n{final_text}"
    )
    return client.complete(system=RULES_UPDATE_SYSTEM_PROMPT, user_message=context, max_tokens=1500).strip()[:MAX_RULES_CHARS]


RULES_EDIT_SYSTEM_PROMPT = """Ты редактируешь свод правил о том, как магазин «Varvikas | Цветной» отвечает покупателям на отзывы или вопросы о товарах. Сотрудник магазина прислал текущий свод правил и инструкцию, что в нём поменять (добавить правило, убрать, переформулировать, сократить и объединить похожие пункты и т.п.). Выполни инструкцию и верни ТОЛЬКО обновлённый свод правил целиком — короткими конкретными пунктами списком (каждый с новой строки, начинается с "- "), без пояснений от себя."""


def edit_rules_by_instruction(*, client: LLMClient, current_rules: str, instruction: str) -> str:
    """Applies a staff member's natural-language instruction to the
    rulebook — the "правила <кого>: <инструкция>" chat command."""
    context = f"Текущий свод правил:\n{current_rules.strip() or '(пока пусто)'}\n\nИнструкция сотрудника:\n{instruction.strip()}"
    return client.complete(system=RULES_EDIT_SYSTEM_PROMPT, user_message=context, max_tokens=1500).strip()[:MAX_RULES_CHARS]
