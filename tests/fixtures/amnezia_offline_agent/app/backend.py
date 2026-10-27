"""Privileged boundary: fixed Docker arguments, dedicated container, no caller shell."""
import base64
import ipaddress
import json
import re
import subprocess
import time
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .domain import Fault, stamp

AWG_FIELDS = {'Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'S3', 'S4', 'H1', 'H2', 'H3', 'H4',
              'I1', 'I2', 'I3', 'I4', 'I5', 'HeaderProtectionKey', 'ContentPaddingAddition',
              'RekeyAfterTime', 'RekeyTimeout', 'RejectAfterTime', 'KeepaliveTimeout',
              'MaxHandshakeAttempts', 'RandomTrailers', 'DisableCookies'}


class ConfigurationRenderer:
    def configuration(self, row, secret):
        params = ''.join(f'{key} = {value}\n' for key, value in self.config['parameters'].items())
        return (f"[Interface]\nPrivateKey = {secret['private']}\nAddress = {row['ip']}/32\n"
                f"DNS = {', '.join(self.config['dns'])}\nMTU = 1280\n{params}\n[Peer]\nPublicKey = {self.server_public_key}\n"
                f"PresharedKey = {secret['psk']}\nEndpoint = {self.config['endpoint']}\n"
                'AllowedIPs = 0.0.0.0/0, ::/0\nPersistentKeepalive = 25\n')

    def amnezia(self, row, secret):
        from .export import guest_profile
        return guest_profile(self.config, row, secret, self.server_public_key, self.configuration(row, secret))


class DockerBackend(ConfigurationRenderer):
    CONTAINER = 'undercore-awg-api'
    INTERFACE = 'awg0'
    MANAGED_LABEL = ('ru.undercore.managed', 'amnezia-config-api')

    def __init__(self, config):
        self.config = config
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}:\d{1,5}', config['endpoint']):
            raise ValueError('invalid endpoint')
        if not 1 <= int(config['endpoint'].rsplit(':', 1)[1]) <= 65535:
            raise ValueError('invalid port')
        for value in config['dns']:
            ipaddress.ip_address(value)
        if not config['dns'] or not isinstance(config['parameters'], dict) or not set(config['parameters']) <= AWG_FIELDS:
            raise ValueError('invalid AWG parameters')
        for value in config['parameters'].values():
            if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 32 for c in value):
                raise ValueError('invalid parameter')
        key = base64.b64decode(config['server_private_key'], validate=True)
        private = X25519PrivateKey.from_private_bytes(key)
        self.server_public_key = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    def run(self, args, data=None, *, timeout=8):
        try:
            result = subprocess.run(['/usr/bin/docker', '--host', 'unix:///run/docker.sock', *args], input=data, capture_output=True, text=True, timeout=timeout, check=False,
                                    env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/nonexistent', 'DOCKER_CONFIG': '/nonexistent'})
        except (OSError, subprocess.TimeoutExpired):
            raise Fault('vpn_unavailable', 503) from None
        if result.returncode:
            raise Fault('vpn_unavailable', 503)
        return result.stdout

    def snapshot(self):
        info = json.loads(self.run(['inspect', self.CONTAINER]))[0]
        if info['Config'].get('Labels', {}).get(self.MANAGED_LABEL[0]) != self.MANAGED_LABEL[1] or not info['State']['Running']:
            raise Fault('managed_container_unavailable', 503)
        if self.run(['exec', self.CONTAINER, 'awg', 'show', self.INTERFACE, 'public-key']).strip() != self.server_public_key:
            raise Fault('server_identity_conflict')
        # No dump: it would expose the server private key.
        peers = set(self.run(['exec', self.CONTAINER, 'awg', 'show', self.INTERFACE, 'peers']).split())
        times = self.run(['exec', self.CONTAINER, 'awg', 'show', self.INTERFACE, 'latest-handshakes'])
        handshakes = {}
        for line in times.splitlines():
            key, value = line.split()
            ts = int(value)
            handshakes[key] = stamp(datetime.fromtimestamp(ts, timezone.utc)) if 0 < ts <= datetime.now(timezone.utc).timestamp() else None
        return info['State']['StartedAt'], peers, handshakes

    def transfer_snapshot(self):
        # One bounded read budget: telemetry cannot starve the expiry executor.
        deadline = time.monotonic() + 2
        def read(args):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Fault('collection_timeout', 503)
            return self.run(args, timeout=remaining)
        before = json.loads(read(['inspect', self.CONTAINER]))[0]
        if before['Config'].get('Labels', {}).get(self.MANAGED_LABEL[0]) != self.MANAGED_LABEL[1] or not before['State']['Running']:
            raise Fault('managed_container_unavailable', 503)
        if read(['exec', self.CONTAINER, 'awg', 'show', self.INTERFACE, 'public-key']).strip() != self.server_public_key:
            raise Fault('server_identity_conflict')
        values = {}
        for line in read(['exec', self.CONTAINER, 'awg', 'show', self.INTERFACE, 'transfer']).splitlines():
            key, rx, tx = line.split()
            if key in values or not rx.isascii() or not tx.isascii() or not rx.isdecimal() or not tx.isdecimal():
                raise Fault('counter_invalid', 503)
            values[key] = {'rx': int(rx), 'tx': int(tx), 'generation': 'direct'}
        after = json.loads(read(['inspect', self.CONTAINER]))[0]
        if before['State']['StartedAt'] != after['State']['StartedAt'] or not after['State']['Running']:
            raise Fault('counter_invalid', 503)
        return before['State']['StartedAt'], values

    def apply(self, rows):
        config = '[Interface]\nPrivateKey = ' + self.config['server_private_key'] + '\n'
        config += 'ListenPort = ' + str(self.config['listen_port']) + '\n'
        config += ''.join(f'{key} = {value}\n' for key, value in self.config['parameters'].items())
        for row, secret in rows:
            config += f"\n[Peer]\nPublicKey = {row['public_key']}\nPresharedKey = {secret['psk']}\nAllowedIPs = {row['ip']}/32\n"
        self.run(['exec', '-i', self.CONTAINER, 'awg', 'syncconf', self.INTERFACE, '/dev/stdin'], config)

    def emergency_stop(self):
        # Fail closed for this dedicated container only. Operator resolves before restart.
        self.run(['stop', '--time', '2', self.CONTAINER])
