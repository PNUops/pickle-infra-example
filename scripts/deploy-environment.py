#!/usr/bin/env python3
"""Deploy a pinned, prebuilt artifact to one explicitly configured environment.

The target host owns /etc/pickle/deploy-targets/{dev,prod}.json. This tool never
builds from a mutable checkout, changes ingress, or chooses a different LXC.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse


COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
STAGING = Path("/root/pickle-deploy-staging")
CONFIG_DIR = Path("/etc/pickle/deploy-targets")
RECEIPTS = Path("/var/lib/pickle-deploy")
TARGETS = ("api", "console", "llm-gateway")


class Refusal(Exception):
    """A deployment precondition was not met."""


def run(*argv, capture=True, check=True):
    result = subprocess.run(argv, text=True, capture_output=capture, check=False)
    if check and result.returncode:
        raise Refusal(f"command failed ({argv[0]}, exit {result.returncode})")
    return result


def guest(ctid, *argv, check=True):
    return run("pct", "exec", str(ctid), "--", *argv, check=check)


def guest_shell(ctid, script, *args, check=True):
    return guest(ctid, "bash", "-c", script, "deploy", *map(str, args), check=check)


def load_config(environment, path=None):
    selected = Path(path) if path is not None else CONFIG_DIR / f"{environment}.json"
    if selected.is_symlink() or not selected.is_file():
        raise Refusal("target configuration is missing or a symlink")
    stat = selected.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022:
        raise Refusal("target configuration must be root-owned and not writable by others")
    config = json.loads(selected.read_text())
    if config.get("environment") != environment:
        raise Refusal("target configuration environment mismatch")
    if config.get("host") != socket.gethostname().split(".")[0]:
        raise Refusal("target configuration host mismatch")
    for target in TARGETS:
        entry = config.get("targets", {}).get(target)
        if not isinstance(entry, dict):
            raise Refusal(f"target configuration lacks {target}")
        if type(entry.get("ctid")) is not int or not 100 <= entry["ctid"] <= 999999:
            raise Refusal(f"invalid {target} CTID")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", entry.get("hostname", "")):
            raise Refusal(f"invalid {target} hostname")
    readiness = urllib.parse.urlsplit(config["targets"]["api"].get("health_url", ""))
    if (readiness.scheme != "http" or readiness.hostname != "127.0.0.1" or readiness.port != 8080
            or readiness.path != "/actuator/health/readiness" or readiness.query or readiness.fragment):
        raise Refusal("API health target must use its local actuator readiness endpoint")
    forbidden = config.get("forbidden_ctids", [])
    if (not isinstance(forbidden, list) or (environment == "dev" and not forbidden) or any(type(value) is not int for value in forbidden)
            or any(entry["ctid"] in forbidden for entry in config["targets"].values())
            or config.get("chat_db", {}).get("ctid") in forbidden):
        raise Refusal("target configuration selects a preserved source CT")
    api_url = config["targets"]["api"].get("db_url_prefix", "")
    database = config.get("chat_db", {}).get("name", "")
    if (not isinstance(api_url, str) or not api_url.startswith("jdbc:postgresql://")
            or not re.fullmatch(r"[a-z][a-z0-9_]*", database)
            or urllib.parse.urlsplit(api_url.removeprefix("jdbc:")).path != "/" + database):
        raise Refusal("API database selection differs from the backup and Chat DB target")
    return config


def check_manifest(config, target, commit, expected_sha=None):
    allowed = config.get("allowed_commits", {}).get(target)
    if not isinstance(allowed, str) or not COMMIT.fullmatch(allowed):
        raise Refusal("target has no pinned compatible commit")
    if commit != allowed:
        raise Refusal("commit differs from the selected environment manifest")
    if config.get("environment") == "dev":
        pin = config.get("approved_artifacts", {}).get(target, {}).get(commit)
        if (not isinstance(pin, dict) or not isinstance(pin.get("sha256"), str)
                or not SHA256.fullmatch(pin["sha256"]) or expected_sha != pin["sha256"]):
            raise Refusal("artifact differs from the protected development digest pin")


def receipt_path(environment, target):
    return RECEIPTS / environment / f"{target}.json"


def write_receipt(environment, target, commit, entry):
    parent = RECEIPTS / environment
    if parent.is_symlink():
        raise Refusal("deployment receipt directory is a symlink")
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    output = receipt_path(environment, target)
    temporary = parent / f".{target}-{os.getpid()}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    receipt = {"environment": environment, "target": target,
               "commit": commit, "completed_at": int(time.time())}
    if target == "console":
        index = f"{entry['webroot']}/index.html"
        receipt["index_sha256"] = guest(entry["ctid"], "sha256sum", index).stdout.split()[0]
    if target in ("api", "llm-gateway"):
        selected = active_link(entry, environment)
        receipt["artifact_sha256"] = guest(entry["ctid"], "sha256sum", selected).stdout.split()[0]
        verify_running_artifact(entry, target, receipt["artifact_sha256"])
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(receipt, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def check_chat_order(config, environment):
    receipts = {}
    for target in ("api", "console"):
        path = receipt_path(environment, target)
        if path.is_symlink() or not path.is_file():
            raise Refusal(f"{target} deployment receipt is missing")
        metadata = path.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise Refusal(f"{target} deployment receipt is not protected")
        receipt = json.loads(path.read_text())
        if (receipt.get("environment") != environment or receipt.get("target") != target
                or receipt.get("commit") != config["allowed_commits"][target]):
            raise Refusal(f"{target} deployed commit differs from compatible manifest")
        receipts[target] = receipt
    api = config["targets"]["api"]
    console = config["targets"]["console"]
    check_ct(api)
    check_ct(console)
    if not check_unit(api, "api", environment, "check"):
        raise Refusal("API still selects the old fixed artifact")
    selected = active_link(api, environment)
    if selected != f"{api['release_dir']}/pickle-api-{config['allowed_commits']['api']}.jar":
        raise Refusal("running API selection differs from compatible manifest")
    index_sha = guest(console["ctid"], "sha256sum",
                      f"{console['webroot']}/index.html").stdout.split()[0]
    if index_sha != receipts["console"].get("index_sha256"):
        raise Refusal("console webroot differs from its deployment receipt")
    database = config.get("chat_db", {})
    if not re.fullmatch(r"[a-z][a-z0-9_]*", database.get("name", "")):
        raise Refusal("Chat check has no safe database name")
    check_ct(database)
    verify_running_artifact(api, "api", receipts["api"].get("artifact_sha256", ""))
    sql = """select (select count(*) from flyway_schema_history
                          where version = '139' and success),
                      generation, applied_generation from llm_gateway_state"""
    result = guest(database["ctid"], "runuser", "-u", "postgres", "--", "psql",
                   "-d", database["name"], "-Atq", "-F", "|", "-c", sql).stdout.strip()
    rows = result.splitlines()
    if len(rows) != 1 or not re.fullmatch(r"[0-9]+\|[0-9]+\|[0-9]+", rows[0]):
        raise Refusal("Chat grant or gateway generation readback is incomplete")
    migrated, generation, applied = map(int, rows[0].split("|"))
    if migrated != 1 or generation < 1 or generation != applied:
        raise Refusal("Chat migration or applied gateway generation is not ready")
    # This result is written only after an independent snapshot/DB compatibility
    # check. It includes the migrated-key baseline and deliberately Chat-free
    # new keys; a blanket requirement to give every key Chat would change policy.
    result_path = config.get("chat_compatibility_file")
    if not result_path:
        raise Refusal("protected Chat compatibility result is missing")
    proof = protected_json(result_path)
    if (proof.get("environment") != environment
            or proof.get("commits") != config["allowed_commits"]
            or proof.get("api_sha256") != receipts["api"].get("artifact_sha256")
            or proof.get("console_index_sha256") != index_sha
            or proof.get("generation") != generation
            or proof.get("existing_key_grants_preserved") is not True
            or proof.get("snapshot_grants_match_database") is not True
            or proof.get("chat_free_keys_verified") is not True):
        raise Refusal("Chat compatibility result differs from running release or grants")
    checked = proof.get("checked_at")
    if type(checked) is not int or not 0 <= time.time() - checked <= 900:
        raise Refusal("Chat compatibility result is stale")


def protected_json(path):
    selected = Path(path)
    if selected.is_symlink() or not selected.is_file():
        raise Refusal("protected compatibility result is missing or a symlink")
    metadata = selected.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise Refusal("compatibility result is not protected")
    return json.loads(selected.read_text())


def api_rollback_policy(config, requested):
    policy = config.get("api_rollback_policy")
    if policy not in ("compatible", "fix-forward"):
        raise Refusal("API manifest lacks an explicit rollback policy")
    if requested and requested != policy:
        raise Refusal("API rollback policy differs from the reviewed release manifest")
    return policy


def verify_running_artifact(entry, target, expected_sha):
    if not SHA256.fullmatch(expected_sha):
        raise Refusal("running artifact has no recorded SHA-256")
    pid = guest_property(entry["ctid"], entry["unit"], "MainPID")
    if not pid.isdecimal() or int(pid) < 1:
        raise Refusal("selected unit has no running process")
    probe = """import hashlib,os,pathlib,sys
