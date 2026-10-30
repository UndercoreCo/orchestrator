"""Guest .vpn import model, checked against AmneziaVPN 5.0.3.0 field types."""

import base64
import json
import struct
import zlib

import pytest
from orchestrator.infrastructure.drivers.amnezia_profile import guest_profile
from orchestrator.domain.models import OrchestratorError

KEY = base64.b64encode(bytes(range(32))).decode()
BASE = f"""[Interface]
PrivateKey = {KEY}
Address = 10.0.0.2/32
DNS = 1.1.1.1, 1.0.0.1
MTU = 1280
Jc = 6
Jmin = 40
Jmax = 70
S1 = 32
S2 = 64
H1 = 10001
H2 = 10002
H3 = 10003
H4 = 10004
{{parameters}}
[Peer]
PublicKey = {KEY}
PresharedKey = {KEY}
Endpoint = vpn.example.test:8443
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""


def decode(value):
    assert value.startswith("vpn://")
    encoded = value[6:]
    packed = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    raw = zlib.decompress(packed[4:])
    assert struct.unpack(">I", packed[:4])[0] == len(raw)
    return json.loads(raw)


@pytest.mark.parametrize(
    ("parameters", "version"),
    [
        ("", None),
        ("I1 = <b 0x01>", "1.5"),
        ("S3 = 0\nS4 = 0", "2"),
        (f"S3 = 8\nS4 = 8\nHeaderProtectionKey = {KEY}\nContentPaddingAddition = 8-16", "3.1"),
        ("RandomTrailers = on\nDisableCookies = on", "3.1"),
    ],
)
def test_each_generation_preserves_native_and_individual_client_fields(parameters, version):
    native = BASE.format(parameters=parameters)
    payload = decode(guest_profile(native))
    assert set(payload) == {
        "description",
        "hostName",
        "dns1",
        "dns2",
        "containers",
        "defaultContainer",
    }
    container = payload["containers"][0]
    assert container["container"] == payload["defaultContainer"] == "amnezia-awg"
    protocol = container["awg"]
    assert protocol.get("protocol_version") == version
    assert protocol["isThirdPartyConfig"] is True
    client = json.loads(protocol["last_config"])
    assert client["config"] == native
    assert payload["hostName"] == client["hostName"] == "vpn.example.test"
    assert client["client_priv_key"] == client["server_pub_key"] == client["psk_key"] == KEY
    assert (
        client["client_pub_key"] == client["clientId"]
        and len(base64.b64decode(client["client_pub_key"])) == 32
    )
    assert client["client_ip"] == "10.0.0.2/32"
    assert client["allowed_ips"] == ["0.0.0.0/0", "::/0"]
    assert (
        client["port"] == 8443
        and client["mtu"] == "1280"
        and client["persistent_keep_alive"] == "25"
    )
    for line in parameters.splitlines():
        key, value = line.split(" = ", 1)
        assert client[key] == value and isinstance(client[key], str)
    assert payload["dns1"] == "1.1.1.1" and payload["dns2"] == "1.0.0.1"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value + "[Peer]\nPublicKey = SECRET_DO_NOT_ECHO\n",
        lambda value: value.replace("MTU = 1280", "UnknownParameter = SECRET_DO_NOT_ECHO"),
        lambda value: value.replace(KEY, "SECRET_DO_NOT_ECHO"),
        lambda value: value.replace("Address = 10.0.0.2/32", "Address = 10.0.0.2/32, 10.0.0.3/32"),
        lambda value: value.replace(
            "Endpoint = vpn.example.test:8443", "Endpoint = https://admin.example.test:443"
        ),
        lambda value: "[DEFAULT]\nPrivateKey = SECRET_DO_NOT_ECHO\n" + value,
    ],
)
def test_ambiguous_unknown_or_invalid_fields_are_rejected_without_secret_errors(mutate):
    with pytest.raises(OrchestratorError) as error:
        guest_profile(mutate(BASE.format(parameters="")))
    assert error.value.code == "node_response_invalid" and "SECRET_DO_NOT_ECHO" not in str(
        error.value
    )


def test_ipv6_endpoint_is_not_split_or_dropped():
    value = BASE.format(parameters="").replace("vpn.example.test:8443", "[2001:db8::1]:8443")
    payload = decode(guest_profile(value))
    assert payload["hostName"] == "2001:db8::1"
