#!/usr/bin/python3 -I
"""Start Java from an inherited immutable JAR descriptor."""

import os
import stat
from pathlib import Path
import sys


ALLOWED_ENV = frozenset(
    """
    PICKLE_CONSOLE_BASE_URL
    PICKLE_CREDENTIALS_KEY
    PICKLE_DB_PASSWORD
    PICKLE_DB_URL
    PICKLE_DB_USER
    PICKLE_DEV_DB_HOST
    PICKLE_DEV_DB_NAME
    PICKLE_DEV_DB_PORT
    PICKLE_DEV_DB_USER
    PICKLE_DEV_DATA_MODE
    PICKLE_DEV_ENABLE_GATEWAYS
    PICKLE_DEV_IP_POOL_CIDR
    PICKLE_DEV_IP_POOL_DNS
    PICKLE_DEV_IP_POOL_GATEWAY
    PICKLE_DEV_IP_POOL_NAME
    PICKLE_DEV_IP_POOL_RESERVED_RANGES
    PICKLE_DEV_JOBRUNR_ENABLED
    PICKLE_DEV_NODE_API_HOST
    PICKLE_DEV_NODE_CPU_THREADS
    PICKLE_DEV_NODE_MEMORY_MB
    PICKLE_DEV_NODE_NAME
    PICKLE_DEV_NODE_STORAGE
    PICKLE_DEV_NODE_VM_BRIDGE
    PICKLE_DEV_REAL_INTEGRATIONS_ENABLED
    PICKLE_DEV_RELAY_NAME
    PICKLE_DEV_RELAY_PORT_END
    PICKLE_DEV_RELAY_PORT_START
    PICKLE_DEV_RELAY_PUBLIC_HOST
    PICKLE_DEV_RELAY_SOURCE_IP
    PICKLE_DEV_ROOT_DOMAIN
    PICKLE_DEV_TEMPLATE_VMID
    PICKLE_DEV_VMID_START
    PICKLE_DNS_GOOGLE_CREDENTIALS
    PICKLE_DNS_GOOGLE_PROJECT
    PICKLE_DNS_GOOGLE_ZONE
    PICKLE_DNS_PROVIDER
    PICKLE_DNS_PRUNE_ORPHANS
    PICKLE_DNS_RECORD_TTL
    PICKLE_GOOGLE_CLIENT_ID
    PICKLE_GOOGLE_CLIENT_SECRET
    PICKLE_GOOGLE_HOSTED_DOMAIN
    PICKLE_GOOGLE_REDIRECT_URI
    PICKLE_ISOLATED_BOOTSTRAP_ENABLED
    PICKLE_JOBRUNR_DASH_ENABLED
    PICKLE_JOBRUNR_DASH_PASS
    PICKLE_JOBRUNR_DASH_USER
    PICKLE_JWT_SECRET
    PICKLE_LLM_BODIES_RATE_LIMIT
    PICKLE_LLM_BODY_READ_KEYS
    PICKLE_LLM_BODY_WRITE_KEY_ID
    PICKLE_LLM_GATEWAY_PREVIOUS_TOKEN
    PICKLE_LLM_GATEWAY_SOURCE_IP
    PICKLE_LLM_GATEWAY_TOKEN
    PICKLE_LLM_MAX_BODIES_BODY_BYTES
    PICKLE_LLM_MAX_SYNC_BODY_BYTES
    PICKLE_LLM_MAX_USAGE_BODY_BYTES
    PICKLE_LLM_QUOTA_DIRECT_ENABLED
    PICKLE_LLM_SYNC_RATE_LIMIT
    PICKLE_LLM_USAGE_RATE_LIMIT
    PICKLE_LLM_USAGE_ROLLUP_DIRECT_ENABLED
    PICKLE_MAIL_FROM
    PICKLE_MFA_ENFORCE_ADMIN
    PICKLE_MOCK_MAIL_SPOOL
    PICKLE_NOTIFICATION_ADMIN_BUNDLE_WINDOW
    PICKLE_OPENROUTER_CREDENTIAL_READ_KEYS
    PICKLE_OPENROUTER_CREDENTIAL_WRITE_KEY_ID
    PICKLE_OPENROUTER_DIRECT_POLLING_ACCOUNT_PUBLIC_IDS
    PICKLE_OPENROUTER_DIRECT_POLLING_ENABLED
    PICKLE_OPENROUTER_DIRECT_POLLING_FIXED_DELAY
    PICKLE_OPENROUTER_URL
    PICKLE_PASSWORD_RESET_BASE_URL
    PICKLE_PROXMOX_CA_CERT
    PICKLE_PROXMOX_TOKEN_ID
    PICKLE_PROXMOX_TOKEN_SECRET
    PICKLE_PROXY_AGENT_TOKEN
    PICKLE_PROXY_AGENT_URL
    PICKLE_PROXY_PUBLIC_IP
    PICKLE_RELAY_FIRST_CONTACT_GRACE_SECONDS
    PICKLE_RELAY_MAX_SYNC_BODY_BYTES
    PICKLE_RELAY_POLL_INTERVAL_SECONDS
    PICKLE_RELAY_RESTRICTED_SOURCE_IPS
    PICKLE_RELAY_RETIREMENT_ENABLED
    PICKLE_RELAY_SYNC_RATE_LIMIT
    PICKLE_SEED_ORGADMIN_EMAIL
    PICKLE_SEED_ORGADMIN_PASSWORD
    PICKLE_SEED_SYSADMIN_EMAIL
    PICKLE_SEED_SYSADMIN_PASSWORD
    PICKLE_SMTP_HOST
    PICKLE_SMTP_PASSWORD
    PICKLE_SMTP_PORT
    PICKLE_SMTP_USERNAME
    PICKLE_SSHGW_GLOBAL_RATE_LIMIT
    PICKLE_SSHGW_RATE_LIMIT
    PICKLE_SSHGW_SOURCE_IP
    PICKLE_SSHGW_TOKEN
    PICKLE_SSH_HOST
    PICKLE_SSH_PLATFORM_PUBLIC_KEY
    PICKLE_SSH_PORT
    PICKLE_TERMINAL_BRIDGE_URL
    PICKLE_TERMINAL_CONTROL_TOKEN
    PICKLE_TERMINAL_PER_ORG_CAP
    PICKLE_TERMINAL_PER_USER_CAP
    PICKLE_TERMINAL_PER_VM_CAP
    PICKLE_TERMINAL_PUBLIC_KEY
    PICKLE_TERMINAL_RATE_LIMIT
    PICKLE_TERMINAL_SINGLE_INSTANCE
    PICKLE_VERIFICATION_BASE_URL
    PICKLE_VM_FIREWALL_BARRIER_GROUP
    PICKLE_VM_FIREWALL_ENABLED
    PICKLE_VM_FIREWALL_PROXY_SOURCE_IPS
    PICKLE_VM_FIREWALL_RELAY_SOURCE_IPS
    PICKLE_VM_FIREWALL_SSH_GATEWAY_SOURCE_IPS
    PICKLE_VM_FIREWALL_TERMINAL_SOURCE_IPS
    """.split()
)


