"""Tests for the gemini_loop_guard plugin."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from code_puppy.config import set_config_value


def _module(name: str):
    return importlib.import_module(f"code_puppy_core_plugins.gemini_loop_guard.{name}")


outcomes = _module("outcomes")
policy = _module("policy")
Outcome = outcomes.Outcome


def grep_result(*lines: str, error: str | None = None) -> dict:
    matches = [
        {"file_path": "a.py", "line_number": n, "line_content": line}
        for n, line in enumerate(lines, start=1)
    ]
    return {"matches": matches, "error": error, "truncated": False}


def empty_grep() -> dict:
    return grep_result()


def run_call(tracker, tool_name, args, result, settings=None):
    """Simulate core: pre hook, then (unless blocked) the tool and post hook."""
    decision = tracker.before_call(tool_name, args, settings or policy.GuardSettings())
    if not (decision and decision.get("blocked")):
        tracker.record_result(tool_name, args, result)
    return decision


# ---------------------------------------------------------------------------
# outcomes.classify
# ---------------------------------------------------------------------------


class TestClassify:
    def test_empty_grep_is_empty(self):
        assert outcomes.classify(empty_grep(), set()) is Outcome.EMPTY

    def test_new_matches_are_productive_then_repeat(self):
        seen: set[str] = set()
        assert outcomes.classify(grep_result("x"), seen, "grep:a") is Outcome.PRODUCTIVE
        # A re-worded search that surfaces the same line learns nothing new.
        assert outcomes.classify(grep_result("x"), seen, "grep:b") is Outcome.REPEAT

    def test_error_without_content_is_error(self):
        result = {"matches": [], "error": "bad regex", "truncated": False}
        assert outcomes.classify(result, set()) is Outcome.ERROR

    def test_failing_command_with_new_output_is_productive(self):
        result = {"success": False, "stdout": "1 failed", "error": "exit 1"}
        assert outcomes.classify(result, set(), "shell:pytest") is Outcome.PRODUCTIVE

    def test_hook_annotated_result_is_unknown(self):
        result = "[hook context]\nnote\n\nmatches=[] error=None"
        assert outcomes.classify(result, set()) is Outcome.UNKNOWN

    def test_pydantic_results_are_understood(self):
        class GrepOutput(BaseModel):
            matches: list[dict] = []
            error: str | None = None

        assert outcomes.classify(GrepOutput(), set()) is Outcome.EMPTY

    def test_volatile_keys_do_not_make_output_new(self):
        seen: set[str] = set()
        first = {"stdout": "same", "execution_time": 0.1}
        second = {"stdout": "same", "execution_time": 0.2}
        assert outcomes.classify(first, seen, "shell:ls") is Outcome.PRODUCTIVE
        assert outcomes.classify(second, seen, "shell:ls") is Outcome.REPEAT

    def test_different_edits_with_identical_confirmation_both_count(self):
        seen: set[str] = set()
        confirmation = {"path": "a.py", "message": "Replacements applied."}
        assert outcomes.classify(confirmation, seen, "edit:1") is Outcome.PRODUCTIVE
        assert outcomes.classify(confirmation, seen, "edit:2") is Outcome.PRODUCTIVE

    @pytest.mark.parametrize("result", ["", "   ", [], {}, None])
    def test_blank_results_are_empty(self, result):
        assert outcomes.classify(result, set()) is Outcome.EMPTY


class TestRealToolShapes:
    """Results exactly as core's tools and the output-limit harness return them."""

    @staticmethod
    def shell(command: str, stdout: str = "", stderr: str = "", exit_code: int = 0):
        from code_puppy.tools.command_runner import ShellCommandOutput

        return ShellCommandOutput(
            success=exit_code == 0,
            command=command,
            error="" if exit_code == 0 else "The process didn't exit cleanly!",
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            execution_time=0.4,
        )

    @staticmethod
    def spill(handle: str, preview: str = '{"matches": [{"line": "x"}]}'):
        from pydantic_ai.messages import ToolReturn

        return ToolReturn(
            return_value=(
                f"[Tool output too large (12,000 chars); stored to handle {handle!r}. "
                f"Read it with read_tool_result(handle={handle!r}, offset=0, "
                "limit=200, from_end=False, pattern=None).]\n"
                f"shape: {{matches: list}}\n{preview}"
            )
        )

    def test_real_grep_output(self):
        from code_puppy.tools.file_operations import GrepOutput, MatchInfo

        seen: set[str] = set()
        match = MatchInfo(
            file_path="a.py", line_number=3, line_content="x", is_context=False
        )
        assert outcomes.classify(GrepOutput(matches=[]), seen) is Outcome.EMPTY
        assert (
            outcomes.classify(GrepOutput(matches=[match]), seen) is Outcome.PRODUCTIVE
        )
        assert outcomes.classify(GrepOutput(matches=[match]), seen) is Outcome.REPEAT

    def test_shell_command_that_prints_nothing_is_empty(self):
        tracker = policy.RunTracker()
        for command in ('git log -S "x"', 'git log -G "x"', 'git log --grep "x"'):
            outcome = tracker.record_result(
                "agent_run_shell_command", {"command": command}, self.shell(command)
            )
            assert outcome is Outcome.EMPTY

    def test_shell_command_with_output_is_productive(self):
        tracker = policy.RunTracker()
        args = {"command": "git log --oneline"}
        result = self.shell(args["command"], stdout="abc123 fix")
        assert (
            tracker.record_result("agent_run_shell_command", args, result)
            is Outcome.PRODUCTIVE
        )

    def test_failed_shell_command_without_output_is_error(self):
        tracker = policy.RunTracker()
        args = {"command": "false"}
        result = self.shell("false", exit_code=1)
        assert (
            tracker.record_result("agent_run_shell_command", args, result)
            is Outcome.ERROR
        )

    def test_same_large_output_spilled_twice_is_a_repeat(self):
        seen: set[str] = set()
        assert outcomes.classify(self.spill("h1"), seen, "grep:a") is Outcome.PRODUCTIVE
        assert outcomes.classify(self.spill("h2"), seen, "grep:b") is Outcome.REPEAT

    def test_different_large_outputs_are_productive(self):
        seen: set[str] = set()
        outcomes.classify(self.spill("h1", "first"), seen, "grep:a")
        assert (
            outcomes.classify(self.spill("h2", "second"), seen, "grep:b")
            is Outcome.PRODUCTIVE
        )

    def test_tool_return_wrapping_structured_output_is_unwrapped(self):
        from pydantic_ai.messages import ToolReturn

        assert outcomes.classify(ToolReturn(return_value=empty_grep()), set()) is (
            Outcome.EMPTY
        )

    def test_reworded_lookups_that_find_nothing_trigger_reflection(self):
        """Modelled on a real Gemini loop: lookups retried with small
        variations, each failing silently or printing nothing."""
        tracker = policy.RunTracker()
        lookups = [
            self.shell("cfg.py db-get KEY", exit_code=1),
            self.shell("cfg.py rest get KEY"),
            self.shell('git log -S "KEY"'),
            self.shell("cfg.py rest dump", exit_code=1),
        ]
        decisions = [
            run_call(tracker, "agent_run_shell_command", {"command": r.command}, r)
            for r in lookups
        ]
        assert decisions[:3] == [None] * 3
        assert "context_message" in decisions[3]

    def test_large_output_seen_again_counts_toward_the_streak(self):
        tracker = policy.RunTracker()
        run_call(tracker, "grep", {"search_string": "-i cooldown"}, self.spill("h1"))
        for n, handle in enumerate(("h2", "h3", "h4")):
            run_call(
                tracker, "grep", {"search_string": f"cooldown {n}"}, self.spill(handle)
            )
        assert len(tracker.streak) == 3


