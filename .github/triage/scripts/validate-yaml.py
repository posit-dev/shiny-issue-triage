#!/usr/bin/env python3
"""Validate repository YAML files used by the triage workflow."""

from __future__ import annotations

from pathlib import Path

import yaml


def main() -> None:
    paths = [
        Path('.github/workflows/team-issue-triage.yml'),
        Path('.github/workflows/ci.yml'),
        Path('.github/triage/team-issue-triage.yaml'),
        Path('.github/triage/labels.yaml'),
    ]
    for path in paths:
        with path.open(encoding='utf-8') as handle:
            yaml.safe_load(handle)
    print(f'Validated {len(paths)} YAML files.')


if __name__ == '__main__':
    main()