pid,target,selected,want=sys.argv[1:]
root=pathlib.Path('/proc')/pid
before=(root/'stat').read_text().rsplit(')',1)[1].split()[19]
cmd=(root/'cmdline').read_bytes().split(bytes([0]))
if target=='api':
    jar=b'/proc/self/fd/9'
    if b'-jar' not in cmd or cmd[cmd.index(b'-jar')+1]!=jar: sys.exit(1)
    if not os.path.samefile(root/'exe','/usr/bin/java'): sys.exit(1)
    running=root/'fd/9'
else:
    running=root/'exe'
a=os.stat(running);b=os.stat(selected)
if (a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino) or b.st_uid!=0 or b.st_mode & 0o022: sys.exit(1)
with open(running,'rb') as f:
    if hashlib.file_digest(f,'sha256').hexdigest()!=want: sys.exit(1)
after=(root/'stat').read_text().rsplit(')',1)[1].split()[19]
sys.exit(0 if before==after else 1)
"""
    if guest(entry["ctid"], "python3", "-c", probe, pid, target,
             entry["active_path"], expected_sha, check=False).returncode:
        raise Refusal("running process does not hold the selected immutable artifact")


def check_release_approval(config, args):
    path = config.get("production_release_approval_file")
    if config.get("release_candidate_ready") is False:
        raise Refusal("production candidate has no fully verified source and artifact combination")
    expected = config.get("expected_artifact_sha256")
    if expected is not None:
        if (not isinstance(expected, dict) or set(expected) != set(TARGETS)
                or any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in expected.values())
                or any(not isinstance(config.get("allowed_commits", {}).get(target), str)
                       or not COMMIT.fullmatch(config["allowed_commits"][target]) for target in TARGETS)
                or expected[args.target] != args.sha256):
            raise Refusal("production release candidate source or artifact digests are not fully verified")
    if not path:
        raise Refusal("production release has no protected approval window")
    approval = protected_json(path)
    now = int(time.time())
    opened, expires = approval.get("approved_at"), approval.get("expires_at")
    if (approval.get("environment") != "prod" or approval.get("host") != config["host"]
            or approval.get("target") != args.target or approval.get("commit") != args.commit
            or approval.get("artifact_sha256") != args.sha256
            or approval.get("approved_by") != "operator"
            or not isinstance(approval.get("authorization_reference"), str)
            or not approval["authorization_reference"].strip()
            or type(opened) is not int or type(expires) is not int
            or not opened <= now < expires or expires - opened > 900
            or approval.get("accepted_recovery_scope") != config.get("backup_verification", {}).get("recovery_scope")):
        raise Refusal("production approval window is missing, expired or not bound to this artifact and recovery scope")
    return approval


def check_production_backup(config):
    rules = config.get("backup_verification", {})
    command = rules.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise Refusal("production has no reviewed independent backup verifier command")
    challenge = secrets.token_hex(32)
    report = json.loads(run(*command, "--challenge", challenge).stdout)
    if (report.get("status") != "VERIFIED" or report.get("challenge") != challenge
            or report.get("reader_hostname") != rules.get("reader_hostname")
            or report.get("database") != rules.get("database")
            or report.get("instance_id") != rules.get("instance_id")
            or report.get("system_identifier") != rules.get("expected_system_identifier")
            or report.get("schema") != rules.get("expected_schema_version")
            or report.get("failed_migrations") != 0
            or report.get("scope") != rules.get("recovery_scope")
            or report.get("verifier_sha256") != rules.get("verifier_sha256")
            or not isinstance(report.get("payload_sha256"), dict)
            or set(report["payload_sha256"]) != {"database.dump", "roles.sql", "manifest.json"}
            or any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in report["payload_sha256"].values())):
        raise Refusal("independent backup verification does not identify this production DB and recovery path")
    now = time.time()
    epoch, verified = report.get("recovery_point_epoch"), report.get("verified_at")
    if (type(epoch) is not int or type(verified) is not int
            or not 0 <= now - epoch <= rules.get("maximum_point_age_seconds", 0)
            or not epoch <= verified <= now
            or now - verified > rules.get("maximum_verification_age_seconds", 0)):
        raise Refusal("independent backup recovery point or verification is stale")
    database = config["chat_db"]
    check_ct(database)
    sql = "select json_build_object('database', current_database(), 'system_identifier', (select system_identifier::text from pg_control_system()), 'schema', (select max(version::int) from flyway_schema_history where success), 'failed_migrations', (select count(*) from flyway_schema_history where not success), 'recovery', pg_is_in_recovery())"
    source = json.loads(guest(database["ctid"], "runuser", "-u", "postgres", "--", "psql", "-XqAt", "-h", "/var/run/postgresql", "-p", "5432", "-U", "postgres", "-w", "-v", "ON_ERROR_STOP=1", "-d", database["name"], "-c", sql).stdout)
    if (source.get("database") != report["database"]
            or source.get("system_identifier") != report["system_identifier"]
            or source.get("schema") != report["schema"]
            or source.get("failed_migrations") != 0 or source.get("recovery") is not False):
        raise Refusal("current production primary differs from the recovered PBS dump")
    return report


def check_development_scope(config):
    if config.get("development_mode") != "real":
        return
    scope = config.get("development_scope", {})
    database = config["chat_db"]
    if scope.get("database_ctid") != database["ctid"] or scope.get("database_name") != database["name"]:
        raise Refusal("real development scope differs from its dedicated DB")
    check_ct(database)
    sql = "select json_build_object('nodes', (select json_agg(json_build_object('name',name,'api_host',api_host,'vm_bridge',vm_bridge,'storage',storage,'pool_name',(select name from ip_pools where id=nodes.ip_pool_id))) from nodes), 'pools', (select json_agg(json_build_object('name',name,'cidr',cidr::text,'gateway',host(gateway),'reserved_ranges',reserved_ranges)) from ip_pools), 'images', (select json_agg(json_build_object('template_vmid',proxmox_vmid,'node_name',(select name from nodes where id=o.node_id))) from os_images o), 'sequence', (select json_build_object('minimum',min_value,'maximum',max_value,'cycle',cycle) from pg_sequences where schemaname='public' and sequencename='vmid_seq'), 'marker', (select value from public.settings where key='dev_environment_marker'))"
    observed = json.loads(guest(database["ctid"], "runuser", "-u", "postgres", "--", "psql", "-XqAt", "-h", "/var/run/postgresql", "-p", "5432", "-U", "postgres", "-w", "-v", "ON_ERROR_STOP=1", "-d", database["name"], "-c", sql).stdout)
    node = {key: scope[key] for key in ("node_name", "node_api_host", "vm_bridge", "storage")}
    node = {"name": node["node_name"], "api_host": node["node_api_host"], "vm_bridge": node["vm_bridge"], "storage": node["storage"], "pool_name": scope["pool_name"]}
    pool = {"name": scope["pool_name"], "cidr": scope["pool_cidr"], "gateway": scope["pool_gateway"], "reserved_ranges": scope["reserved_ranges"]}
    image = {"template_vmid": scope["template_vmid"], "node_name": scope["node_name"]}
    sequence = {"minimum": scope["vmid_minimum"], "maximum": scope["vmid_maximum"], "cycle": False}
    if observed != {"nodes": [node], "pools": [pool], "images": [image], "sequence": sequence, "marker": scope["dev_environment_marker"]}:
        raise Refusal("real development DB inventory or bounded VMID sequence differs from the manifest")


def check_ct(entry):
    ctid = entry["ctid"]
    config = run("pct", "config", str(ctid)).stdout
    if f"hostname: {entry['hostname']}\n" not in config + "\n":
        raise Refusal(f"CT{ctid} hostname mismatch")
    if run("pct", "status", str(ctid)).stdout.strip() != f"status: running":
        raise Refusal(f"CT{ctid} is not running")


def guest_property(ctid, unit, prop):
    return guest(ctid, "systemctl", "show", "-P", prop, unit).stdout.strip()


def effective_argv(value):
    parts = re.findall(r"argv\[\]=([^;}]*)", value)
    if len(parts) != 1:
        raise Refusal("unit does not expose exactly one ExecStart command")
    try:
        return shlex.split(parts[0])
    except ValueError as exc:
        raise Refusal("unit ExecStart cannot be parsed") from exc


def check_env_rules(entry):
    required = entry.get("required_env", {})
    prefixes = entry.get("prefix_env", {})
    nonempty = entry.get("required_nonempty_env", [])
    if (not isinstance(nonempty, list)
            or any(not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) for key in nonempty)
            or len(nonempty) != len(set(nonempty))):
        raise Refusal("invalid required nonempty environment rules")
    if not isinstance(required, dict) or not isinstance(prefixes, dict):
        raise Refusal("invalid target env rules")
    probe = """import json,pathlib,re,sys,urllib.parse
