"""Composition root: the only place that assembles runtime implementations."""

from pathlib import Path
from orchestrator.infrastructure.clock import BOOT_ID
from orchestrator.config.settings import Settings
from orchestrator.infrastructure.observability import Observability
from orchestrator.infrastructure.events import EventTail
from orchestrator.infrastructure.settings import read_settings, validate_nodes
from orchestrator.infrastructure.sqlite.inventory import read_inventory
from orchestrator.infrastructure.validation import inspect_configuration
from orchestrator.infrastructure.credentials import CredentialFiles
from orchestrator.infrastructure.drivers.amnezia_leases import AmneziaLeaseTransport
from orchestrator.infrastructure.secrets import read_secret
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.infrastructure.drivers.amnezia_http import AgentAPI
from orchestrator.infrastructure.drivers.amneziawg import AmneziaAgentDriver
from orchestrator.infrastructure.drivers.trusttunnel import TrustTunnelAgentDriver
from orchestrator.application.drivers import DriverRegistry, LeaseRegistry
from orchestrator.application.gateway import AgentGateway
from orchestrator.application.nodes import NodeRegistry


def configured_drivers(api, experimental_trusttunnel=False):
    enabled = [AmneziaAgentDriver(api)]
    if experimental_trusttunnel:
        enabled.append(TrustTunnelAgentDriver(api))
    return DriverRegistry(enabled)


def build_gateway(
    nodes,
    store,
    api=None,
    drivers=None,
    policy=None,
    telemetry=None,
    experimental_trusttunnel=False,
):
    api = api if api is not None else AgentAPI(policy=policy.agents if policy else None)
    drivers = drivers if drivers is not None else configured_drivers(api, experimental_trusttunnel)
    return AgentGateway(
        nodes,
        store,
        drivers,
        LeaseRegistry({"amneziawg": AmneziaLeaseTransport(api)}),
        api,
        BOOT_ID,
        policy=policy,
        telemetry=telemetry,
    )


def build_registry(store, initial):
    return NodeRegistry(store, initial, CredentialFiles(store.path.parent))


def load_settings(path=None, secrets=True):
    settings = read_settings(path, secrets, Settings.model_validate_json)
    records = read_inventory(Path(settings.state_directory) / "assignments.sqlite3")
    effective = settings.model_copy(update={"nodes": records}) if records is not None else settings
    with AgentAPI(policy=settings.policy.agents) as api:
        drivers = configured_drivers(api, settings.experimental_trusttunnel)
        for node in validate_nodes(effective, secrets):
            drivers.for_node(node)
    return settings


def validate_configuration(path, structure_only=False, probe=False):
    if probe and structure_only:
        raise ValueError("Probe requires credentials")
    settings = load_settings(path, secrets=not structure_only)
    _, nodes = inspect_configuration(settings, structure_only)
    if probe:
        with AgentAPI(policy=settings.policy.agents) as api:
            drivers = configured_drivers(api, settings.experimental_trusttunnel)
            for record in nodes:
                node = record.node(read_secret(record.api_key_file).decode())
                drivers.for_node(node).verify(node)
    return {"valid": True, "nodes": len(nodes), "secrets_checked": not structure_only}


def agent_service():
    settings = load_settings()
    store = Assignments(Path(settings.state_directory), policy=settings.policy)
    registry = build_registry(store, settings.nodes)
    service = build_gateway(
        list(registry.nodes().values()),
        store,
        policy=settings.policy,
        experimental_trusttunnel=settings.experimental_trusttunnel,
        telemetry=Observability(event_tail=EventTail(settings.state_directory)),
    )
    service.registry = registry
    return service, read_secret(settings.backend_token_file).decode()


def agent_app():
    from orchestrator.interfaces.http.app import create_agent_app

    service, token = agent_service()
    # Management is local CLI only. A legacy admin token cannot enable HTTP management.
    return create_agent_app(service, token)


def panel_app(token_file, port=8793, management=False):
    """Assemble a reader only: no gateway, migrations, node credentials or workers."""
    import os
    import secrets
    from orchestrator.infrastructure.sqlite.panel import PanelJournal
    from orchestrator.interfaces.http.panel import create_panel_app

    path = Path(os.environ.get("ORCHESTRATOR_SETTINGS", "/etc/vpn-orchestrator/settings.json"))
    settings = Settings.model_validate_json(path.read_bytes())
    token = read_secret(token_file).decode()
    if secrets.compare_digest(token, read_secret(settings.backend_token_file).decode()):
        raise ValueError("Panel and backend credentials must differ")
    return create_panel_app(
        PanelJournal(settings.state_directory),
        EventTail(settings.state_directory),
        token,
        f"http://127.0.0.1:{port}",
        administration=administration_service() if management else None,
    )


def administration_service():
    import os
    from orchestrator.application.administration import Administration
    from orchestrator.infrastructure.sqlite.administration import OperatorAudit
    from orchestrator.infrastructure.policy_editor import PolicyFile

    path = Path(os.environ.get("ORCHESTRATOR_SETTINGS", "/etc/vpn-orchestrator/settings.json"))
    settings = load_settings(path)
    audit = OperatorAudit(settings.state_directory)
    return Administration(
        lambda: agent_service()[0], audit, PolicyFile(path, Settings.model_validate_json)
    )
