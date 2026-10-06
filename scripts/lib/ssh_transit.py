"""Offline persistent SSH transit candidates; no live mutation commands."""
from dataclasses import dataclass, fields
import hashlib
import ipaddress
import json
from pathlib import PurePosixPath
import re


class TransitError(ValueError):
    """The supplied exact transport or input-file pins are unsafe."""


def address(text):
    if not isinstance(text, str):
        raise TransitError('IPv4 addresses must be strings')
    try:
        value = ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError as error:
        raise TransitError('One normalized IPv4 host is required') from error
    if (str(value) != text or value.is_unspecified or value.is_multicast or
            value.is_loopback or value.is_link_local or value.is_reserved):
        raise TransitError('One normalized usable IPv4 host is required')
    return text


@dataclass(frozen=True)
class Config:
    source_listen: str
    relay_peer: str
    target_node: str
    target_listener: str
    transit_source: str
    target_binary: str

    @classmethod
    def load(cls, text):
        value = json.loads(text)
        if not isinstance(value, dict) or set(value) != {item.name for item in fields(cls)}:
            raise TransitError('Supply exactly the documented transport fields')
        result = cls(**value)
        result.validate()
        return result

    def validate(self):
        values = [address(getattr(self, name)) for name in
                  ('source_listen', 'relay_peer', 'target_node', 'target_listener', 'transit_source')]
        if len(set(values)) != len(values):
            raise TransitError('Transport endpoint and peer identities must be distinct')
        if (not isinstance(self.target_binary, str) or
                not re.fullmatch(r'/opt/pickle/[A-Za-z0-9_./-]+/sshgw-proxyfront', self.target_binary) or
                '..' in PurePosixPath(self.target_binary).parts or '//' in self.target_binary):
            raise TransitError('Target binary must be an exact absolute application path')


def units(c):
    c.validate()
    return {
        'source/pickle-ssh-transit.socket': f'''[Unit]
Description=Pickle peer-only raw SSH transit receiver
After=network-online.target wg-quick@wg0.service nftables.service
Requires=wg-quick@wg0.service nftables.service

[Socket]
ListenStream={c.source_listen}:2224
BindToDevice=wg0
Accept=no
Service=pickle-ssh-transit.service
NoDelay=true

[Install]
WantedBy=sockets.target
''',
        'source/pickle-ssh-transit.service': f'''[Unit]
Description=Pickle raw SSH transit to the active production node
After=network-online.target
Requires=pickle-ssh-transit.socket

[Service]
ExecStart=/usr/lib/systemd/systemd-socket-proxyd {c.target_node}:2224
User=pickle
Group=pickle
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
RestrictAddressFamilies=AF_INET AF_UNIX
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
''',
        'target/pickle-ssh-transit-front.service': f'''[Unit]
Description=Pickle PROXY-required private SSH transit frontend
After=network-online.target isolated-services-firewall.service networking.service sshpiperd.service
Requires=isolated-services-firewall.service networking.service sshpiperd.service

[Service]
User=pickle
Group=pickle
ExecStart={c.target_binary} --listen {c.target_listener}:2224 --upstream 127.0.0.1:2222 --peer {c.transit_source}/32
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
RestrictAddressFamilies=AF_INET AF_UNIX
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
''',
    }


def source_firewall(c, original, expected_sha256):
    """Add only an exact relay input beside the pinned existing peer :22 rule."""
    c.validate()
    if (not isinstance(original, bytes) or not isinstance(expected_sha256, str) or
            not re.fullmatch(r'[0-9a-f]{64}', expected_sha256) or
            hashlib.sha256(original).hexdigest() != expected_sha256):
        raise TransitError('Source persistent firewall bytes differ from the independent pin')
    text = original.decode('utf-8')
    anchor = f'iifname "wg0" ip saddr {c.relay_peer} tcp dport 22 accept'
    lines = text.splitlines(keepends=True)
    candidates = [index for index, line in enumerate(lines) if line.strip() == anchor]
    if len(candidates) != 1 or re.search(r'(?<![0-9])2224(?![0-9])', text):
        raise TransitError('Source peer anchor differs or transit port already has an owner')
    index = candidates[0]
    prefix = ''.join(lines[:index])
    if not re.search(r'table inet sshgw\s*\{[^{}]*chain input\s*\{[^{}]*$', prefix):
        raise TransitError('Source peer anchor is outside the expected input chain')
    indentation = lines[index][:-len(lines[index].lstrip())]
    line = (f'{indentation}iifname "wg0" ip saddr {c.relay_peer} '
            f'ip daddr {c.source_listen} tcp dport 2224 ct state new accept\n')
    lines.insert(index + 1, line)
    return ''.join(lines).encode('utf-8')
