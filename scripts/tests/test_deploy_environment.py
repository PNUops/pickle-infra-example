"""Failure-path checks for the environment deployment tool."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy-environment.py"
SPEC = importlib.util.spec_from_file_location("deploy_environment", SCRIPT)
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeployEnvironmentTest(unittest.TestCase):
    def test_manifest_refuses_unselected_commit(self):
        config = {"allowed_commits": {"api": "a" * 40}}
        with self.assertRaisesRegex(deploy.Refusal, "differs"):
            deploy.check_manifest(config, "api", "b" * 40)
        deploy.check_manifest(config, "api", "a" * 40)

    def test_gateway_refuses_before_api_and_console_receipts(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(deploy, "RECEIPTS", Path(folder)):
            with self.assertRaisesRegex(deploy.Refusal, "api deployment receipt is missing"):
                deploy.check_chat_order({"allowed_commits": {"api": "a" * 40}}, "dev")

    def test_gateway_requires_migration_and_explicit_compatibility_with_chat_free_keys(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(deploy, "RECEIPTS", Path(folder)):
            receipts = Path(folder) / "dev"
            receipts.mkdir()
            for target, commit in (("api", "a" * 40), ("console", "b" * 40)):
                (receipts / f"{target}.json").write_text(json.dumps({
                    "environment": "dev", "target": target, "commit": commit,
                    "index_sha256": "c" * 64, "artifact_sha256": "d" * 64,
                }))
            config = {
                "allowed_commits": {"api": "a" * 40, "console": "b" * 40},
                "targets": {
                    "api": {"ctid": 901, "release_dir": "/opt/pickle/api/releases"},
                    "console": {"ctid": 901, "webroot": "/var/www/pickle-console-dev"},
                },
                "chat_db": {"ctid": 902, "hostname": "example-db", "name": "pickle_dev"},
            }
            proof = Path(folder) / "chat.json"
            config["chat_compatibility_file"] = str(proof)
            proof.write_text(json.dumps({
                "environment": "dev", "commits": config["allowed_commits"],
                "api_sha256": "d" * 64, "console_index_sha256": "c" * 64,
                "generation": 2, "existing_key_grants_preserved": True,
                "snapshot_grants_match_database": True, "chat_free_keys_verified": True,
                "checked_at": int(time.time()),
            }))
            def fake_guest(ctid, *_argv):
                answer = "c" * 64 + "  index.html\n" if ctid == 901 else self.database_result
                return type("Result", (), {"stdout": answer})()

            with patch.object(deploy, "check_ct"), patch.object(deploy, "check_unit", return_value=True), \
                 patch.object(deploy, "active_link", return_value="/opt/pickle/api/releases/pickle-api-" + "a" * 40 + ".jar"), \
                 patch.object(deploy, "verify_running_artifact"),                  patch.object(deploy, "guest", side_effect=fake_guest):
                self.database_result = "0|2|2\n"
                with self.assertRaisesRegex(deploy.Refusal, "Chat migration"):
                    deploy.check_chat_order(config, "dev")
                self.database_result = "1|2|3\n"
                with self.assertRaisesRegex(deploy.Refusal, "generation"):
                    deploy.check_chat_order(config, "dev")
                self.database_result = "1|2|2\n"
                deploy.check_chat_order(config, "dev")
                evidence = json.loads(proof.read_text())
                evidence["chat_free_keys_verified"] = False
                proof.write_text(json.dumps(evidence))
                with self.assertRaisesRegex(deploy.Refusal, "compatibility result"):
                    deploy.check_chat_order(config, "dev")

    def test_archive_rejects_escape_and_links(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bad.tar.gz"
            with tarfile.open(target, "w:gz") as archive:
                item = tarfile.TarInfo("../outside")
                item.size = 0
                archive.addfile(item)
            with self.assertRaisesRegex(deploy.Refusal, "unsafe path"):
                deploy.validate_archive(target, "console")
            with tarfile.open(target, "w:gz") as archive:
                item = tarfile.TarInfo("index.html")
                item.type = tarfile.SYMTYPE
                item.linkname = "/etc/passwd"
                archive.addfile(item)
            with self.assertRaisesRegex(deploy.Refusal, "non-regular"):
                deploy.validate_archive(target, "console")

    def test_dev_console_archive_rejects_operating_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bundle.tar.gz"
            payload = b"<html>https://llm.example.com/v1</html>"
            with tarfile.open(target, "w:gz") as archive:
                item = tarfile.TarInfo("index.html")
                item.size = len(payload)
                archive.addfile(item, io.BytesIO(payload))
            with self.assertRaisesRegex(deploy.Refusal, "operating endpoint"):
                deploy.validate_archive(target, "console", ["llm.example.com"])

    def test_console_archive_accepts_tar_root_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bundle.tar.gz"
            with tarfile.open(target, "w:gz") as archive:
                root = tarfile.TarInfo(".")
                root.type = tarfile.DIRTYPE
                archive.addfile(root)
                payload = b"<html></html>"
                index = tarfile.TarInfo("./index.html")
                index.size = len(payload)
                archive.addfile(index, io.BytesIO(payload))
            deploy.validate_archive(target, "console")

    def test_unit_refuses_unselected_artifact_before_a_restart(self):
        entry = {
            "ctid": 901,
            "unit": "pickle-api-dev.service",
            "active_path": "/opt/pickle/api/current-dev.jar",
            "env_file": "/etc/pickle/development.env",
            "db_url_prefix": "jdbc:postgresql://198.18.0.60:5432/pickle_dev",
        }
        values = {
            "LoadState": "loaded",
            "ExecStart": "{ path=/usr/bin/java ; argv[]=/usr/bin/java -jar /opt/pickle/api/old.jar --spring.profiles.active=dev }",
            "EnvironmentFiles": "/etc/pickle/development.env (ignore_errors=no)",
            "Environment": "",
            "PassEnvironment": "",
        }
        with patch.object(deploy, "guest_property", side_effect=lambda _ct, _unit, prop: values[prop]):
            with self.assertRaisesRegex(deploy.Refusal, "managed artifact"):
                deploy.check_unit(entry, "api", "dev", "check")

    def test_later_environment_file_cannot_override_dev_database(self):
        entry = {
            "ctid": 901,
            "unit": "pickle-api-dev.service",
            "active_path": "/opt/pickle/api/current-dev.jar",
            "env_file": "/etc/pickle/development.env",
        }
        values = {
            "LoadState": "loaded",
            "ExecStart": "{ path=/usr/bin/java ; argv[]=/usr/bin/java -jar /opt/pickle/api/current-dev.jar --spring.profiles.active=dev }",
            "EnvironmentFiles": "/etc/pickle/development.env (ignore_errors=no) /etc/pickle/production.env (ignore_errors=no)",
        }
        with patch.object(deploy, "guest_property", side_effect=lambda _ct, _unit, prop: values[prop]):
            with self.assertRaisesRegex(deploy.Refusal, "exactly its expected env"):
                deploy.check_unit(entry, "api", "dev", "check")

    def test_production_fixed_binary_is_readable_but_not_applyable(self):
        entry = {
            "ctid": 913,
            "unit": "llm-gateway-production.service",
            "active_path": "/opt/pickle/llm-gateway/bin/current-production",
            "legacy_exec_path": "/opt/pickle/llm-gateway/bin/llm-gateway-old",
            "legacy_sha256": "a" * 64,
            "env_file": "/etc/pickle/llm-gateway-production.env",
        }
        values = {
            "LoadState": "loaded",
            "ExecStart": "{ path=/opt/pickle/llm-gateway/bin/llm-gateway-old ; argv[]=/opt/pickle/llm-gateway/bin/llm-gateway-old }",
            "EnvironmentFiles": "/etc/pickle/llm-gateway-production.env (ignore_errors=no)",
            "Environment": "",
            "ActiveState": "active",
        }
        response = type("Result", (), {"stdout": "a" * 64 + "  binary\n", "returncode": 0})()
        with patch.object(deploy, "guest_property", side_effect=lambda _ct, _unit, prop: values[prop]), \
             patch.object(deploy, "guest", return_value=response):
            self.assertFalse(deploy.check_unit(entry, "llm-gateway", "prod", "check"))
            with self.assertRaisesRegex(deploy.Refusal, "managed artifact"):
                deploy.check_unit(entry, "llm-gateway", "prod", "apply")

    def test_failed_gateway_health_restores_both_previous_links(self):
        entry = {
            "ctid": 901,
            "unit": "llm-gateway-dev.service",
            "active_path": "/opt/pickle/llm-gateway/bin/current-dev",
            "keygen_path": "/opt/pickle/llm-gateway/bin/llm-keygen-dev",
        }
        calls = []
        links = []

        def fake_guest(_ctid, *argv, **_kwargs):
            calls.append(argv)
            return type("Result", (), {"returncode": 0})()

        with patch.object(deploy, "guest", side_effect=fake_guest), \
             patch.object(deploy, "select_link", side_effect=lambda ct, link, value: links.append((ct, link, value))), \
             patch.object(deploy, "wait_health", side_effect=(False, True)):
            with self.assertRaisesRegex(deploy.Refusal, "previous artifact restored"):
                deploy.activate_binary(entry, "llm-gateway", "/releases/new", "/releases/new-keygen",
                                       "/releases/old", "/releases/old-keygen", "compatible")
        self.assertIn((901, entry["active_path"], "/releases/old"), links)
        self.assertIn((901, entry["keygen_path"], "/releases/old-keygen"), links)
        self.assertEqual(sum(1 for call in calls if call[:2] == ("systemctl", "restart")), 2)

    def test_development_unit_rejects_named_upstream_and_other_direct_overrides(self):
        entry = {"ctid": 901, "unit": "gateway-dev.service", "active_path": "/bin/current-dev",
                 "env_file": "/etc/pickle/gateway-dev.env"}
        values = {"LoadState": "loaded", "ExecStart": "{ argv[]=/bin/current-dev }",
                  "EnvironmentFiles": "/etc/pickle/gateway-dev.env (ignore_errors=no)",
                  "Environment": "", "PassEnvironment": "", "UnsetEnvironment": ""}
        for override in ("LLMGW_UPSTREAM_1_BASE_URL=https://operating.example.com",
                         "LLMGW_LISTEN=0.0.0.0:8081", "PICKLE_PROXY_AGENT_URL=https://operating.example.com",
                         "SPRING_APPLICATION_JSON={}", "JAVA_TOOL_OPTIONS=-Dspring.profiles.active=prod"):
            values["Environment"] = override
            with patch.object(deploy, "guest_property", side_effect=lambda _ct, _unit, prop: values[prop]), \
                 patch.object(deploy, "guest", return_value=type("Result", (), {"returncode": 0})()):
                with self.assertRaisesRegex(deploy.Refusal, "overrides"):
                    deploy.check_unit(entry, "llm-gateway", "dev", "apply")

    def test_environment_parser_rejects_generic_override_channels(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / "env"
            entry = {"ctid": 901, "env_file": str(env), "required_env": {"PICKLE_DNS_PROVIDER": "noop"},
                     "prefix_env": {"PICKLE_OPENROUTER_URL": "http://127.0.0.1:"}}
            def execute_probe(_ctid, *argv, **kwargs):
                return subprocess.run(argv, capture_output=True, text=True)
            safe = "PICKLE_DNS_PROVIDER=noop\nPICKLE_OPENROUTER_URL=http://127.0.0.1:1234\nPICKLE_DEV_IP_POOL_RESERVED_RANGES='[{\"start\":\"192.0.2.1\",\"end\":\"192.0.2.10\"}]'\n"
            with patch.object(deploy, "guest", side_effect=execute_probe):
                env.write_text(safe)
                deploy.check_env_rules(entry)
                for dangerous in ("SPRING_DATASOURCE_URL=jdbc:postgresql://operating.example.com/db",
                                  "SPRING_APPLICATION_JSON={}", "JDK_JAVA_OPTIONS=-Dspring.profiles.active=prod",
                                  "JAVA_TOOL_OPTIONS=-javaagent:evil", "LD_PRELOAD=/evil", "PYTHONPATH=/evil",
                                  "PICKLE_DNS_PROVIDER=noop"):
                    env.write_text(safe + dangerous + "\n")
                    with self.assertRaisesRegex(deploy.Refusal, "env or endpoint"):
                        deploy.check_env_rules(entry)

    def test_failed_api_with_fix_forward_does_not_restart_previous_artifact(self):
        entry = {"ctid": 901, "unit": "api-dev.service", "active_path": "/api/current-dev"}
        result = type("Result", (), {"returncode": 1})()
        with patch.object(deploy, "guest", return_value=result) as calls, patch.object(deploy, "select_link") as links:
            with self.assertRaisesRegex(deploy.Refusal, "fix-forward"):
                deploy.activate_binary(entry, "api", "/releases/new", None, "/releases/old", None, "fix-forward")
        self.assertEqual(links.call_count, 1)
        self.assertEqual(calls.call_count, 1)
        with self.assertRaisesRegex(deploy.Refusal, "explicit rollback"):
            deploy.api_rollback_policy({}, None)
        with self.assertRaisesRegex(deploy.Refusal, "differs"):
            deploy.api_rollback_policy({"api_rollback_policy": "fix-forward"}, "compatible")

    def test_production_apply_rejects_flag_and_free_form_backup_without_guest_commands(self):
        arguments = ["deploy", "apply", "prod", "api", "a" * 40, "/root/pickle-deploy-staging/artifact.jar", "b" * 64,
                     "--backup-point", "claimed-PBS-point", "--rollback-policy", "fix-forward"]
        with patch.object(sys, "argv", arguments), patch.object(deploy.os, "geteuid", return_value=0), \
             patch.dict(os.environ, {"PICKLE_PRODUCTION_RELEASE_APPROVED": "1"}), patch.object(deploy, "load_config", return_value={}), patch.object(deploy, "run") as calls:
            with self.assertRaisesRegex(deploy.Refusal, "approval"):
                deploy.main()
        calls.assert_not_called()

    def test_running_process_must_hold_selected_jar_or_executable(self):
        entry = {"ctid": 901, "unit": "api-dev.service", "active_path": "/api/current-dev"}
        result = type("Result", (), {"returncode": 1})()
        with patch.object(deploy, "guest_property", return_value="123"), patch.object(deploy, "guest", return_value=result):
            with self.assertRaisesRegex(deploy.Refusal, "running process"):
                deploy.verify_running_artifact(entry, "api", "a" * 64)
            with self.assertRaisesRegex(deploy.Refusal, "running process"):
                deploy.verify_running_artifact(entry, "llm-gateway", "a" * 64)
        with patch.object(deploy, "guest_property", return_value="0"):
            with self.assertRaisesRegex(deploy.Refusal, "no running process"):
                deploy.verify_running_artifact(entry, "api", "a" * 64)

    def test_archive_rejects_duplicates_and_gateway_aliases(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "bundle.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name in ("index.html", "./index.html"):
                    archive.addfile(tarfile.TarInfo(name))
            with self.assertRaisesRegex(deploy.Refusal, "duplicate"):
                deploy.validate_archive(archive_path, "console")
            with tarfile.open(archive_path, "w:gz") as archive:
                for name in ("./llm-gateway", "llm-keygen"):
                    archive.addfile(tarfile.TarInfo(name))
            with self.assertRaisesRegex(deploy.Refusal, "names must be exact"):
                deploy.validate_archive(archive_path, "llm-gateway")

    def test_loopback_url_prefix_cannot_hide_a_remote_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / "env"
            entry = {"ctid": 901, "env_file": str(env), "required_env": {"PICKLE_DNS_PROVIDER": "noop"},
                     "prefix_env": {"PICKLE_OPENROUTER_URL": "http://127.0.0.1:"}}
            def execute_probe(_ctid, *argv, **kwargs):
                return subprocess.run(argv, capture_output=True, text=True)
            env.write_text("PICKLE_DNS_PROVIDER=noop\nPICKLE_OPENROUTER_URL=http://127.0.0.1:80@operating.example.com\n")
            with patch.object(deploy, "guest", side_effect=execute_probe):
                with self.assertRaisesRegex(deploy.Refusal, "env or endpoint"):
                    deploy.check_env_rules(entry)

    def test_console_archive_rejects_privileged_metadata_and_repeated_dot_aliases(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "bundle.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                member = tarfile.TarInfo("index.html")
                member.mode = 0o4755
                archive.addfile(member)
            with self.assertRaisesRegex(deploy.Refusal, "privileged"):
                deploy.validate_archive(archive_path, "console")
            with tarfile.open(archive_path, "w:gz") as archive:
                for name in ("index.html", "././index.html"):
                    archive.addfile(tarfile.TarInfo(name))
            with self.assertRaisesRegex(deploy.Refusal, "duplicate"):
                deploy.validate_archive(archive_path, "console")

    def test_api_wrapper_keeps_open_jar_and_drops_generic_overrides(self):
        wrapper_spec = importlib.util.spec_from_file_location("api_start", SCRIPT.parent / "pickle-api-start.py")
        wrapper = importlib.util.module_from_spec(wrapper_spec)
        wrapper_spec.loader.exec_module(wrapper)
        with tempfile.TemporaryDirectory() as folder:
            jar = Path(folder) / "releases" / "api.jar"
            jar.parent.mkdir()
            jar.write_bytes(b"selected immutable JAR")
            # Isolate descriptor manipulation from the harness process.
            isolated = """import importlib.util,os,sys,types
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('wrapper',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
jar=Path(sys.argv[2])
def execute(path,argv,env):
    assert os.read(9,4096)==b'selected immutable JAR'
    assert os.get_inheritable(9)
    assert '--spring.config.location=classpath:/application.yml' in argv
    assert '--server.address=127.0.0.1' in argv
    assert '-Xmx1g' in argv
    assert env['PICKLE_DEV_DB_HOST']=='192.0.2.60'
    assert env['PICKLE_DEV_JOBRUNR_ENABLED']=='true'
    assert env['PICKLE_DEV_REAL_INTEGRATIONS_ENABLED']=='true'
    assert argv[argv.index('-jar')+1]=='/proc/self/fd/9'
    assert env['PICKLE_DB_PASSWORD']=='development-password'
    assert not any(key in env for key in ('JAVA_TOOL_OPTIONS','SPRING_APPLICATION_JSON','LD_PRELOAD','PICKLE_LLM_OPENROUTER_BASE_URL'))
with patch.object(sys,'argv',['wrapper','/opt/pickle/api/current-dev.jar','dev']), patch.object(m.Path,'resolve',return_value=jar), patch.object(m.Path,'parent',new_callable=lambda:property(lambda self:Path('/opt/pickle/api/releases'))), patch.object(m,'require_protected_directories'), patch.object(m.os,'fstat',return_value=types.SimpleNamespace(st_uid=0,st_mode=0o100640)), patch.object(m.os,'execve',side_effect=execute), patch.dict(os.environ,{'PICKLE_DEV_DB_HOST':'192.0.2.60','PICKLE_DEV_JOBRUNR_ENABLED':'true','PICKLE_DEV_REAL_INTEGRATIONS_ENABLED':'true','PICKLE_DB_PASSWORD':'development-password','JAVA_TOOL_OPTIONS':'evil','SPRING_APPLICATION_JSON':'{}','LD_PRELOAD':'evil','PICKLE_LLM_OPENROUTER_BASE_URL':'https://operating.example.com'}):
    m.main()
os.close(9)
"""
            subprocess.run([sys.executable, "-c", isolated, str(SCRIPT.parent / "pickle-api-start.py"), str(jar)], check=True)

    def test_api_wrapper_refuses_writable_or_nonroot_artifact_ancestors(self):
        from types import SimpleNamespace
        wrapper_spec = importlib.util.spec_from_file_location("api_start", SCRIPT.parent / "pickle-api-start.py")
        wrapper = importlib.util.module_from_spec(wrapper_spec)
        wrapper_spec.loader.exec_module(wrapper)
        for metadata in (SimpleNamespace(st_uid=1001, st_mode=0o40755),
                         SimpleNamespace(st_uid=0, st_mode=0o40775),
                         SimpleNamespace(st_uid=0, st_mode=0o120777)):
            with patch.object(wrapper.Path, "lstat", return_value=metadata):
                with self.assertRaisesRegex(RuntimeError, "ancestor directories"):
                    wrapper.require_protected_directories(Path("/opt/pickle/api/releases"))

    def test_guest_artifact_directory_probe_refuses_user_owned_ancestors(self):
        with tempfile.TemporaryDirectory() as folder:
            entry = {"ctid": 901, "active_path": str(Path(folder) / "current.jar"), "release_dir": folder}
            def execute_probe(_ctid, *argv, **kwargs):
                return subprocess.run(argv, capture_output=True, text=True)
            with patch.object(deploy, "guest", side_effect=execute_probe):
                with self.assertRaisesRegex(deploy.Refusal, "ancestor directories"):
                    deploy.check_artifact_directories(entry)

    def test_wrapper_preserves_production_heap_and_listen_address(self):
        wrapper_spec = importlib.util.spec_from_file_location("api_start", SCRIPT.parent / "pickle-api-start.py")
        wrapper = importlib.util.module_from_spec(wrapper_spec)
        wrapper_spec.loader.exec_module(wrapper)
        from types import SimpleNamespace
        with patch.object(sys, "argv", ["wrapper", "/opt/pickle/api/production-current.jar", "prod"]), \
             patch.object(wrapper, "require_protected_directories"), \
             patch.object(wrapper.Path, "resolve", return_value=Path("/opt/pickle/api/releases/production.jar")), \
             patch.object(wrapper.os, "open", return_value=8), \
             patch.object(wrapper.os, "fstat", return_value=SimpleNamespace(st_uid=0, st_mode=0o100640)), \
             patch.object(wrapper.os, "dup2"), patch.object(wrapper.os, "close"), patch.object(wrapper.os, "execve") as execute:
            wrapper.main()
        argv = execute.call_args.args[1]
        self.assertIn("-Xmx2g", argv)
        self.assertIn("--server.address=0.0.0.0", argv)
        self.assertIn("--jobrunr.background-job-server.enabled=true", argv)
        self.assertIn("--pickle.network-policy.enabled=true", argv)
        self.assertIn("--pickle.vm-firewall.enabled=true", argv)
        self.assertIn("--pickle.dns.provider=google", argv)

    def test_nonempty_secret_rule_refuses_missing_empty_and_whitespace_without_printing_values(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / "env"
            entry = {"ctid": 901, "env_file": str(env), "required_env": {"PICKLE_DNS_PROVIDER": "noop"},
                     "prefix_env": {}, "required_nonempty_env": ["PICKLE_PROXMOX_TOKEN_SECRET"]}
            def execute_probe(_ctid, *argv, **kwargs):
                return subprocess.run(argv, capture_output=True, text=True)
            with patch.object(deploy, "guest", side_effect=execute_probe):
                for value in ("", "PICKLE_PROXMOX_TOKEN_SECRET=\n", "PICKLE_PROXMOX_TOKEN_SECRET='   '\n"):
                    env.write_text("PICKLE_DNS_PROVIDER=noop\n" + value)
                    with self.assertRaisesRegex(deploy.Refusal, "env or endpoint"):
                        deploy.check_env_rules(entry)
                env.write_text("PICKLE_DNS_PROVIDER=noop\nPICKLE_PROXMOX_TOKEN_SECRET=private-development-value\n")
                deploy.check_env_rules(entry)

    def test_production_approval_window_must_bind_commit_artifact_and_recovery_scope(self):
        from types import SimpleNamespace
        arguments = SimpleNamespace(target="api", commit="a" * 40, sha256="b" * 64)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "approval.json"
            config = {"host": "example-prod-host", "production_release_approval_file": str(path),
                      "backup_verification": {"recovery_scope": "independent file recovery"}}
            approval = {"environment": "prod", "host": "example-prod-host", "target": "api",
                        "commit": "a" * 40, "artifact_sha256": "b" * 64, "approved_by": "operator",
                        "authorization_reference": "explicit approved release request", "approved_at": int(time.time())-1,
                        "expires_at": int(time.time())+60, "accepted_recovery_scope": "independent file recovery"}
            path.write_text(json.dumps(approval))
            deploy.check_release_approval(config, arguments)
            for field, value in (("commit", "c"*40), ("artifact_sha256", "d"*64),
                                 ("expires_at", int(time.time())-1), ("accepted_recovery_scope", "unverified")):
                wrong = dict(approval, **{field: value})
                path.write_text(json.dumps(wrong))
                with self.assertRaisesRegex(deploy.Refusal, "approval window"):
                    deploy.check_release_approval(config, arguments)

    def test_production_backup_result_requires_live_challenge_fresh_point_and_db_identity(self):
        now = int(time.time())
        rules = {"command": ["reviewed-reader"], "reader_hostname": "example-reader", "database": "pickle_prod",
                 "instance_id": "example-instance", "expected_system_identifier": "123456789",
                 "expected_schema_version": 137, "recovery_scope": "independent file recovery",
                 "verifier_sha256": "e" * 64, "maximum_point_age_seconds": 600, "maximum_verification_age_seconds": 120}
        config = {"backup_verification": rules, "chat_db": {"ctid": 914, "name": "pickle_prod"}}
        report = {"status": "VERIFIED", "reader_hostname": "example-reader", "database": "pickle_prod",
                  "instance_id": "example-instance", "system_identifier": "123456789", "schema": 137,
                  "failed_migrations": 0, "scope": "independent file recovery", "verifier_sha256": "e" * 64,
                  "payload_sha256": {"database.dump": "a"*64, "roles.sql": "b"*64, "manifest.json": "c"*64},
                  "recovery_point_epoch": now-20, "verified_at": now-1}
        source = {"database": "pickle_prod", "system_identifier": "123456789", "schema": 137,
                  "failed_migrations": 0, "recovery": False}
        def reader(*argv):
            return type("Result", (), {"stdout": json.dumps(dict(report, challenge=argv[-1]))})()
        with patch.object(deploy, "run", side_effect=reader), patch.object(deploy, "check_ct"), \
             patch.object(deploy, "guest", return_value=type("Result", (), {"stdout": json.dumps(source)})()):
            deploy.check_production_backup(config)
            for field, value in (("recovery_point_epoch", now-601), ("system_identifier", "999"),
                                 ("scope", "unverified"), ("verifier_sha256", "0"*64)):
                original = report[field]
                report[field] = value
                with self.assertRaises(deploy.Refusal):
                    deploy.check_production_backup(config)
                report[field] = original

    def test_real_development_scope_refuses_unbounded_sequence_or_wrong_template(self):
        scope = {"database_ctid": 902, "database_name": "pickle_dev", "node_name": "example-dev-host",
                 "node_api_host": "https://example-dev-host:8006", "vm_bridge": "vmbr-dev", "storage": "dev-storage",
                 "template_vmid": 9901, "vmid_minimum": 900000, "vmid_maximum": 900009,
                 "pool_name": "dev-guest", "pool_cidr": "198.18.10.0/24", "pool_gateway": "198.18.10.1", "dev_environment_marker": True,
                 "reserved_ranges": [{"from": "198.18.10.0", "to": "198.18.10.99"}]}
        config = {"development_mode": "real", "development_scope": scope, "chat_db": {"ctid": 902, "name": "pickle_dev"}}
        observed = {"nodes": [{"name": "example-dev-host", "api_host": "https://example-dev-host:8006",
                               "vm_bridge": "vmbr-dev", "storage": "dev-storage", "pool_name": "dev-guest"}],
                    "pools": [{"name": "dev-guest", "cidr": "198.18.10.0/24", "gateway": "198.18.10.1",
                               "reserved_ranges": scope["reserved_ranges"]}],
                    "images": [{"template_vmid": 9901, "node_name": "example-dev-host"}],
                    "sequence": {"minimum": 900000, "maximum": 900009, "cycle": False}, "marker": True}
        def query(*args):
            return type("Result", (), {"stdout": json.dumps(observed)})()
        with patch.object(deploy, "check_ct"), patch.object(deploy, "guest", side_effect=query):
            deploy.check_development_scope(config)
            observed["marker"] = False
            with self.assertRaisesRegex(deploy.Refusal, "inventory"):
                deploy.check_development_scope(config)
            observed["marker"] = True
            observed["sequence"]["maximum"] = 999999
            with self.assertRaisesRegex(deploy.Refusal, "bounded VMID"):
                deploy.check_development_scope(config)
            observed["sequence"]["maximum"] = 900009
            observed["nodes"][0]["pool_name"] = "wrong-pool"
            with self.assertRaisesRegex(deploy.Refusal, "inventory"):
                deploy.check_development_scope(config)
            observed["nodes"][0]["pool_name"] = "dev-guest"
            observed["images"][0]["template_vmid"] = 9001
            with self.assertRaisesRegex(deploy.Refusal, "inventory"):
                deploy.check_development_scope(config)

    def test_production_api_db_url_requires_exact_database_and_host_with_tls(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / "production.env"
            entry = {"ctid": 911, "unit": "example-api.service", "active_path": "/api/current.jar",
                     "startup_wrapper": "/wrapper", "startup_wrapper_sha256": "a"*64, "env_file": str(env),
                     "db_url_prefix": "jdbc:postgresql://198.19.0.21:5432/pickle_prod"}
            values = {"LoadState": "loaded", "ExecStart": "{ argv[]=/wrapper /api/current.jar prod }",
                      "EnvironmentFiles": str(env)+" (ignore_errors=no)", "Environment": "", "ActiveState": "active"}
            def guest(_ctid, *argv, **kwargs):
                if argv[0] == "sha256sum":
                    return type("Result", (), {"stdout": "a"*64+"  wrapper", "returncode": 0})()
                if argv[:2] == ("python3", "-c") and "rows=" in argv[2]:
                    return subprocess.run(argv, capture_output=True, text=True)
                return type("Result", (), {"stdout": "", "returncode": 0})()
            correct = "jdbc:postgresql://198.19.0.21:5432/pickle_prod?sslmode=verify-full&sslrootcert=/etc/pickle/example-db-ca.crt"
            with patch.object(deploy, "guest_property", side_effect=lambda c,u,p: values[p]), \
                 patch.object(deploy, "guest", side_effect=guest):
                env.write_text("PICKLE_DB_URL="+correct+"\n")
                self.assertTrue(deploy.check_unit(entry, "api", "prod", "apply"))
                for quote in (chr(39), chr(34)):
                    env.write_text("PICKLE_DB_URL="+quote+correct+quote+"\n")
                    self.assertTrue(deploy.check_unit(entry, "api", "prod", "apply"))
                for url in (chr(39)+correct, chr(34)+correct+chr(39), correct+chr(39), correct.replace("/pickle_prod?", "/pickle_prod_other?"),
                            correct.replace("198.19.0.21", "198.19.0.22"),
                            correct.replace("5432", "5433"),
                            correct.replace("verify-full", "disable"),
                            correct+"&currentSchema=other"):
                    env.write_text("PICKLE_DB_URL="+url+"\n")
                    with self.assertRaisesRegex(deploy.Refusal, "selected database URL"):
                        deploy.check_unit(entry, "api", "prod", "apply")

    def test_inactive_production_candidate_cannot_skip_source_or_artifact_verification(self):
        from types import SimpleNamespace
        arguments = SimpleNamespace(target="api", commit="a"*40, sha256="b"*64)
        with self.assertRaisesRegex(deploy.Refusal, "fully verified"):
            deploy.check_release_approval({"release_candidate_ready": False}, arguments)
        candidate = {"expected_artifact_sha256": {"api": "REPLACE", "console": "c"*64, "llm-gateway": "d"*64},
                     "allowed_commits": {"api": "a"*40, "console": "c"*40, "llm-gateway": "REPLACE"}}
        with self.assertRaisesRegex(deploy.Refusal, "source or artifact"):
            deploy.check_release_approval(candidate, arguments)

    def test_target_config_refuses_aggregate_api_health_and_preserved_source_ct(self):
        from types import SimpleNamespace
        config = {"environment": "dev", "host": "example-dev-host", "forbidden_ctids": [904, 905],
                  "chat_db": {"ctid": 902, "hostname": "example-db-dev", "name": "pickle_dev"},
                  "targets": {target: {"ctid": 901, "hostname": "example-app-dev"} for target in deploy.TARGETS}}
        config["targets"]["api"].update(health_url="http://127.0.0.1:8080/actuator/health/readiness",
                                         db_url_prefix="jdbc:postgresql://198.18.0.60:5432/pickle_dev")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dev.json"
            with patch.object(deploy.Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o100600)), \
                 patch.object(deploy.socket, "gethostname", return_value="example-dev-host"), patch.object(deploy, "run") as commands:
                path.write_text(json.dumps(config))
                deploy.load_config("dev", path)
                config["targets"]["api"]["health_url"] = "http://127.0.0.1:8080/actuator/health"
                path.write_text(json.dumps(config))
                with self.assertRaisesRegex(deploy.Refusal, "readiness"):
                    deploy.load_config("dev", path)
                config["targets"]["api"]["health_url"] += "/readiness"
                config["targets"]["api"]["ctid"] = 904
                path.write_text(json.dumps(config))
                with self.assertRaisesRegex(deploy.Refusal, "preserved source CT"):
                    deploy.load_config("dev", path)
                commands.assert_not_called()

    def test_api_health_probe_uses_local_readiness_and_fails_on_nonready_response(self):
        entry = {"ctid": 901, "health_url": "http://127.0.0.1:8080/actuator/health/readiness"}
        with patch.object(deploy, "guest", return_value=type("Result", (), {"returncode": 0, "stdout": "503"})()) as commands:
            self.assertFalse(deploy.verify_local_health(entry, "api"))
        self.assertEqual(commands.call_count, 1)
        self.assertEqual(commands.call_args.args[-1], entry["health_url"])

    def test_development_digest_pin_rejects_relabelled_and_unapproved_artifacts(self):
        config = {"environment": "dev", "allowed_commits": {"api": "a"*40},
                  "approved_artifacts": {"api": {"a"*40: {"sha256": "b"*64}}}}
        deploy.check_manifest(config, "api", "a"*40, "b"*64)
        for digest in ("0"*64, None):
            with self.assertRaisesRegex(deploy.Refusal, "digest pin"):
                deploy.check_manifest(config, "api", "a"*40, digest)
        config["approved_artifacts"]["api"] = {}
        with self.assertRaisesRegex(deploy.Refusal, "digest pin"):
            deploy.check_manifest(config, "api", "a"*40, "b"*64)

    def test_development_wrong_digest_stops_check_and_apply_before_guest_access(self):
        config = {"environment": "dev", "allowed_commits": {"api": "a"*40},
                  "approved_artifacts": {"api": {"a"*40: {"sha256": "b"*64}}}}
        for action in ("check", "apply"):
            arguments = ["deploy", action, "dev", "api", "a"*40, "/staging/other.jar", "0"*64]
            with patch.object(sys, "argv", arguments), patch.object(deploy.os, "geteuid", return_value=0), \
                 patch.object(deploy, "load_config", return_value=config), patch.object(deploy, "check_ct") as guest, \
                 patch.object(deploy, "artifact") as artifact:
                with self.assertRaisesRegex(deploy.Refusal, "digest pin"):
                    deploy.main()
                guest.assert_not_called()
                artifact.assert_not_called()

    def test_gateway_apply_proof_refusal_occurs_before_binary_install_or_restart(self):
        from unittest.mock import MagicMock
        arguments = ["deploy", "apply", "dev", "llm-gateway", "a"*40, "/root/pickle-deploy-staging/bundle.tar.gz", "b"*64]
        entry = {"ctid": 901}
        config = {"allowed_commits": {"llm-gateway": "a"*40}, "targets": {"llm-gateway": entry}}
        lock = MagicMock()
        lock.__enter__.return_value = lock
        lock.fileno.return_value = 42
        with tempfile.TemporaryDirectory() as folder, patch.object(deploy, "RECEIPTS", Path(folder)), \
             patch.object(sys, "argv", arguments), patch.object(deploy.os, "geteuid", return_value=0), \
             patch.object(deploy, "load_config", return_value=config), patch.object(deploy, "check_ct"), \
             patch.object(deploy, "check_unit", return_value=True), patch.object(deploy, "active_link", return_value="previous"), \
             patch.object(deploy, "keygen_link", return_value="previous-keygen"), \
             patch.object(deploy, "artifact", return_value=Path("bundle.tar.gz")), patch.object(deploy, "validate_archive"), \
             patch.object(deploy.Path, "open", return_value=lock), patch.object(deploy.fcntl, "flock"), \
             patch.object(deploy, "install_binary") as install, patch.object(deploy, "activate_binary") as activate:
            with self.assertRaisesRegex(deploy.Refusal, "api deployment receipt is missing"):
                deploy.main()
        install.assert_not_called()
        activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
