"""Tests for the emoji_filter plugin."""

from __future__ import annotations

import importlib
import io
from unittest.mock import patch

import pytest


def _plugin_module():
    return importlib.import_module(
        "code_puppy_core_plugins.emoji_filter.register_callbacks"
    )


def _config_module():
    return importlib.import_module("code_puppy_core_plugins.emoji_filter.config")


def _stripper_module():
    return importlib.import_module("code_puppy_core_plugins.emoji_filter.stripper")


# ---------------------------------------------------------------------------
# Stripper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("hello world", "hello world"),
        ("hello 🐶 world", "hello  world"),
        ("🚀🚀🚀launch🚀", "launch"),
        ("flag: 🇺🇸 done", "flag:  done"),
        ("heart: ❤️ love", "heart:  love"),
        ("", ""),
        ("no emoji 123 + - = ?", "no emoji 123 + - = ?"),
    ],
)
def test_strip_emojis_param(raw, expected):
    assert _stripper_module().strip_emojis(raw) == expected


def test_strip_emojis_non_string_passthrough():
    strip = _stripper_module().strip_emojis
    assert strip(None) is None
    assert strip(42) == 42


def test_contains_emoji_detects():
    contains = _stripper_module().contains_emoji
    assert contains("hi 🐶")
    assert not contains("plain text")
    assert not contains(None)


# ---------------------------------------------------------------------------
# Config toggle
# ---------------------------------------------------------------------------


def test_is_enabled_defaults_to_true(tmp_path, monkeypatch):
    cfg_file = tmp_path / "puppy.cfg"
    monkeypatch.setattr("code_puppy.config.CONFIG_FILE", str(cfg_file))
    assert _config_module().is_enabled() is True


def test_set_enabled_persists_off(tmp_path, monkeypatch):
    cfg_file = tmp_path / "puppy.cfg"
    monkeypatch.setattr("code_puppy.config.CONFIG_FILE", str(cfg_file))
    cfg = _config_module()
    cfg.set_enabled(False)
    assert cfg.is_enabled() is False
    cfg.set_enabled(True)
    assert cfg.is_enabled() is True


# ---------------------------------------------------------------------------
# pre_tool_call dispatch
# ---------------------------------------------------------------------------


def test_pre_tool_call_strips_create_file_content():
    module = _plugin_module()
    args = {"file_path": "x.py", "content": "print('hi 🐶')"}
    with patch.object(module, "is_enabled", return_value=True):
        module._on_pre_tool_call("create_file", args)
    assert args["content"] == "print('hi ')"


def test_pre_tool_call_strips_replace_in_file_new_str_only():
    module = _plugin_module()
    args = {
        "file_path": "x.py",
        "replacements": [
            {"old_str": "keep 🐶 me", "new_str": "no emoji 🎉 here"},
            {"old_str": "plain", "new_str": "also plain"},
        ],
    }
    with patch.object(module, "is_enabled", return_value=True):
        module._on_pre_tool_call("replace_in_file", args)

    # old_str must be untouched (search string!)
    assert args["replacements"][0]["old_str"] == "keep 🐶 me"
    assert args["replacements"][0]["new_str"] == "no emoji  here"
    assert args["replacements"][1]["new_str"] == "also plain"


def test_pre_tool_call_strips_edit_file_content_payload():
    module = _plugin_module()
    args = {"payload": {"file_path": "x.py", "content": "🚀 lift off"}}
    with patch.object(module, "is_enabled", return_value=True):
        module._on_pre_tool_call("edit_file", args)
    assert args["payload"]["content"] == " lift off"


def test_pre_tool_call_strips_edit_file_replacements_payload():
    module = _plugin_module()
    args = {
        "payload": {
            "file_path": "x.py",
            "replacements": [{"old_str": "🐶 search", "new_str": "🎉 fresh"}],
        }
    }
    with patch.object(module, "is_enabled", return_value=True):
        module._on_pre_tool_call("edit_file", args)
    rep = args["payload"]["replacements"][0]
    assert rep["old_str"] == "🐶 search"  # search untouched
    assert rep["new_str"] == " fresh"


