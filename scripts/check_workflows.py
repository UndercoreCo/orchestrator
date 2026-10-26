#!/usr/bin/env python3
"""Fail CI when action pins or essential release gates are accidentally removed."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    for path in (root / ".github/workflows").glob("*.yml"):
        text = path.read_text()
        for action in re.findall(r"uses:\s*(\S+)", text):
            if not re.fullmatch(r"[\w.-]+/[\w./-]+@[a-f0-9]{40}", action):
                raise ValueError(f"Unpinned action in {path.name}")
        if "pull_request_target" in text:
            raise ValueError("Privileged PR workflow requires separate review")
    release = (root / ".github/workflows/release.yml").read_text()
    for required in (
        "needs: verify",
        "cancel-in-progress: false",
        "fetch-depth: 0",
        "git merge-base --is-ancestor",
        "scripts/publish_release.py",
        "python -m pytest",
        "scripts/check_secrets.py",
        "pip_audit -r requirements.lock",
        "actions/attest@",
        "subject-path: dist/*.tar.gz",
        "attestations: write",
        "artifact-metadata: write",
        "id-token: write",
    ):
        if required not in release:
            raise ValueError("Missing release gate: " + required)
    for name in ("checks", "release"):
        text = (root / f".github/workflows/{name}.yml").read_text()
        if any(f"'{version}'" not in text for version in ("3.12", "3.13", "3.14")):
            raise ValueError("Missing supported Python version")
    print("Pinned actions and release gates verified")


if __name__ == "__main__":
    check()
