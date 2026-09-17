"""HTTP fields layered over the existing custom form and persistence loop."""

import json
from urllib.parse import urlsplit

from code_puppy.i18n import t


def read_config(form):
    config = json.loads(form.json_config)
    if not isinstance(config, dict):
        raise ValueError(t("mcp.http_form.object_required"))
    return config


def validate_headers(headers):
    """Non-empty string keys, string values, no CRLF (header injection)."""
    if not isinstance(headers, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not key
        or any(char in key + value for char in "\r\n")
        for key, value in headers.items()
    ):
        raise ValueError(t("mcp.http_form.invalid_headers"))


def validate_config(config):
    url = config.get("url", "")
    if not isinstance(url, str):
        raise ValueError(t("mcp.http_form.invalid_url"))
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in url)
    ):
        raise ValueError(t("mcp.http_form.invalid_url"))
    headers = config.get("headers", {})
    validate_headers(headers)
    from code_puppy.mcp_.http_auth import http_auth

    # Reuse transport security policy; constructing OAuth does not start login.
    http_auth(config, url, headers)


def save_config(form, config):
    form.json_config = json.dumps(config, indent=2)


def input_text(title, initial):
    from termflow.tui import TextInputBuilder
    from code_puppy.command_line.tui_style import menu_style

    builder = TextInputBuilder(title).initial(initial).alt_screen(False)
    style = menu_style()
    if style is not None:
        builder.style(style)
    result = builder.build().run()
    return None if result.cancelled else result.value


def choose_auth(config):
    from termflow.tui import MenuBuilder, MenuItem
    from code_puppy.command_line.tui_style import themed

    result = (
        themed(
            MenuBuilder(t("mcp.http_form.auth"))
            .items(
                [
                    MenuItem(t("mcp.http_form.none"), value="none"),
                    MenuItem(t("mcp.http_form.oauth"), value="oauth"),
                    MenuItem(t("mcp.http_form.headers_auth"), value="headers"),
                ]
            )
            .alt_screen(False)
        )
        .build()
        .run()
    )
    if result.cancelled or result.item is None:
        return False
    if result.item.value == "oauth":
        config["auth"] = "oauth"
        # New OAuth selections need time for a human to finish browser login.
        if config.get("timeout", 30) == 30:
            config["timeout"] = 330
    else:
        config.pop("auth", None)
    if result.item.value == "none":
        headers = config.get("headers", {})
        config["headers"] = {
            key: val for key, val in headers.items() if key.lower() != "authorization"
        }
    if result.item.value == "headers":
        return edit_headers(config)
    return True


def edit_headers(config):
    value = input_text(
        t("mcp.http_form.headers_prompt"), json.dumps(config.get("headers", {}))
    )
    if value is None:
        return False
    try:
        headers = json.loads(value or "{}")
    except ValueError:
        # A JSONDecodeError message is noise in the footer; say what we mean.
        raise ValueError(t("mcp.http_form.invalid_headers")) from None
    validate_headers(headers)
    if headers:
        config["headers"] = headers
    else:
        config.pop("headers", None)
    return True


class HTTPMenu:
    def __init__(self, form, original, overrides):
        self.form = form
        self.original = original
        self.overrides = overrides

    def run(self):
        from termflow.tui import MenuBuilder, MenuItem
        from code_puppy.command_line.tui_style import themed

        form = self.form
        while True:
            try:
                config = read_config(form)
            except (ValueError, TypeError):
                # Advanced malformed JSON remains editable, never discarded.
                return self.original(form, **self.overrides).run()
            auth = (
                "oauth"
                if config.get("auth") == "oauth"
                else (
                    "headers_auth"
                    if any(
                        key.lower() == "authorization"
                        for key in config.get("headers", {})
                    )
                    else "none"
                )
            )
            items = [
                MenuItem(t("mcp.http_form.name", name=form.server_name), value="name"),
                MenuItem(t("mcp.http_form.type"), value="type"),
                MenuItem(t("mcp.http_form.url"), value="http_url"),
                MenuItem(
                    t("mcp.http_form.auth_value", auth=t("mcp.http_form." + auth)),
                    value="http_auth",
                ),
                MenuItem(t("mcp.http_form.headers"), value="http_headers"),
                MenuItem(t("mcp.http_form.advanced"), value="json"),
                MenuItem(t("mcp.http_form.save"), value="save"),
                MenuItem(t("mcp.http_form.cancel"), value="cancel"),
            ]
            # Do not echo credentials, header values, or URL query secrets.
            builder = themed(
                MenuBuilder(t("mcp.http_form.title")).items(items).alt_screen(False)
            )
            if form.validation_error:
                builder.footer_hint(form.validation_error)
            result = builder.build().run()
            if result.cancelled or result.item is None:
                return result
            field = result.item.value
            try:
                if field == "http_url":
                    value = input_text(t("mcp.http_form.url"), config.get("url", ""))
                    if value is not None:
                        config["url"] = value.strip()
                        save_config(form, config)
                elif field == "http_auth":
                    if choose_auth(config):
                        save_config(form, config)
                elif field == "http_headers":
                    if edit_headers(config):
                        save_config(form, config)
                elif field == "save":
                    validate_config(config)
                    return result
                else:
                    return result
                form.validation_error = None
            except (ValueError, TypeError) as exc:
                form.validation_error = str(exc)


def install_http_form():
    from code_puppy.command_line.mcp import custom_server_form as core

    original = core.run_form_flow
    if getattr(original, "_http_fields", False):
        return

    def menu(form, **overrides):
        if form._get_current_type() == "http":
            return HTTPMenu(form, core.build_form_menu, overrides)
        return core.build_form_menu(form, **overrides)

    def type_menu(form):
        old_type = form._get_current_type()
        untouched = (
            form.json_config.strip() == core.CUSTOM_SERVER_EXAMPLES[old_type].strip()
        )
        core.run_type_menu(form)
        if old_type != "http" and form._get_current_type() == "http" and untouched:
            save_config(form, {"type": "http", "url": "", "timeout": 30})

    def run(form, **kwargs):
        kwargs.setdefault("menu_factory", menu)
        kwargs.setdefault("type_menu", type_menu)
        return original(form, **kwargs)

    run._http_fields = True
    core.run_form_flow = run