values={}
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    line=line.strip()
    if not line or line.startswith('#'): continue
    key,sep,value=line.partition('=')
    if not sep or not re.fullmatch(r'[A-Z_][A-Z0-9_]*',key) or key in values: sys.exit(1)
    value=value.strip()
    quote=value[:1] if value[:1] in (chr(34),chr(39)) else None
    if quote:
        if len(value)<2 or value[-1]!=quote: sys.exit(1)
        value=value[1:-1]
    forbidden=(chr(92),quote) if quote else (chr(92),chr(34),chr(39))
    if any(c in value for c in forbidden) or (not quote and any(c.isspace() for c in value)): sys.exit(1)
    if key.startswith(('SPRING_','JAVA_','JDK_','_JAVA','LD_')) and not (key=='SPRING_PROFILES_ACTIVE' and not value): sys.exit(1)
    if key in ('PATH','PYTHONPATH','PYTHONHOME','CLASSPATH','ENV','BASH_ENV','GODEBUG'): sys.exit(1)
    values[key]=value
rules=json.loads(sys.argv[2])
for key,want in rules['required'].items():
    if values.get(key,'')!=want: sys.exit(1)
for key in rules['nonempty']:
    if not values.get(key,'').strip(): sys.exit(1)
allowed=rules.get('allowed_env')
if allowed and any(key not in allowed for key in values): sys.exit(1)
def local_endpoint(value,want):
    if not value.startswith(want): return False
    if want.startswith('http://'):
        parsed=urllib.parse.urlsplit(value)
        try: port=parsed.port
        except ValueError: return False
        return parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and not parsed.username and not parsed.password and port is not None and 1<=port<=65535
    return bool(re.fullmatch(r'127[.]0[.]0[.]1:[0-9]{1,5}',value)) and 1<=int(value.rsplit(':',1)[1])<=65535
