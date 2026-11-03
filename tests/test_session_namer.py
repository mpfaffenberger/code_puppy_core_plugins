"""Tests for the session_namer plugin: policy, parsing, incremental naming."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from code_puppy_core_plugins.session_namer import namer
from code_puppy_core_plugins.session_namer.register_callbacks import (
    _enabled,
    _on_post_autosave,
    _on_session_browser_open,
)


class Part:
    def __init__(self, part_kind, content):
        self.part_kind = part_kind
        self.content = content


class Msg:
    def __init__(self, kind, parts):
        self.kind = kind
        self.parts = parts


def _history():
    return [
        Msg("request", [Part("user-prompt", "fix the flaky renderer test")]),
        Msg("response", [Part("text", "On it -- the race is in teardown.")]),
        Msg("request", [Part("user-prompt", "also add a regression test")]),
    ]


USER_META = {"title": "Mine", "title_source": "user"}
OWNERSHIP_CASES = [
    ({}, False),
    ({"title_source": "user"}, True),
    ({"title_source": "auto"}, False),
    ({"title_source": None}, False),
]


class TestUserTitled:
    @pytest.mark.parametrize(("meta", "expected"), OWNERSHIP_CASES)
    def test_fallback_when_core_lacks_helper(self, monkeypatch, meta, expected):
        import code_puppy.session_storage as storage

        monkeypatch.delattr(storage, "has_user_title", raising=False)
        assert namer._user_titled(meta) is expected

    @pytest.mark.parametrize(("meta", "expected"), OWNERSHIP_CASES)
    def test_matches_core_helper(self, meta, expected):
        import code_puppy.session_storage as storage

        if not hasattr(storage, "has_user_title"):
            pytest.skip("core predates /session rename")
        assert namer._user_titled(meta) is expected
        assert storage.has_user_title(meta) is expected


class TestNamingNeeded:
    def test_never_named_needs_naming(self):
        assert namer.naming_needed({"message_count": 3}) is True

    def test_recently_named_skips(self):
        meta = {"ai_named_at": 10, "message_count": 12}
        assert namer.naming_needed(meta) is False

    def test_grown_session_renames(self):
        meta = {"ai_named_at": 10, "message_count": 10 + namer.RENAME_DELTA}
        assert namer.naming_needed(meta) is True

    @pytest.mark.parametrize(
        "meta",
        [
            {**USER_META, "message_count": 3},
            {**USER_META, "ai_named_at": 1, "message_count": 1 + namer.RENAME_DELTA},
        ],
        ids=["never-named", "grown"],
    )
    def test_user_titled_never_needs_naming(self, meta):
        assert namer.naming_needed(meta) is False


class TestParseNaming:
    def test_parses_clean_json(self):
        raw = json.dumps(
            {"title": "Fix renderer", "subtitle": "teardown race", "tags": ["pyte"]}
        )
        parsed = namer.parse_naming(raw)
        assert parsed == {
            "title": "Fix renderer",
            "subtitle": "teardown race",
            "tags": ["pyte"],
        }

    def test_parses_json_wrapped_in_prose_or_fences(self):
        raw = 'Sure!\n```json\n{"title": "T", "tags": ["a"]}\n```'
        parsed = namer.parse_naming(raw)
        assert parsed["title"] == "T"
        assert parsed["subtitle"] == ""

    def test_cleans_tags(self):
        raw = json.dumps(
            {"title": "T", "tags": ["#PyTe", "  ", "ok", 7, "b", "c", "extra"]}
        )
        parsed = namer.parse_naming(raw)
        assert parsed["tags"] == ["pyte", "ok", "b", "c"]

    def test_rejects_garbage_and_missing_title(self):
        assert namer.parse_naming("no json here") is None
        assert namer.parse_naming(json.dumps({"subtitle": "x"})) is None
        assert namer.parse_naming(json.dumps({"title": "  "})) is None


class TestDigestAndPrompt:
    def test_digest_includes_user_and_assistant_turns(self):
        digest = namer.build_digest(_history())
        assert "user: fix the flaky renderer test" in digest
        assert "assistant: On it" in digest

    def test_digest_respects_since_offset(self):
        digest = namer.build_digest(_history(), since=2)
        assert "fix the flaky" not in digest
        assert "regression test" in digest

    def test_digest_respects_char_budget(self):
        history = [Msg("request", [Part("user-prompt", "x" * 300)]) for _ in range(50)]
        assert len(namer.build_digest(history)) <= namer._DIGEST_CHAR_BUDGET + 10

    def test_prompt_feeds_previous_summary_for_incremental_update(self):
        previous = {"title": "Old title", "subtitle": "old", "tags": ["a"]}
        prompt = namer.build_prompt("user: new stuff", previous)
        assert "Old title" in prompt
        assert "Conversation since then" in prompt
        fresh = namer.build_prompt("user: new stuff", None)
        assert "Previous summary" not in fresh


class TestNameSession:
    def _run(self, tmp_path, meta, model_output):
        (tmp_path / "s1_meta.json").write_text(json.dumps(meta))

        async def fake_ask(model, prompt):
            fake_ask.prompt = prompt
            return model_output

        live = dict(meta)
        with (
            patch.object(namer, "_ask", fake_ask),
            patch.object(namer, "resolve_model_name", return_value="gpt-cheap"),
            patch(
                "code_puppy.session_storage.load_session",
                return_value=_history(),
            ),
        ):
            changed = namer.name_session(tmp_path, "s1", live_meta=live)
        return changed, live, fake_ask

    def test_first_naming_writes_sidecar_and_live_meta(self, tmp_path):
        output = json.dumps(
            {"title": "Fix renderer", "subtitle": "teardown", "tags": ["pyte"]}
        )
        changed, live, ask = self._run(tmp_path, {"message_count": 3}, output)
        assert changed is True
        sidecar = json.loads((tmp_path / "s1_meta.json").read_text())
        assert sidecar["title"] == "Fix renderer"
        assert sidecar["tags"] == ["pyte"]
        assert sidecar["ai_named_at"] == 3
        assert live["title"] == "Fix renderer"
        assert "Previous summary" not in ask.prompt

    def test_rename_feeds_previous_summary(self, tmp_path):
        meta = {
            "message_count": 3 + namer.RENAME_DELTA,
            "ai_named_at": 1,
            "title": "Old title",
            "subtitle": "old",
            "tags": ["a"],
        }
        output = json.dumps({"title": "New title", "tags": []})
        changed, live, ask = self._run(tmp_path, meta, output)
        assert changed is True
        assert "Previous summary" in ask.prompt
        assert "Old title" in ask.prompt
        assert live["title"] == "New title"

    def test_recently_named_is_noop(self, tmp_path):
        meta = {"message_count": 5, "ai_named_at": 5}
        changed, live, _ = self._run(tmp_path, meta, "{}")
        assert changed is False
        assert live.get("title") is None

    def test_empty_digest_snoozes_without_model_call(self, tmp_path):
        (tmp_path / "s1_meta.json").write_text(json.dumps({"message_count": 1}))
        with (
            patch.object(namer, "resolve_model_name", side_effect=AssertionError),
            patch(
                "code_puppy.session_storage.load_session",
                return_value=[Msg("request", [Part("tool-return", "noise")])],
            ),
        ):
            assert namer.name_session(tmp_path, "s1") is False
        sidecar = json.loads((tmp_path / "s1_meta.json").read_text())
        assert sidecar["ai_named_at"] == 1  # snoozed, not retried forever

    def test_unparseable_output_writes_nothing(self, tmp_path):
        changed, live, _ = self._run(
            tmp_path, {"message_count": 3}, "I refuse to answer in JSON"
        )
        assert changed is False
        sidecar = json.loads((tmp_path / "s1_meta.json").read_text())
        assert "title" not in sidecar

    def test_user_titled_session_skips_model_call(self, tmp_path):
        path = tmp_path / "s1_meta.json"
        path.write_text(json.dumps({**USER_META, "message_count": 3}))
        before = path.read_text()
        with (
            patch.object(namer, "resolve_model_name", side_effect=AssertionError),
            patch(
                "code_puppy.session_storage.load_session", side_effect=AssertionError
            ),
        ):
            assert namer.name_session(tmp_path, "s1") is False
        assert path.read_text() == before

    def test_rename_during_model_call_keeps_user_title(self, tmp_path):
        from code_puppy.command_line.session_browser_data import merge_sidecar

        path = tmp_path / "s1_meta.json"
        path.write_text(json.dumps({"message_count": 3}))

        async def ask_while_user_renames(model, prompt):
            merge_sidecar(
                tmp_path, "s1", {"title": "User pick", "title_source": "user"}
            )
            return json.dumps({"title": "AI pick", "subtitle": "ai", "tags": ["ai"]})

        live = {"message_count": 3}
        with (
            patch.object(namer, "_ask", ask_while_user_renames),
            patch.object(namer, "resolve_model_name", return_value="gpt-cheap"),
            patch("code_puppy.session_storage.load_session", return_value=_history()),
        ):
            assert namer.name_session(tmp_path, "s1", live_meta=live) is False
        assert json.loads(path.read_text()) == {
            "message_count": 3,
            "title": "User pick",
            "title_source": "user",
        }
        assert live == {"message_count": 3}

    def test_live_meta_mirrors_disk_when_core_keeps_user_title(self, tmp_path):
        # A rename landing between the re-check and the write: core's guard keeps it.
        def guarded_merge(base_dir, session_name, updates):
            kept = {key: value for key, value in updates.items() if key != "title"}
            stored = {"message_count": 3, **USER_META, **kept}
            (base_dir / f"{session_name}_meta.json").write_text(json.dumps(stored))
            return True

        with patch(
            "code_puppy.command_line.session_browser_data.merge_sidecar", guarded_merge
        ):
            changed, live, _ = self._run(
                tmp_path,
                {"message_count": 3},
                json.dumps({"title": "AI pick", "tags": ["ai"]}),
            )
        assert changed is True
        assert (live["title"], live["tags"], live["ai_named_at"]) == ("Mine", ["ai"], 3)


class TestSubmit:
    def test_deduplicates_inflight_sessions(self, tmp_path):
        started = []
        with patch.object(namer, "name_session", side_effect=started.append):
            with namer._inflight_lock:
                namer._inflight.add("busy")
            try:
                assert namer.submit(tmp_path, "busy") is False
            finally:
                with namer._inflight_lock:
                    namer._inflight.discard("busy")


class TestHooks:
    def test_enabled_defaults_on_and_respects_off(self):
        with patch("code_puppy.config.get_value", return_value=None):
            assert _enabled() is True
        with patch("code_puppy.config.get_value", return_value="off"):
            assert _enabled() is False

    def test_browser_open_backfills_up_to_limit(self, tmp_path):
        entries = [
            (f"s{i}", {"message_count": 5}) for i in range(namer.BACKFILL_LIMIT + 5)
        ]
        with (
            patch("code_puppy.config.get_value", return_value=None),
            patch.object(namer, "submit", return_value=True) as mock_submit,
        ):
            _on_session_browser_open(str(tmp_path), entries)
        assert mock_submit.call_count == namer.BACKFILL_LIMIT

    def test_browser_open_skips_named_sessions(self, tmp_path):
        entries = [
            ("named", {"message_count": 5, "ai_named_at": 5}),
            ("unnamed", {"message_count": 5}),
        ]
        with (
            patch("code_puppy.config.get_value", return_value=None),
            patch.object(namer, "submit", return_value=True) as mock_submit,
        ):
            _on_session_browser_open(str(tmp_path), entries)
        assert mock_submit.call_count == 1
        assert mock_submit.call_args.args[1] == "unnamed"

    def test_disabled_backfills_nothing(self, tmp_path):
        with (
            patch("code_puppy.config.get_value", return_value="off"),
            patch.object(namer, "submit") as mock_submit,
        ):
            _on_session_browser_open(str(tmp_path), [("s", {"message_count": 5})])
        mock_submit.assert_not_called()

    def test_post_autosave_skips_user_titled_session(self, tmp_path):
        (tmp_path / "s1_meta.json").write_text(
            json.dumps({**USER_META, "message_count": 40})
        )
        metadata = SimpleNamespace(
            json_path=tmp_path / "s1.json", session_name="s1", message_count=40
        )
        with (
            patch("code_puppy.config.get_value", return_value=None),
            patch.object(namer, "submit") as mock_submit,
        ):
            _on_post_autosave(metadata)
        mock_submit.assert_not_called()

    def test_browser_open_skips_user_titled_sessions(self, tmp_path):
        entries = [
            ("mine", {**USER_META, "message_count": 5}),
            ("auto", {"message_count": 5}),
        ]
        with (
            patch("code_puppy.config.get_value", return_value=None),
            patch.object(namer, "submit", return_value=True) as mock_submit,
        ):
            _on_session_browser_open(str(tmp_path), entries)
        assert [call.args[1] for call in mock_submit.call_args_list] == ["auto"]
