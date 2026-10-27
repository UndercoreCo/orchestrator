"""Real candidate agent/HTTP/controller lifecycle with simulated network separation."""

import importlib.util
import sys
import time
from pathlib import Path
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError
import test_node_switch
from test_node_switch import pair, export
from test_leases import enroll
from test_recovery import request as recovery_request
from orchestrator.application.leases import NodeLeases
from orchestrator.application.recovery import Recovery, reconcile
from orchestrator.bootstrap import build_gateway
from orchestrator.config.policy import RuntimePolicy
from orchestrator.domain.models import OrchestratorError
from orchestrator.infrastructure.sqlite.assignments import Assignments

# Separate package: the old pinned app fixture still tests v1 compatibility.
path = Path(__file__).parent / "fixtures/amnezia_offline_agent/app/__init__.py"
spec = importlib.util.spec_from_file_location(
    "offline_agent", path, submodule_search_locations=[str(path.parent)]
)
module = importlib.util.module_from_spec(spec)
sys.modules["offline_agent"] = module
spec.loader.exec_module(module)
from offline_agent import domain, control_lease
from offline_agent.main import create_app


@pytest.fixture
def offline_pair(request, monkeypatch):
    monkeypatch.setattr(test_node_switch, "Engine", domain.Engine)
    monkeypatch.setattr(test_node_switch, "Store", domain.Store)
    monkeypatch.setattr(test_node_switch, "create_app", create_app)
    value = request.getfixturevalue("pair")
    gateway, engines, *_ = value
    gateway.policy = RuntimePolicy.model_validate({"leases": {"offline_duration_seconds": 600}})
    clock = {"mono": time.monotonic(), "wall": domain.now()}
    for engine in engines.values():
        engine.control_lease.clock = lambda: clock["mono"]
    monkeypatch.setattr(control_lease, "now", lambda: clock["wall"])
    monkeypatch.setattr(domain, "now", lambda: clock["wall"])
    # All controller processes share the monotonic epoch, as on the Linux host.
    monkeypatch.setattr(time, "time", lambda: clock["wall"].timestamp())
    monkeypatch.setattr(time, "monotonic", lambda: clock["mono"])
    enroll(gateway)
    return value, clock


def advance(clock, seconds):
    clock["mono"] += seconds
    clock["wall"] += timedelta(seconds=seconds)


def test_outage_keeps_existing_tunnel_but_not_control_readiness(offline_pair):
    (gateway, engines, states, _, ident, _), clock = offline_pair
    before = export(gateway, ident)
    keys = engines["a"].backend.peers.copy()
    states["a"]["down"] = True
    advance(clock, 91)
    engines["a"].reconcile()
    assert engines["a"].backend.peers == keys
    assert engines["a"].control_lease.allowed()
    assert not NodeLeases(gateway).ready(gateway.nodes["a"])
    states["a"]["down"] = False
    NodeLeases(gateway).heartbeat(gateway.nodes["a"])
    assert NodeLeases(gateway).ready(gateway.nodes["a"])
    assert export(gateway, ident).revision == before.revision
    assert engines["a"].backend.peers == keys


def test_lost_long_grant_reply_preserves_bound_through_smaller_policy_and_restart(offline_pair):
    (gateway, engines, states, _, ident, _), clock = offline_pair
    before = export(gateway, ident)
    gateway.policy = RuntimePolicy.model_validate({"leases": {"offline_duration_seconds": 3600}})
    states["a"]["lose"] = "/v1/control-lease"
    with pytest.raises(OrchestratorError):
        NodeLeases(gateway).heartbeat(gateway.nodes["a"])
    assert engines["a"].control_lease.allowed()
    assert gateway.store.leases.state("a")["fence_window_seconds"] == 3725
    restarted = build_gateway(
        list(gateway.nodes.values()), Assignments(gateway.store.path.parent), gateway.resources
    )
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError, match="lease_waiting"):
        Recovery(restarted).recover(ident, recovery_request(before.revision))
    advance(clock, 901)
    engines["a"].reconcile()
    assert engines["a"].backend.peers
    assert not NodeLeases(restarted).expired(restarted.nodes["a"])
    assert not engines["b"].rows()


