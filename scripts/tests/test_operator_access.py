import base64
import contextlib
import hashlib
import importlib.util
import json
import io
import os
from pathlib import Path
import socket
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("operator_access", Path(__file__).resolve().parents[1] / "lib/operator_access.py")
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)

PUBLIC_KEY = b"ssh-ed25519 " + base64.b64encode(struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))) + b" fixture\n"
POLICY = """permitrootlogin no
pubkeyauthentication yes
usepam yes
usedns no
hostbasedauthentication no
gssapiauthentication no
kbdinteractiveauthentication no
passwordauthentication yes
forcecommand none
strictmodes yes
authenticationmethods any
authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2
authorizedkeyscommand none
trustedusercakeys none
pubkeyacceptedalgorithms ssh-ed25519,rsa-sha2-512
"""


class OperatorAccessTest(unittest.TestCase):
    def test_key_is_one_plain_ed25519_key_with_hash_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "id.pub"
            key.write_bytes(PUBLIC_KEY)
            line = access.read_public_key(key, hashlib.sha256(PUBLIC_KEY).hexdigest())
            self.assertTrue(line.startswith(access.KEY_OPTIONS.encode() + b" ssh-ed25519 "))
            for invalid in (PUBLIC_KEY + PUBLIC_KEY, b"command=bad " + PUBLIC_KEY, PUBLIC_KEY.replace(b"ssh-ed25519 ", b"ssh-rsa ", 1)):
                key.write_bytes(invalid)
                with self.assertRaises(access.AccessError):
                    access.read_public_key(key, hashlib.sha256(invalid).hexdigest())
            with self.assertRaises(access.AccessError):
                access.read_public_key(key, "0" * 64)

    def test_public_key_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / "original"
            original.write_bytes(PUBLIC_KEY)
            link = Path(temporary) / "link"
            link.symlink_to(original)
            with self.assertRaises(OSError):
                access.read_public_key(link, hashlib.sha256(PUBLIC_KEY).hexdigest())

    def test_context_uses_current_connection_without_reusing_a_host_address(self):
        args = SimpleNamespace(ssh_source_address=None, ssh_local_address=None, ssh_port=None)
        with patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.44 54321 198.51.100.30 22"}):
            self.assertEqual(access.ssh_context(args), {"source": "192.0.2.44", "local": "198.51.100.30", "port": 22})
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(access.AccessError):
            access.ssh_context(args)

    def test_locked_account_can_use_existing_password_enabled_pam_policy(self):
        with patch.object(access, "run", return_value=SimpleNamespace(stdout=POLICY)) as run:
            access.check_sshd({"source": "192.0.2.44", "local": "198.51.100.30", "port": 22})
        self.assertIn("host=192.0.2.44", run.call_args.args[0][-1])
        self.assertNotIn("host=198.51.100.30", run.call_args.args[0][-1])

    def test_incompatible_ssh_policy_fails_without_rewriting_config(self):
        variants = [POLICY.replace("usepam yes", "usepam no"), POLICY + "allowusers existing\n",
                    POLICY.replace("forcecommand none", "forcecommand internal-sftp"),
                    POLICY.replace("permitrootlogin no", "permitrootlogin yes"),
                    POLICY.replace("kbdinteractiveauthentication no", "kbdinteractiveauthentication yes")]
        for policy in variants:
            with patch.object(access, "run", return_value=SimpleNamespace(stdout=policy)), self.assertRaises(access.AccessError):
                access.check_sshd({"source": "192.0.2.44", "local": "198.51.100.30", "port": 22})

    def test_freshness_refuses_preexisting_account_and_group(self):
        info = SimpleNamespace(st_mode=0o40755, st_uid=0)
        with patch.object(access.Path, "lstat", return_value=info), patch.object(access.pwd, "getpwnam", return_value=object()), self.assertRaises(access.AccessError):
            access.assert_fresh()
        with patch.object(access.Path, "lstat", return_value=info), patch.object(access.pwd, "getpwnam", side_effect=KeyError), patch.object(access.grp, "getgrnam", return_value=object()), self.assertRaises(access.AccessError):
            access.assert_fresh()

    def test_sudo_proof_drops_inherited_sudo_identity(self):
        with patch.dict(os.environ, {"SUDO_USER": "root", "SUDO_UID": "0", "LD_PRELOAD": "/bad"}), patch.object(access.subprocess, "run") as run:
            run.return_value.returncode, run.return_value.stdout = 0, "0\n"
            access.prove_sudo()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["/usr/sbin/runuser", "-u", "pickle", "--"])
        self.assertEqual(argv[4:], ["/usr/bin/sudo", "-k", "-n", "--", "/usr/bin/id", "-u"])
        self.assertNotIn("SUDO_UID", run.call_args.kwargs["env"])
        self.assertNotIn("SUDO_USER", run.call_args.kwargs["env"])
        self.assertNotIn("LD_PRELOAD", run.call_args.kwargs["env"])

    def test_preflight_does_not_create_account_or_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key = directory / "id.pub"
            key.write_bytes(PUBLIC_KEY)
            args = SimpleNamespace(public_key_file=key, public_key_sha256=access.digest(PUBLIC_KEY),
                                   backup_dir=directory / "state", apply=False)
            with patch.object(access, "ssh_context", return_value={}), patch.object(access, "assert_fresh"), patch.object(access, "private_directory"), patch.object(access, "check_sshd", return_value={}), patch.object(access, "run") as run:
                result = access.enroll(args)
            self.assertFalse(result["changes"])
            self.assertFalse(args.backup_dir.exists())
            self.assertEqual(run.call_args.args[0], ["/usr/sbin/visudo", "-c"])

    def test_rollback_with_unwritable_account_and_state_withdraws_owned_grants(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key, sudo = directory / "authorized_keys", directory / "sudoers"
            key.write_bytes(PUBLIC_KEY)
            sudo.write_bytes(access.SUDO_RULE)
            state = {"uid": os.getuid(), "authorized_line": PUBLIC_KEY.decode()}
            stderr = io.StringIO()
            with patch.object(access, "KEY_FILE", key), patch.object(access, "SUDO_FILE", sudo), patch.object(access, "account_matches"), patch.object(access, "check_account_directories"), patch.object(access, "read_owned_file", side_effect=lambda path, uid: path.read_bytes()), patch.object(access, "disable_account", side_effect=access.AccessError("usermod failed")), patch.object(access, "save_state", side_effect=OSError("state filesystem full")), contextlib.redirect_stderr(stderr):
                access.rollback_enrollment(state, directory)
            self.assertFalse(key.exists())
            self.assertFalse(sudo.exists())
            self.assertIn("account disable: usermod failed", stderr.getvalue())
            self.assertIn("rollback state write", stderr.getvalue())
            self.assertIn("Access may remain", stderr.getvalue())

    def test_rollback_preserves_changed_grant_and_reports_remaining_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key, sudo = directory / "authorized_keys", directory / "sudoers"
            key.write_bytes(PUBLIC_KEY + b"# later edit\n")
            state = {"uid": os.getuid(), "authorized_line": PUBLIC_KEY.decode()}
            stderr = io.StringIO()
            with patch.object(access, "KEY_FILE", key), patch.object(access, "SUDO_FILE", sudo), patch.object(access, "account_matches"), patch.object(access, "check_account_directories"), patch.object(access, "read_owned_file", side_effect=lambda path, uid: path.read_bytes()), patch.object(access, "disable_account"), patch.object(access, "save_state"), contextlib.redirect_stderr(stderr):
                access.rollback_enrollment(state, directory)
            self.assertTrue(key.exists())
            self.assertIn("Changed grant preserved", stderr.getvalue())
            self.assertIn("Access may remain", stderr.getvalue())


@unittest.skipUnless(os.environ.get("PICKLE_TEST_OPERATOR_ROOT") == "1" and os.geteuid() == 0,
                     "Requires a disposable root container with sudo and useradd")
class OperatorAccessRootIntegrationTest(unittest.TestCase):
    def test_enroll_and_revoke_use_real_useradd_visudo_and_sudo(self):
        # Only sshd policy is a fixture; account and sudo operations run in the container.
        with tempfile.TemporaryDirectory(prefix="operator-test-", dir="/root") as temporary:
            directory = Path(temporary)
            key = directory / "id.pub"
            key.write_bytes(PUBLIC_KEY)
            args = SimpleNamespace(public_key_file=key, public_key_sha256=access.digest(PUBLIC_KEY),
                                   backup_dir=directory / "state", apply=True)
            originals = {name: Path("/etc", name).read_bytes() for name in ("sudoers",)}
            Path("/etc/skel/must-not-be-copied").write_text("fixture")
            with patch.object(access, "ssh_context", return_value={}), patch.object(access, "check_sshd", return_value={"fixture": True}):
                result = access.enroll(args)
            self.assertTrue(result["sudo_root_verified"])
            self.assertFalse((access.HOME / "must-not-be-copied").exists())
            access.locked_password()
            self.assertEqual(access.SUDO_FILE.stat().st_mode & 0o777, 0o440)
            self.assertEqual(access.KEY_FILE.stat().st_mode & 0o777, 0o600)
            for name, original in originals.items():
                self.assertEqual(Path("/etc", name).read_bytes(), original)
            extra = b"# preserve another line\n"
            with access.KEY_FILE.open("ab") as output:
                output.write(extra)
            revoke = SimpleNamespace(state_dir=args.backup_dir, apply=True, sessions_quiesced=False)
            with self.assertRaises(access.AccessError):
                access.revoke(revoke)
            self.assertTrue(access.SUDO_FILE.exists())
            revoke.sessions_quiesced = True
            result = access.revoke(revoke)
            self.assertEqual(result["mode"], "revoked")
            self.assertFalse(access.SUDO_FILE.exists())
            self.assertEqual(access.KEY_FILE.read_bytes(), extra)
            self.assertTrue(access.HOME.exists())
            self.assertEqual(access.pwd.getpwnam("pickle").pw_shell, "/usr/sbin/nologin")
            self.assertEqual(json.loads((args.backup_dir / "state.json").read_text())["phase"], "revoked")


if __name__ == "__main__":
    unittest.main()