# ---------------------------------------------------------------------------
# policy.RunTracker
# ---------------------------------------------------------------------------


class TestReflect:
    def test_long_productive_run_never_intervenes(self):
        tracker = policy.RunTracker()
        for n in range(30):
            decision = run_call(
                tracker,
                "read_file",
                {"file_path": f"f{n}.py"},
                {"content": f"body {n}"},
            )
            assert decision is None

    def test_reflects_after_three_unproductive_calls_and_tool_still_runs(self):
        tracker = policy.RunTracker()
        for pattern in ("Foo", "foo", "FooBar"):
            assert (
                run_call(tracker, "grep", {"search_string": pattern}, empty_grep())
                is None
            )

        decision = tracker.before_call(
            "grep", {"search_string": "Baz"}, policy.GuardSettings()
        )

        assert "blocked" not in decision
        message = decision["context_message"]
        assert "last 3 tool calls found nothing new" in message
        assert '{"search_string":"FooBar"} -> no results' in message
        assert "fine to say so" in message

    def test_reflects_once_per_streak(self):
        tracker = policy.RunTracker()
        decisions = [
            run_call(tracker, "grep", {"search_string": f"p{n}"}, empty_grep())
            for n in range(8)
        ]
        assert sum(1 for d in decisions if d and "context_message" in d) == 1

    def test_productive_call_resets_streak_and_rearms(self):
        tracker = policy.RunTracker()
        for n in range(4):
            run_call(tracker, "grep", {"search_string": f"p{n}"}, empty_grep())
        run_call(tracker, "grep", {"search_string": "hit"}, grep_result("found"))
        assert tracker.streak == []
        decisions = [
            run_call(tracker, "grep", {"search_string": f"q{n}"}, empty_grep())
            for n in range(4)
        ]
        assert decisions[-1] and "context_message" in decisions[-1]

    def test_reflect_after_zero_disables(self):
        tracker = policy.RunTracker()
        settings = policy.GuardSettings(reflect_after=0)
        for n in range(10):
            assert run_call(tracker, "grep", {"q": n}, empty_grep(), settings) is None


