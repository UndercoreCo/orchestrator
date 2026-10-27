# Release review: controller 0.6.0 and AWG agent 1.1.0

Local review and isolated single-node acceptance, 27 October 2026 (Asia/Tashkent).
No production access or changes, merge, push, tags or public release in this run.

Controller commit: `1b74d1388e3697061ac7cf7074e080f4355b7e62`.
Agent base commit: `df9a12f0a71e516ae056d550f9096940e069ac15`.

## Review finding

The agent release allowlist omitted `test_offline_permission.py`. The existing
allowlist regression test reproduced this defect. Adding `offline_permission`
to the service test groups fixes it: 25 release tests and 21 subtests pass.
The rebuilt candidate includes all 121 Linux agent tests; all pass. Only the
release manifest and the added test differ from the original agent archive;
its executable code is unchanged. The packaging fix is local and uncommitted.
A clean-commit artifact must replace this candidate before publication.

Controller: 437 Linux tests, contract and architecture checks, types, lint,
format, workflow policy, configuration validation, secret scan and dependency
audit passed. Agent: 121 Linux tests from full source and from corrected archive.
Previously blocked GitHub agent jobs are not treated as successful CI.

## Upgrade/rollback review

The actual install/update scripts and systemd were exercised on disposable
Ubuntu 24.04: 0.5.1 → 0.5.2 → 0.5.3 → 0.6.0. Settings and the journal survived.
An intentionally failing 0.6.0 service caused the updater to restore 0.5.3;
the live journal, synthetic denied assignment and stored fencing bound survived.
A subsequent normal update succeeded. An earlier malformed test fixture with
an assignment to an unregistered node was correctly rejected in preflight;
no product guard was disabled to exercise the later health-failure path.

## Required deployment constraints

- Initial deployment keeps offline permission disabled (default 0).
- `offline_duration_seconds` is global for lease-enabled nodes, including draining
  ones. One-node opt-in requires a separate controller until all relevant agents
  support v2. Do not enable it across a mixed old/new fleet.
- Update agent HTTP and state components together. Root runtime is unchanged.
- State-agent restart closes access until a new sequence; offline permission
  does not bypass the independent 90-second watchdog or client expiry.
- The configured 300–3600 seconds start at the last grant, not outage detection.
- Preserve the full stored fencing window after uncertain revoke/transfer.
- Roll back only to a prepared compatible version, retaining the live journal.
  Remove the new policy field for old controller code. Do not restore stale data
  or return to 0.5.1 once the journal uses schema 2.
- TrustTunnel has no new offline guarantee in this release.

## Evidence location and limits

Local release packet: `work/orchestrator-release-20261027/RELEASE-REVIEW.md`
in the Amnesia operations workspace; test evidence:
`diagnostics/20261027-orchestrator-release/`.

This run uses an isolated local ARM64 Linux VM and native AWG v3.1.20260812
builds. The pinned amd64 Docker engine crashed under local emulation, so this
is not a qualification of that binary on production hardware. The preceding
native Linux amd64 acceptance is recorded in
[26 October acceptance](linux-offline-acceptance-2026-10-26.md).
Traffic checks exercise real ICMP through an encrypted tunnel, not public web
access, DNS, country-specific filtering, host reboot or desktop clients.

## Completed single-node checks

The 623-second real tunnel run passed outage continuity, client expiry, denial
of fresh access without current control, full offline deadline, recovery with
identical configuration, restart closed until a new grant, and independent
watchdog after SIGSTOP. The 64.2-second code rollback and re-upgrade passed all
five checks, retaining identity, disabled state, configuration and 425-second
fencing bound on the live database. After explicit user approval, cleanup verified all 87 local containers use ARM64,
found no active QEMU process and no binfmt handlers in a fresh audit namespace
after lab shutdown. No shared handler or Docker Rosetta process was modified.
The stopped lab container was removed; all other 86 containers retain the same
image, running state, start time and restart count. Evidence: cleanup-result.json.
