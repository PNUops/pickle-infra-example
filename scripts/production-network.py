#!/usr/bin/env python3
"""Install scoped node guards and a manually owned production gateway."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).parent / "lib"))
sys.dont_write_bytecode = True
from production_network import CHAINS, NetBirdMarkNotReady, firewall_plan, nft_guard_text, parse_netbird_accept_mark, validate_config

BASE = Path("/var/lib/example-production-network")
STATE = BASE / "state.json"
CONFIG = Path("/etc/pickle/production-network.json")
RUNTIME = Path("/usr/local/libexec/example-production-network")
OWNER = Path("/etc/pve/priv/example-production-network-owner.json")
UNIT = "example-production-network.service"
TIMER = "example-production-network-rollback"
COMMENT = "example-production-network"
TABLE = "pickle_production_l2"
INET_TABLE = "pickle_production_guard"
MARK_READY_TIMEOUT = 15.0
MARK_READY_INTERVAL = 0.25
HOOKS = {"raw": ("raw", "PREROUTING"), "mangle": ("mangle", "PREROUTING"),
         "input": ("filter", "INPUT"), "forward": ("filter", "FORWARD"), "nat": ("nat", "POSTROUTING")}
BASELINE_FILES = ["/etc/network/interfaces", "/etc/hosts", "/etc/resolv.conf", "/etc/ssh/sshd_config",
                  "/etc/ssh/sshd_config.d/10-pickle.conf", "/etc/pve/corosync.conf"]


def run(argv, *, stdin=None, check=True, timeout=40):
    result = subprocess.run(list(map(str, argv)), input=stdin, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{argv[0]} failed: {result.stderr.strip()}")
    return result


def stop_transient_unit(unit):
    """Stop one transient unit, accepting only systemd's already-collected state."""
    stopped = run(["systemctl", "stop", unit], check=False)
    if stopped.returncode == 0:
        return
    load_state = run(["systemctl", "show", unit, "-p", "LoadState", "--value"]).stdout.strip()
    if stopped.returncode == 5 and load_state == "not-found":
        return
    raise RuntimeError(f"systemctl stop {unit} failed: {stopped.stderr.strip()} (LoadState={load_state})")


def api(path, *arguments):
    return json.loads(run(["pvesh", "get", path, *arguments, "--output-format", "json"]).stdout)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def protected(path):
    info = path.stat()
    assert not path.is_symlink() and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700, f"unprotected directory: {path}"


def hashes():
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in BASELINE_FILES}


def cluster_check(config, empty=False):
    rows = api("/cluster/status")
    cluster = [row for row in rows if row.get("type") == "cluster"]
    nodes = [row for row in rows if row.get("type") == "node"]
    assert len(cluster) == 1 and cluster[0]["name"] == config["cluster"] and cluster[0]["quorate"] == 1, "cluster is not quorate"
    assert {row["name"] for row in nodes} == set(config["nodes"]), "unexpected cluster membership"
    assert api("/cluster/ha/resources") == [], "HA resources are outside this procedure"
    local = socket.gethostname().split(".")[0]
    assert not api(f"/nodes/{local}/firewall/options").get("nftables"), "Proxmox nftables backend is outside this procedure"
    if empty:
        assert all(row["online"] for row in nodes), "both nodes must be online"
        assert api("/cluster/resources", "--type", "vm") == [], "guests must be absent for initial installation"
        quorum = run(["pvecm", "status"]).stdout
        assert re.search(r"Expected votes:\s+3\b", quorum) and re.search(r"Total votes:\s+3\b", quorum) and "Qdevice" in quorum, "the witness must be voting before initial network changes"


