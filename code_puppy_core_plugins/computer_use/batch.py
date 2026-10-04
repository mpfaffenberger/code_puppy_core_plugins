"""Shared, synchronous batch engine; the async adapter offloads the entire batch."""

from __future__ import annotations

import math
import time

from .backend_types import ComputerUseError

MAX_BATCH_STEPS = 20
ACTIONS = {
    "set_value": "set_value",
    "perform_action": "perform_action",
    "select_text": "select_text",
    "press_key": "press_key",
    "type_text": "type_text",
    "scroll": "scroll_pages",
    "drag": "drag_pixel",
}


def run_batch(backend, revision, steps, settler):
    if not 1 <= len(steps) <= MAX_BATCH_STEPS:
        return {"success": False, "error": "A batch must contain 1 to 20 steps."}
    completed = []
    initial = None
    try:
        initial = backend.require_state(revision)
        for index, step in enumerate(steps):
            action = str(step.get("action", ""))
            kwargs = {key: value for key, value in step.items() if key != "action"}
            try:
                # Even wait steps honor expiry and emergency pause.
                backend.require_state(revision)
                if action == "wait":
                    seconds = float(kwargs.get("seconds", 1))
                    if not math.isfinite(seconds) or not 0 <= seconds <= 10:
                        raise ComputerUseError(
                            "Wait seconds must be finite and within 0..10"
                        )
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        backend.require_state(revision)
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                    result = {"success": True, "seconds": seconds}
                elif action == "click":
                    if "element_id" in kwargs and ("x" in kwargs or "y" in kwargs):
                        raise ComputerUseError("Provide element_id or x/y, not both")
                    method = (
                        backend.click if "element_id" in kwargs else backend.click_pixel
                    )
                    result = method(revision, consume=False, **kwargs)
                elif action in ACTIONS:
                    if action == "perform_action":
                        # The outer 'action' is the batch discriminator.
                        kwargs["action"] = kwargs.pop("action_name")
                    result = getattr(backend, ACTIONS[action])(
                        revision, consume=False, **kwargs
                    )
                else:
                    raise ComputerUseError(f"Unknown action: {action}")
            except Exception as exc:  # noqa: BLE001 - stop the batch on any provider failure.
                result = {"success": False, "error": str(exc)}
            completed.append({"index": index, "action": action, "result": result})
            if not result.get("success"):
                backend.invalidate_state()
                return {
                    "success": False,
                    "completed_steps": completed,
                    "warning": "A failed action may have partially executed; inspect before retrying.",
                }
        backend.require_state(revision, consume=True)
        settle = settler(backend.snapshot, initial.application)
        updated = backend.get_app_state(initial.application, 100)
        if not updated.get("success"):
            return {
                "success": True,
                "completed_steps": completed,
                "updated_state_error": updated.get("error"),
            }
        updated["completed_steps"] = completed
        updated["ui_settle"] = settle
        return updated
    except Exception as exc:  # noqa: BLE001 - stop the batch on any provider failure.
        backend.invalidate_state()
        return {"success": False, "error": str(exc), "completed_steps": completed}
