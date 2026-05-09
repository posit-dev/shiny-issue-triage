#!/usr/bin/env python3
"""Enforce engine guardrails declared in .github/triage/team-issue-triage.yaml.

Reads ``engine.forbidden_secrets`` and ``engine.forbidden_direct_endpoints``
from the triage config and grep-checks the workflow file (and any extra files
passed on the command line) for either. Exits non-zero on the first hit so the
workflow fails before invoking Claude.

Intended usage:
    python .github/triage/scripts/check-engine-guardrails.py \
        .github/workflows/team-issue-triage.yml
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

import yaml


def fail(message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)
    sys.exit(1)


def load_guardrails(cfg_path: pathlib.Path) -> tuple[list[str], list[str]]:
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    engine = cfg.get("engine") or {}
    secrets = [str(s).strip() for s in engine.get("forbidden_secrets") or [] if str(s).strip()]
    endpoints = [str(e).strip() for e in engine.get("forbidden_direct_endpoints") or [] if str(e).strip()]
    return secrets, endpoints


def scan(paths: list[pathlib.Path], secrets: list[str], endpoints: list[str]) -> list[str]:
    secret_pattern = (
        re.compile("|".join(re.escape(s) for s in secrets)) if secrets else None
    )
    endpoint_pattern = (
        re.compile("|".join(re.escape(e) for e in endpoints)) if endpoints else None
    )
    findings: list[str] = []
    for path in paths:
        if not path.exists():
            fail(f"Cannot scan missing file: {path}")
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), start=1):
            if secret_pattern and secret_pattern.search(line):
                findings.append(f"{path}:{line_no}: forbidden secret reference: {line.strip()}")
            if endpoint_pattern and endpoint_pattern.search(line):
                findings.append(f"{path}:{line_no}: forbidden endpoint reference: {line.strip()}")
    return findings


def main() -> None:
    cfg_path = pathlib.Path(os.environ.get("TRIAGE_CONFIG", ".github/triage/team-issue-triage.yaml"))
    if not cfg_path.exists():
        fail(f"Triage config not found at {cfg_path}.")

    extra = [pathlib.Path(arg) for arg in sys.argv[1:]]
    paths = extra or [pathlib.Path(".github/workflows/team-issue-triage.yml")]

    secrets, endpoints = load_guardrails(cfg_path)
    if not secrets and not endpoints:
        print("No guardrails declared in engine.forbidden_secrets / engine.forbidden_direct_endpoints.")
        return

    findings = scan(paths, secrets, endpoints)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        fail(
            "Engine guardrails were violated. Remove the references above or update "
            f"{cfg_path} if the guardrail itself is wrong."
        )

    print(
        f"Engine guardrails OK. Checked {len(paths)} file(s) against "
        f"{len(secrets)} forbidden secret(s) and {len(endpoints)} forbidden endpoint(s)."
    )


if __name__ == "__main__":
    main()
