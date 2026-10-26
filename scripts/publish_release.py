#!/usr/bin/env python3
"""Publish verified immutable assets, or verify an identical existing release."""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(*args, cwd=ROOT):
    return subprocess.check_output(args, cwd=cwd, text=True, timeout=180)


def check_source(tag, commit):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("invalid_release_identity")
    actual = command("git", "rev-parse", "--verify", "refs/tags/" + tag + "^{commit}").strip()
    if actual != commit:
        raise ValueError("release_tag_commit_mismatch")
    # A release can use an older main commit, but never an unmerged side branch.
    command("git", "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main")


def local_assets(directory, tag):
    archive = directory / ("undercore-orchestrator-" + tag + ".tar.gz")
    checksum = directory / "SHA256SUMS"
    for path in (archive, checksum):
        if not path.is_file() or path.is_symlink():
            raise ValueError("release_asset_missing")
    expected = hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n"
    if checksum.read_text() != expected:
        raise ValueError("release_checksum_mismatch")
    return (archive, checksum)


def find_release(repo, tag):
    # Listing avoids treating authentication/network failures as a missing release.
    pages = json.loads(command("gh", "api", f"repos/{repo}/releases", "--paginate", "--slurp"))
    matches = [item for page in pages for item in page if item["tag_name"] == tag]
    if len(matches) > 1:
        raise ValueError("duplicate_release")
    return matches[0] if matches else None


def verify_existing(repo, tag, release, assets):
    remote = release["assets"]
    expected = {p.name: p for p in assets}
    if (
        release["prerelease"]
        or len(remote) != len(expected)
        or {item["name"] for item in remote} != set(expected)
        or any(item["size"] != expected[item["name"]].stat().st_size for item in remote)
    ):
        raise ValueError("existing_release_assets_differ")
    with tempfile.TemporaryDirectory(prefix="orchestrator-release-check-") as tmp:
        for path in assets:
            command(
                "gh",
                "release",
                "download",
                tag,
                "--repo",
                repo,
                "--pattern",
                path.name,
                "--dir",
                tmp,
            )
            if (Path(tmp) / path.name).read_bytes() != path.read_bytes():
                raise ValueError("existing_release_assets_differ")


def publish(repo, tag, commit, directory):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid_repository")
    check_source(tag, commit)
    assets = local_assets(directory, tag)
    # Attestation must bind these exact bytes to the source tag, commit and workflow.
    command(
        "gh",
        "attestation",
        "verify",
        str(assets[0]),
        "--repo",
        repo,
        "--signer-workflow",
        repo + "/.github/workflows/release.yml",
        "--source-digest",
        commit,
        "--source-ref",
        "refs/tags/" + tag,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    )
    release = find_release(repo, tag)
    created = release is None
    if created:
        # Draft prevents exposing incomplete assets if upload is interrupted.
        command(
            "gh",
            "release",
            "create",
            tag,
            *(str(p) for p in assets),
            "--repo",
            repo,
            "--verify-tag",
            "--draft",
            "--title",
            "Undercore Orchestrator " + tag,
            "--notes",
            "Independent orchestrator release. See docs/public/operations.md "
            "for installation, controlled updates and rollback policy.",
        )
        release = find_release(repo, tag)
        if release is None:
            raise ValueError("created_release_not_found")
    verify_existing(repo, tag, release, assets)
    if release["draft"]:
        command("gh", "release", "edit", tag, "--repo", repo, "--draft=false")
        release = find_release(repo, tag)
        if release is None or release["draft"]:
            raise ValueError("release_not_published")
        verify_existing(repo, tag, release, assets)
    return {"tag": tag, "commit": commit, "result": "published" if created else "verified"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--directory", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(json.dumps(publish(args.repo, args.tag, args.commit, args.directory)))


if __name__ == "__main__":
    main()