for key,want in rules['prefixes'].items():
    if not local_endpoint(values.get(key,''),want): sys.exit(1)
upstream=rules.get('upstream_prefix')
if upstream:
    endpoints=[v for k,v in values.items() if k.startswith('LLMGW_UPSTREAM_') and k.endswith('_BASE_URL')]
    if not endpoints or any(not local_endpoint(v,upstream) for v in endpoints): sys.exit(1)
"""
    rules = json.dumps({"required": required, "prefixes": prefixes, "nonempty": nonempty,
                        "upstream_prefix": entry.get("upstream_prefix"), "allowed_env": entry.get("allowed_env")})
    if guest(entry["ctid"], "python3", "-c", probe, entry["env_file"], rules,
             check=False).returncode:
        raise Refusal("selected env or endpoint differs from the environment allowlist")


def check_unit(entry, target, environment, action):
    ctid = entry["ctid"]
    unit = entry["unit"]
    if guest_property(ctid, unit, "LoadState") != "loaded":
        raise Refusal(f"{target} unit is not loaded")
    executable = guest_property(ctid, unit, "ExecStart")
    argv = effective_argv(executable)
    managed = entry["active_path"] in argv
    if not managed:
        legacy = entry.get("legacy_exec_path")
        expected = entry.get("legacy_sha256")
        if environment != "prod" or action != "check" or not legacy or not expected:
            raise Refusal(f"{target} unit does not select its managed artifact path")
        if legacy not in argv or not SHA256.fullmatch(expected):
            raise Refusal("production fixed artifact differs from the documented path")
        actual = guest(ctid, "sha256sum", legacy).stdout.split()[0]
        if actual != expected:
            raise Refusal("production fixed artifact SHA-256 mismatch")
    env_files = guest_property(ctid, unit, "EnvironmentFiles")
    if env_files != f"{entry['env_file']} (ignore_errors=no)":
        raise Refusal(f"{target} unit does not select exactly its expected env file")
    metadata_probe = "import os,sys; p=sys.argv[1]; s=os.stat(p); sys.exit(os.path.islink(p) or s.st_uid!=0 or bool(s.st_mode & 0o022))"
    if guest(ctid, "python3", "-c", metadata_probe, entry["env_file"], check=False).returncode:
        raise Refusal("selected env file is not root-owned and protected")
    if environment == "dev":
        for prop in ("Environment", "PassEnvironment", "UnsetEnvironment"):
            if guest_property(ctid, unit, prop):
                raise Refusal("development unit overrides or inherits application environment")
        manager_probe = """import subprocess,sys
