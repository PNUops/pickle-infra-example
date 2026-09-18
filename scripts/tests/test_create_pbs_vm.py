#!/usr/bin/env python3
"""Regression tests for the PBS VM firmware and seed attachment boundary."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("pbs_firmware", ROOT / "scripts/lib/pbs_firmware.py")
assert SPEC and SPEC.loader
firmware = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(firmware)


class PBSFirmwareTest(unittest.TestCase):
    def test_requires_explicit_readonly_loader_and_vars_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader, template = root / "CODE.fd", root / "VARS.fd"
            loader.touch()
            template.touch()
            capabilities = root / "capabilities.xml"
            capabilities.write_text(
                f"<domainCapabilities><machine>pc-i440fx-test</machine><arch>x86_64</arch><os>"
                f"<enum name='firmware'><value>efi</value></enum><loader supported='yes'><value>{loader}</value>"
                "<enum name='type'><value>pflash</value></enum><enum name='readonly'><value>yes</value></enum>"
                "</loader></os></domainCapabilities>"
            )
            descriptor_dir = root / "firmware"
            descriptor_dir.mkdir()
            (descriptor_dir / "uefi.json").write_text(
                '{"interface-types":["uefi"],"mapping":{"device":"flash",'
                f'"executable":{{"filename":"{loader}"}},"nvram-template":{{"filename":"{template}"}}}},'
                '"targets":[{"architecture":"x86_64","machines":["pc-i440fx-*"]}]}'
            )
            self.assertEqual(
                firmware.resolve_firmware(str(capabilities), str(loader), str(template), (descriptor_dir,)),
                (str(loader), str(template)),
            )

    def test_rejects_unadvertised_or_mismatched_firmware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader, template = root / "CODE.fd", root / "VARS.fd"
            loader.touch()
            template.touch()
            capabilities = root / "capabilities.xml"
            capabilities.write_text(
                "<domainCapabilities><machine>pc-i440fx-test</machine><arch>x86_64</arch><os>"
                "<enum name='firmware'><value>efi</value></enum><loader supported='yes'>"
                "<value>/different</value><enum name='type'><value>pflash</value></enum>"
                "<enum name='readonly'><value>yes</value></enum></loader></os></domainCapabilities>"
            )
            with self.assertRaises(ValueError):
                firmware.resolve_firmware(str(capabilities), str(loader), str(template), (root,))

    def test_generated_domain_keeps_selection_and_disables_tpm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader, template = root / "CODE.fd", root / "VARS.fd"
            domain = root / "domain.xml"
            domain.write_text(
                f"<domain><os><loader readonly='yes' type='pflash'>{loader}</loader>"
                f"<nvram template='{template}'>{root / 'guest_VARS.fd'}</nvram></os><devices/></domain>"
            )
            firmware.validate_domain(str(domain), str(loader), str(template))
            domain.write_text(domain.read_text().replace("<devices/>", "<devices><tpm/></devices>"))
            with self.assertRaises(ValueError):
                firmware.validate_domain(str(domain), str(loader), str(template))

    def test_rejects_non_uefi_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader, template = root / "CODE.fd", root / "VARS.fd"
            loader.touch()
            template.touch()
            capabilities = root / "capabilities.xml"
            capabilities.write_text(
                f"<domainCapabilities><machine>pc-i440fx-test</machine><arch>x86_64</arch><os>"
                f"<enum name='firmware'><value>efi</value></enum><loader supported='yes'><value>{loader}</value>"
                "<enum name='type'><value>pflash</value></enum><enum name='readonly'><value>yes</value></enum>"
                "</loader></os></domainCapabilities>"
            )
            (root / "not-uefi.json").write_text(
                '{"interface-types":["bios"],"mapping":{"device":"flash",'
                f'"executable":{{"filename":"{loader}"}},"nvram-template":{{"filename":"{template}"}}}},'
                '"targets":[{"architecture":"x86_64","machines":["pc-i440fx-*"]}]}'
            )
            with self.assertRaises(ValueError):
                firmware.resolve_firmware(str(capabilities), str(loader), str(template), (root,))

    def test_creator_uses_uefi_and_readonly_virtio_seed(self) -> None:
        source = (ROOT / "scripts/create-pbs-vm.sh").read_text()
        self.assertIn("--uefi-loader", source)
        self.assertIn("--uefi-vars-template", source)
        self.assertIn('loader.readonly=yes,loader.type=pflash,nvram.template=', source)
        self.assertIn("--tpm none", source)
        self.assertIn("format=raw,bus=virtio,readonly=on,serial=PBS_SEED", source)
        self.assertNotIn("seed.iso,device=cdrom", source)
        self.assertIn("--dry-run --print-xml", source)
        self.assertIn('qemu-img convert -f qcow2 -O qcow2 "$boot_image" "$boot_disk"', source)
        self.assertNotIn('qemu-img resize "$boot_image"', source)
        self.assertIn("domain이 이미 있습니다", source)
        self.assertIn("전용 디렉터리가 비어 있지 않습니다", source)


if __name__ == "__main__":
    unittest.main()
