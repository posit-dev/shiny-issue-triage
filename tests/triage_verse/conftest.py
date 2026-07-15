"""Shared fixtures for executor tests.

The executor now calls gh_mod.gh_mutation() for writes, which internally calls
the module-level gh.run_gh. This fixture patches gh.run_gh for all executor
tests so mutations route through the test's FakeGh instance.
"""

from __future__ import annotations

import pytest

from triage_verse import gh


class GhRelay:
    """Stands in for gh.run_gh; delegates to .target (a FakeGh instance)."""

    def __init__(self):
        self.target = None

    def install(self, fake):
        self.target = fake

    def __call__(self, args, *, input=None, **kwargs):
        if self.target is None:
            raise AssertionError("gh.run_gh called but no FakeGh installed on relay")
        return self.target(args, input=input, **kwargs)


@pytest.fixture(autouse=True)
def gh_relay(monkeypatch, request):
    """Auto-patch gh.run_gh for executor tests (where gh_mutation must route to FakeGh)."""
    module = request.node.module.__name__ if hasattr(request.node, "module") else ""
    if "test_executor" not in module:
        yield GhRelay()  # no-op: don't patch for non-executor tests
        return
    relay = GhRelay()
    monkeypatch.setattr(gh, "run_gh", relay)
    yield relay