class TestDedupe:
    def test_exact_repeat_in_streak_is_skipped_with_a_calm_result(self):
        tracker = policy.RunTracker()
        run_call(tracker, "grep", {"search_string": "Foo"}, empty_grep())

        decision = tracker.before_call(
            "grep", {"search_string": "Foo"}, policy.GuardSettings()
        )

        assert decision["blocked"] is True
        assert "returned no results earlier" in decision["tool_result"]
        assert tracker.streak[-1].outcome is Outcome.REPEAT

    def test_repeat_after_productive_call_runs(self):
        tracker = policy.RunTracker()
        run_call(tracker, "shell", {"command": "pytest"}, {"stdout": "1 failed"})
        run_call(tracker, "edit", {"path": "a.py", "new": "fix"}, {"message": "ok"})
        assert (
            tracker.before_call("shell", {"command": "pytest"}, policy.GuardSettings())
            is None
        )

    def test_repeat_after_unknown_outcome_runs(self):
        """A hook-annotated call (e.g. emoji-stripped write) may have changed state."""
        tracker = policy.RunTracker()
        run_call(
            tracker, "shell", {"command": "pytest"}, {"stdout": "boom", "error": "1"}
        )
        run_call(
            tracker,
            "shell",
            {"command": "pytest"},
            {"stdout": "boom", "error": "1"},
            policy.GuardSettings(dedupe=False),
        )
        tracker.record_result(
            "create_file", {"path": "a.py"}, "[hook context]\nx\n\nok"
        )
        assert (
            tracker.before_call("shell", {"command": "pytest"}, policy.GuardSettings())
            is None
        )

    def test_rerunning_tests_after_a_second_edit_to_same_file_runs(self):
        tracker = policy.RunTracker()
        failing = {"stdout": "1 failed", "error": "exit 1"}
        confirmation = {"path": "a.py", "message": "Replacements applied."}
        run_call(tracker, "shell", {"command": "pytest"}, failing)
        run_call(tracker, "edit", {"path": "a.py", "new": "v1"}, confirmation)
        run_call(tracker, "shell", {"command": "pytest"}, failing)
        run_call(tracker, "edit", {"path": "a.py", "new": "v2"}, confirmation)
        assert (
            tracker.before_call("shell", {"command": "pytest"}, policy.GuardSettings())
            is None
        )

    def test_dedupe_can_be_disabled(self):
        tracker = policy.RunTracker()
        settings = policy.GuardSettings(dedupe=False, reflect_after=0)
        run_call(tracker, "grep", {"q": "Foo"}, empty_grep(), settings)
        assert tracker.before_call("grep", {"q": "Foo"}, settings) is None


class TestBackstop:
    def test_off_by_default(self):
        tracker = policy.RunTracker()
        for n in range(20):
            decision = run_call(tracker, "grep", {"q": n}, empty_grep())
            assert not (decision and decision.get("blocked"))

    def test_blocks_once_enabled_threshold_is_reached(self):
        tracker = policy.RunTracker()
        settings = policy.GuardSettings(block_after=5)
        for n in range(5):
            run_call(tracker, "grep", {"q": n}, empty_grep(), settings)
        decision = tracker.before_call("grep", {"q": "next"}, settings)
        assert decision["blocked"] is True
        assert "answer the user" in decision["error_message"]


