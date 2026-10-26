"""Experimental endpoint-document adapter, NOT a lifecycle-capable NodeDriver.

Pinned export: TrustTunnel endpoint 1.1.0; consumer: TrustTunnelClient 1.1.11.
The stock exporter hardcodes has_ipv6=true: the caller must supply actual node support.
"""

import json
import re
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from cryptography import x509
from pydantic import BaseModel, ConfigDict, Field, field_validator
from orchestrator.domain.contracts import TransportConfiguration


class EndpointExport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    hostname: str = Field(min_length=1, max_length=253)
    addresses: list[str] = Field(min_length=1, max_length=8)
    username: str = Field(min_length=1, max_length=255, repr=False)
    password: str = Field(min_length=1, max_length=1024, repr=False)
    has_ipv6: bool
    skip_verification: Literal[False]
    certificate: str = Field(default="", max_length=16384, repr=False)
    upstream_protocol: Literal["http2", "http3"]
    anti_dpi: bool = False
    custom_sni: Literal[""] = ""
    client_random_prefix: Literal[""] = ""
    name: str = Field(default="", max_length=255)
    dns_upstreams: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("skip_verification", mode="before")
    @classmethod
    def tls_required(cls, value):
        if value is not False:
            raise ValueError("TLS verification is required")
        return value

    @field_validator("hostname")
    @classmethod
    def valid_host(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
            raise ValueError("Invalid TLS hostname")
        if any(not label or len(label) > 63 for label in value.split(".")):
            raise ValueError("Invalid TLS hostname")
        return value

    @field_validator("addresses")
    @classmethod
    def valid_addresses(cls, values):
        for value in values:
            if len(value) > 300 or any(c.isspace() for c in value):
                raise ValueError("Invalid endpoint address")
            address = urlsplit("//" + value)
            if (
                not address.hostname
                or address.username is not None
                or address.password is not None
                or address.port is None
                or not 1 <= address.port <= 65535
                or address.path
                or address.query
                or address.fragment
            ):
                raise ValueError("Invalid endpoint address")
        return values

    @field_validator("username", "password")
    @classmethod
    def printable(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid credential")
        return value

    @field_validator("certificate")
    @classmethod
    def valid_certificate(cls, value):
        if value:
            x509.load_pem_x509_certificate(value.encode())
        return value

    @field_validator("dns_upstreams")
    @classmethod
    def bounded_dns(cls, values):
        if any(not v or len(v) > 1024 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("Invalid DNS upstream")
        return values


def configuration(document: str, *, ipv6_available: bool, transport: str) -> TransportConfiguration:
    """Validate a node export, preserving credentials in memory and requiring verified TLS."""
    try:
        if type(ipv6_available) is not bool or transport not in ("http2", "http3"):
            raise ValueError()
        if len(document.encode()) > 65536 or "\0" in document:
            raise ValueError()
        profile = EndpointExport.model_validate(tomllib.loads(document))
        values = profile.model_dump(exclude={"name", "custom_sni", "client_random_prefix"})
        values.update(has_ipv6=ipv6_available, upstream_protocol=transport)
        data = "[endpoint]\n" + "".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in values.items()
        )
        return TransportConfiguration(protocol="trusttunnel", format="trusttunnel-toml", data=data)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid_trusttunnel_configuration") from None
