#!/usr/bin/env python3
"""Read-only controller checks; successful receipts can commit armed node changes."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).parent / "lib"))
sys.dont_write_bytecode = True
from production_network import validate_config


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def ssh(node, command, *, mesh=None, campus=None):
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5", "-o", "ControlPath=none"]
    if mesh:
        argv += ["-o", "ProxyJump=none", "-o", "ProxyCommand=none", "-o", "Hostname=" + mesh,
                 "-o", "HostKeyAlias=" + campus]
    return run([*argv, node, shlex.join(command)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).parents[1] / "hosts/production/network.json")
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config = validate_config(json.loads(args.config.read_text()))
    assert args.ca_file.is_file(), "public cluster CA file is required"
    assert args.output_dir.is_dir(), "create the evidence directory before running"
    assert args.output_dir.stat().st_uid == os.geteuid() and args.output_dir.stat().st_mode & 0o777 == 0o700, "evidence directory must be private"
    results = {}
    for node, addresses in config["nodes"].items():
        peer = next(name for name in config["nodes"] if name != node)
        state = json.loads(ssh(node, ["/bin/bash", "/usr/local/libexec/example-production-network/apply-production-network.sh", "status"]))
        assert state["host"] == node and state["state"]["phase"] == "active"
        proof = {"host": node, "operation_id": state["state"]["operation_id"]}
        proof["native_ssh"] = ssh(node, ["hostname", "-s"]) == node
        proof["mesh_ssh"] = ssh(node, ["hostname", "-s"], mesh=addresses["mesh"], campus=addresses["campus"]) == node
        curl = ["curl", "--noproxy", "*", "-fsS", "--connect-timeout", "5", "--max-time", "10", "--cacert", "/etc/pve/pve-root-ca.pem",
                "--resolve", f"{node}:8006:{addresses['campus']}", f"https://{node}:8006/", "-o", "/dev/null", "-w", "%{http_code}"]
        proof["native_https"] = ssh(peer, curl) == "200"
        curl[curl.index("/etc/pve/pve-root-ca.pem")] = str(args.ca_file)
        curl[curl.index("--resolve") + 1] = f"{node}:8006:{addresses['mesh']}"
        proof["mesh_https"] = run(curl) == "200"
        if addresses.get("bmc_address"):
            proof["bmc_https"] = ssh(node, ["curl", "--noproxy", "*", "-ksS", "--connect-timeout", "5", "--max-time", "10", "-o", "/dev/null",
                                                "-w", "%{http_code}", "https://" + addresses["bmc_address"] + "/"]) == "200"
        assert all(value is True for key, value in proof.items() if key.endswith(("_ssh", "_https"))), "management check failed"
        proof["verified_at"] = time.time()
        results[node] = (proof, state)
    # Do not issue any receipt if either host failed.
    for node, (proof, state) in results.items():
        (args.output_dir / f"{node}-commit-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
        (args.output_dir / f"{node}-network-state.json").write_text(json.dumps(state, indent=2) + "\n")
    print("Both native and mesh management paths passed; guest-policy and rollback tests remain separate.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"verify-production-network: {error}", file=sys.stderr)
        sys.exit(1)