def mesh_profile(config, node):
    path = Path("/var/lib/netbird/default.json")
    assert path.stat().st_uid == 0 and stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = json.loads(path.read_text())
    assert all(raw.get(key) is True for key in ("DisableDNS", "DisableClientRoutes", "DisableServerRoutes")), "NetBird routing/DNS posture changed"
    assert all(not raw.get(key) for key in ("DisableFirewall", "ServerSSHAllowed", "RemoteJobsAllowed")), "unexpected NetBird capabilities"
    if config["nodes"][node].get("bmc_interface"):
        assert config["nodes"][node]["bmc_interface"] in raw["IFaceBlackList"], "BMC interface is an ICE candidate"
    environment = run(["systemctl", "show", "netbird.service", "-p", "Environment", "--value"]).stdout
    assert "NB_DISABLE_SSH_CONFIG=true" in shlex.split(environment), "NetBird SSH config protection missing"
    # Never return identity keys or the entire profile.
    return {"mtu": raw["MTU"]}


def accept_mark():
    source = Path("/usr/share/perl5/PVE/Firewall.pm").read_text()
    found = []
    for binary in ("iptables", "ip6tables"):
        found.append(parse_netbird_accept_mark(run([binary, "-t", "filter", "-S"]).stdout,
                                               run([binary, "-t", "mangle", "-S"]).stdout, source))
    assert found[0] == found[1], "IPv4 and IPv6 NetBird mark layouts differ"
    return found[0]


def wait_for_accept_mark(expected, state):
    """Wait only for NetBird's transient empty rule state, never a changed layout."""
    started = time.monotonic()
    deadline = started + MARK_READY_TIMEOUT
    attempts = 0
    consecutive = 0
    last_not_ready = None

    def record(now, ready):
        state["accept_mark_readiness"] = {
            "attempts": attempts,
            "elapsed_seconds": round(max(0.0, now - started), 3),
            "consecutive_successes": consecutive,
            "ready": ready,
        }
        if last_not_ready is not None:
            state["accept_mark_readiness"]["last_not_ready"] = last_not_ready
        write_json(STATE, state)

    while time.monotonic() < deadline:
        attempts += 1
        try:
            observed = accept_mark()
        except NetBirdMarkNotReady as error:
            consecutive = 0
            last_not_ready = str(error)
        else:
            assert observed == expected, "NetBird changed its mark layout while restarting"
            completed = time.monotonic()
            if completed >= deadline:
                record(completed, False)
                break
            consecutive += 1
            last_not_ready = None
            record(completed, consecutive == 2)
            if consecutive == 2:
                return observed
        completed = time.monotonic()
        record(completed, False)
        remaining = deadline - completed
        if remaining <= 0:
            break
        time.sleep(min(MARK_READY_INTERVAL, remaining))
    finished = time.monotonic()
    record(finished, False)
    raise RuntimeError(f"NetBird mark rules were not ready after {attempts} attempts in {finished - started:.3f}s")


def check_links(config, node):
    links = json.loads(run(["ip", "-j", "-d", "link"]).stdout)
    by_name = {row["ifname"]: row for row in links}
    for name, specification in config["vnets"].items():
        bridge, vxlan = by_name[name], by_name["vxlan_" + name]
        assert bridge.get("linkinfo", {}).get("info_kind") == "bridge"
        assert not bridge.get("linkinfo", {}).get("info_data", {}).get("vlan_filtering", 0), "guest trunks are not supported"
        assert vxlan.get("linkinfo", {}).get("info_kind") == "vxlan"
        assert vxlan["linkinfo"]["info_data"]["id"] == specification["vni"]
        assert vxlan.get("master") in (name, bridge["ifindex"]), "VXLAN has a different master"
        assert bridge["mtu"] == vxlan["mtu"] == config["guest_mtu"]
        fdb = json.loads(run(["bridge", "-j", "fdb", "show", "dev", "vxlan_" + name]).stdout)
        expected_peer = next(row["mesh"] for other, row in config["nodes"].items() if other != node)
        assert {row["dst"] for row in fdb if row.get("dst")} == {expected_peer}, "unexpected VXLAN remote peer"
        route = json.loads(run(["ip", "-j", "route", "get", expected_peer]).stdout)
        assert len(route) == 1 and route[0]["dev"] == config["mesh_interface"]
        assert route[0].get("prefsrc") == config["nodes"][node]["mesh"], "VXLAN route uses a different source"
    assert by_name[config["mesh_interface"]]["mtu"] == config["host_mtu"]