safe={'PATH','LANG','LC_ALL','LC_CTYPE','HOME','TERM','TZ'}
rows=subprocess.run(['systemctl','show-environment'],capture_output=True,text=True,check=True).stdout.splitlines()
sys.exit(1 if any(row.partition('=')[0] not in safe for row in rows) else 0)
"""
        if guest(ctid, "python3", "-c", manager_probe, check=False).returncode:
            raise Refusal("manager environment supplies an application override")
    if target == "api" and managed:
        wrapper = entry.get("startup_wrapper")
        if not wrapper or argv != [wrapper, entry["active_path"], environment]:
            raise Refusal("API unit does not use the immutable-JAR startup wrapper")
        actual = guest(ctid, "sha256sum", wrapper).stdout.split()[0]
        if actual != entry.get("startup_wrapper_sha256"):
            raise Refusal("API startup wrapper SHA-256 mismatch")
        if guest(ctid, "python3", "-c",
                 "import os,sys; p=sys.argv[1]; s=os.stat(p); sys.exit(os.path.islink(p) or s.st_uid!=0 or bool(s.st_mode & 0o022))",
                 wrapper, check=False).returncode:
            raise Refusal("API startup wrapper is not protected")
    elif target == "api":
        profiles = [part for part in argv if part.startswith("--spring.profiles.active=")]
        if profiles != [f"--spring.profiles.active={environment}"]:
            raise Refusal("API effective profile does not match environment")
    elif environment == "dev" and argv != [entry["active_path"]]:
        raise Refusal("development gateway has an ExecStart override")
    if environment == "dev":
        if not entry.get("required_env") or not entry.get("prefix_env"):
            raise Refusal("development target lacks endpoint allowlist")
        check_env_rules(entry)
    if target == "api":
        prefix = entry.get("db_url_prefix")
        if not prefix or not prefix.startswith("jdbc:postgresql://"):
            raise Refusal("API target lacks an explicit database URL prefix")
        # Read the selected env inside the guest and return only a boolean. A
        # connection URL can include credentials; its bytes never reach stdout.
        probe = """import pathlib,sys,urllib.parse
rows=[line.partition('=')[2].strip() for line in
      pathlib.Path(sys.argv[1]).read_text().splitlines()
      if line.startswith('PICKLE_DB_URL=')]
if len(rows)!=1: sys.exit(1)
value=rows[0]
quote=value[:1] if value[:1] in (chr(34),chr(39)) else None
if quote:
    if len(value)<2 or value[-1]!=quote: sys.exit(1)
    value=value[1:-1]
