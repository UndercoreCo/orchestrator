# Undercore Orchestrator

A standalone VPN control service for trusted backends. It assigns devices to VPN
nodes, returns connection parameters and exports, and coordinates recovery and
revocation. VPN traffic goes directly from the client to the node.

**Status: early release, 0.x.** AmneziaWG has a working lifecycle driver;
TrustTunnel has an experimental driver enabled explicitly in configuration.
Both require compatible node agents, not an ordinary upstream VPN server.
The node-agent runtime and automatic node installer are not yet publicly shipped.
You can run the orchestrator and its tests without a node, but cannot establish a
real VPN connection that way. See the [support matrix](docs/public/support.md)
for the difference between implemented, tested and publicly installable features.

## Responsibilities

- Your backend authenticates users, checks entitlement and reserves a device slot.
- The orchestrator selects a compatible node and maintains a durable assignment.
- The node agent creates peers, enforces expiry and confirms revocation.
- Your VPN client uses the returned parameters to establish the tunnel.

No Undercore website or account is required to run or manage this service. The
service token belongs to your backend, never to an end-user application.

## Start locally

Python 3.12+ is required. Run from the repository root on Linux or macOS:

```sh
git clone https://github.com/UndercoreCo/orchestrator.git
cd orchestrator
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/undercore-orchestrator init --directory "$PWD/local"
.venv/bin/undercore-orchestrator --settings "$PWD/local/settings.json" serve
```

In another terminal, from the same directory:

```sh
.venv/bin/undercore-orchestrator --settings "$PWD/local/settings.json" health
.venv/bin/undercore-orchestrator --settings "$PWD/local/settings.json" list servers
```

Expect a healthy process and an empty node list. This does not demonstrate VPN
connectivity. `local/` is ignored by Git; do not share its credentials.
Production installation uses Linux/systemd and a verified release archive.

## Capabilities and limits

| Area | Available today |
|---|---|
| VPN lifecycle | AmneziaWG through a compatible Undercore agent |
| Exports | `.conf`, Amnezia `.vpn`, PNG QR |
| Recovery | Durable switching, retries of the same operation, confirmed revocation |
| Management | Local CLI, standalone optional panel, validated configuration, SQLite journal |
| Distribution | Release archives, installer and controlled updater |
| TrustTunnel | Experimental lifecycle driver, opt-in; compatible managed agent required |
| WireGuard | Export policy only; no complete lifecycle driver |
| Node provisioning | Not yet automated |
| Deployment | One controller installation with local SQLite; no active-active HA |
| Selection | Compatibility, node availability/mode and capacity; no client speed test |

Nodes with short control leases depend on regular controller heartbeats: a long
controller outage can stop their VPN access. Plan maintenance accordingly.

## Documentation

**[Read the documentation website](https://undercoreco.github.io/orchestrator/)**

The detailed guides are currently in Russian.

- [Documentation index](docs/public/index.md)
- [Installation and node registration](docs/public/quickstart.md)
- [Backend integration walkthrough](docs/public/integration.md)
- [HTTP API](docs/public/api.md) and [OpenAPI 3.1 for new integrations](contracts/openapi.json)
- [Configuration](docs/public/configuration.md) and [operations](docs/public/operations.md)
- [Architecture](docs/public/architecture.md) and [driver development](docs/public/drivers.md)
- [Contributing](docs/public/contributing.md), [security reporting](docs/public/security.md)
- [Roadmap to 1.0](docs/public/roadmap.md) and [support matrix](docs/public/support.md)

## Development

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_contracts.py
.venv/bin/python scripts/check_architecture.py
.venv/bin/ruff check orchestrator scripts tests
.venv/bin/ruff format --check orchestrator scripts tests
```

Tests use a pinned agent fixture and fake VPN runtime. They do not require SSH,
production credentials or a running VPN server. The project uses the [MIT license](LICENSE); see [provenance and dependencies](docs/public/licensing.md).