def check_initial_routes(config):
    targets = [ipaddress.IPv4Network(specification["cidr"]) for specification in config["vnets"].values()]
    routes = json.loads(run(["ip", "-j", "-4", "route", "show", "table", "all"]).stdout)
    for route in routes:
        if route.get("dst") in (None, "default", "0.0.0.0/0"):
            continue
        network = ipaddress.IPv4Network(route["dst"], strict=False)
        assert not any(network.overlaps(target) for target in targets), "existing route overlaps a proposed VNet"


def install_chains(plan):
    for binary, families in plan.items():
        tables = {}
        for name, rules in families.items():
            table, hook = HOOKS[name]
            chain = CHAINS[name]
            lines = tables.setdefault(table, ["*" + table])
            lines += [f":{chain} - [0:0]", f"-F {chain}"]
            lines += [shlex.join(["-A", chain, *rule]) for rule in rules]
            lines.append(f"-A {chain} -j RETURN")
        for table, lines in tables.items():
            # Move only our hooks in the same atomic transaction as our chains.
            for name in families:
                target_table, hook = HOOKS[name]
                if target_table != table:
                    continue
                rule = ["-m", "comment", "--comment", COMMENT, "-j", CHAINS[name]]
                current = run([binary, "-t", table, "-S", hook]).stdout
                copies = sum(shlex.split(line) == ["-A", hook, *rule] for line in current.splitlines())
                lines += [shlex.join(["-D", hook, *rule])] * copies
                lines.append(shlex.join(["-I", hook, "1", *rule]))
            run([binary + "-restore", "--noflush", "--wait", "10"], stdin="\n".join([*lines, "COMMIT", ""]))


def install_l2_guard(config, mark):
    names = ", ".join(json.dumps(name) for name in config["vnets"])
    value, mask, pve_mask = mark
    non_pve = mask ^ pve_mask
    clear = mask ^ value
    mark_lines = []
    for name in config["vnets"]:
        mark_lines += [f'  iifname "vxlan_{name}" meta mark & 0x{non_pve:x} == 0x{value:x} counter meta mark set meta mark & 0x{clear:x}',
                       f'  iifname "vxlan_{name}" meta mark & 0x{non_pve:x} != 0 counter drop']
    text = f'''table bridge {TABLE} {{
 chain prerouting {{
  type filter hook prerouting priority -310; policy accept;
{chr(10).join(mark_lines)}
 }}
 chain forward {{
  type filter hook forward priority -310; policy accept;
  meta ibrname {{ {names} }} ether type != {{ ip, ip6, arp }} counter drop
 }}
 chain input {{
  type filter hook input priority -310; policy accept;
  meta ibrname {{ {names} }} ether type != {{ ip, ip6, arp }} counter drop
 }}
}}
'''
    exists = run(["nft", "list", "table", "bridge", TABLE], check=False).returncode == 0
    batch = (f"flush table bridge {TABLE}\n" if exists else "") + text
    run(["nft", "--check", "--file", "-"], stdin=batch)
    run(["nft", "--file", "-"], stdin=batch)


def install_priority_guard(config, node, mark, active):
    exists = run(["nft", "list", "table", "inet", INET_TABLE], check=False).returncode == 0
    batch = (f"flush table inet {INET_TABLE}\n" if exists else "") + nft_guard_text(config, node, mark, active, INET_TABLE)
    run(["nft", "--check", "--file", "-"], stdin=batch)
    run(["nft", "--file", "-"], stdin=batch)


