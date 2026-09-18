#!/usr/bin/env python3
"""Validate an explicit UEFI loader/vars pair against libvirt host metadata."""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


DEFAULT_DESCRIPTOR_DIRS = (Path("/usr/share/qemu/firmware"), Path("/etc/qemu/firmware"))


def _enum_values(parent: ET.Element, name: str) -> set[str]:
    enum = parent.find(f"enum[@name='{name}']")
    return {node.text.strip() for node in enum.findall("value") if node.text} if enum is not None else set()


def resolve_firmware(
    domcaps_path: str,
    loader_path: str,
    vars_path: str,
    descriptor_dirs: tuple[Path, ...] = DEFAULT_DESCRIPTOR_DIRS,
) -> tuple[str, str]:
    root = ET.parse(domcaps_path).getroot()
    if root.tag != "domainCapabilities":
        raise ValueError("virsh did not produce domain capabilities XML")
    arch = (root.findtext("arch") or "").strip()
    machine = (root.findtext("machine") or "").strip()
    os_node = root.find("os")
    loader_caps = os_node.find("loader") if os_node is not None else None
    if os_node is None or "efi" not in _enum_values(os_node, "firmware"):
        raise ValueError("domain capabilities do not advertise EFI firmware")
    if loader_caps is None or loader_caps.get("supported") != "yes":
        raise ValueError("domain capabilities do not support a firmware loader")
    advertised = {node.text.strip() for node in loader_caps.findall("value") if node.text}
    if loader_path not in advertised:
        raise ValueError("selected UEFI loader is not advertised by domain capabilities")
    if "pflash" not in _enum_values(loader_caps, "type") or "yes" not in _enum_values(loader_caps, "readonly"):
        raise ValueError("domain capabilities do not support a read-only pflash loader")

    paths = (loader_path, vars_path)
    for path in paths:
        if not os.path.isabs(path) or not os.path.isfile(path):
            raise ValueError(f"UEFI firmware path is not an existing absolute file: {path}")

    for directory in descriptor_dirs:
        for descriptor_path in sorted(directory.glob("*.json")):
            try:
                descriptor = json.loads(descriptor_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            mapping = descriptor.get("mapping", {})
            executable = mapping.get("executable", {}).get("filename")
            template = mapping.get("nvram-template", {}).get("filename")
            if (
                "uefi" not in descriptor.get("interface-types", [])
                or mapping.get("device") != "flash"
                or executable != loader_path
                or template != vars_path
            ):
                continue
            for target in descriptor.get("targets", []):
                patterns = target.get("machines", [])
                if target.get("architecture") == arch and any(fnmatch.fnmatchcase(machine, pattern) for pattern in patterns):
                    return paths
    raise ValueError("selected loader and vars template are not a supported firmware descriptor pair")


def _validate_domain_root(root: ET.Element, loader_path: str, vars_path: str) -> None:
    os_node = root.find("os")
    if root.tag != "domain" or os_node is None:
        raise ValueError("virt-install did not produce domain OS XML")

    loader = os_node.find("loader")
    nvram = os_node.find("nvram")
    if loader is None or (loader.text or "").strip() != loader_path:
        raise ValueError("virt-install did not preserve the selected UEFI loader")
    if loader.get("type") != "pflash" or loader.get("readonly") != "yes":
        raise ValueError("UEFI loader must be read-only pflash")
    if nvram is None or nvram.get("template") != vars_path:
        raise ValueError("virt-install did not preserve the selected vars template")
    if root.find("./devices/tpm") is not None:
        raise ValueError("virt-install added an unrequested TPM device")


def validate_domain(xml_path: str, loader_path: str, vars_path: str) -> None:
    _validate_domain_root(ET.parse(xml_path).getroot(), loader_path, vars_path)


def validate_domain_xml(xml: str, loader_path: str, vars_path: str) -> None:
    _validate_domain_root(ET.fromstring(xml), loader_path, vars_path)


def main() -> int:
    if len(sys.argv) < 5 or sys.argv[1] not in {"preflight", "domain"}:
        print("usage: pbs_firmware.py preflight DOMCAPS LOADER VARS [DESCRIPTOR_DIR ...] | domain DOMAIN_XML LOADER VARS", file=sys.stderr)
        return 2
    try:
        if sys.argv[1] == "preflight":
            directories = tuple(Path(path) for path in sys.argv[5:]) or DEFAULT_DESCRIPTOR_DIRS
            loader, template = resolve_firmware(sys.argv[2], sys.argv[3], sys.argv[4], directories)
            print(loader)
            print(template)
        else:
            if len(sys.argv) != 5:
                raise ValueError("domain validation takes exactly three arguments")
            validate_domain(sys.argv[2], sys.argv[3], sys.argv[4])
    except (ET.ParseError, OSError, ValueError) as exc:
        print(f"pbs firmware: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
