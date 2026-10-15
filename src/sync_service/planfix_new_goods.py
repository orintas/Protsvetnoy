from __future__ import annotations

from typing import Any

from .planfix_client import PlanFixClient
from .planfix_log import PlanFixSyncLog

# Ported from the "Запуск подзадач с новинками" Make.com scenario: a batch
# task carries a list of new products in a "Товары" custom field; this
# creates one "Проработка продукта" subtask per product, each tagged with
# that one product via "Товар" — same parent/project as the batch task.
PRODUCT_TASK_TEMPLATE_ID = 4213  # "Проработка продукта" — confirmed live 2026-10-16 via GET /task/templates (the previous value, 3121686, wasn't a real template id at all: PlanFix silently created tasks under the account's default "Стандартный шаблон" (id 6) instead, which doesn't carry the "Товар" field — that's what the "Custom field not permitted" error actually was)
GOODS_FIELD_ID = 82078  # "Товары" on the batch task — Set of directory values
PRODUCT_FIELD_ID = 82080  # "Товар" on each subtask — Directory entry

# Fallback notification when subtask creation fails outright — same project
# the Make scenario's onError branch used ("ПланФикс: обучение и поддержка").
ERROR_PROJECT_ID = 60364
ERROR_TASK_TITLE = "Задачи по обработке новинок не создались"


def _directory_entries(task: dict[str, Any], field_id: int) -> list[dict[str, Any]]:
    """Extracts the list of {"id", "value"} directory-entry references from
    a task's customFieldData for the given field.

    Tolerates either a bare list or a single dict for the field's value —
    same defensive spirit as planfix_contact_sync._custom_field_value.
    PlanFix's "Set of directory values" array shape confirmed live
    2026-10-16 against a real batch task (402193): `value` is a list of
    `{"id": <directory entry key>, "value": <display text>}` dicts.
    """
    for entry in task.get("customFieldData", []) or []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("field") or {}).get("id") != field_id:
            continue
        value = entry.get("value")
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict) and v.get("id") is not None]
        if isinstance(value, dict) and value.get("id") is not None:
            return [value]
        return []
    return []


def _already_created(log: PlanFixSyncLog, external_id: str) -> bool:
    """Whether a "new_goods_task_created" row already exists for this exact
    (task, product) pair. The trigger fires on "Задача создана или изменена
    и стала соответствовать условиям" — a task that's edited again while
    still matching (status=Завершенная, template=«Установить артикулы
    новинок») can fire this webhook more than once for the same batch, so
    subtask creation is deduplicated per product rather than per task: a
    retry after a partial failure only recreates the ones that actually
    failed, and a harmless re-fire after full success recreates nothing."""
    return any(entry.get("external_id") == external_id and entry.get("kind") == "new_goods_task_created" for entry in log.search(external_id))


def launch_new_product_tasks(planfix: PlanFixClient, *, task_id: int, log: PlanFixSyncLog) -> list[int]:
    """For the given task (expected to carry the "Товары" field, 82078 — a
    list of product directory entries), creates one "Проработка продукта"
    subtask per product not already created for this task (see
    _already_created): same parent and project as the triggering task, with
    that one product set on the "Товар" field (82080). PlanFix's own
    template auto-names each subtask from that field (e.g. "MC1137 -
    Проработка продукта", confirmed against real tasks the Make scenario
    already created) — no title is constructed here, matching the scenario
    it replaces, which only ever sent a static "Проработка продукта" title
    too.

    Returns the newly created subtask ids. On total failure (can't read the
    task, or every subtask creation failed), also creates a notification
    task in the fallback project — mirrors the Make scenario's onError
    branch.
    """
    try:
        task = planfix.get_task(task_id, field_ids=[GOODS_FIELD_ID])
        parent_id = (task.get("parent") or {}).get("id")
        project_id = (task.get("project") or {}).get("id")
        entries = _directory_entries(task, GOODS_FIELD_ID)
    except Exception as error:
        log.add("new_goods_error", "error", f"Задача {task_id}: не удалось прочитать поле «Товары»: {error}", str(task_id))
        _create_error_task(planfix, log, task_id=task_id, error=error)
        return []

    if not entries:
        log.add("new_goods_run", "success", f"Задача {task_id}: поле «Товары» пустое, подзадачи не создавались", str(task_id))
        return []

    created_ids: list[int] = []
    attempted = 0
    for entry in entries:
        entry_key = f"{task_id}:{entry['id']}"
        if _already_created(log, entry_key):
            continue
        attempted += 1
        product_name = str(entry.get("value") or entry.get("id"))
        try:
            new_id = planfix.create_task(
                name="Проработка продукта",
                template_id=PRODUCT_TASK_TEMPLATE_ID,
                parent_id=parent_id,
                project_id=project_id,
                custom_data=[{"field": {"id": PRODUCT_FIELD_ID}, "value": {"id": entry["id"]}}],
            )
            created_ids.append(new_id)
            log.add("new_goods_task_created", "success", f"Задача {task_id}: создана подзадача {new_id} для «{product_name}»", entry_key)
        except Exception as error:
            log.add("new_goods_error", "error", f"Задача {task_id}: не удалось создать подзадачу для «{product_name}»: {error}", entry_key)

    if attempted == 0:
        log.add("new_goods_run", "success", f"Задача {task_id}: все товары уже были обработаны ранее — повторный запуск пропущен", str(task_id))
    elif not created_ids:
        _create_error_task(planfix, log, task_id=task_id, error=RuntimeError("ни одна подзадача не создана"))

    return created_ids


def _create_error_task(planfix: PlanFixClient, log: PlanFixSyncLog, *, task_id: int, error: Exception) -> None:
    try:
        planfix.create_task(
            name=ERROR_TASK_TITLE,
            project_id=ERROR_PROJECT_ID,
            description=f"Задача {task_id}: {error}",
        )
    except Exception as nested_error:
        log.add("new_goods_error", "error", f"Задача {task_id}: не удалось создать и уведомление об ошибке: {nested_error}", str(task_id))