def guard_fingerprints():
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items() if key not in ("packets", "bytes", "handle", "metainfo")}
        if isinstance(value, list):
            return [normalize(item) for item in value if not isinstance(item, dict) or "metainfo" not in item]
        return value
    fingerprints = {}
    for family, table in (("bridge", TABLE), ("inet", INET_TABLE)):
        state = json.loads(run(["nft", "--json", "list", "table", family, table]).stdout)
        fingerprints[table] = hashlib.sha256(json.dumps(normalize(state), sort_keys=True).encode()).hexdigest()
    return fingerprints


def clear_chains():
    for binary in ("iptables", "ip6tables"):
        for name, (table, hook) in HOOKS.items():
            if name == "nat" and binary == "ip6tables":
                continue
            rule = ["-m", "comment", "--comment", COMMENT, "-j", CHAINS[name]]
            while run([binary, "-w", "10", "-t", table, "-C", hook, *rule], check=False).returncode == 0:
                run([binary, "-w", "10", "-t", table, "-D", hook, *rule])
            if run([binary, "-t", table, "-S", CHAINS[name]], check=False).returncode == 0:
                run([binary, "-w", "10", "-t", table, "-F", CHAINS[name]])
                run([binary, "-w", "10", "-t", table, "-X", CHAINS[name]])
    if run(["nft", "list", "table", "bridge", TABLE], check=False).returncode == 0:
        run(["nft", "delete", "table", "bridge", TABLE])
    if run(["nft", "list", "table", "inet", INET_TABLE], check=False).returncode == 0:
        run(["nft", "delete", "table", "inet", INET_TABLE])


def sysctl(key, value=None):
    return run(["sysctl", "-n", key] if value is None else ["sysctl", "-w", f"{key}={value}"]).stdout.strip()


def set_mesh_mtu(config, node, mtu):
    run(["netbird", "down"])
    command = ["netbird", "up", "--mtu", str(mtu), "--disable-dns", "--disable-client-routes", "--disable-server-routes"]
    if config["nodes"][node].get("bmc_interface"):
        command += ["--extra-iface-blacklist", config["nodes"][node]["bmc_interface"]]
    run(command, timeout=75)
    assert mesh_profile(config, node)["mtu"] == mtu
    assert int(Path(f"/sys/class/net/{config['mesh_interface']}/mtu").read_text()) == mtu


def remove_gateways(config):
    for name, specification in config["vnets"].items():
        if Path("/sys/class/net", name).exists():
            address = specification["gateway"] + "/" + str(ipaddress.IPv4Network(specification["cidr"]).prefixlen)
            rows = json.loads(run(["ip", "-j", "-4", "address", "show", "dev", name]).stdout)
            if any(item.get("local") == specification["gateway"] for row in rows for item in row.get("addr_info", [])):
                run(["ip", "address", "del", address, "dev", name])


def reconcile(config, node, state):
    cluster_check(config)
    mesh_profile(config, node)
    check_links(config, node)
    owner = json.loads(OWNER.read_text())
    assert owner["schema"] == 1 and owner["cluster"] == config["cluster"] and owner["zone"] == config["zone"]
    assert owner["owner"] in config["nodes"]
    active = owner["owner"] == node
    mark = accept_mark()
    assert list(mark) == state["accept_mark"], "NetBird mark layout changed"
    guest_firewall_check()
    # Install guards before any routing is enabled.
    install_chains(firewall_plan(config, node, mark, active))
    install_priority_guard(config, node, mark, active)
    install_l2_guard(config, mark)
    state["guard_fingerprints"] = guard_fingerprints()
    for name, specification in config["vnets"].items():
        sysctl(f"net.ipv6.conf.{name}.autoconf", 0)
        sysctl(f"net.ipv6.conf.{name}.accept_ra", 0)
        sysctl(f"net.ipv6.conf.{name}.disable_ipv6", 1)
        addresses = json.loads(run(["ip", "-j", "-4", "address", "show", "dev", name]).stdout)
        actual = {item["local"] for row in addresses for item in row.get("addr_info", [])}
        assert actual <= {specification["gateway"]}, "unrelated address on owned VNet"
        if active:
            run(["ip", "address", "replace", specification["gateway"] + "/" + str(ipaddress.IPv4Network(specification["cidr"]).prefixlen), "dev", name])
    if not active:
        remove_gateways(config)
    sysctl("net.bridge.bridge-nf-call-iptables", 1)
    sysctl("net.bridge.bridge-nf-call-ip6tables", 1)
    sysctl("net.ipv6.conf.all.forwarding", 0)
    sysctl("net.ipv4.ip_forward", 1 if active else 0)
    assert hashes() == state["baseline_hashes"], "native management or Corosync files changed"


