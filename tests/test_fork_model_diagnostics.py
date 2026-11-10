"""Expected fork model-lookup failures retain a local DEBUG diagnostic."""

import logging
from unittest.mock import Mock

from code_puppy.agents import agent_manager
from code_puppy_core_plugins.fork import register_callbacks as fork


def test_model_resolution_failure_logs_debug_and_override_bypasses_lookup(
    monkeypatch, caplog
):
    failure = LookupError("synthetic unknown agent")
    load_agent = Mock(side_effect=failure)
    monkeypatch.setattr(agent_manager, "load_agent", load_agent)

    with caplog.at_level(logging.DEBUG, logger=fork.__name__):
        assert fork._resolve_fork_model_name("missing", None) == ""
        records = [record for record in caplog.records if record.name == fork.__name__]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG
        assert records[0].exc_info[1] is failure
        assert fork._resolve_fork_model_name("missing", "override") == "override"
        assert (
            len([record for record in caplog.records if record.name == fork.__name__])
            == 1
        )
    load_agent.assert_called_once_with("missing")
