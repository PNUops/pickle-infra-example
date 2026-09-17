"""Enroll and revoke one dedicated operator account without changing sshd policy."""
import argparse
import base64
import datetime
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys

ACCOUNT = "pickle"
HOME = Path("/home/pickle")
KEY_FILE = HOME / ".ssh/authorized_keys"
SUDO_FILE = Path("/etc/sudoers.d/90-pickle")
SUDO_RULE = b"pickle ALL=(ALL:ALL) NOPASSWD: ALL\n"
KEY_OPTIONS = "no-agent-forwarding,no-port-forwarding,no-X11-forwarding"
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/root"}


class AccessError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise AccessError(message)


def run(argv, *, check=True):
    result = subprocess.run(argv, env=SAFE_ENV, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise AccessError(f"{Path(argv[0]).name} failed (exit {result.returncode}); no credentials were logged")
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


def new_file(path, data, mode=0o600, owner=None):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
        if owner:
            os.fchown(output.fileno(), *owner)
        os.fchmod(output.fileno(), mode)


def save_state(directory, state):
    state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    temporary = directory / "state.next"
    new_file(temporary, (json.dumps(state, indent=2) + "\n").encode())
    os.replace(temporary, directory / "state.json")


def private_directory(path):
    require(path.is_absolute(), "An absolute private directory is required")
    for parent in reversed([path, *path.parents]):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                f"Unsafe directory component: {parent}")
    require(stat.S_IMODE(path.stat().st_mode) == 0o700, f"Expected root-owned 0700: {path}")


def read_public_key(path, expected):
    require(re.fullmatch(r"[0-9a-f]{64}", expected), "Expected a lowercase SHA256")
    require(path.is_absolute(), "Public key path must be absolute")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        require(stat.S_ISREG(os.fstat(source.fileno()).st_mode), "Public key must be a regular file")
        raw = source.read(4097)
    require(len(raw) <= 4096 and digest(raw) == expected, "Public key size or SHA256 mismatch")
    try:
        text = raw.decode("ascii")
        require(len(text.splitlines()) == 1, "Exactly one public key line is required")
        fields = text.strip().split(None, 2)
        require(len(fields) >= 2 and fields[0] == "ssh-ed25519", "Only a plain Ed25519 public key is accepted")
        wire = base64.b64decode(fields[1], validate=True)
        expected_prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
        require(len(wire) == 51 and wire.startswith(expected_prefix), "Invalid Ed25519 public key encoding")
        require(base64.b64encode(wire).decode() == fields[1], "Public key encoding must be canonical")
    except (UnicodeError, ValueError) as error:
        raise AccessError("Invalid public key encoding") from error
    return f"{KEY_OPTIONS} ssh-ed25519 {fields[1]} pickle-node-admin\n".encode()


def ssh_context(args):
    connection = os.environ.get("SSH_CONNECTION", "").split()
    source = args.ssh_source_address or (connection[0] if len(connection) == 4 else None)
    local = args.ssh_local_address or (connection[2] if len(connection) == 4 else None)
    port = args.ssh_port or (connection[3] if len(connection) == 4 else None)
    try:
        source, local, port = str(ipaddress.ip_address(source)), str(ipaddress.ip_address(local)), int(port)
        require(1 <= port <= 65535, "Invalid SSH port")
    except (ValueError, TypeError) as error:
        raise AccessError("Preserve SSH_CONNECTION through sudo or supply the three SSH context arguments") from error
    return {"source": source, "local": local, "port": port}