def guest_firewall_check():
    if api("/cluster/resources", "--type", "vm"):
        options = Path("/etc/pve/firewall/cluster.fw").read_text()
        assert re.search(r"(?m)^\s*enable:\s*1\s*$", options), "PVE firewall is not enabled while guests exist"
        for binary in ("iptables", "ip6tables"):
            assert run([binary, "-S", "PVEFW-FORWARD"], check=False).returncode == 0, "PVE guest firewall chain is absent"


def validate_current(config, node, state):
    """Commit validation is read-only; it cannot invalidate its controller proof."""
    cluster_check(config)
    assert mesh_profile(config, node)["mtu"] == config["host_mtu"]
    check_links(config, node)
    guest_firewall_check()
    for key in ("net.bridge.bridge-nf-call-iptables", "net.bridge.bridge-nf-call-ip6tables"):
        assert sysctl(key) == "1", "bridged guest filtering is disabled"
    assert list(accept_mark()) == state["accept_mark"], "NetBird mark layout changed"
    assert guard_fingerprints() == state["guard_fingerprints"], "owned priority guard changed"
    owner = json.loads(OWNER.read_text())
    assert owner["cluster"] == config["cluster"] and owner["zone"] == config["zone"] and owner["owner"] in config["nodes"]
    active = owner["owner"] == node
    assert sysctl("net.ipv4.ip_forward") == ("1" if active else "0")
    assert sysctl("net.ipv6.conf.all.forwarding") == "0"
    plan = firewall_plan(config, node, tuple(state["accept_mark"]), active)
    for name, specification in config["vnets"].items():
        rows = json.loads(run(["ip", "-j", "-4", "address", "show", "dev", name]).stdout)
        addresses = {item["local"] for row in rows for item in row.get("addr_info", [])}
        assert addresses == ({specification["gateway"]} if active else set()), "gateway ownership does not match"
        assert sysctl(f"net.ipv6.conf.{name}.disable_ipv6") == "1"
        assert sysctl(f"net.ipv6.conf.{name}.autoconf") == "0"
    for binary, families in plan.items():
        for name, rules in families.items():
            table, hook = HOOKS[name]
            expected_hook = ["-A", hook, "-m", "comment", "--comment", COMMENT, "-j", CHAINS[name]]
            current = [shlex.split(line) for line in run([binary, "-t", table, "-S", hook]).stdout.splitlines() if line.startswith("-A ")]
            assert current and current[0] == expected_hook, "owned hook is not first; revalidate after NetBird restart"
            chain_rules = [line for line in run([binary, "-t", table, "-S", CHAINS[name]]).stdout.splitlines() if line.startswith("-A ")]
            assert len(chain_rules) == len(rules) + 1, "unexpected owned chain contents"
            for rule in [*rules, ["-j", "RETURN"]]:
                run([binary, "-t", table, "-C", CHAINS[name], *rule])


def install_runtime(config):
    source = Path(__file__).parent
    RUNTIME.mkdir(parents=True, exist_ok=False)
    (RUNTIME / "lib").mkdir()
    for name in ("production-network.py", "apply-production-network.sh"):
        shutil.copyfile(source / name, RUNTIME / name)
        (RUNTIME / name).chmod(0o755)
    shutil.copyfile(source / "lib/production_network.py", RUNTIME / "lib/production_network.py")
    (RUNTIME / "lib/production_network.py").chmod(0o644)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    write_json(CONFIG, config)


