"""Theme selection must not leave success chatter in the transcript."""

from unittest.mock import patch

from code_puppy_core_plugins.theme.register_callbacks import _handle_theme


def test_theme_menu_selection_is_silent():
    module = "code_puppy_core_plugins.theme.register_callbacks"
    with (
        patch(module + "._run_interactive_picker", return_value="ocean"),
        patch(module + "._apply_theme") as apply_theme,
        patch("code_puppy.messaging.emit_info") as info,
    ):
        assert _handle_theme("/theme", "theme") is True
    apply_theme.assert_called_once_with("ocean", announce=False)
    info.assert_not_called()
