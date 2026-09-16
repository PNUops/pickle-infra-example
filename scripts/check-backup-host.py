#!/usr/bin/env python3
"""Collect root-readable disk health without installing tools or changing disks."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess


def run(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
        output = result.stdout.strip()
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            pass
        return {"argv": argv, "exit_code": result.returncode, "output": output,
                "stderr": result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"argv": argv, "error": str(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname().split(".")[0] != args.expected_host:
        parser.error("root privileges and the exact host name are required")
    result = {"host": socket.gethostname(),
              "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "mutations": "none", "health_assessment": "operator_review_required",
              "raid_health": "UNKNOWN"}
    result["disks"] = run(["lsblk", "--json", "--bytes", "--output", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT,ROTA,MODEL"])
    result["filesystems"] = [run(["findmnt", "--json", "--target", target, "--output", "SOURCE,TARGET,FSTYPE,OPTIONS"])
                             for target in ("/", "/home")]
    result["capacity"] = run(["df", "-B1", "/", "/home"])
    result["mdstat"] = Path("/proc/mdstat").read_text() if Path("/proc/mdstat").exists() else None
    result["memory"] = run(["free", "-b"])
    result["pci_storage"] = run(["lspci", "-nn", "-d", "::0104"])
    result["libvirt_domains"] = run(["virsh", "-c", "qemu:///system", "list", "--all"])
    result["docker"] = run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    result["smart"] = []
    smartctl = shutil.which("smartctl")
    if smartctl:
        scan = run([smartctl, "--scan-open", "--json"])
        result["smart_scan"] = scan
        output = scan.get("output", {})
        devices = output.get("devices", []) if isinstance(output, dict) else []
        for device in devices:
            name, kind = device.get("name"), device.get("type")
            if not isinstance(name, str) or not name.startswith("/dev/"):
                continue
            command = [smartctl, "--health", "--attributes", "--json"]
            if isinstance(kind, str):
                command.extend(["--device", kind])
            command.append(name)
            result["smart"].append(run(command))
    else:
        result["smart_unavailable"] = "smartmontools is absent; no package was installed"
    result["raid_controllers"] = []
    candidates = ["perccli64", "perccli", "storcli64", "storcli",
                  "/opt/MegaRAID/perccli/perccli64", "/opt/MegaRAID/storcli/storcli64"]
    seen = set()
    for candidate in candidates:
        path = shutil.which(candidate) if "/" not in candidate else candidate
        if not path or not os.access(path, os.X_OK):
            continue
        resolved = str(Path(path).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        result["raid_controllers"].append(run([path, "/call", "show", "all", "J"]))
    if not result["raid_controllers"]:
        result["raid_health_unavailable"] = "No existing PERC/StorCLI tool found; RAID level, physical disks and cache protection remain unverified"
    else:
        result["raid_health"] = "COLLECTED_REQUIRES_REVIEW"
    result["packages"] = run(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n",
                              "smartmontools", "nvme-cli", "libvirt-daemon-system", "qemu-system-x86"])
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