def install_boot_unit():
    Path("/etc/systemd/system/" + UNIT).write_text(f'''[Unit]
Description=Production VNet gateway and host guards
Wants=network-online.target netbird.service pve-firewall.service
After=network-online.target netbird.service pve-cluster.service pve-firewall.service
Before=pve-guests.service
StartLimitIntervalSec=0

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=180s
ExecStart=/bin/bash {RUNTIME}/apply-production-network.sh reconcile --config {CONFIG} --apply
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
''')
    hook = Path("/etc/network/if-up.d/example-production-network")
    hook.write_text('''#!/usr/bin/env bash
set -euo pipefail
case "${IFACE:-}" in
  pinfra|pguest) systemctl --no-block restart example-production-network.service ;;
esac
''')
    hook.chmod(0o755)
    dependency = Path("/etc/systemd/system/pve-guests.service.d/example-production-network.conf")
    dependency.parent.mkdir(parents=True, exist_ok=True)
    dependency.write_text("[Unit]\nRequires=example-production-network.service\nAfter=example-production-network.service\n")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", UNIT])


def local_checks(config, node, state):
    assert hashes() == state["baseline_hashes"], "management/Corosync configuration changed"
    campus = config["nodes"][node]["campus"]
    assert run(["curl", "--noproxy", "*", "-fsS", "--connect-timeout", "5", "--max-time", "10", "--cacert", "/etc/pve/pve-root-ca.pem",
                "--resolve", f"{node}:8006:{campus}", f"https://{node}:8006/", "-o", "/dev/null", "-w", "%{http_code}"]).stdout == "200"
    if config["nodes"][node].get("bmc_address"):
        assert run(["curl", "--noproxy", "*", "-ksS", "--connect-timeout", "5", "--max-time", "10", "-o", "/dev/null", "-w", "%{http_code}",
                    "https://" + config["nodes"][node]["bmc_address"] + "/"]).stdout == "200", "BMC path failed"


