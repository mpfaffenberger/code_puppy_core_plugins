"""Contract tests for the ACP theme catalog (list ext method, meta, change pushes)."""

from __future__ import annotations

import asyncio

import pytest
from acp import RequestError

from code_puppy_core_plugins.acp import state, themes
from code_puppy_core_plugins.theme.catalog import build_catalog


class _ThemeSource:
    """Stands in for read_active_theme_id; counts reads so tests can wait on them."""

    def __init__(self, value):
        self.value = value
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.value


class _FakeConnection:
    def __init__(self):
        self.sent = []
        self.fail = False

    async def ext_notification(self, method, params):
        if self.fail:
            raise ConnectionError("transport closed")
        self.sent.append((method, params))


@pytest.fixture(autouse=True)
def _restore_connection():
    previous = (state.get_connection(), state.get_loop())
    state.set_connection(None, None)
    yield
    state.set_connection(*previous)


@pytest.fixture
def theme_source(monkeypatch):
    source = _ThemeSource("ocean")
    monkeypatch.setattr(themes, "read_active_theme_id", source)
    return source


@pytest.fixture
def connection():
    fake = _FakeConnection()
    state.set_connection(fake, None)
    return fake


async def _wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.005)


@pytest.mark.parametrize("active", [None, "ocean"])
async def test_list_returns_full_contract_without_a_session(theme_source, active):
    theme_source.value = active

    result = await themes.handle_ext_method(themes.EXT_LIST, {})

    assert result == {
        "schemaVersion": 1,
        "active": active,
        "themes": build_catalog(),
    }


@pytest.mark.parametrize(
    "method",
    [
        "code_puppy/themes/set",
        "code_puppy/theme/set",
        "code_puppy/themes/list/extra",
        "other/thing",
    ],
)
async def test_unknown_ext_method_is_method_not_found(method):
    with pytest.raises(RequestError) as raised:
        await themes.handle_ext_method(method, {})

    assert raised.value.code == -32601
    assert raised.value.data == {"method": "_" + method}


@pytest.mark.parametrize("active", [None, "ocean"])
def test_session_meta_reports_active_theme(theme_source, active):
    theme_source.value = active

    assert themes.session_meta() == {themes.META_KEY: {"active": active}}


async def test_first_observation_is_baseline_and_sends_nothing(
    theme_source, connection
):
    watcher = themes.ThemeWatcher()

    await watcher.poll_once()

    assert connection.sent == []


async def test_change_sends_exactly_one_notification(theme_source, connection):
    watcher = themes.ThemeWatcher()
    await watcher.poll_once()

    theme_source.value = "dusk"
    await watcher.poll_once()

    assert connection.sent == [(themes.EXT_CHANGED, {"active": "dusk"})]


async def test_unchanged_theme_sends_nothing(theme_source, connection):
    watcher = themes.ThemeWatcher()

    for _ in range(3):
        await watcher.poll_once()

    assert connection.sent == []


async def test_change_to_none_then_to_value_notifies_each_transition(
    theme_source, connection
):
    watcher = themes.ThemeWatcher()
    await watcher.poll_once()

    theme_source.value = None
    await watcher.poll_once()
    theme_source.value = "dusk"
    await watcher.poll_once()

    assert connection.sent == [
        (themes.EXT_CHANGED, {"active": None}),
        (themes.EXT_CHANGED, {"active": "dusk"}),
    ]


async def test_failed_send_is_swallowed_and_later_polls_still_notify(
    theme_source, connection
):
    watcher = themes.ThemeWatcher()
    await watcher.poll_once()
    connection.fail = True
    theme_source.value = "dusk"

    await watcher.poll_once()
    connection.fail = False
    theme_source.value = "ember"
    await watcher.poll_once()

    assert connection.sent == [(themes.EXT_CHANGED, {"active": "ember"})]


async def test_failed_read_is_swallowed_and_loop_recovers(monkeypatch, connection):
    calls = iter([RuntimeError("disk gone"), "ocean", "dusk"])

    def read():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(themes, "read_active_theme_id", read)
    watcher = themes.ThemeWatcher()

    await watcher.poll_once()
    await watcher.poll_once()
    await watcher.poll_once()

    assert connection.sent == [(themes.EXT_CHANGED, {"active": "dusk"})]


async def test_change_without_connection_updates_baseline(theme_source):
    watcher = themes.ThemeWatcher()
    await watcher.poll_once()
    theme_source.value = "dusk"
    await watcher.poll_once()  # no connection: must not raise

    fake = _FakeConnection()
    state.set_connection(fake, None)
    await watcher.poll_once()

    assert fake.sent == []


def test_poll_interval_defaults_to_poll_seconds_and_accepts_override():
    assert themes.POLL_SECONDS == 2.0
    assert themes.ThemeWatcher()._poll_seconds == themes.POLL_SECONDS
    assert themes.ThemeWatcher(0.5)._poll_seconds == 0.5


async def test_start_is_idempotent_and_runs_one_task(theme_source):
    before = len(asyncio.all_tasks())
    watcher = themes.ThemeWatcher(poll_seconds=60)

    watcher.start()
    watcher.start()

    assert len(asyncio.all_tasks()) == before + 1
    watcher.stop()


async def test_stop_cancels_the_running_task(theme_source):
    watcher = themes.ThemeWatcher(poll_seconds=60)
    watcher.start()
    task = watcher._task
    await _wait_until(lambda: theme_source.reads >= 1)

    watcher.stop()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_stop_is_safe_when_never_started_and_when_repeated():
    watcher = themes.ThemeWatcher()

    watcher.stop()
    watcher.stop()


async def test_running_loop_observes_one_change_and_notifies_once(
    theme_source, connection
):
    watcher = themes.ThemeWatcher(poll_seconds=0.01)
    watcher.start()
    await _wait_until(lambda: theme_source.reads >= 1)

    theme_source.value = "dusk"
    await _wait_until(lambda: connection.sent)
    reads_after_change = theme_source.reads
    await _wait_until(lambda: theme_source.reads >= reads_after_change + 3)
    watcher.stop()

    assert connection.sent == [(themes.EXT_CHANGED, {"active": "dusk"})]
