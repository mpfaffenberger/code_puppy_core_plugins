"""HTTP configuration is editable without hand-writing a server JSON blob."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_puppy_core_plugins.mcp_binding_prompt import http_form


def selected(value):
    return SimpleNamespace(cancelled=False, item=SimpleNamespace(value=value))


def test_url_only_configuration():
    http_form.validate_config({"url": "https://example.com/mcp"})


@pytest.mark.parametrize(
    "config",
    [
        {"url": "bad"},
        {"url": "https://user:secret@example.com/mcp"},
        {"url": "https://example.com", "headers": {"X": 42}},
        {"url": "https://example.com", "headers": {"X": "bad\r\nvalue"}},
        {"url": "http://example.com", "auth": "oauth"},
        {
            "url": "https://example.com",
            "auth": "oauth",
            "headers": {"AUTHORIZATION": "Bearer secret"},
        },
    ],
)
def test_invalid_config_blocked_before_save(config):
    with pytest.raises(ValueError):
        http_form.validate_config(config)


def test_oauth_selection_sets_timeout_and_keeps_optional_headers():
    config = {
        "url": "https://example.com",
        "timeout": 30,
        "headers": {"X-Tenant": "$TENANT"},
    }
    builder = MagicMock()
    builder.build.return_value.run.return_value = selected("oauth")
    with patch("code_puppy.command_line.tui_style.themed", return_value=builder):
        assert http_form.choose_auth(config)
    assert config["auth"] == "oauth"
    assert config["timeout"] == 330
    assert config["headers"] == {"X-Tenant": "$TENANT"}


def test_http_menu_edits_url_then_returns_existing_save_action():
    form = SimpleNamespace(
        json_config='{"timeout": 55}', server_name="remote", validation_error=None
    )
    builder = MagicMock()
    builder.build.return_value.run.side_effect = [
        selected("http_url"),
        selected("save"),
    ]
    with (
        patch("code_puppy.command_line.tui_style.themed", return_value=builder),
        patch.object(http_form, "input_text", return_value="https://example.com/mcp"),
    ):
        result = http_form.HTTPMenu(form, MagicMock(), {}).run()
    assert result.item.value == "save"
    assert json.loads(form.json_config) == {
        "url": "https://example.com/mcp",
        "timeout": 55,
    }


@pytest.mark.parametrize(
    "typed",
    [
        "not json",
        '{"": "empty key"}',
        '{"X": "bad\\r\\nvalue"}',
        '{"X": 42}',
    ],
)
def test_edit_headers_rejects_bad_input_with_translated_error(typed):
    """Edit-time rules match save-time rules; no raw JSONDecodeError leaks."""
    config = {}
    with patch.object(http_form, "input_text", return_value=typed):
        with pytest.raises(ValueError) as excinfo:
            http_form.edit_headers(config)
    assert "Expecting" not in str(excinfo.value)
    assert config == {}


def test_cancel_headers_leaves_config_unchanged():
    config = {"headers": {"Authorization": "Bearer $TOKEN"}}
    with patch.object(http_form, "input_text", return_value=None):
        assert not http_form.edit_headers(config)
    assert config == {"headers": {"Authorization": "Bearer $TOKEN"}}


def test_startup_adapter_uses_http_menu_and_preserves_core_save_loop(monkeypatch):
    from code_puppy.command_line.mcp import custom_server_form as core

    original = MagicMock(return_value=True)
    original._http_fields = False
    monkeypatch.setattr(core, "run_form_flow", original)
    http_form.install_http_form()
    form = MagicMock()
    form._get_current_type.return_value = "http"
    assert core.run_form_flow(form)
    factory = original.call_args.kwargs["menu_factory"]
    assert isinstance(factory(form), http_form.HTTPMenu)
    wrapper = core.run_form_flow
    http_form.install_http_form()
    assert core.run_form_flow is wrapper