def test_pre_tool_call_strips_shell_command():
    module = _plugin_module()
    args = {"command": "echo 🐶 hello"}
    with patch.object(module, "is_enabled", return_value=True):
        module._on_pre_tool_call("agent_run_shell_command", args)
    assert args["command"] == "echo  hello"


def test_pre_tool_call_noop_when_disabled():
    module = _plugin_module()
    args = {"file_path": "x.py", "content": "keep 🐶 emoji"}
    with patch.object(module, "is_enabled", return_value=False):
        module._on_pre_tool_call("create_file", args)
    assert args["content"] == "keep 🐶 emoji"


def test_pre_tool_call_ignores_unrelated_tools():
    module = _plugin_module()
    args = {"file_path": "🐶.txt"}  # delete_file shouldn't strip
    with patch.object(module, "is_enabled", return_value=True):
        module._on_pre_tool_call("delete_file", args)
    assert args["file_path"] == "🐶.txt"


def test_pre_tool_call_ignores_delete_snippet():
    """delete_snippet is a search op — never strip its snippet."""
    module = _plugin_module()
    args = {"file_path": "x.py", "snippet": "🚀 launch"}
    with patch.object(module, "is_enabled", return_value=True):
        module._on_pre_tool_call("delete_snippet", args)
    assert args["snippet"] == "🚀 launch"