def test_full_fence_then_move_and_restore_never_resurrect_old_peer(offline_pair):
    (gateway, engines, states, _, ident, _), clock = offline_pair
    before = export(gateway, ident)
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError, match="lease_waiting"):
        Recovery(gateway).recover(ident, recovery_request(before.revision))
    advance(clock, 599)
    engines["a"].reconcile()
    assert engines["a"].backend.peers
    assert not engines["b"].rows()
    assert not NodeLeases(gateway).expired(gateway.nodes["a"])
    advance(clock, 1)
    engines["a"].reconcile()
    assert not engines["a"].backend.peers
    advance(clock, 124)
    assert not NodeLeases(gateway).expired(gateway.nodes["a"])
    advance(clock, 1)
    NodeLeases(gateway).heartbeat(gateway.nodes["b"])
    assert reconcile(gateway)["completed"] == 1
    assert export(gateway, ident).node_id == "b"
    states["a"]["down"] = False
    NodeLeases(gateway).restore(gateway.nodes["a"])
    assert not engines["a"].backend.peers
    assert not engines["a"].rows()[0]["enabled"]
    assert len(engines["b"].backend.peers) == 1


def test_confirmed_revoke_still_moves_immediately(offline_pair):
    (gateway, engines, _, _, ident, _), _clock = offline_pair
    before = export(gateway, ident)
    result = Recovery(gateway).recover(ident, recovery_request(before.revision))
    assert result.node_id == "b"
    assert not engines["a"].backend.peers and engines["b"].backend.peers


def test_old_agent_refuses_opt_in_without_sending_new_grant(pair):
    gateway, engines, *_ = pair
    enroll(gateway)
    old = engines["a"].control_lease.row()
    gateway.policy = RuntimePolicy.model_validate({"leases": {"offline_duration_seconds": 600}})
    with pytest.raises(OrchestratorError, match="control_lease_unavailable"):
        NodeLeases(gateway).heartbeat(gateway.nodes["a"])
    assert engines["a"].control_lease.row() == old


@pytest.mark.parametrize("seconds", [-1, 1, 299, 3601, True])
def test_offline_policy_rejects_unsafe_limits(seconds):
    with pytest.raises(ValidationError):
        RuntimePolicy.model_validate({"leases": {"offline_duration_seconds": seconds}})


def test_deny_during_partition_never_provisions_target_after_wait(offline_pair):
    (gateway, engines, states, _, ident, _), clock = offline_pair
    export(gateway, ident)
    states["a"]["down"] = True
    with pytest.raises(OrchestratorError):
        Recovery(gateway).recover(ident, recovery_request())
    with pytest.raises(OrchestratorError):
        gateway.client(ident, "disable")
    advance(clock, 725)
    engines["a"].reconcile()
    NodeLeases(gateway).heartbeat(gateway.nodes["b"])
    reconcile(gateway)
    assert not engines["a"].backend.peers and not engines["b"].backend.peers
    assert gateway.store.leases.cached(ident)["denied"] == 1
    with pytest.raises(OrchestratorError):
        export(gateway, ident)


@pytest.mark.parametrize("value", [None, True, "3600", 0, 86401])
def test_malformed_agent_capability_is_not_accepted(value):
    from orchestrator.infrastructure.drivers.amnezia_leases import AmneziaLeaseTransport

    class API:
        def request(self, *args):
            return {
                "control_lease": {
                    "version": 1,
                    "supported_versions": [1, 2],
                    "max_offline_seconds": value,
                    "server_time": "2026-10-26T00:00:00Z",
                }
            }

        def validate_identity(self, *args):
            pass

    with pytest.raises(OrchestratorError, match="control_lease_unavailable"):
        AmneziaLeaseTransport(API()).observe(None)


@pytest.mark.parametrize("change", [{"offline_until": None}, {"fresh": False}, {"fresh": 1}])
def test_agent_must_confirm_exact_offline_grant(change):
    from orchestrator.infrastructure.drivers.amnezia_leases import AmneziaLeaseTransport
    from orchestrator.domain.records import LeaseGrant

    class API:
        def request(self, node, method, path, payload):
            assert payload["version"] == 2
            return {**payload, "required": True, "active": True, "fresh": True, **change}

    with pytest.raises(OrchestratorError, match="control_lease_unconfirmed"):
        AmneziaLeaseTransport(API()).renew(None, LeaseGrant("controller", 1, "fresh", "offline"))
