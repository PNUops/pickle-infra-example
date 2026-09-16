#!/usr/bin/env python3
"""Create or remove the exact production VXLAN objects through the PVE API."""
import argparse
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).parent / "lib"))
sys.dont_write_bytecode = True
from production_network import validate_config, validate_sdn_inventory

OWNER_FILE = Path("/etc/pve/priv/example-production-network-owner.json")


def api(method, path, *arguments):
    process = subprocess.run(["pvesh", method, path, *map(str, arguments), "--output-format", "json"],
                             capture_output=True, text=True, timeout=45)
    if process.returncode:
        raise RuntimeError(f"PVE {method} {path}: {process.stderr.strip()}")
    return json.loads(process.stdout) if process.stdout.strip() else None


def check_cluster(config, require_witness=True):
    status = api("get", "/cluster/status")
    cluster = [row for row in status if row.get("type") == "cluster"]
    nodes = [row for row in status if row.get("type") == "node"]
    assert len(cluster) == 1 and cluster[0]["name"] == config["cluster"] and cluster[0]["quorate"] == 1
    assert {row["name"] for row in nodes} == set(config["nodes"]) and all(row["online"] for row in nodes)
    assert api("get", "/cluster/resources", "--type", "vm") == [], "guest inventory is not empty"
    assert api("get", "/cluster/ha/resources") == [], "HA resources are present"
    quorum = subprocess.run(["pvecm", "status"], capture_output=True, text=True, check=True).stdout
    if require_witness:
        assert re.search(r"Expected votes:\s+3\b", quorum) and re.search(r"Total votes:\s+3\b", quorum) and "Qdevice" in quorum, "the witness must be voting before initial SDN changes"
    for endpoint in ("controllers", "fabrics/fabric"):
        assert api("get", "/cluster/sdn/" + endpoint) == [], "unrelated SDN configuration exists"
    zones, vnets = api("get", "/cluster/sdn/zones"), api("get", "/cluster/sdn/vnets")
    validate_sdn_inventory(config, zones, vnets)
    for vnet in vnets:
        assert api("get", f"/cluster/sdn/vnets/{vnet['vnet']}/subnets") == [], "SDN subnet/gateway is not owned here"
    return zones, vnets


def network_tasks(node):
    return {row["upid"]: row for row in api("get", f"/nodes/{node}/tasks", "--typefilter", "srvreload", "--source", "all", "--limit", "50")
            if row.get("id") == "networking"}


def active_network_tasks(node):
    return {row["upid"] for row in api("get", f"/nodes/{node}/tasks", "--typefilter", "srvreload", "--source", "active", "--limit", "50")
            if row.get("id") == "networking"}


def wait_apply(config, upid, baseline):
    parent_node = upid.split(":")[1]
    deadline = time.monotonic() + 120
    children = {}
    while time.monotonic() < deadline:
        parent = api("get", f"/nodes/{parent_node}/tasks/{upid}/status")
        if parent.get("status") == "stopped":
            assert parent.get("exitstatus") == "OK", "SDN parent task failed"
        for node in config["nodes"]:
            assert not (active_network_tasks(node) & baseline[node]), "a baseline networking task is still running"
            fresh = set(network_tasks(node)) - baseline[node]
            assert len(fresh) <= 1, "multiple concurrent networking tasks; ownership is ambiguous"
            if fresh:
                child = next(iter(fresh))
                result = api("get", f"/nodes/{node}/tasks/{child}/status")
                if result.get("status") == "stopped":
                    assert result.get("exitstatus") == "OK", f"network reload failed on {node}"
                    children[node] = child
        if parent.get("status") == "stopped" and len(children) == len(config["nodes"]):
            assert time.monotonic() <= deadline, "SDN completion exceeded the deadline"
            print(json.dumps({"parent": upid, "node_tasks": children}))
            return
        time.sleep(1)
    raise RuntimeError("SDN task completion was not established before the deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).parents[1] / "hosts/production/network.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    config = validate_config(json.loads(args.config.read_text()))
    assert os.geteuid() == 0 and socket.gethostname().split(".")[0] == config["gateway_owner"], "run on the initial gateway host as root"
    zones, vnets = check_cluster(config, require_witness=not args.rollback)
    if OWNER_FILE.exists():
        owner = json.loads(OWNER_FILE.read_text())
        assert owner == {"schema": 1, "cluster": config["cluster"], "zone": config["zone"], "owner": config["gateway_owner"]}, "gateway ownership changed; do not overwrite it"
    if args.rollback:
        assert OWNER_FILE.exists(), "no ownership record for rollback"
    print(json.dumps({"operation": "remove" if args.rollback else "create", "zone": config["zone"], "vnets": config["vnets"], "apply": args.apply}))
    if not args.apply:
        return
    # This rejects pending changes; it never steals or force-releases another lock.
    token = api("create", "/cluster/sdn/lock")
    committed = False
    claimed = False
    try:
        zones, vnets = check_cluster(config, require_witness=not args.rollback)
        if not args.rollback and not OWNER_FILE.exists():
            record = {"schema": 1, "cluster": config["cluster"], "zone": config["zone"], "owner": config["gateway_owner"]}
            OWNER_FILE.write_text(json.dumps(record) + "\n")
            claimed = True
        if args.rollback:
            for name in config["vnets"]:
                if any(row["vnet"] == name for row in vnets):
                    api("delete", "/cluster/sdn/vnets/" + name, "--lock-token", token)
            if zones:
                api("delete", "/cluster/sdn/zones/" + config["zone"], "--lock-token", token)
        else:
            if not zones:
                api("create", "/cluster/sdn/zones", "--zone", config["zone"], "--type", "vxlan",
                    "--nodes", ",".join(config["nodes"]), "--peers", ",".join(node["mesh"] for node in config["nodes"].values()),
                    "--mtu", config["guest_mtu"], "--lock-token", token)
            for name, vnet in config["vnets"].items():
                if not any(row["vnet"] == name for row in vnets):
                    api("create", "/cluster/sdn/vnets", "--vnet", name, "--zone", config["zone"], "--tag", vnet["vni"], "--lock-token", token)
        assert all(not active_network_tasks(node) for node in config["nodes"]), "a networking reload is already running"
        baseline = {node: set(network_tasks(node)) for node in config["nodes"]}
        assert all(not active_network_tasks(node) for node in config["nodes"]), "networking reload raced with the baseline"
        upid = api("set", "/cluster/sdn", "--lock-token", token, "--release-lock", "0")
        committed = True
        wait_apply(config, upid, baseline)
        if args.rollback:
            OWNER_FILE.unlink()
        else:
            record = {"schema": 1, "cluster": config["cluster"], "zone": config["zone"], "owner": config["gateway_owner"]}
            temporary = OWNER_FILE.with_suffix(".new")
            temporary.write_text(json.dumps(record) + "\n")
            temporary.replace(OWNER_FILE)
    except Exception:
        if not committed:
            api("create", "/cluster/sdn/rollback", "--lock-token", token, "--release-lock", "0")
            if claimed:
                OWNER_FILE.unlink()
        raise
    finally:
        api("delete", "/cluster/sdn/lock", "--lock-token", token)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"production-sdn: {error}", file=sys.stderr)
        sys.exit(1)
