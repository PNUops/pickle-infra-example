#!/usr/bin/env python3
"""Export only one development Certbot lineage as recipient-encrypted CMS."""
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile

MAXIMUM = 2 * 1024 * 1024


def archive(root, lineage):
    root = root.resolve(strict=True)
    if not lineage or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in lineage):
        raise ValueError('Invalid lineage')
    output = io.BytesIO()
    files = [('renewal/' + lineage + '.conf', False)]
    for name in ('cert.pem', 'chain.pem', 'fullchain.pem', 'privkey.pem'):
        path = root / 'live' / lineage / name
        if not path.is_symlink():
            raise ValueError('Certbot live link required')
        target = path.resolve(strict=True)
        if target.parent != (root / 'archive' / lineage).resolve(strict=True):
            raise ValueError('Cross-lineage link refused')
        files.extend([(str(target.relative_to(root)), False),
                      ('live/' + lineage + '/' + name, True)])
    with tarfile.open(fileobj=output, mode='w') as tar:
        for relative, link in files:
            path = root / relative
            if link:
                item = tarfile.TarInfo(relative)
                item.type = tarfile.SYMTYPE
                item.linkname = os.readlink(path)
                item.mode = 0o777
                tar.addfile(item)
            else:
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Regular lineage file required')
                data = path.read_bytes()
                if not data or len(data) > MAXIMUM:
                    raise ValueError('Lineage file size refused')
                item = tarfile.TarInfo(relative)
                item.size = len(data)
                item.mode = 0o600
                tar.addfile(item, io.BytesIO(data))
    if len(output.getvalue()) > MAXIMUM:
        raise ValueError('Lineage archive size refused')
    return output.getvalue()


def main():
    # Installation pins the public recipient and lineage in a root-owned config.
    import json
    config = json.loads(Path('/etc/pickle-dev-tls/export.json').read_text())
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != config['hostname']:
        raise ValueError('Wrong export host')
    if os.environ.get('SSH_ORIGINAL_COMMAND', '') not in ('', 'dev-tls-export'):
        raise ValueError('SSH command refused')
    if config['ctid'] != 100:
        raise ValueError('Wrong TLS container')
    guest = ('import io,os; from pathlib import Path; import tarfile; ' +
             '\n' + 'MAXIMUM=' + str(MAXIMUM) + '\n' +
             __import__('inspect').getsource(archive) +
             '\nimport sys; sys.stdout.buffer.write(archive(Path("/etc/letsencrypt"), ' +
             repr(config['lineage']) + '))')
    result = subprocess.run(['pct', 'exec', '100', '--', '/usr/bin/python3', '-I', '-c', guest],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=30, check=True)
    data = result.stdout
    if len(data) > MAXIMUM:
        raise ValueError('Export exceeds limit')
    result = subprocess.run(['openssl', 'cms', '-encrypt', '-binary', '-outform', 'DER',
                             '-aes-256-gcm', config['recipient_certificate']], input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                            check=True)
    sys.stdout.buffer.write(result.stdout)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, KeyError):
        print('Development TLS export failed', file=sys.stderr)
        sys.exit(1)
