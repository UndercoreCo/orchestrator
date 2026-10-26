import json
import tomllib
import pytest
from orchestrator.infrastructure.drivers.trusttunnel_configuration import configuration
from orchestrator.application.drivers import DriverRegistry
from orchestrator.application.ports import DriverCapabilities


def document(**changes):
    value = dict(
        hostname="vpn.example.invalid",
        addresses=["203.0.113.10:9443"],
        username="test-user",
        password="synthetic-password",
        has_ipv6=True,
        skip_verification=False,
        certificate="",
        upstream_protocol="http2",
    )
    value.update(changes)
    return "\n".join(f"{k} = {json.dumps(v)}" for k, v in value.items())


@pytest.mark.parametrize("transport", ["http2", "http3"])
def test_adapt_export_without_inheriting_client_routing_policy(transport):
    result = configuration(document(), ipv6_available=False, transport=transport)
    data = tomllib.loads(result.data)
    assert set(data) == {"endpoint"}
    assert data["endpoint"]["has_ipv6"] is False
    assert data["endpoint"]["skip_verification"] is False
    assert data["endpoint"]["upstream_protocol"] == transport
    assert data["endpoint"]["password"] == "synthetic-password"
    assert "synthetic-password" not in repr(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"skip_verification": True},
        {"skip_verification": 0},
        {"addresses": ["https://host:9443"]},
        {"addresses": ["user:password@host:9443"]},
        {"addresses": ["host:0"]},
        {"addresses": ["host:9443/path"]},
        {"addresses": ["host:9443?secret=x"]},
        {"addresses": ["host:99999"]},
        {"hostname": "invalid host"},
        {"hostname": "foo..bar"},
        {"password": "private\npassword"},
        {"certificate": "not a certificate"},
        {"client_random_prefix": "aabb"},
        {"custom_sni": "unverified.example"},
        {"listener": {"tun": {}}},
        {"unexpected": "secret"},
    ],
)
def test_unsafe_or_unimplemented_options_fail_without_echoing_payload(changes):
    with pytest.raises(ValueError) as caught:
        configuration(document(**changes), ipv6_available=False, transport="http2")
    assert str(caught.value) == "invalid_trusttunnel_configuration"


def test_parse_errors_do_not_leak_input():
    with pytest.raises(ValueError, match="^invalid_trusttunnel_configuration$"):
        configuration("password = SECRET", ipv6_available=False, transport="http2")


def test_export_only_driver_cannot_enter_live_registry():
    class ExportOnly:
        capabilities = DriverCapabilities(protocol="trusttunnel")

    with pytest.raises(ValueError, match="lifecycle"):
        DriverRegistry([ExportOnly()])
