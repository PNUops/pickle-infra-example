import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import re

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/apply-os-catalog.sh'

class CatalogSchemaGuardTests(unittest.TestCase):
    def run_mode(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            trace = temp / 'trace'
            pct = temp / 'pct'
            qm = temp / 'qm'
            pct.write_text("""#!/bin/sh
set -eu
printf 'pct %s\n' "$*" >> "$TRACE"
if [ "$1" = config ]; then echo 'hostname: pickle-app'; exit 0; fi
if [ "$1" = exec ]; then
  input=$(cat)
  printf '%s\n' "$input" >> "$TRACE.sql"
  if [ ! -e "$TRACE.schema" ]; then
    : > "$TRACE.schema"
    printf '%s\n' "$SCHEMA_MODE"
  elif printf '%s' "$input" | grep -q 'insert into os_images'; then
    printf 't|DISABLED\n'
  fi
fi
""")
            qm.write_text("""#!/bin/sh
set -eu
printf 'qm %s\n' "$*" >> "$TRACE"
case "$2" in
  1001) echo 'name: ubuntu-2404-template' ;;
  1002) echo 'name: ubuntu-2604-template' ;;
  1003) echo 'name: ubuntu-2204-template' ;;
  1004) echo 'name: debian-13-template' ;;
  1005) echo 'name: debian-12-template' ;;
  1006) echo 'name: rocky-10-template' ;;
  1007) echo 'name: rocky-9-template' ;;
esac
""")
            pct.chmod(0o700)
            qm.chmod(0o700)
            environment = {**os.environ, 'PATH': f'{temp}:{os.environ["PATH"]}',
                           'TRACE': str(trace), 'SCHEMA_MODE': mode,
                           'PICKLE_APP_CTID': '101', 'PICKLE_NODE': 'pve1'}
            result = subprocess.run([str(SCRIPT)], env=environment, text=True,
                                    capture_output=True, check=False)
            calls = trace.read_text().splitlines() if trace.exists() else []
            sql = trace.with_suffix('.sql').read_text() if trace.with_suffix('.sql').exists() else ''
            return result, calls, sql

    def test_legacy_global_mode_runs_existing_writer_path(self):
        result, calls, sql = self.run_mode('legacy-global')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(line.startswith('qm config') for line in calls), 7)
        self.assertEqual(sum(line.startswith('pct exec') for line in calls), 9)
        self.assertEqual(len(re.findall(r'insert into os_images', sql, re.IGNORECASE)), 7)

    def test_node_scoped_mode_refuses_before_host_or_db_write(self):
        result, calls, sql = self.run_mode('node-scoped')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('scripts/register-image.py', result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith('pct exec'))
        self.assertNotRegex(sql.lower(), r'\b(insert|update|delete|truncate|alter|create|drop)\b')

    def test_both_mode_refuses_before_host_or_db_write(self):
        result, calls, sql = self.run_mode('both')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('legacy global', result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith('pct exec'))
        self.assertNotRegex(sql.lower(), r'\b(insert|update|delete|truncate|alter|create|drop)\b')

    def test_neither_mode_refuses_before_host_or_db_write(self):
        result, calls, sql = self.run_mode('neither')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('지원되는 이미지 unique', result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith('pct exec'))
        self.assertNotRegex(sql.lower(), r'\b(insert|update|delete|truncate|alter|create|drop)\b')

if __name__ == '__main__':
    unittest.main()
