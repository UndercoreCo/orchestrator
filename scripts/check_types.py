"""Static witnesses: every runtime implementation must satisfy its application port."""

from orchestrator.application.ports import NodeDriver, LeaseTransport
from orchestrator.application.repository_ports import (
    Journal,
    Credentials,
    PanelReader,
    OperatorJournal,
    PolicyEditor,
)
from orchestrator.infrastructure.sqlite.assignments import Assignments
from orchestrator.infrastructure.drivers.amneziawg import AmneziaAgentDriver
from orchestrator.infrastructure.drivers.trusttunnel import TrustTunnelAgentDriver
from orchestrator.infrastructure.drivers.amnezia_leases import AmneziaLeaseTransport
from orchestrator.infrastructure.credentials import CredentialFiles
from orchestrator.infrastructure.sqlite.panel import PanelJournal
from orchestrator.infrastructure.sqlite.administration import OperatorAudit
from orchestrator.infrastructure.policy_editor import PolicyFile


def check_implementations(
    store: Assignments,
    driver: AmneziaAgentDriver,
    leases: AmneziaLeaseTransport,
    files: CredentialFiles,
    panel: PanelJournal,
    audit: OperatorAudit,
    policies: PolicyFile,
) -> tuple[
    Journal, NodeDriver, LeaseTransport, Credentials, PanelReader, OperatorJournal, PolicyEditor
]:
    return store, driver, leases, files, panel, audit, policies


def check_trusttunnel_driver(driver: TrustTunnelAgentDriver) -> NodeDriver:
    return driver
