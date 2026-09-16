#!/usr/bin/env python3
"""Read storage health using reviewed, hash-pinned portable diagnostic tools."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile


def snapshot(source, digest, destination):
    """Copy and verify before executing bytes outside operator-writable storage."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Expected a lowercase SHA256")
    if not Path(source).is_absolute():
        raise ValueError("Expected an absolute tool path")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as incoming:
        metadata = os.fstat(incoming.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 64 * 1024 * 1024:
            raise ValueError("Expected a regular executable of at most 64 MiB")
        data = incoming.read(64 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Tool SHA256 mismatch")
    if data[:4] != b"\x7fELF":
        raise ValueError("Expected an extracted Linux ELF binary")
    with destination.open("xb") as output:
        output.write(data)
    destination.chmod(0o700)
    return str(destination)


def run(argv, directory):
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C",
           "LC_ALL": "C", "HOME": str(directory)}
    try:
        process = subprocess.run(argv, cwd=directory, env=env, capture_output=True,
                                 text=True, errors="replace", timeout=90)
        output = process.stdout.strip()
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            pass
        return {"arguments": [Path(argv[0]).name, *argv[1:]],
                "exit_code": process.returncode, "output": output,
                "stderr": process.stderr.strip()}
    except subprocess.TimeoutExpired as error:
        def decoded(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
        return {"arguments": [Path(argv[0]).name, *argv[1:]], "error": str(error),
                "timed_out": True, "partial_output": decoded(error.stdout),
                "partial_stderr": decoded(error.stderr)}
    except OSError as error:
        return {"arguments": [Path(argv[0]).name, *argv[1:]], "error": str(error)}


def collect(smartctl, raid_cli, directory):
    result = {"host": socket.gethostname(),
              "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "health_assessment": "REQUIRES_REVIEW",
              "operations": "Read-only controller, virtual/physical disk and SMART queries; no self-tests",
              "smart_version": run([smartctl, "--version"], directory)}
    result["raid"] = [run([raid_cli, *arguments], directory) for arguments in (
        ["/call", "show", "all", "noforeign", "J"],
        ["/call/vall", "show", "all", "J"],
        ["/call/eall/sall", "show", "all", "J"],
        ["/call/bbu", "show", "all", "J"],
        ["/call/cv", "show", "all", "J"],
    )]
    # MegaRAID SMART discovery may create ioctl device nodes; PERC supplies disk health.
    result["smart_scope"] = "NVMe only; physical RAID drive health comes from PERC"
    result["smart"] = []
    for name in ("/dev/nvme0n1", "/dev/nvme1n1"):
        if not Path(name).exists():
            result["smart"].append({"device": name, "error": "Expected NVMe namespace is absent"})
            continue
        result["smart"].append(run([smartctl, "--all", "--json", "--device", "nvme", name], directory))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    for name in ("smartctl", "raid-cli"):
        parser.add_argument(f"--{name}", required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname().split(".")[0] != args.expected_host:
        parser.error("Root privileges and the exact host name are required")
    os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="pickle-storage-check-", dir="/run") as temporary:
            directory = Path(temporary)
            smartctl = snapshot(args.smartctl, args.smartctl_sha256, directory / "smartctl")
            raid_cli = snapshot(args.raid_cli, args.raid_cli_sha256, directory / "perccli64")
            result = collect(smartctl, raid_cli, directory)
            result["tool_sha256"] = {"smartctl": args.smartctl_sha256, "raid_cli": args.raid_cli_sha256}
            print(json.dumps(result, indent=2, ensure_ascii=False))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Storage collection refused: {error}\n")


if __name__ == "__main__":
    main()