# ---------------------------------------------------------------------------
# config + hook wiring
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults(self):
        config = _module("config")
        assert config.is_enabled() is True
        assert config.guard_settings() == policy.GuardSettings(3, True, 0, 3)

    def test_values_and_garbage(self):
        config = _module("config")
        set_config_value(config.REFLECT_AFTER_KEY, "5")
        set_config_value(config.BLOCK_AFTER_KEY, "not-a-number")
        set_config_value(config.DEDUPE_KEY, "off")
        set_config_value(config.VARIATION_AFTER_KEY, "0")
        assert config.guard_settings() == policy.GuardSettings(5, False, 0, 0)

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("gemini-3-flash", True),
            ("Gemini-4-Argon", True),
            ("claude-4", False),
            (None, False),
        ],
    )
    def test_gemini_scoping(self, name, expected):
        assert _module("config").is_gemini_model(name) is expected


class _Agent:
    def __init__(self, model_name: str):
        self.model_name = model_name

    def get_model_name(self) -> str:
        return self.model_name


@pytest.fixture
def hooks(monkeypatch):
    module = _module("register_callbacks")
    module._trackers.clear()
    state = SimpleNamespace(agent=None, module=module)
    monkeypatch.setattr(module, "get_executing_agent", lambda: state.agent)
    return state


def _loop(hooks, calls: int) -> list:
    decisions = []
    for n in range(calls):
        args = {"search_string": f"p{n}"}
        decisions.append(hooks.module._on_pre_tool_call("grep", args))
        hooks.module._on_post_tool_call("grep", args, empty_grep(), 1.0)
    return decisions


class TestHooks:
    def test_gemini_agent_gets_reflection(self, hooks):
        hooks.agent = _Agent("gemini-3-flash")
        assert any(d and "context_message" in d for d in _loop(hooks, 4))

    def test_other_models_are_untouched(self, hooks):
        hooks.agent = _Agent("claude-sonnet-4")
        assert _loop(hooks, 10) == [None] * 10
        assert len(hooks.module._trackers) == 0

    def test_no_agent_run_means_no_tracking(self, hooks):
        assert _loop(hooks, 10) == [None] * 10

    def test_disabled_by_config(self, hooks):
        set_config_value(_module("config").ENABLED_KEY, "false")
        hooks.agent = _Agent("gemini-3-flash")
        assert _loop(hooks, 10) == [None] * 10

    def test_new_run_starts_clean(self, hooks):
        hooks.agent = _Agent("gemini-3-flash")
        _loop(hooks, 3)
        hooks.module._on_agent_run_start("code-puppy", "gemini-3-flash", "session")
        assert _loop(hooks, 3) == [None] * 3

    def test_sub_agents_have_their_own_streak(self, hooks):
        parent, child = _Agent("gemini-3-flash"), _Agent("gemini-3-flash")
        hooks.agent = parent
        _loop(hooks, 3)
        hooks.agent = child
        assert _loop(hooks, 3) == [None] * 3


# ---------------------------------------------------------------------------
# variation: same tool, small variations, whatever the calls return
# ---------------------------------------------------------------------------

variation = _module("variation")
SHELL = "agent_run_shell_command"


def _nudges(tracker, calls, settings=None):
    """Run (tool, args) calls, each returning something new; collect reflections."""
    messages = []
    for number, (tool, args) in enumerate(calls):
        result = {"stdout": f"fresh output {number}"}
        decision = run_call(tracker, tool, args, result, settings)
        if decision and "context_message" in decision:
            messages.append((number + 1, decision["context_message"]))
    return messages


def _shell(command: str):
    return SHELL, {"command": command}


def _lookups(count: int):
    """Reworded config lookups: same script, small differences each time."""
    flags = ["db-show KEY", "db-show 59 KEY", "db-show KEY x", "db-show 740 KEY"]
    return [
        _shell(f"python3 tools/cfg.py {flags[n % len(flags)]}") for n in range(count)
    ]