def execution_arguments(environment, values):
    mode = values.get("PICKLE_DEV_DATA_MODE", "synthetic")
    if mode not in ("synthetic", "production-copy") or (environment == "prod" and mode != "synthetic"):
        raise RuntimeError("invalid development data mode for this environment")
    profile = "isolated" if mode == "production-copy" else environment
    address = "127.0.0.1" if environment == "dev" else "0.0.0.0"
    arguments = [f"--spring.profiles.active={profile}", f"--server.address={address}"]
    if mode == "production-copy":
        # A restored queue and inventory may refer to live infrastructure. Keep
        # every copied record while refusing external effects at runtime.
        arguments.extend([
            "--jobrunr.background-job-server.enabled=false", "--jobrunr.dashboard.enabled=false",
            "--pickle.mfa.enforce-admin=true", "--pickle.isolated-bootstrap.enabled=false",
            "--pickle.llm.quota-direct.enabled=false", "--pickle.llm.usage-rollup-direct.enabled=false",
            "--pickle.openrouter.direct-polling.enabled=false", "--pickle.dns.provider=none",
            "--pickle.dns.prune-orphans=false", "--pickle.relay-retirement.enabled=false",
            "--pickle.vm-firewall.enabled=false", "--pickle.proxmox.token-id=",
            "--pickle.proxmox.token-secret=", "--pickle.proxy-agent.base-url=http://127.0.0.1:19443",
            "--pickle.proxy-agent.token=", "--pickle.terminal.bridge-control-base-url=http://127.0.0.1:18083",
            "--pickle.terminal.bridge-control-token=", "--pickle.sshgw.token=",
            "--pickle.sshgw.allowed-source-ip=127.0.0.1", "--pickle.llm-gateway.token=",
            "--pickle.llm-gateway.previous-token=", "--pickle.llm-gateway.allowed-source-ip=127.0.0.1",
            "--pickle.openrouter.base-url=http://127.0.0.1:18085",
            "--pickle.oauth.google.client-id=", "--pickle.oauth.google.client-secret=",
        ])
    elif environment == "prod":
        arguments.extend(["--jobrunr.background-job-server.enabled=true", "--jobrunr.dashboard.enabled=false",
                          "--pickle.network-policy.enabled=true", "--pickle.vm-firewall.enabled=true",
                          "--pickle.dns.provider=google"])
    return arguments


def require_protected_directories(path):
    for selected in (path, *path.parents):
        metadata = selected.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise RuntimeError("artifact ancestor directories must be root-owned and protected")


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in ("dev", "prod"):
        raise RuntimeError("expected a managed JAR link and environment")
    environment = sys.argv[2]
    expected = "/opt/pickle/api/current-dev.jar" if environment == "dev" else "/opt/pickle/api/production-current.jar"
    if sys.argv[1] != expected:
        raise RuntimeError("unexpected managed JAR link")
    require_protected_directories(Path(expected).parent)
    require_protected_directories(Path("/opt/pickle/api/releases"))
    selected = Path(expected).resolve(strict=True)
    if selected.parent != Path("/opt/pickle/api/releases"):
        raise RuntimeError("JAR is outside managed releases")
    fd = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW)
    metadata = os.fstat(fd)
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RuntimeError("JAR must be root-owned and protected")
    os.dup2(fd, 9, inheritable=True)
    if fd != 9:
        os.close(fd)
    # Spring/JVM generic override channels and loader settings are intentionally
    # absent. Product variables come from the one selected EnvironmentFile.
    environment_values = {key: value for key, value in os.environ.items()
                          if key in ALLOWED_ENV}
    environment_values.update(PATH="/usr/bin:/bin", LANG="C.UTF-8")
    heap = "-Xmx1g" if environment == "dev" else "-Xmx2g"
    argv = ["/usr/bin/java", "-Xms256m", heap, "-jar", "/proc/self/fd/9",
            "--spring.config.location=classpath:/application.yml"]
    argv.extend(execution_arguments(environment, environment_values))
    os.execve("/usr/bin/java", argv, environment_values)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as exc:
        print(f"API start refused: {exc}", file=sys.stderr)
        sys.exit(1)
