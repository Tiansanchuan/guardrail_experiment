"""Lazy registry: importing this module never imports tasks or model SDKs."""

from importlib import import_module

from va_experiment.models import TaskAdapter


def get_task_adapter(task_id: str) -> TaskAdapter:
    modules = {
        "bank_transfer_2": "banking", "american_airlines_2": "travel",
        "spam_filter_update": "spam",
        "api_key_calendar_agendas_2": "calendar_key", "expenditure_accounting": "expenditure",
    }
    if task_id not in modules:
        raise ValueError(f"Unknown task ID: {task_id}")
    return import_module(f"va_experiment.tasks.{modules[task_id]}").Adapter()