def check_sshd(context):
    run(["/usr/sbin/sshd", "-t"])
    connection = (f"user={ACCOUNT},host={context['source']},addr={context['source']},"
                  f"laddr={context['local']},lport={context['port']}")
    output = run(["/usr/sbin/sshd", "-T", "-C", connection]).stdout
    policy = dict(line.split(None, 1) for line in output.splitlines() if " " in line)
    required = {"permitrootlogin": "no", "pubkeyauthentication": "yes", "usepam": "yes",
                "usedns": "no", "hostbasedauthentication": "no", "gssapiauthentication": "no",
                "kbdinteractiveauthentication": "no", "forcecommand": "none", "strictmodes": "yes"}
    for key, value in required.items():
        require(policy.get(key) == value, f"Existing sshd policy requires review: {key} must be {value}")
    require(policy.get("authenticationmethods") in ("any", "publickey"), "Existing SSH authentication chain is incompatible")
    for key in ("allowusers", "denyusers", "allowgroups", "denygroups"):
        require(not policy.get(key), f"Existing {key} requires a separate account access review")
    keys = policy.get("authorizedkeysfile", "").split()
    require(keys and keys[0] in (".ssh/authorized_keys", "%h/.ssh/authorized_keys")
            and all(key in (".ssh/authorized_keys", "%h/.ssh/authorized_keys", ".ssh/authorized_keys2") for key in keys),
            "Existing AuthorizedKeysFile must use the new account's standard key files")
    require(policy.get("authorizedkeyscommand") == "none" and policy.get("trustedusercakeys") == "none",
            "External SSH key or certificate authorization requires a separate review")
    algorithms = policy.get("pubkeyacceptedalgorithms", "").split(",")
    require("ssh-ed25519" in algorithms, "Existing sshd policy does not accept Ed25519 keys")
    return policy


def assert_fresh():
    for path in (HOME.parent, SUDO_FILE.parent):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                f"Unsafe account target parent: {path}")
    for getter in (pwd.getpwnam, grp.getgrnam):
        try:
            getter(ACCOUNT)
        except KeyError:
            continue
        raise AccessError("Account or group pickle already exists; refusing to adopt it")
    for path in (HOME, SUDO_FILE):
        require(not os.path.lexists(path), f"Target already exists: {path}")


def locked_password():
    rows = [line.split(":") for line in Path("/etc/shadow").read_text().splitlines()
            if line.startswith(ACCOUNT + ":")]
    require(len(rows) == 1 and rows[0][1].startswith(("!", "*")), "New account password is not locked")


def validate_sudo(directory):
    candidate = directory / "sudoers.candidate"
    new_file(candidate, SUDO_RULE, 0o440)
    combined = directory / "sudoers.preview"
    new_file(combined, f"@include /etc/sudoers\n@include {candidate}\n".encode(), 0o440)
    run(["/usr/sbin/visudo", "-c", "-f", str(candidate)])
    run(["/usr/sbin/visudo", "-c", "-f", str(combined)])


def prove_sudo():
    # runuser must enter the real target uid; a root-run `sudo -l` is not proof.
    result = run(["/usr/sbin/runuser", "-u", ACCOUNT, "--", "/usr/bin/sudo", "-k", "-n", "--", "/usr/bin/id", "-u"])
    require(result.stdout.strip() == "0", "New account did not obtain root through noninteractive sudo")


def account_matches(state):
    account, group = pwd.getpwnam(ACCOUNT), grp.getgrnam(ACCOUNT)
    require(account.pw_uid == state["uid"] and account.pw_gid == state["gid"]
            and group.gr_gid == state["gid"] and account.pw_dir == str(HOME),
            "Account identity differs from the recorded enrollment")
    return account


def disable_account(state):
    account_matches(state)
    run(["/usr/sbin/usermod", "--lock", "--expiredate", "1970-01-02", "--shell", "/usr/sbin/nologin", ACCOUNT])


def rollback_enrollment(state, directory):
    failures = []
    try:
        account_matches(state)
    except (AccessError, KeyError, OSError) as error:
        print(f"Rollback ownership check failed: {error}; access may remain. Use the existing administrator session.", file=sys.stderr)
        return
    try:
        disable_account(state)
    except (AccessError, OSError, subprocess.SubprocessError) as error:
        failures.append(f"account disable: {error}")
    # Withdraw each owned grant independently, even if the account database is unwritable.
    for path, expected, uid in ((KEY_FILE, state["authorized_line"].encode(), state["uid"]),
                                (SUDO_FILE, SUDO_RULE, 0)):
        try:
            if os.path.lexists(path):
                if path == KEY_FILE:
                    check_account_directories(state["uid"])
                require(read_owned_file(path, uid) == expected, f"Changed grant preserved: {path}")
                path.unlink()
        except (AccessError, OSError) as error:
            failures.append(f"grant withdrawal {path}: {error}")
    state["phase"] = "failed_cleanup_pending" if failures else "failed_disabled"
    try:
        save_state(directory, state)
    except OSError as error:
        failures.append(f"rollback state write: {error}")
    for failure in failures:
        print(f"Rollback incomplete: {failure}", file=sys.stderr)
    if failures:
        print("Access may remain. Keep the existing administrator session and inspect account, keys, sudoers and elevated processes.", file=sys.stderr)


