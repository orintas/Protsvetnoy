from sync_service.planfix_log import PlanFixSyncLog
from sync_service.planfix_new_goods import (
    ERROR_PROJECT_ID,
    GOODS_FIELD_ID,
    PRODUCT_FIELD_ID,
    PRODUCT_TASK_TEMPLATE_ID,
    launch_new_product_tasks,
)


class FakePlanFixClient:
    def __init__(self, *, task=None, create_error_for=None):
        self._task = task
        self._create_error_for = create_error_for or set()
        self.created_tasks: list[dict] = []
        self._next_id = 1000

    def get_task(self, task_id, *, field_ids=()):
        if self._task is None:
            raise RuntimeError("task not found")
        return self._task

    def create_task(self, *, name, template_id=None, parent_id=None, project_id=None, status=None, description="", custom_data=None):
        self.created_tasks.append({
            "name": name, "template_id": template_id, "parent_id": parent_id,
            "project_id": project_id, "status": status, "description": description, "custom_data": custom_data,
        })
        if custom_data:
            entry_id = custom_data[0]["value"]["id"]
            if entry_id in self._create_error_for:
                raise RuntimeError(f"boom for {entry_id}")
        self._next_id += 1
        return self._next_id


def _task_with_goods(entries, *, parent_id=7232, project_id=4206):
    return {
        "id": 7230, "parent": {"id": parent_id}, "project": {"id": project_id},
        "customFieldData": [{"field": {"id": GOODS_FIELD_ID}, "value": entries}],
    }


def test_launch_new_product_tasks_creates_one_subtask_per_entry(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    task = _task_with_goods([{"id": 20, "value": "MC1137"}, {"id": 21, "value": "MC1138"}])
    planfix = FakePlanFixClient(task=task)

    created = launch_new_product_tasks(planfix, task_id=7230, log=log)

    assert len(created) == 2
    assert len(planfix.created_tasks) == 2
    first = planfix.created_tasks[0]
    assert first["name"] == "Проработка продукта"
    assert first["template_id"] == PRODUCT_TASK_TEMPLATE_ID
    assert first["parent_id"] == 7232
    assert first["project_id"] == 4206
    assert first["status"] == "ACCEPTED"
    assert first["custom_data"] == [{"field": {"id": PRODUCT_FIELD_ID}, "value": {"id": 20}}]
    kinds = [e["kind"] for e in log.recent()]
    assert kinds == ["new_goods_task_created", "new_goods_task_created"]


def test_launch_new_product_tasks_handles_a_single_dict_value_shape(tmp_path):
    """Set of directory values might come back as a bare dict instead of a
    one-item list for a field holding exactly one entry — defensive, same
    tolerance as planfix_contact_sync._custom_field_value."""
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    task = {
        "id": 7230, "parent": {"id": 7232}, "project": {"id": 4206},
        "customFieldData": [{"field": {"id": GOODS_FIELD_ID}, "value": {"id": 20, "value": "MC1137"}}],
    }
    planfix = FakePlanFixClient(task=task)

    created = launch_new_product_tasks(planfix, task_id=7230, log=log)

    assert len(created) == 1


def test_launch_new_product_tasks_does_nothing_when_the_goods_field_is_empty(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    task = {"id": 7230, "parent": {"id": 7232}, "project": {"id": 4206}, "customFieldData": []}
    planfix = FakePlanFixClient(task=task)

    created = launch_new_product_tasks(planfix, task_id=7230, log=log)

    assert created == []
    assert planfix.created_tasks == []
    kinds = [e["kind"] for e in log.recent()]
    assert kinds == ["new_goods_run"]


def test_launch_new_product_tasks_continues_after_one_subtask_fails(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    task = _task_with_goods([{"id": 20, "value": "MC1137"}, {"id": 21, "value": "MC1138"}])
    planfix = FakePlanFixClient(task=task, create_error_for={20})

    created = launch_new_product_tasks(planfix, task_id=7230, log=log)

    assert len(created) == 1  # only MC1138 succeeded
    kinds = sorted(e["kind"] for e in log.recent())
    assert kinds == sorted(["new_goods_error", "new_goods_task_created"])


def test_launch_new_product_tasks_creates_fallback_error_task_when_everything_fails(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    task = _task_with_goods([{"id": 20, "value": "MC1137"}])
    planfix = FakePlanFixClient(task=task, create_error_for={20})

    created = launch_new_product_tasks(planfix, task_id=7230, log=log)

    assert created == []
    error_task = next(t for t in planfix.created_tasks if t["project_id"] == ERROR_PROJECT_ID)
    assert error_task["name"] == "Задачи по обработке новинок не создались"
    assert "7230" in error_task["description"]


def test_launch_new_product_tasks_creates_fallback_error_task_when_the_batch_task_cannot_be_read(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    planfix = FakePlanFixClient(task=None)

    created = launch_new_product_tasks(planfix, task_id=9999, log=log)

    assert created == []
    assert len(planfix.created_tasks) == 1
    assert planfix.created_tasks[0]["project_id"] == ERROR_PROJECT_ID
    kinds = [e["kind"] for e in log.recent()]
    assert "new_goods_error" in kinds


# --- re-fires of the same trigger (the real one is "task edited and still
# matches the condition", which can fire more than once for one batch) ---

def test_launch_new_product_tasks_does_not_recreate_subtasks_on_a_harmless_refire(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    task = _task_with_goods([{"id": 20, "value": "MC1137"}, {"id": 21, "value": "MC1138"}])
    planfix = FakePlanFixClient(task=task)

    first = launch_new_product_tasks(planfix, task_id=7230, log=log)
    second = launch_new_product_tasks(planfix, task_id=7230, log=log)

    assert len(first) == 2
    assert second == []  # nothing new created the second time
    assert len(planfix.created_tasks) == 2  # no duplicate POSTs to PlanFix
    kinds = [e["kind"] for e in log.recent()]
    assert kinds.count("new_goods_task_created") == 2
    assert "все товары уже были обработаны ранее" in log.recent()[0]["message"]


def test_launch_new_product_tasks_only_retries_the_product_that_failed_before(tmp_path):
    log = PlanFixSyncLog(str(tmp_path / "planfix.sqlite3"))
    task = _task_with_goods([{"id": 20, "value": "MC1137"}, {"id": 21, "value": "MC1138"}])
    planfix = FakePlanFixClient(task=task, create_error_for={20})

    first = launch_new_product_tasks(planfix, task_id=7230, log=log)
    assert first == [1001]  # only MC1138 succeeded the first time

    planfix_retry = FakePlanFixClient(task=task)  # the transient failure is gone now
    second = launch_new_product_tasks(planfix_retry, task_id=7230, log=log)

    assert len(second) == 1  # only the previously-failed MC1137 gets (re)created
    assert planfix_retry.created_tasks[0]["custom_data"] == [{"field": {"id": PRODUCT_FIELD_ID}, "value": {"id": 20}}]