def test_pre_tool_call_handles_non_dict_args_gracefully():
    module = _plugin_module()
    with patch.object(module, "is_enabled", return_value=True):
        # Should not raise on weird input
        module._on_pre_tool_call("create_file", "not a dict")  # type: ignore[arg-type]
        module._on_pre_tool_call("create_file", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# context_message return value (notifies the model via tool result)
# ---------------------------------------------------------------------------


def test_pre_tool_call_returns_context_message_when_stripping_create_file():
    module = _plugin_module()
    args = {"file_path": "x.py", "content": "hi \U0001f436"}
    with patch.object(module, "is_enabled", return_value=True):
        result = module._on_pre_tool_call("create_file", args)
    assert isinstance(result, dict)
    msg = result["context_message"]
    assert "create_file" in msg
    assert "content" in msg
    assert "emoji_filter" in msg.lower()


def test_pre_tool_call_returns_none_when_no_emojis():
    module = _plugin_module()
    args = {"file_path": "x.py", "content": "plain ascii only"}
    with patch.object(module, "is_enabled", return_value=True):
        result = module._on_pre_tool_call("create_file", args)
    assert result is None


def test_pre_tool_call_returns_none_when_disabled_even_with_emojis():
    module = _plugin_module()
    args = {"file_path": "x.py", "content": "hi \U0001f436"}
    with patch.object(module, "is_enabled", return_value=False):
        result = module._on_pre_tool_call("create_file", args)
    assert result is None


def test_pre_tool_call_context_message_for_shell_command():
    module = _plugin_module()
    args = {"command": "echo \U0001f436 hi"}
    with patch.object(module, "is_enabled", return_value=True):
        result = module._on_pre_tool_call("agent_run_shell_command", args)
    assert isinstance(result, dict)
    assert "command" in result["context_message"]


def test_pre_tool_call_context_message_for_edit_file_payload():
    module = _plugin_module()
    args = {"payload": {"file_path": "x.py", "content": "\U0001f680 lift off"}}
    with patch.object(module, "is_enabled", return_value=True):
        result = module._on_pre_tool_call("edit_file", args)
    assert isinstance(result, dict)
    assert "payload.content" in result["context_message"]


# ---------------------------------------------------------------------------
# Streaming callback — DOES NOT touch ThinkingPart
# ---------------------------------------------------------------------------


def test_stream_event_strips_text_part():
    module = _plugin_module()
    from pydantic_ai.messages import TextPart

    part = TextPart(content="hi \U0001f436 there")
    with patch.object(module, "is_enabled", return_value=True):
        module._on_stream_event(
            "part_start",
            {"part_type": "TextPart", "part": part},
        )
    assert part.content == "hi  there"


def test_stream_event_strips_text_part_delta():
    module = _plugin_module()
    from pydantic_ai.messages import TextPartDelta

    delta = TextPartDelta(content_delta="hello \U0001f389 world")
    with patch.object(module, "is_enabled", return_value=True):
        module._on_stream_event(
            "part_delta",
            {"delta_type": "TextPartDelta", "delta": delta},
        )
    assert delta.content_delta == "hello  world"


def test_stream_event_leaves_thinking_alone():
    """Thinking output must NEVER be touched."""
    module = _plugin_module()
    from pydantic_ai.messages import ThinkingPart, ThinkingPartDelta

    part = ThinkingPart(content="thinking \U0001f914 hard")
    delta = ThinkingPartDelta(content_delta="more \U0001f4ad thoughts")
    with patch.object(module, "is_enabled", return_value=True):
        module._on_stream_event(
            "part_start",
            {"part_type": "ThinkingPart", "part": part},
        )
        module._on_stream_event(
            "part_delta",
            {"delta_type": "ThinkingPartDelta", "delta": delta},
        )

    assert part.content == "thinking \U0001f914 hard"
    assert delta.content_delta == "more \U0001f4ad thoughts"


def test_stream_event_supports_string_fallback_shape():
    module = _plugin_module()
    event_data = {"part_type": "TextPart", "content": "keep \U0001f436"}
    delta_data = {"delta_type": "TextPartDelta", "content_delta": "keep \U0001f389"}
    with patch.object(module, "is_enabled", return_value=True):
        module._on_stream_event("part_start", event_data)
        module._on_stream_event("part_delta", delta_data)
    assert event_data["content"] == "keep "
    assert delta_data["content_delta"] == "keep "


def test_stream_event_respects_disabled_flag():
    module = _plugin_module()
    from pydantic_ai.messages import TextPartDelta

    delta = TextPartDelta(content_delta="keep \U0001f436 me")
    with patch.object(module, "is_enabled", return_value=False):
        module._on_stream_event(
            "part_delta",
            {"delta_type": "TextPartDelta", "delta": delta},
        )
    assert delta.content_delta == "keep \U0001f436 me"


def test_stream_event_does_not_patch_pydantic_constructor():
    """The plugin must not replace pydantic-ai message constructors."""
    module = _plugin_module()
    from pydantic_ai.messages import TextPart

    original_init = TextPart.__init__
    module._on_stream_event("part_start", {})
    assert TextPart.__init__ is original_init


def test_render_wrapper_filters_terminal_output():
    module = _plugin_module()
    module._install_render_wrapper()

    import termflow

    target = io.StringIO()
    renderer = termflow.Renderer(output=target)
    with patch.object(module, "is_enabled", return_value=True):
        renderer.output.write("hello \U0001f436 world")
        renderer.output.flush()

    assert target.getvalue() == "hello  world"

    disabled_target = io.StringIO()
    disabled_renderer = termflow.Renderer(output=disabled_target)
    with patch.object(module, "is_enabled", return_value=False):
        disabled_renderer.output.write("hello \U0001f436 world")
        disabled_renderer.output.flush()
    assert disabled_target.getvalue() == "hello \U0001f436 world"
    assert getattr(termflow.Renderer, module._RENDER_WRAPPER_FLAG) is True


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------


def test_custom_help_lists_emoji_filter():
    entries = dict(_plugin_module()._custom_help())
    assert "emoji-filter" in entries


def test_handle_command_ignores_unrelated():
    assert _plugin_module()._handle_command("/nope", "nope") is None


def test_handle_command_toggles(tmp_path, monkeypatch):
    cfg_file = tmp_path / "puppy.cfg"
    monkeypatch.setattr("code_puppy.config.CONFIG_FILE", str(cfg_file))
    module = _plugin_module()
    cfg = _config_module()

    for command, start, end in (
        ("on", False, True),
        ("off", True, False),
    ):
        cfg.set_enabled(start)
        with patch("code_puppy.messaging.emit_info"):
            result = module._handle_command(f"/emoji-filter {command}", "emoji-filter")
        assert result is True
        assert cfg.is_enabled() is end


def test_handle_command_status(tmp_path, monkeypatch):
    cfg_file = tmp_path / "puppy.cfg"
    monkeypatch.setattr("code_puppy.config.CONFIG_FILE", str(cfg_file))
    module = _plugin_module()
    with patch("code_puppy.messaging.emit_info") as mock_info:
        result = module._handle_command("/emoji-filter status", "emoji-filter")
    assert result is True
    assert mock_info.called