def enroll(args):
    authorized = read_public_key(args.public_key_file, args.public_key_sha256)
    context = ssh_context(args)
    assert_fresh()
    private_directory(args.backup_dir.parent)
    require(re.fullmatch(r"/[A-Za-z0-9_./-]+", str(args.backup_dir)), "Backup path must not contain sudoers metacharacters")
    require(not os.path.lexists(args.backup_dir), "Backup/state directory must be new")
    policy = check_sshd(context)
    run(["/usr/sbin/visudo", "-c"])
    if not args.apply:
        return {"mode": "preflight", "account": ACCOUNT, "grant": "unrestricted passwordless root sudo",
                "ssh_context": context, "changes": False}
    args.backup_dir.mkdir(mode=0o700)
    state = {"schema_version": 1, "account": ACCOUNT, "host": socket.gethostname(), "phase": "prepared",
             "public_key_sha256": args.public_key_sha256, "authorized_line": authorized.decode(),
             "sudoers_sha256": digest(SUDO_RULE), "ssh_context": context}
    save_state(args.backup_dir, state)
    try:
        for filename in ("passwd", "shadow", "group", "gshadow", "sudoers", "subuid", "subgid"):
            source = Path("/etc", filename)
            if filename not in ("subuid", "subgid") or source.exists():
                new_file(args.backup_dir / f"before-{filename}", source.read_bytes())
        new_file(args.backup_dir / "sshd-effective.json", (json.dumps(policy, indent=2) + "\n").encode())
        validate_sudo(args.backup_dir)
        skeleton = args.backup_dir / "empty-skeleton"
        skeleton.mkdir(mode=0o700)
        assert_fresh()
        run(["/usr/sbin/useradd", "--user-group", "--create-home", "--home-dir", str(HOME),
             "--skel", str(skeleton), "--shell", "/bin/bash", "--password", "!", "--no-log-init", ACCOUNT])
        account = pwd.getpwnam(ACCOUNT)
        state.update(uid=account.pw_uid, gid=account.pw_gid, phase="account_created")
        save_state(args.backup_dir, state)
        require(not active_processes(account.pw_uid), "Allocated UID already has processes; no access will be granted")
        require(os.getgrouplist(ACCOUNT, account.pw_gid) == [account.pw_gid], "Unexpected supplementary groups")
        locked_password()
        require(not os.path.lexists(HOME / ".ssh"), "Skeleton unexpectedly supplied an SSH directory")
        require(HOME.lstat().st_uid == account.pw_uid and stat.S_ISDIR(HOME.lstat().st_mode), "Unexpected new home owner/type")
        HOME.chmod(0o700)
        (HOME / ".ssh").mkdir(mode=0o700)
        os.chown(HOME / ".ssh", account.pw_uid, account.pw_gid)
        check_sshd(context)
        new_file(SUDO_FILE, SUDO_RULE, 0o440, (0, 0))
        run(["/usr/sbin/visudo", "-c"])
        prove_sudo()
        new_file(KEY_FILE, authorized, 0o600, (account.pw_uid, account.pw_gid))
        state["phase"] = "active"
        save_state(args.backup_dir, state)
    except BaseException:
        if "uid" in state:
            rollback_enrollment(state, args.backup_dir)
        print(f"Enrollment incomplete. Review {args.backup_dir}/state.json; use revoke-operator-access.py with this state directory.", file=sys.stderr)
        raise
    return {"mode": "applied", "account": ACCOUNT, "uid": state["uid"], "state_dir": str(args.backup_dir),
            "sudo_root_verified": True, "ssh_login_verified": False, "grant": "unrestricted passwordless root sudo"}


def active_processes(uid):
    pids = []
    for entry in Path("/proc").glob("[0-9]*/status"):
        try:
            match = re.search(r"^Uid:\s+(.*)$", entry.read_text(), re.MULTILINE)
            if match and uid in map(int, match.group(1).split()):
                pids.append(int(entry.parent.name))
        except FileNotFoundError:
            continue
    return sorted(pids)


