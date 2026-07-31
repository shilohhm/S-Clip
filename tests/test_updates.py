"""Tests for the update check.

Nothing here touches the network. The fetch is substituted, which leaves the
parts worth testing: version comparison, the daily throttle, the opt-out, and
the rule that no failure anywhere is allowed to raise.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from sclip.core import updates
from sclip.core.updates import Release, check_for_update, is_newer, parse_version

_DAY = 24 * 60 * 60


class TestVersionComparison:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2.1.0", (2, 1, 0)),
            ("v2.1.0", (2, 1, 0)),
            ("  v10.0.3  ", (10, 0, 3)),
            ("2.1", (2, 1)),
            ("2.1.0-beta.1", (2, 1, 0)),
            ("not-a-version", None),
            ("", None),
        ],
    )
    def test_parse_version(self, text: str, expected: tuple[int, ...] | None) -> None:
        assert parse_version(text) == expected

    @pytest.mark.parametrize(
        ("candidate", "current", "expected"),
        [
            ("v2.1.0", "2.0.0", True),
            ("v2.0.1", "2.0.0", True),
            ("v3.0.0", "2.9.9", True),
            ("v2.0.0", "2.0.0", False),
            ("v2.0.0", "2.1.0", False),
            # 2.1 and 2.1.0 are the same release, not an upgrade.
            ("v2.1", "2.1.0", False),
            ("v2.1.0", "2.1", False),
            # Double-digit components must not compare as strings.
            ("v2.10.0", "2.9.0", True),
            ("v2.9.0", "2.10.0", False),
        ],
    )
    def test_is_newer(self, candidate: str, current: str, expected: bool) -> None:
        assert is_newer(candidate, current) is expected

    @pytest.mark.parametrize(
        ("candidate", "current"),
        [("garbage", "2.0.0"), ("v2.1.0", "garbage"), ("", "")],
    )
    def test_unparseable_versions_never_prompt(self, candidate: str, current: str) -> None:
        # A check that cannot be trusted should stay quiet rather than guess.
        assert is_newer(candidate, current) is False


class TestCheckForUpdate:
    @pytest.fixture()
    def state(self, tmp_path: Path) -> Path:
        return tmp_path / "update-check.json"

    def test_a_newer_release_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, state: Path
    ) -> None:
        monkeypatch.setattr(updates, "_fetch_latest_tag", lambda: "v2.1.0")
        result = check_for_update("2.0.0", state_file=state, now=1000.0)
        assert isinstance(result, Release)
        assert result.version == "2.1.0"
        assert result.url.startswith("https://github.com/")
        assert result.headline == "S-Clip 2.1.0 is available."

    def test_the_current_version_reports_nothing(
        self, monkeypatch: pytest.MonkeyPatch, state: Path
    ) -> None:
        monkeypatch.setattr(updates, "_fetch_latest_tag", lambda: "v2.1.0")
        assert check_for_update("2.1.0", state_file=state, now=1000.0) is None

    def test_the_opt_out_prevents_any_network_call(
        self, monkeypatch: pytest.MonkeyPatch, state: Path
    ) -> None:
        # The switch has to stop the request, not merely hide the result.
        def explode() -> str:
            raise AssertionError("the network must not be touched when disabled")

        monkeypatch.setattr(updates, "_fetch_latest_tag", explode)
        assert check_for_update("2.0.0", state_file=state, enabled=False, now=1000.0) is None
        assert not state.exists()

    def test_a_second_check_the_same_day_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, state: Path
    ) -> None:
        calls: list[int] = []

        def counted() -> str:
            calls.append(1)
            return "v2.1.0"

        monkeypatch.setattr(updates, "_fetch_latest_tag", counted)
        assert check_for_update("2.0.0", state_file=state, now=1000.0) is not None
        assert check_for_update("2.0.0", state_file=state, now=1000.0 + _DAY / 2) is None
        assert len(calls) == 1

    def test_the_check_resumes_the_next_day(
        self, monkeypatch: pytest.MonkeyPatch, state: Path
    ) -> None:
        monkeypatch.setattr(updates, "_fetch_latest_tag", lambda: "v2.1.0")
        assert check_for_update("2.0.0", state_file=state, now=1000.0) is not None
        assert check_for_update("2.0.0", state_file=state, now=1000.0 + _DAY) is not None

    def test_a_failing_check_still_backs_off(
        self, monkeypatch: pytest.MonkeyPatch, state: Path
    ) -> None:
        # Otherwise an offline machine retries on every single launch.
        calls: list[int] = []

        def failing() -> None:
            calls.append(1)

        monkeypatch.setattr(updates, "_fetch_latest_tag", failing)
        assert check_for_update("2.0.0", state_file=state, now=1000.0) is None
        assert check_for_update("2.0.0", state_file=state, now=1000.0 + 60) is None
        assert len(calls) == 1

    def test_a_clock_that_moved_backwards_does_not_wedge_the_check(
        self, monkeypatch: pytest.MonkeyPatch, state: Path
    ) -> None:
        # A timezone change or NTP correction would otherwise park the next
        # check arbitrarily far in the future.
        monkeypatch.setattr(updates, "_fetch_latest_tag", lambda: "v2.1.0")
        state.write_text(json.dumps({"checked_at": 9_000_000.0}), encoding="utf-8")
        assert check_for_update("2.0.0", state_file=state, now=1000.0) is not None

    @pytest.mark.parametrize("content", ['{"checked_at": "yesterday"}', "not json at all", ""])
    def test_a_corrupt_state_file_is_treated_as_never_checked(
        self, monkeypatch: pytest.MonkeyPatch, state: Path, content: str
    ) -> None:
        monkeypatch.setattr(updates, "_fetch_latest_tag", lambda: "v2.1.0")
        state.write_text(content, encoding="utf-8")
        assert check_for_update("2.0.0", state_file=state, now=1000.0) is not None

    def test_an_unwritable_state_directory_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The config directory can be read-only in a locked-down environment.
        monkeypatch.setattr(updates, "_fetch_latest_tag", lambda: "v2.1.0")

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(Path, "write_text", refuse)
        result = check_for_update("2.0.0", state_file=tmp_path / "s.json", now=1000.0)
        assert isinstance(result, Release)


class TestFetchFailures:
    """Every way the network can fail has to come back as ``None``."""

    @pytest.mark.parametrize(
        "failure",
        [
            urllib.error.URLError("no route to host"),
            OSError("connection reset"),
            ValueError("bad url"),
        ],
    )
    def test_network_failures_are_contained(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(updates.urllib.request, "urlopen", explode)
        assert updates._fetch_latest_tag() is None

    @pytest.mark.parametrize(
        "payload",
        [
            b"not json",
            b"[]",  # a list where an object was expected
            b'{"no_tag_here": 1}',
            b'{"tag_name": ""}',
            b'{"tag_name": 42}',
        ],
    )
    def test_unexpected_responses_are_contained(
        self, monkeypatch: pytest.MonkeyPatch, payload: bytes
    ) -> None:
        class _Response:
            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def read(self, _limit: int | None = None) -> bytes:
                return payload

        monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_k: _Response())
        assert updates._fetch_latest_tag() is None

    def test_a_good_response_yields_the_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Response:
            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def read(self, _limit: int | None = None) -> bytes:
                return b'{"tag_name": "v2.1.0", "name": "S-Clip v2.1.0"}'

        monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_k: _Response())
        assert updates._fetch_latest_tag() == "v2.1.0"