class TestVariation:
    def test_reworded_calls_reflect_even_though_each_returns_something_new(self):
        nudges = _nudges(policy.RunTracker(), _lookups(4))
        assert [number for number, _ in nudges] == [3]
        message = nudges[0][1]
        assert "[loop check]" in message
        assert "near-identical" in message
        assert "tools/cfg.py db-show KEY" in message
        assert "answer now" in message

    def test_fires_once_per_run_of_look_alikes(self):
        nudges = _nudges(policy.RunTracker(), _lookups(8))
        assert len(nudges) == 1

    def test_never_fires_for_read_file_or_list_files(self):
        calls = [("read_file", {"file_path": "tools/cfg.py"})] * 6 + [
            ("list_files", {"directory": "tools"})
        ] * 6
        assert _nudges(policy.RunTracker(), calls) == []

    def test_different_symbols_in_the_same_directory_are_different_searches(self):
        calls = [
            ("grep", {"search_string": name, "directory": "/repo/some/long/dir"})
            for name in ("getAlgoTasks", "capacity|consumed", "AUTO_JUDGEMENT")
        ]
        assert _nudges(policy.RunTracker(), calls) == []

    def test_looking_at_different_files_is_progress(self):
        calls = [
            _shell(f"cd /repo && git show origin/feature-x:src/{name}")
            for name in ("config.py", "constants.py", "runner.py", "task.py")
        ]
        assert _nudges(policy.RunTracker(), calls) == []

    def test_an_edit_clears_the_history(self):
        tracker = policy.RunTracker()
        calls = _lookups(2) + [("replace_in_file", {"file_path": "a.py"})]
        calls += _lookups(2)
        assert _nudges(tracker, calls) == []

    def test_sleep_polling_is_exempt(self):
        calls = [
            _shell(f"sleep 30; tail -n 3 /tmp/run.log # check {n}") for n in range(6)
        ]
        assert _nudges(policy.RunTracker(), calls) == []

    def test_zero_disables_it(self):
        settings = policy.GuardSettings(reflect_after=0, variation_after=0)
        assert _nudges(policy.RunTracker(), _lookups(8), settings) == []

    def test_threshold_is_configurable(self):
        settings = policy.GuardSettings(reflect_after=0, variation_after=5)
        nudges = _nudges(policy.RunTracker(), _lookups(8), settings)
        assert [number for number, _ in nudges] == [5]

    def test_rearms_once_the_calls_stop_resembling_each_other(self):
        unrelated = [
            _shell(command)
            for command in (
                "ls docs",
                "git status",
                "pwd",
                "whoami",
                "date",
                "uname -a",
                "df -h",
                "uptime",
            )
        ]
        calls = _lookups(3) + unrelated + _lookups(3)
        assert len(_nudges(policy.RunTracker(), calls)) == 2

    def test_one_combined_message_when_both_signals_trigger(self):
        tracker = policy.RunTracker()
        settings = policy.GuardSettings(reflect_after=2, variation_after=3)
        nothing = {"stdout": "", "stderr": ""}
        decisions = [
            run_call(
                tracker,
                SHELL,
                {"command": f"python3 tools/cfg.py db-show KEY {suffix}"},
                nothing,
                settings,
            )
            for suffix in ("a", "b", "c")
        ]
        assert decisions[:2] == [None, None]
        text = decisions[2]["context_message"]
        assert text.count("[loop check]") == 1
        assert "found nothing new" in text and "near-identical" in text

    def test_variation_message_alone_does_not_claim_an_empty_streak(self):
        text = _nudges(policy.RunTracker(), _lookups(3))[0][1]
        assert "found nothing new" not in text

    def test_never_blocks_whatever_the_backstop_says_about_it(self):
        tracker = policy.RunTracker()
        decisions = [
            run_call(tracker, tool, args, {"stdout": str(n)})
            for n, (tool, args) in enumerate(_lookups(12))
        ]
        assert not any(d and d.get("blocked") for d in decisions)


class TestCallShape:
    def shape(self, command: str):
        return variation.call_shape(SHELL, {"command": command})

    def test_cd_env_and_activate_noise_is_ignored(self):
        plain = self.shape("python tools/cfg.py db-show KEY")
        noisy = self.shape(
            "cd /Users/x/proj && export FOO=bar && BAZ='a b' source .venv/bin/activate "
            "&& python tools/cfg.py db-show KEY"
        )
        assert noisy.tokens == plain.tokens

    def test_absolute_paths_reduce_to_their_basename(self):
        a = self.shape(
            "/Users/alice/proj/.venv/bin/python /Users/alice/proj/tools/run.py"
        )
        b = self.shape("/home/bob/other/.venv/bin/python /opt/elsewhere/tools/run.py")
        assert a.tokens == b.tokens

    def test_options_with_equals_are_kept(self):
        assert "env" in self.shape("tool --env=dev").tokens

    def test_grep_compares_only_the_pattern(self):
        a = variation.call_shape("grep", {"search_string": "Foo", "directory": "/a/b"})
        b = variation.call_shape("grep", {"search_string": "Foo", "directory": "/c/d"})
        assert a == b

    def test_non_dict_args_do_not_crash(self):
        assert variation.call_shape(SHELL, "ls -la").tokens == {"ls", "la"}
