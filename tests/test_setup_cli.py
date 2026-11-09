"""Tests for the setup_cli plugin (scriptable configuration)."""

from __future__ import annotations

import argparse
import json

from code_puppy_core_plugins.setup_cli import cli, register_callbacks


def test_register_cli_args_adds_setup_remainder():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="*")
    register_callbacks._register_cli_args(parser)
    args = parser.parse_args(["--setup", "doctor", "--json"])
    assert args.setup == ["doctor", "--json"]
    plain = parser.parse_args([])
    assert plain.setup is None


def test_handle_cli_args_passes_through_without_setup():
    args = argparse.Namespace(setup=None)
    assert register_callbacks._handle_cli_args(args) is None


def test_handle_cli_args_handles_setup(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "code_puppy_core_plugins.setup_cli.cli.main",
        lambda tokens: seen.update(tokens=tokens) or 7,
    )
    result = register_callbacks._handle_cli_args(argparse.Namespace(setup=["doctor"]))
    assert result == {"handled": True, "exit_code": 7}
    assert seen["tokens"] == ["doctor"]


def test_doctor_json_shape(capsys):
    code = cli.main(["doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert set(report) >= {"ok", "checks", "providers"}
    assert code in (0, 1)
    assert any(c["check"] == "identity" for c in report["checks"])


def test_config_set_get_roundtrip(capsys):
    assert cli.main(["config", "set", "puppy_name", "Testy"]) == 0
    capsys.readouterr()
    assert cli.main(["config", "get", "puppy_name", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["value"] == "Testy"
    assert data["set"] is True


def test_config_get_masks_secrets(capsys, monkeypatch):
    from code_puppy import config as cp_config

    monkeypatch.setattr(cp_config, "get_value", lambda k: "sk-real-secret-value")
    assert cli.main(["config", "get", "OPENAI_API_KEY", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "real-secret" not in json.dumps(data)
    assert data["value"] == "(set, hidden)"


def test_model_add_and_default(capsys, monkeypatch, tmp_path):
    import code_puppy.command_line.add_model_menu as amm
    import code_puppy.config as cp_config
    import code_puppy.model_factory as mf

    from code_puppy_core_plugins.setup_cli import model_cmds

    # EXTRA_MODELS_FILE is an import-time constant in several modules and
    # is not covered by conftest's DATA_DIR pinning; retarget every copy
    # or this test writes into the developer's real extra_models.json.
    fake_extra = str(tmp_path / "extra_models.json")
    for module in (amm, cp_config, mf):
        monkeypatch.setattr(module, "EXTRA_MODELS_FILE", fake_extra, raising=False)

    from code_puppy.models_dev_parser import ModelInfo, ProviderInfo

    def FakeModel():
        return ModelInfo(
            provider_id="openai",
            model_id="acme-large",
            name="Acme Large",
            tool_call=True,
            temperature=True,
            context_length=200000,
        )

    def FakeProvider():
        return ProviderInfo(
            id="openai",
            name="OpenAI",
            env=["OPENAI_API_KEY"],
            api="https://api.openai.com/v1",
        )

    class FakeRegistry:
        data_source = "test"

        def get_providers(self):
            return {"openai": FakeProvider()}

        def get_models(self, provider_id=None):
            return [FakeModel()]

    monkeypatch.setattr(model_cmds, "_get_registry", lambda: FakeRegistry())
    monkeypatch.setattr(model_cmds, "_catalog", lambda: {})
    assert cli.main(["model", "add", "openai/acme-large", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["key"] == "openai-acme-large"
    assert data["added"] is True

    monkeypatch.setattr(model_cmds, "_catalog", lambda: {"openai-acme-large": {}})
    assert cli.main(["model", "default", "openai-acme-large", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["default"] == "openai-acme-large"

    assert cli.main(["model", "add", "no-separator"]) == 1


def test_mcp_add_list_remove(capsys):
    assert (
        cli.main(
            ["mcp", "add", "echoer", "--command", "uvx", "--arg", "mcp-echo", "--json"]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["mcp", "list", "--json"]) == 0
    servers = json.loads(capsys.readouterr().out)["servers"]
    assert any(s["name"] == "echoer" and s["transport"] == "stdio" for s in servers)
    assert cli.main(["mcp", "bind", "code-puppy", "echoer"]) == 0
    assert cli.main(["mcp", "unbind", "code-puppy", "echoer"]) == 0
    assert cli.main(["mcp", "remove", "echoer"]) == 0


def test_export_excludes_credentials(tmp_path, capsys):
    from code_puppy import config as cp_config

    cfg = tmp_path / "puppy.cfg"
    cfg.write_text(
        "[puppy]\npuppy_name = Testy\nOPENAI_API_KEY = sk-should-not-leak\n",
        encoding="utf-8",
    )
    from code_puppy_core_plugins.setup_cli import export_cmds

    dropped = export_cmds._sanitized_puppy_cfg(cfg, tmp_path / "out.cfg")
    text = (tmp_path / "out.cfg").read_text(encoding="utf-8")
    assert "sk-should-not-leak" not in text
    assert "Testy" in text
    assert dropped  # at least the key was reported
    _ = cp_config  # keep import for fixture parity with conftest isolation


def test_auth_list_json(capsys):
    assert cli.main(["auth", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    ids = {entry["id"] for entry in data["oauth"]}
    assert {"meta", "claude-code", "codex", "copilot", "grok"} <= ids
    env_vars = {e["env_var"] for e in data["api_keys"]}
    # Sourced from core's credential_env_var_names() composite, not a
    # plugin-maintained list: well-known keys are always present, and a
    # models.dev-only var (e.g. ZHIPU_API_KEY) joins once a configured
    # model references it.
    assert "OPENAI_API_KEY" in env_vars


def test_unknown_provider_errors(capsys):
    code = cli.main(["auth", "status", "nope-not-real", "--json"])
    assert code == 2


def test_auth_status_accepts_models_dev_env_var(capsys, monkeypatch):
    # A var known only through models.dev (not any curated list) must be
    # treated as an API-key provider, not "unknown provider".
    from code_puppy_core_plugins.setup_cli import providers

    monkeypatch.setattr(
        providers, "_models_dev_env_vars", lambda: frozenset({"ACME_API_KEY"})
    )
    monkeypatch.setattr(providers, "credential_present", lambda env: False)
    assert cli.main(["auth", "status", "ACME_API_KEY", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "api-key"


def test_auth_logout_clears_stored_api_key(capsys, monkeypatch, tmp_path):
    # Regression: logout used to call save_credential(env, ""), which core
    # rejects (empty values raise), so the key was never removed. Logout
    # must delete from the shared secret store, drop any legacy puppy.cfg
    # value, and clear the process env; a second logout is a no-op.
    import configparser
    import os

    from code_puppy import config as cp_config
    from code_puppy import config_file, secret_store

    deleted = []
    monkeypatch.setattr(
        secret_store, "delete_secret", lambda name: deleted.append(name)
    )
    parser = configparser.ConfigParser()
    parser[cp_config.DEFAULT_SECTION] = {"ZHIPU_API_KEY": "sekrit"}
    monkeypatch.setattr(config_file, "mutate_config", lambda path, fn: fn(parser))
    monkeypatch.setattr(cp_config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cp_config, "CONFIG_FILE", str(tmp_path / "puppy.cfg"))
    monkeypatch.setenv("ZHIPU_API_KEY", "sekrit")

    assert cli.main(["auth", "logout", "ZHIPU_API_KEY", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["cleared"] is True
    assert deleted == ["provider_ZHIPU_API_KEY"]
    assert not parser.has_option(cp_config.DEFAULT_SECTION, "ZHIPU_API_KEY")
    assert "ZHIPU_API_KEY" not in os.environ

    # Idempotent: nothing stored anymore, still success.
    assert cli.main(["auth", "logout", "ZHIPU_API_KEY", "--json"]) == 0
    capsys.readouterr()


def _plan_stub(services):
    return {
        "credentials": [
            {
                "service": s,
                "env_var": "X_API_KEY",
                "domain": "api.example.com",
                "models": [],
                "host_credential_present": True,
            }
            for s in services
        ],
        "oauth": [],
        "egress": [],
        "unsupported": [],
        "default_model": None,
    }


def test_sbx_plan_reports_and_prunes_stale_secrets(capsys, monkeypatch):
    from code_puppy_core_plugins.setup_cli import sbx_plan

    # Only global service secrets are ours to prune; sandbox-scoped and
    # registry rows are ignored.
    assert sbx_plan._parse_secret_ls(
        "SCOPE TYPE NAME SECRET\n"
        "(global)   service   meta    (stored)\n"
        "(global)   service   zhipu   (stored)\n"
        "mybox      service   scoped  (stored)\n"
        "(global)   registry  ghcr.io (stored)\n"
    ) == ["meta", "zhipu"]

    monkeypatch.setattr(
        sbx_plan, "build_plan", lambda provider=None: _plan_stub(["meta"])
    )
    monkeypatch.setattr(
        sbx_plan, "_stored_sbx_secrets", lambda: ["meta", "other", "zhipu"]
    )
    removed = []
    monkeypatch.setattr(
        sbx_plan, "_remove_sbx_secret", lambda n: removed.append(n) or None
    )

    # Plain plan reports staleness but deletes nothing.
    assert cli.main(["sbx", "plan", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stale_sbx_secrets"] == ["other", "zhipu"]
    assert data["pruned_sbx_secrets"] == []
    assert removed == []

    # --prune removes exactly the stale ones.
    assert cli.main(["sbx", "plan", "--json", "--prune"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pruned_sbx_secrets"] == ["other", "zhipu"]
    assert data["stale_sbx_secrets"] == []
    assert removed == ["other", "zhipu"]


def test_sbx_plan_prune_rejected_with_provider_filter(capsys, monkeypatch):
    from code_puppy_core_plugins.setup_cli import sbx_plan

    monkeypatch.setattr(sbx_plan, "build_plan", lambda provider=None: _plan_stub([]))
    code = cli.main(["sbx", "plan", "--json", "--provider", "meta", "--prune"])
    assert code == 2
    capsys.readouterr()


def _patch_models_dev(monkeypatch, mapping):
    from code_puppy_core_plugins.setup_cli import providers

    monkeypatch.setattr(providers, "_models_dev_providers", lambda: mapping)


def test_auth_login_by_provider_id_saves_canonical_env(capsys, monkeypatch):
    # A models.dev provider id resolves to its declared env vars; the key
    # is stored under env[0], the name generated model configs reference.
    import io
    import sys

    import code_puppy.provider_credentials as pc

    _patch_models_dev(monkeypatch, {"acme-plan": ["ACME_API_KEY", "ACME_ALT_KEY"]})
    saved = {}
    monkeypatch.setattr(
        pc, "save_credential", lambda e, v: saved.update(env=e, value=v)
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("sekrit\n"))
    assert cli.main(["auth", "login", "acme-plan", "--stdin", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert saved == {"env": "ACME_API_KEY", "value": "sekrit"}
    assert data["env_var"] == "ACME_API_KEY"
    assert data["provider"] == "acme-plan"


def test_auth_status_by_provider_id(capsys, monkeypatch):
    from code_puppy_core_plugins.setup_cli import providers

    _patch_models_dev(monkeypatch, {"acme-plan": ["ACME_API_KEY", "ACME_ALT_KEY"]})
    monkeypatch.setattr(providers, "credential_present", lambda e: e == "ACME_ALT_KEY")
    assert cli.main(["auth", "status", "acme-plan", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["authenticated"] is True
    assert data["provider"] == "acme-plan"
    assert data["env_vars"] == {"ACME_API_KEY": False, "ACME_ALT_KEY": True}

    monkeypatch.setattr(providers, "credential_present", lambda e: False)
    assert cli.main(["auth", "status", "acme-plan", "--json"]) == 1
    capsys.readouterr()


def test_auth_logout_by_provider_id_clears_all_envs(capsys, monkeypatch):
    from code_puppy_core_plugins.setup_cli import auth_cmds

    _patch_models_dev(monkeypatch, {"acme-plan": ["ACME_API_KEY", "ACME_ALT_KEY"]})
    cleared = []
    monkeypatch.setattr(auth_cmds, "_clear_stored_credential", cleared.append)
    assert cli.main(["auth", "logout", "acme-plan", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert cleared == ["ACME_API_KEY", "ACME_ALT_KEY"]
    assert data["env_vars"] == ["ACME_API_KEY", "ACME_ALT_KEY"]
    assert data["provider"] == "acme-plan"


def test_auth_provider_id_errors(capsys, monkeypatch):
    _patch_models_dev(monkeypatch, {"local-only": []})
    # Known provider that declares no API-key env var.
    assert cli.main(["auth", "login", "local-only", "--json"]) == 2
    capsys.readouterr()
    # Dashed id unknown to models.dev is not treated as an env var.
    assert cli.main(["auth", "login", "nope-not-real", "--json"]) == 2
    capsys.readouterr()
    assert cli.main(["auth", "status", "nope-not-real", "--json"]) == 2
    capsys.readouterr()


def test_sbx_plan_derives_only_used_credentials(monkeypatch):
    from code_puppy_core_plugins.setup_cli import providers
    from code_puppy_core_plugins.setup_cli.sbx_plan import build_plan

    monkeypatch.setattr(providers, "credential_present", lambda env: False)
    monkeypatch.setattr(providers, "oauth_status", lambda p: {"authenticated": False})

    catalog = {
        "meta-muse-spark-1.3": {"type": "meta_oauth", "name": "muse-spark-1.3"},
        "zai-coding-glm": {"type": "zai_coding", "name": "glm-5"},
        "groq-llama": {
            "type": "custom_openai",
            "name": "llama-3.3-70b",
            "custom_endpoint": {
                "url": "https://api.groq.com/openai/v1",
                "api_key": "$GROQ_API_KEY",
            },
        },
        "claude-code-opus": {"type": "claude_code", "name": "claude-opus"},
        "mystery": {"type": "telepathy", "name": "mind"},
    }
    plan = build_plan(catalog)
    services = {c["service"] for c in plan["credentials"]}
    assert services == {"meta", "zai", "groq"}
    zai = next(c for c in plan["credentials"] if c["service"] == "zai")
    assert zai["env_var"] == "ZAI_API_KEY" and zai["domain"] == "api.z.ai"
    assert zai["header"] == "Authorization" and zai["format"] == "Bearer %s"
    groq = next(c for c in plan["credentials"] if c["service"] == "groq")
    assert groq["domain"] == "api.groq.com"
    # No credential values ever appear in a plan.
    assert "sk-" not in json.dumps(plan)
    assert [o["id"] for o in plan["oauth"]] == ["claude-code"]
    assert "api.anthropic.com" in plan["egress"]
    assert plan["unsupported"][0]["model"] == "mystery"


def test_sbx_plan_meta_only_catalog(capsys, monkeypatch):
    from code_puppy_core_plugins.setup_cli import sbx_plan

    monkeypatch.setattr(sbx_plan, "build_plan", lambda **_: _META_PLAN)
    assert cli.main(["sbx", "plan", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [c["service"] for c in data["credentials"]] == ["meta"]


_META_PLAN = {
    "credentials": [
        {
            "service": "meta",
            "env_var": "META_API_KEY",
            "domain": "api.meta.ai",
            "header": "Authorization",
            "format": "Bearer %s",
            "models": ["meta-x"],
            "host_credential_present": True,
            "transport": "proxy-managed-env",
        }
    ],
    "oauth": [],
    "egress": ["api.meta.ai"],
    "unsupported": [],
    "default_model": "meta-x",
}


def test_creds_mixin_declares_env_vars(tmp_path):
    from code_puppy_core_plugins.setup_cli.sbx_plan import write_creds_mixin

    dest = write_creds_mixin(tmp_path / "creds", _META_PLAN)
    assert dest is not None
    text = (dest / "code-puppy-creds.yaml").read_text(encoding="utf-8")
    # This declaration is what makes sbx create META_API_KEY in the guest.
    assert "name: META_API_KEY" in text
    assert "proxyManaged: true" in text
    assert "domain: api.meta.ai" in text
    assert "api.meta.ai:443" in text
    assert "sk-" not in text
    assert (dest / "code-puppy-creds.dockerfile").is_file()


def test_export_sbx_bundle_includes_creds_mixin(capsys, monkeypatch, tmp_path):
    from code_puppy_core_plugins.setup_cli import sbx_plan

    monkeypatch.setattr(sbx_plan, "build_plan", lambda **_: _META_PLAN)
    dest = tmp_path / "bundle"
    assert cli.main(["export", str(dest), "--sbx", "--json"]) == 0
    manifest = json.loads((dest / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["creds_mixin"] is not None
    creds_yaml = dest / "code-puppy-creds" / "code-puppy-creds.yaml"
    assert "name: META_API_KEY" in creds_yaml.read_text(encoding="utf-8")