def read_owned_file(path, uid):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o022,
            f"Unsafe owned file: {path}")
    return path.read_bytes()


def check_account_directories(uid):
    for path in (HOME, HOME / ".ssh"):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o022,
                f"Unsafe account directory: {path}")


def revoke(args):
    private_directory(args.state_dir)
    state_path = args.state_dir / "state.json"
    state = json.loads(read_owned_file(state_path, 0))
    require(state.get("schema_version") == 1 and state.get("account") == ACCOUNT
            and state.get("host") == socket.gethostname(), "State does not identify this account and host")
    require(isinstance(state.get("uid"), int) and isinstance(state.get("gid"), int),
            "Account creation was not recorded; inspect partial setup manually before claiming ownership")
    account_matches(state)
    sudo = read_owned_file(SUDO_FILE, 0) if os.path.lexists(SUDO_FILE) else None
    require(sudo is None or digest(sudo) == state["sudoers_sha256"] == digest(SUDO_RULE), "Owned sudoers file has changed; preserve it for manual review")
    key = None
    if os.path.lexists(KEY_FILE):
        check_account_directories(state["uid"])
        key = read_owned_file(KEY_FILE, state["uid"])
    line = state["authorized_line"].encode()
    require(line.startswith((KEY_OPTIONS + " ssh-ed25519 ").encode()) and line.endswith(b"\n"), "Invalid enrollment key state")
    remaining = b"".join(part for part in key.splitlines(keepends=True) if part != line) if key is not None else None
    pids = active_processes(state["uid"])
    if not args.apply:
        return {"mode": "preflight", "account": ACCOUNT, "active_uid_pids": pids, "changes": False,
                "remaining_key_lines": len(remaining.splitlines()) if remaining else 0}
    require(args.sessions_quiesced, "Stop pickle login sessions and elevated jobs first; then pass --sessions-quiesced")
    require(not pids, f"Account processes remain: {pids}; quiesce them before revocation")
    disable_account(state)
    state["phase"] = "revoking"
    save_state(args.state_dir, state)
    if sudo is not None:
        require(read_owned_file(SUDO_FILE, 0) == sudo, "Sudoers changed during revocation")
        SUDO_FILE.unlink()
        run(["/usr/sbin/visudo", "-c"])
    if key is not None and remaining != key:
        require(read_owned_file(KEY_FILE, state["uid"]) == key, "Authorized keys changed during revocation")
        replacement = KEY_FILE.with_name("authorized_keys.revoke")
        new_file(replacement, remaining, 0o600, (state["uid"], state["gid"]))
        os.replace(replacement, KEY_FILE)
    state["phase"] = "revoked"
    save_state(args.state_dir, state)
    return {"mode": "revoked", "account": ACCOUNT, "home_preserved": True,
            "remaining_key_lines": len(remaining.splitlines()) if remaining else 0}


def main(mode):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--apply", action="store_true")
    if mode == "enroll":
        parser.add_argument("--public-key-file", type=Path, required=True)
        parser.add_argument("--public-key-sha256", required=True)
        parser.add_argument("--backup-dir", type=Path, required=True)
        parser.add_argument("--ssh-source-address")
        parser.add_argument("--ssh-local-address")
        parser.add_argument("--ssh-port", type=int)
    else:
        parser.add_argument("--state-dir", type=Path, required=True)
        parser.add_argument("--sessions-quiesced", action="store_true")
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0 and socket.gethostname() == args.expected_host, "Root privileges and the exact hostname are required")
        os.umask(0o077)
        require(os.environ.get("SUDO_USER") != ACCOUNT, "Use a separate existing administrator session")
        for tool in ("useradd", "usermod", "visudo", "runuser", "sudo", "sshd"):
            require(shutil.which(tool, path=SAFE_ENV["PATH"]), f"Required installed tool is missing: {tool}")
        # O_NOFOLLOW and root ownership keep the shared local lock unambiguous.
        fd = os.open("/run/lock/pickle-operator-access.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            info = os.fstat(lock.fileno())
            require(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077, "Unsafe access lock")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(enroll(args) if mode == "enroll" else revoke(args), indent=2))
    except (AccessError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Operator access refused: {error}\n")
