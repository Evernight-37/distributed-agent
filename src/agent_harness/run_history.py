"""Read displayable execution metadata from receiver results."""

import json

from agent_harness.usage import execution_telemetry


def run_details(result):
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return {}
    if not isinstance(result, dict):
        return {}
    details = execution_telemetry(result)
    for key in ("requested_model", "effective_model", "reasoning_effort", "effective_reasoning_effort"):
        value = result.get(key)
        if isinstance(value, str) and 0 < len(value) <= 120:
            details[key] = value
    if "effective_reasoning_effort" in result and result["effective_reasoning_effort"] is None:
        details["effective_reasoning_effort"] = None
    return details


def task_run_details(db, job):
    if not job:
        return {}
    row = db.execute("SELECT result FROM pull_jobs WHERE id=?", (job,)).fetchone()
    return run_details(row["result"]) if row else {}