def finish_successful_rollback(config, node, state):
    """Disarm the owned timer only after rollback verification has succeeded."""
    local_checks(config, node, state)
    stop_transient_unit(TIMER + ".timer")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="check", choices=("check", "prepare", "activate", "reconcile", "status", "selfcheck", "commit", "rollback"))
    default_config = CONFIG if Path(__file__).parent == RUNTIME else Path(__file__).parents[1] / "hosts/production/network.json"
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--operation-id")
    parser.add_argument("--proof", type=Path)
    parser.add_argument("--rollback-seconds", type=int, default=300)
    args = parser.parse_args()
    assert os.geteuid() == 0, "root is required"
    os.umask(0o077)
    lock_file = None
    if args.apply and args.mode != "selfcheck":
        descriptor = os.open("/run/lock/example-production-network.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        assert os.fstat(descriptor).st_uid == 0
        lock_file = os.fdopen(descriptor, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    node = socket.gethostname().split(".")[0]
    state = json.loads(STATE.read_text()) if STATE.exists() else None
    config = validate_config(state["config"] if args.mode == "rollback" and state else json.loads(args.config.read_text()))
    assert node in config["nodes"], "this script never operates on another host"
    if state and args.mode not in ("prepare", "rollback"):
        assert config == state["config"], "installed configuration changed outside this operation"
    if args.mode == "status":
        gateway = json.loads(OWNER.read_text()).get("owner") if OWNER.exists() else None
        print(json.dumps({"host": node, "state": state, "gateway_owner": gateway,
                          "gateway_role": "primary" if gateway == node else "standby", "ipv4_forwarding": sysctl("net.ipv4.ip_forward"),
                          "ipv6_forwarding": sysctl("net.ipv6.conf.all.forwarding"),
                          "mark_counters": run(["iptables", "-t", "mangle", "-nvxL", CHAINS["mangle"]], check=False).stdout,
                          "bridge_guard_counters": run(["nft", "list", "table", "bridge", TABLE], check=False).stdout,
                          "priority_guard_counters": run(["nft", "list", "table", "inet", INET_TABLE], check=False).stdout}, indent=2))
        return
    if args.mode == "selfcheck":
        assert args.apply and state and state["operation_id"] == args.operation_id
        write_json(BASE / "rollback-execution-proof.json", {"operation_id": args.operation_id, "host": node})
        return
    if args.mode in ("check", "prepare", "activate"):
        cluster_check(config, empty=True)
        profile = mesh_profile(config, node)
        mark = accept_mark()
        if args.mode in ("check", "prepare"):
            check_initial_routes(config)
            assert profile["mtu"] == 1280, "unexpected starting MTU"
            assert sysctl("net.ipv4.ip_forward") == sysctl("net.ipv6.conf.all.forwarding") == "0"
        print(json.dumps({"host": node, "mode": args.mode, "mesh_profile": profile, "accept_mark": mark, "apply": args.apply}))
    if not args.apply:
        if args.mode not in ("check", "prepare", "activate"):
            print(json.dumps({"host": node, "mode": args.mode, "apply": False, "state": state}))
        return
    if args.mode == "prepare":
        assert (state is None or state["phase"] == "rolled_back") and not RUNTIME.exists() and not CONFIG.exists(), "prior installation needs explicit reconciliation"
        assert args.backup_dir and args.backup_dir.is_absolute()
        protected(args.backup_dir)
        assert 60 <= args.rollback_seconds <= 900
        if "SSH_CONNECTION" in os.environ:
            assert os.environ["SSH_CONNECTION"].split()[2] == config["nodes"][node]["campus"], "change MTU through native SSH"
        for path in (Path("/etc/systemd/system/" + UNIT), Path("/etc/network/if-up.d/example-production-network"),
                     Path("/etc/systemd/system/pve-guests.service.d/example-production-network.conf")):
            assert not path.exists(), "unrelated runtime file already exists"
        for binary in ("iptables", "ip6tables"):
            for table in {item[0] for item in HOOKS.values()}:
                rules = run([binary, "-t", table, "-S"]).stdout
                assert not any(chain in rules for chain in CHAINS.values()), "owned chain name already exists"
        for family, table in (("bridge", TABLE), ("inet", INET_TABLE)):
            assert run(["nft", "list", "table", family, table], check=False).returncode != 0, "owned guard table already exists"
        BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
        protected(BASE)
        if state is not None:
            write_json(args.backup_dir / "previous-rolled-back-state.json", state)
        install_runtime(config)
        state = {"schema": 1, "node": node, "operation_id": uuid.uuid4().hex, "pending": True, "phase": "preparing",
                 "config": config, "accept_mark": list(mark), "old_mtu": profile["mtu"], "baseline_hashes": hashes(),
                 "baseline_bridge_nf": {key: sysctl(key) for key in ("net.bridge.bridge-nf-call-iptables", "net.bridge.bridge-nf-call-ip6tables")},
                 "backup_dir": str(args.backup_dir)}
        write_json(STATE, state)
        write_json(args.backup_dir / "production-network-before.json", state)
        wrapper = RUNTIME / "apply-production-network.sh"
        run(["systemd-run", "--unit", TIMER + "-selfcheck", "--on-active", "1s", "--timer-property", "AccuracySec=100ms", "/bin/bash", wrapper,
             "selfcheck", "--config", CONFIG, "--operation-id", state["operation_id"], "--apply"])
        proof_path = BASE / "rollback-execution-proof.json"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            proof_ok = proof_path.exists() and json.loads(proof_path.read_text())["operation_id"] == state["operation_id"]
            result = run(["systemctl", "show", TIMER + "-selfcheck.service", "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus"]).stdout
            if proof_ok and "ActiveState=inactive" in result and "Result=success" in result and "ExecMainStatus=0" in result:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("the rollback interpreter did not execute through its test timer")
        stop_transient_unit(TIMER + "-selfcheck.timer")
        stop_transient_unit(TIMER + "-selfcheck.service")
        run(["systemd-run", "--unit", TIMER, "--on-active", str(args.rollback_seconds) + "s", "--timer-property", "AccuracySec=1s",
             "/bin/bash", wrapper, "rollback", "--config", CONFIG, "--operation-id", state["operation_id"], "--apply"])
        plan = firewall_plan(config, node, mark, False)
        for family in plan.values():
            family["mangle"] = []
        install_chains(plan)
        install_priority_guard(config, node, mark, False)
        set_mesh_mtu(config, node, config["host_mtu"])
        wait_for_accept_mark(mark, state)
        install_chains(plan)
        assert sysctl("net.ipv4.ip_forward") == sysctl("net.ipv6.conf.all.forwarding") == "0"
        local_checks(config, node, state)
        state["phase"] = "prepared"
        write_json(STATE, state)
    elif args.mode in ("activate", "reconcile"):
        assert state and state["phase"] in ("prepared", "active"), "node is not prepared"
        if args.mode == "activate":
            assert state["pending"], "initial activation is already committed"
            assert run(["systemctl", "is-active", TIMER + ".timer"], check=False).returncode == 0, "rollback timer is not armed"
        reconcile(config, node, state)
        if args.mode == "activate":
            install_boot_unit()
        state["phase"] = "active"
        write_json(STATE, state)
        local_checks(config, node, state)
    elif args.mode == "commit":
        assert state and state["phase"] == "active" and args.proof
        proof = json.loads(args.proof.read_text())
        assert proof["operation_id"] == state["operation_id"] and proof["host"] == node
        assert 0 <= time.time() - proof["verified_at"] < 180, "controller proof is stale"
        assert all(proof[key] is True for key in ("native_ssh", "mesh_ssh", "native_https", "mesh_https"))
        if config["nodes"][node].get("bmc_address"):
            assert proof["bmc_https"] is True
        validate_current(config, node, state)
        local_checks(config, node, state)
        assert time.time() - proof["verified_at"] < 180, "controller proof expired during validation"
        run(["systemctl", "stop", TIMER + ".timer"])
        state["pending"] = False
        write_json(STATE, state)
    elif args.mode == "rollback":
        assert state and (args.operation_id is None or args.operation_id == state["operation_id"])
        if not state["pending"]:
            raise RuntimeError("committed networking requires a separate maintenance rollback")
        state["phase"] = "rolling_back"
        write_json(STATE, state)
        run(["systemctl", "disable", UNIT], check=False)
        run(["systemctl", "stop", "--no-block", UNIT], check=False)
        sysctl("net.ipv4.ip_forward", 0)
        sysctl("net.ipv6.conf.all.forwarding", 0)
        remove_gateways(config)
        set_mesh_mtu(config, node, state["old_mtu"])
        sysctl("net.ipv4.ip_forward", 0)
        sysctl("net.ipv6.conf.all.forwarding", 0)
        clear_chains()
        if not api("/cluster/resources", "--type", "vm"):
            for key, value in state["baseline_bridge_nf"].items():
                sysctl(key, value)
        for path in (Path("/etc/systemd/system/" + UNIT), Path("/etc/network/if-up.d/example-production-network"),
                     Path("/etc/systemd/system/pve-guests.service.d/example-production-network.conf")):
            path.unlink(missing_ok=True)
        run(["systemctl", "daemon-reload"])
        finish_successful_rollback(config, node, state)
        state["phase"] = "rolled_back"
        state["pending"] = False
        write_json(STATE, state)
        write_json(Path(state["backup_dir"]) / "production-network-rollback.json", state)
        CONFIG.unlink()
        for path in (RUNTIME / "production-network.py", RUNTIME / "apply-production-network.sh", RUNTIME / "lib/production_network.py"):
            path.unlink()
        (RUNTIME / "lib").rmdir()
        RUNTIME.rmdir()
        print("Node rollback complete; remove only the owned cluster SDN objects separately while both nodes are quorate.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"production-network: {error}", file=sys.stderr)
        sys.exit(1)
