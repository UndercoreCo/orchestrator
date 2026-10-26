import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from test_release_checks import load

TAG = "v1.2.3"
COMMIT = "a" * 40
REPO = "example/orchestrator"


@pytest.fixture
def publisher(tmp_path, monkeypatch):
    module = load("publish_release")
    archive = tmp_path / f"undercore-orchestrator-{TAG}.tar.gz"
    archive.write_bytes(b"immutable test archive")
    checksum = tmp_path / "SHA256SUMS"
    checksum.write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n"
    )
    files = {p.name: p.read_bytes() for p in (archive, checksum)}
    state = {"release": None, "files": files, "calls": [], "fail": None}

    def command(*args, **kwargs):
        state["calls"].append(args)
        if state["fail"] and args[: len(state["fail"])] == state["fail"]:
            raise subprocess.CalledProcessError(1, args)
        if args[:2] == ("git", "rev-parse"):
            return COMMIT
        if args[:2] == ("gh", "api"):
            return json.dumps([[state["release"]] if state["release"] else []])
        if args[:3] == ("gh", "release", "create"):
            state["release"] = release(draft=True)
        if args[:3] == ("gh", "release", "download"):
            name = args[args.index("--pattern") + 1]
            (Path(args[args.index("--dir") + 1]) / name).write_bytes(state["files"][name])
        if args[:3] == ("gh", "release", "edit"):
            state["release"]["draft"] = False
        return ""

    def release(draft=False):
        return {
            "tag_name": TAG,
            "draft": draft,
            "prerelease": False,
            "assets": [{"name": k, "size": len(v)} for k, v in files.items()],
        }

    monkeypatch.setattr(module, "command", command)
    return module, tmp_path, state, release


def writes(state):
    return [c for c in state["calls"] if c[:2] == ("gh", "release") and c[2] in ("create", "edit")]


def test_publish_then_repeat_verifies_without_overwriting(publisher):
    module, directory, state, _ = publisher
    assert module.publish(REPO, TAG, COMMIT, directory)["result"] == "published"
    assert [c[2] for c in writes(state)] == ["create", "edit"]
    state["calls"].clear()
    assert module.publish(REPO, TAG, COMMIT, directory)["result"] == "verified"
    assert not writes(state)
    attest = next(c for c in state["calls"] if c[:3] == ("gh", "attestation", "verify"))
    assert attest[attest.index("--source-digest") + 1] == COMMIT
    assert attest[attest.index("--source-ref") + 1] == "refs/tags/" + TAG


def test_matching_draft_is_resumed_without_upload(publisher):
    module, directory, state, release = publisher
    state["release"] = release(draft=True)
    module.publish(REPO, TAG, COMMIT, directory)
    assert [c[2] for c in writes(state)] == ["edit"]


@pytest.mark.parametrize("draft", [False, True])
@pytest.mark.parametrize("corruption", ["bytes", "missing", "extra", "prerelease"])
def test_existing_mismatch_is_never_repaired_or_published(publisher, draft, corruption):
    module, directory, state, release = publisher
    state["release"] = release(draft=draft)
    if corruption == "bytes":
        name = next(iter(state["files"]))
        state["files"][name] = b"x" * len(state["files"][name])
    elif corruption == "missing":
        state["release"]["assets"].pop()
    elif corruption == "extra":
        state["release"]["assets"].append({"name": "extra", "size": 0})
    else:
        state["release"]["prerelease"] = True
    with pytest.raises(ValueError, match="existing_release_assets_differ"):
        module.publish(REPO, TAG, COMMIT, directory)
    assert not writes(state)


@pytest.mark.parametrize("failure", [("gh", "api"), ("gh", "attestation"), ("git", "merge-base")])
def test_network_auth_attestation_or_unmerged_source_failure_stops_publication(publisher, failure):
    module, directory, state, _ = publisher
    state["fail"] = failure
    with pytest.raises(subprocess.CalledProcessError):
        module.publish(REPO, TAG, COMMIT, directory)
    assert not writes(state)


def test_tag_must_point_to_exact_commit(publisher):
    module, directory, state, _ = publisher
    with pytest.raises(ValueError, match="release_tag_commit_mismatch"):
        module.publish(REPO, TAG, "b" * 40, directory)
    assert not writes(state)


def test_invalid_local_checksum_stops_publication(publisher):
    module, directory, state, _ = publisher
    (directory / "SHA256SUMS").write_text("wrong")
    with pytest.raises(ValueError, match="release_checksum_mismatch"):
        module.publish(REPO, TAG, COMMIT, directory)
    assert not writes(state)


def test_failed_upload_does_not_publish_draft(publisher):
    module, directory, state, _ = publisher
    state["fail"] = ("gh", "release", "create")
    with pytest.raises(subprocess.CalledProcessError):
        module.publish(REPO, TAG, COMMIT, directory)
    assert all(c[2] != "edit" for c in writes(state))