forbidden=(chr(92),quote) if quote else (chr(92),chr(34),chr(39))
if any(c in value for c in forbidden) or any(c.isspace() for c in value): sys.exit(1)
rows=[value]
if not value.startswith('jdbc:postgresql://'): sys.exit(1)
try:
    observed=urllib.parse.urlsplit(rows[0].removeprefix('jdbc:'))
    expected=urllib.parse.urlsplit(sys.argv[2].removeprefix('jdbc:'))
    query=urllib.parse.parse_qsl(observed.query,keep_blank_values=True)
    parameters=dict(query)
    valid=(observed.scheme==expected.scheme=='postgresql'
           and observed.hostname==expected.hostname and observed.port==expected.port
           and observed.path==expected.path and not observed.username and not observed.password
           and not observed.fragment and len(query)==2 and len(parameters)==2
           and parameters.get('sslmode')=='verify-full'
           and parameters.get('sslrootcert','').startswith('/')
           and '..' not in pathlib.Path(parameters['sslrootcert']).parts)
except (ValueError,KeyError):
    valid=False
sys.exit(0 if valid else 1)
"""
        if guest(ctid, "python3", "-c", probe, entry["env_file"], prefix,
                 check=False).returncode:
            raise Refusal("API selected database URL does not match environment")
    if environment == "prod" and guest_property(ctid, unit, "ActiveState") != "active":
        raise Refusal("production unit is not active")
    return managed


def artifact(path, expected_sha):
    selected = Path(path)
    if selected.is_symlink() or not selected.is_file():
        raise Refusal("artifact is missing or a symlink")
    resolved = selected.resolve(strict=True)
    if STAGING not in resolved.parents:
        raise Refusal("artifact must be beneath the protected staging directory")
    metadata = selected.stat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise Refusal("artifact must be root-owned and not writable by others")
    with selected.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected_sha:
        raise Refusal("artifact SHA-256 mismatch")
    return selected


def validate_archive(path, target, forbidden_literals=()):
    with tarfile.open(path, "r:gz") as archive:
        names = set()
        for member in archive.getmembers():
            name = str(Path(member.name))
            if not member.name or name.startswith("/") or ".." in Path(name).parts:
                raise Refusal("archive contains an unsafe path")
            if name in names:
                raise Refusal("archive contains a duplicate path")
            if not (member.isfile() or member.isdir()):
                raise Refusal("archive contains a non-regular entry")
            if target == "console" and member.mode & 0o7000:
                raise Refusal("console archive contains privileged file modes")
            if target == "llm-gateway" and member.name != name:
                raise Refusal("gateway archive names must be exact")
            if member.isfile() and any(literal.encode() in archive.extractfile(member).read()
                                       for literal in forbidden_literals):
                raise Refusal("archive contains an operating endpoint forbidden in this environment")
            names.add(name)
        if target == "llm-gateway" and names != {"llm-gateway", "llm-keygen"}:
            raise Refusal("gateway archive must contain exactly two binaries")
        if target == "console" and "index.html" not in names:
            raise Refusal("console archive lacks index.html")


def check_artifact_directories(entry):
    paths = [str(Path(entry["active_path"]).parent), entry["release_dir"]]
    if entry.get("keygen_path"):
        paths.append(str(Path(entry["keygen_path"]).parent))
    probe = """import pathlib,stat,sys
for value in sys.argv[1:]:
    path=pathlib.Path(value)
    for selected in (path,*path.parents):
        metadata=selected.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=0 or metadata.st_mode & 0o022: sys.exit(1)
