"""Small keyboard-driven profile picker with inline profile creation."""

from code_puppy.i18n import t
from code_puppy_core_plugins.termflow_tui import FragmentTUI, fragments_to_lines

from . import config


class ProfileMenu:
    def __init__(self):
        self.names = config.list_profiles()
        self.index = (
            self.names.index(config.active_profile())
            if config.active_profile() in self.names
            else 0
        )
        self.creating = False
        self.name = ""
        self.message = ""

    def render(self):
        fragments = [("class:tui.title", t("profiles.title") + "\n\n")]
        # Keep the selection visible even with a large profile collection.
        start = max(0, self.index - 5)
        for index in range(start, min(len(self.names), start + 10)):
            name = self.names[index]
            label = t(
                "profiles.row",
                cursor=">" if index == self.index else " ",
                name=name,
                active="*" if name == config.active_profile() else " ",
            )
            fragments.append(
                (
                    "class:tui.selected" if index == self.index else "class:tui.text",
                    label + "\n",
                )
            )
        fragments.append(("class:tui.help", "\n" + t("profiles.keys") + "\n"))
        if self.creating:
            fragments.append(
                ("class:tui.input", t("profiles.new_name", name=self.name) + "\n")
            )
        if self.message:
            fragments.append(("class:tui.warning", self.message))
        return fragments_to_lines(fragments)

    def on_key(self, key):
        if key in ("ctrl-c", "\x03", "\x1b", "escape"):
            if self.creating:
                self.creating = False
                return False
            return True
        try:
            if self.creating:
                if key in ("\r", "\n", "enter"):
                    config.create_profile(self.name)
                    self.names = config.list_profiles()
                    self.index = self.names.index(self.name)
                    self.creating = False
                    self.message = t("profiles.created", name=self.name)
                elif key in ("\x7f", "\b", "backspace"):
                    self.name = self.name[:-1]
                elif (
                    len(key) == 1
                    and key.isascii()
                    and key.isprintable()
                    and len(self.name) < 64
                ):
                    self.name += key
            elif key in ("up", "\x1b[A", "k"):
                self.index = (self.index - 1) % len(self.names)
            elif key in ("down", "\x1b[B", "j"):
                self.index = (self.index + 1) % len(self.names)
            elif key == "c":
                self.creating, self.name, self.message = True, "", ""
            elif key in ("\r", "\n", "enter"):
                from .register_callbacks import switch_profile

                switch_profile(self.names[self.index])
                self.message = t("profiles.active", name=config.active_profile())
            elif key == "q":
                return True
        except (OSError, ValueError, RuntimeError) as exc:
            self.message = t("profiles.error", detail=str(exc))
        return False


def run_menu():
    import sys

    if not sys.stdin.isatty():
        raise ValueError(t("profiles.no_tty"))
    menu = ProfileMenu()
    FragmentTUI(menu.render, menu.on_key).run()
