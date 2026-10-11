"""Bounded HTTPS transport shared by node adapters, without protocol policy."""

import json
import re
import ssl
import httpx
from orchestrator.application.execution import bounded, DeadlineExceeded, CapacityExceeded
from orchestrator.infrastructure.drivers.http_pool import HTTPPool, QueueFull
from orchestrator.application.telemetry import Action, Reason, Stage, span, trace_headers
from orchestrator.domain.models import OrchestratorError


class NodeHTTP:
    def __init__(self, transport=None, policy=None):
        from orchestrator.config.policy import AgentPolicy

        self.policy = policy or AgentPolicy()
        self._transport = transport
        self._pool = HTTPPool(self.policy, transport)

    @property
    def transport(self):
        return self._transport

    @transport.setter
    def transport(self, transport):
        self._pool.close()
        self._transport = transport
        self._pool = HTTPPool(self.policy, transport)

    def close(self):
        self._pool.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @bounded
    def request(
        self,
        node,
        method,
        path,
        payload=None,
        text=False,
        *,
        no_store=False,
        legacy_post_route=False,
    ):
        with span(Stage.AGENT, node_id=node.id, action=self.action(method, path)) as observation:
            try:

                async def send(client):
                    async with client.stream(
                        method,
                        node.api_url.rstrip("/") + path,
                        headers={"Authorization": "Bearer " + node.api_key, **trace_headers()},
                        json=payload,
                    ) as response:
                        observation.status = response.status_code
                        if response.status_code != 200:
                            observation.reason = Reason.UPSTREAM
                            # Never forward arbitrary response text or exception details.
                            status = (
                                response.status_code
                                if response.status_code in (404, 409, 410, 422)
                                else 503
                            )
                            # The old agent's POST /clients/{id}/{operation} wildcard
                            # makes an unknown GET export return 405 with Allow: POST.
                            if (
                                legacy_post_route
                                and response.status_code == 405
                                and response.headers.get("allow") == "POST"
                            ):
                                status = 405
                            raise OrchestratorError("node_request_failed", status)
                        body = bytearray()
                        limit = 65536 if text else 4 * 1024 * 1024
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            body.extend(chunk)
                            if len(body) > limit:
                                raise ValueError()
                        cache_directives = {
                            item.strip().lower()
                            for item in response.headers.get("cache-control", "").split(",")
                        }
                        if (text or no_store) and "no-store" not in cache_directives:
                            raise ValueError()
                        if text:
                            return body.decode()
                        return json.loads(body)

                return self._pool.run(node.id, send)
            except DeadlineExceeded:
                observation.reason = Reason.DEADLINE
                raise
            except QueueFull:
                observation.reason = Reason.SATURATED
                raise CapacityExceeded() from None
            except OrchestratorError:
                raise
            except httpx.TimeoutException:
                observation.reason = Reason.TIMEOUT
                raise OrchestratorError("node_unavailable", 503) from None
            except httpx.TransportError as error:
                cause = error
                seen = set()
                while cause is not None and id(cause) not in seen:
                    seen.add(id(cause))
                    if isinstance(cause, ssl.SSLError):
                        observation.reason = Reason.TLS
                        break
                    cause = cause.__cause__ or cause.__context__
                else:
                    observation.reason = Reason.NETWORK
                raise OrchestratorError("node_unavailable", 503) from None
            except (ValueError, UnicodeError):
                observation.reason = Reason.INVALID
                raise OrchestratorError("node_unavailable", 503) from None
            except Exception:
                observation.reason = Reason.INTERNAL
                raise OrchestratorError("node_unavailable", 503) from None

    @staticmethod
    def action(method, path):
        if path == "/v1/health":
            return Action.HEALTH
        if path == "/v1/clients":
            return Action.LIST if method == "GET" else Action.CREATE
        if path.startswith("/v1/control-lease"):
            return Action.LEASE
        match = re.fullmatch(r"/v1/clients/(?:wgapi|ttapi)_[a-f0-9]{32}(?:/([a-z]+))?", path)
        if match:
            try:
                return Action(match[1] or "get")
            except ValueError:
                pass
        return Action.OTHER