"""
    if guest(entry["ctid"], "python3", "-c", probe, *paths, check=False).returncode:
        raise Refusal("artifact ancestor directories must be root-owned and protected")


def active_link(entry, environment):
    ctid = entry["ctid"]
    link = entry["active_path"]
    check_artifact_directories(entry)
    previous = guest(ctid, "readlink", link, check=False)
    if previous.returncode == 0:
        value = previous.stdout.strip()
        if not value.startswith(entry["release_dir"] + "/"):
            raise Refusal("active link points outside managed releases")
        if guest(ctid, "test", "-f", value, check=False).returncode:
            raise Refusal("active link target is missing")
        return value
    if guest(ctid, "test", "-e", link, check=False).returncode == 0:
        raise Refusal("active artifact path exists but is not a managed symlink")
    if environment == "prod" or guest_property(ctid, entry["unit"], "ActiveState") == "active":
        raise Refusal("active artifact link is missing")
    return None


def keygen_link(entry, environment):
    if "keygen_path" not in entry:
        raise Refusal("gateway target lacks a managed keygen path")
    candidate = dict(entry, active_path=entry["keygen_path"])
    return active_link(candidate, environment)


def check_console(entry):
    ctid = entry["ctid"]
    webroot = entry["webroot"]
    if not webroot.startswith("/var/www/"):
        raise Refusal("console webroot is outside /var/www")
    result = guest_shell(ctid,
        'nginx -T 2>/dev/null | grep -Fq -- "root $1;"', webroot, check=False)
    if result.returncode:
        raise Refusal("nginx does not select the configured console webroot")
    guest(ctid, "nginx", "-t")


def verify_local_health(entry, target):
    ctid = entry.get("probe_ctid", entry["ctid"])
    if target == "console":
        result = guest_shell(ctid, '''set -euo pipefail
root="$1"; host="$2"
installed=$(sha256sum "$root/index.html" | cut -d ' ' -f 1)
served=$(curl -fsS --max-time 5 -H "Host: $host" http://127.0.0.1/ | sha256sum | cut -d ' ' -f 1)
[ "$installed" = "$served" ]
asset=$(grep -oE '/assets/[^" ]+\\.js' "$root/index.html" | head -n 1)
[ -n "$asset" ]
probe=$(curl -fsS --max-time 5 -H "Host: $host" -o /dev/null \\
  -w '%{size_download} %{content_type}' "http://127.0.0.1$asset")
read -r bytes kind <<<"$probe"
[ "$bytes" -gt 10000 ]
case "$kind" in *javascript*|*ecmascript*) exit 0 ;; *) exit 1 ;; esac
''', entry["webroot"], entry["http_host"], check=False)
        return result.returncode == 0
    else:
        result = guest(ctid, "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                       "--max-time", "5", entry["health_url"], check=False)
    return result.returncode == 0 and result.stdout.strip() == "200"


def wait_health(entry, target):
    for _ in range(30):
        if verify_local_health(entry, target):
            return True
        time.sleep(2)
    return False


def push_if_absent(ctid, source, destination, expected_sha, mode, owner):
    exists = guest(ctid, "test", "-e", destination, check=False).returncode == 0
    if not exists:
        guest(ctid, "mkdir", "-p", str(Path(destination).parent))
        run("pct", "push", str(ctid), str(source), destination)
        guest(ctid, "chown", owner, destination)
        guest(ctid, "chmod", mode, destination)
    if guest(ctid, "python3", "-c", "import os,sys; p=sys.argv[1]; s=os.stat(p); sys.exit(os.path.islink(p) or s.st_uid!=0 or bool(s.st_mode & 0o022))", destination, check=False).returncode:
        raise Refusal("guest release artifact is not protected")
    actual = guest(ctid, "sha256sum", destination).stdout.split()[0]
    if actual != expected_sha:
        raise Refusal("guest artifact SHA-256 mismatch")


def install_binary(entry, target, source, commit, expected_sha):
    ctid = entry["ctid"]
    release = entry["release_dir"]
    if target == "api":
        destination = f"{release}/pickle-api-{commit}.jar"
        push_if_absent(ctid, source, destination, expected_sha, "0640", "root:pickle")
        return destination, None
    destination = f"{release}/llm-gateway-{commit}"
    keygen = f"{release}/llm-keygen-{commit}"
    with tempfile.TemporaryDirectory(prefix="pickle-gateway-stage-") as folder:
        with tarfile.open(source, "r:gz") as archive:
            for name, output in (("llm-gateway", destination), ("llm-keygen", keygen)):
                data = archive.extractfile(name).read()
                local = Path(folder) / name
                local.write_bytes(data)
                digest = hashlib.sha256(data).hexdigest()
                push_if_absent(ctid, local, output, digest, "0750", "root:pickle-llmgw")
    return destination, keygen


def select_link(ctid, link, target):
    guest_shell(ctid, '''set -euo pipefail
link="$1"; target="$2"; temporary="${link}.next.$$"
trap 'rm -f -- "$temporary"' EXIT
ln -s -- "$target" "$temporary"
mv -Tf -- "$temporary" "$link"
''', link, target)


def activate_binary(entry, target, destination, keygen, previous, previous_keygen,
                    rollback_policy):
    ctid = entry["ctid"]
    link = entry["active_path"]
    unit = entry["unit"]
    select_link(ctid, link, destination)
    if keygen:
        try:
            select_link(ctid, entry["keygen_path"], keygen)
        except Refusal:
            if previous:
                select_link(ctid, link, previous)
            raise
    restarted = guest(ctid, "systemctl", "restart", unit, check=False).returncode == 0
    if restarted and wait_health(entry, target):
        expected = guest(ctid, "sha256sum", destination).stdout.split()[0]
        try:
            verify_running_artifact(entry, target, expected)
            return
        except Refusal:
            pass
    if previous and rollback_policy == "compatible":
        select_link(ctid, link, previous)
        if previous_keygen:
            select_link(ctid, entry["keygen_path"], previous_keygen)
        guest(ctid, "systemctl", "restart", unit)
        if not wait_health(entry, target):
            raise Refusal("health failed; previous artifact restored but its health failed")
        raise Refusal("health failed; previous artifact restored")
    raise Refusal("health failed; fix-forward required, selected artifact retained")


def activate_console(entry, source, commit, expected_sha):
    ctid = entry["ctid"]
    webroot = entry["webroot"]
    release = f"{webroot}.releases/{commit}"
    backup = f"{webroot}.before-{int(time.time())}"
    if guest(ctid, "test", "-e", release, check=False).returncode == 0:
        raise Refusal("console release directory already exists")
    guest(ctid, "mkdir", "-p", release)
    uploaded = f"{release}/bundle.tar.gz"
    run("pct", "push", str(ctid), str(source), uploaded)
    if guest(ctid, "sha256sum", uploaded).stdout.split()[0] != expected_sha:
        raise Refusal("guest console archive SHA-256 mismatch")
    guest(ctid, "tar", "--no-same-owner", "--no-same-permissions", "-xzf", uploaded, "-C", release)
    guest(ctid, "rm", "--", uploaded)
    guest(ctid, "chown", "-R", "root:root", release)
    guest(ctid, "find", release, "-type", "d", "-exec", "chmod", "0755", "{}", "+")
    guest(ctid, "find", release, "-type", "f", "-exec", "chmod", "0644", "{}", "+")
    if guest(ctid, "test", "-e", backup, check=False).returncode == 0:
        raise Refusal("console backup path already exists")
    old = guest(ctid, "test", "-d", webroot, check=False).returncode == 0
    if old:
        guest(ctid, "mv", webroot, backup)
    moved = guest(ctid, "mv", release, webroot, check=False).returncode == 0
    valid = moved and guest(ctid, "nginx", "-t", check=False).returncode == 0
    reloaded = valid and guest(ctid, "systemctl", "reload", "nginx", check=False).returncode == 0
    if reloaded and wait_health(entry, "console"):
        return
    if old:
        if moved:
            failed = f"{webroot}.failed-{int(time.time())}"
            guest(ctid, "mv", webroot, failed)
        guest(ctid, "mv", backup, webroot)
        guest(ctid, "systemctl", "reload", "nginx")
        raise Refusal("console deploy failed; previous webroot restored")
    raise Refusal("console deploy failed; no previous webroot exists")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply"))
    parser.add_argument("environment", choices=("dev", "prod"))
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("commit")
    parser.add_argument("artifact")
    parser.add_argument("sha256")
    parser.add_argument("--rollback-policy", choices=("compatible", "fix-forward"))
    parser.add_argument("--backup-point")
    parser.add_argument("--backup-check", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise Refusal("deployment requires root on the selected Proxmox host")
    if not COMMIT.fullmatch(args.commit) or not SHA256.fullmatch(args.sha256):
        raise Refusal("commit or artifact SHA-256 is not a full lowercase digest")
    config = load_config(args.environment)
    if args.environment == "prod" and args.action == "apply":
        check_release_approval(config, args)
    check_manifest(config, args.target, args.commit, args.sha256)
    entry = config["targets"][args.target]
    check_ct(entry)
    previous_keygen = None
    if args.target == "console":
        check_console(entry)
    else:
        managed = check_unit(entry, args.target, args.environment, args.action)
        previous = active_link(entry, args.environment) if managed else None
        if args.target == "llm-gateway" and managed:
            previous_keygen = keygen_link(entry, args.environment)
    source = artifact(args.artifact, args.sha256)
    if args.target != "api":
        validate_archive(source, args.target, entry.get("forbidden_literals", ()))
    rollback_policy = api_rollback_policy(config, args.rollback_policy) if args.target == "api" else "compatible"
    check_development_scope(config)
    if args.environment == "prod" and args.backup_check:
        check_production_backup(config)
    print(f"preflight OK: {args.environment} {args.target} {args.commit} {args.sha256}")
    if args.action == "check":
        return
    lock_path = Path(f"/run/lock/pickle-deploy-{args.environment}.lock")
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refusal("another deployment owns the environment lock") from exc
        check_manifest(config, args.target, args.commit, args.sha256)
        check_ct(entry)
        source = artifact(args.artifact, args.sha256)
        if args.target != "api":
            validate_archive(source, args.target, entry.get("forbidden_literals", ()))
        if args.target == "console":
            check_console(entry)
        else:
            check_unit(entry, args.target, args.environment, "apply")
            previous = active_link(entry, args.environment)
            if args.target == "llm-gateway":
                previous_keygen = keygen_link(entry, args.environment)
        check_development_scope(config)
        if args.environment == "prod":
            check_release_approval(config, args)
            check_production_backup(config)
            # The verifier can take time; the approval must still be current.
            check_release_approval(config, args)
        if args.target == "llm-gateway":
            check_chat_order(config, args.environment)
        if args.target == "console":
            activate_console(entry, source, args.commit, args.sha256)
        else:
            destination, keygen = install_binary(entry, args.target, source, args.commit, args.sha256)
            activate_binary(entry, args.target, destination, keygen, previous, previous_keygen,
                            rollback_policy)
        write_receipt(args.environment, args.target, args.commit, entry)
    print(f"deployment OK: {args.environment} {args.target} {args.commit}")


if __name__ == "__main__":
    try:
        main()
    except (Refusal, OSError, json.JSONDecodeError, tarfile.TarError) as exc:
        print(f"deploy refused: {exc}", file=sys.stderr)
        sys.exit(1)
