"""Observe and validate bounded Linux hardware facts without inventory defaults."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import time

from health_result_transport import bounded_process

MAX_TEXT = 256
MAX_CPUS = 65536
MAX_GPUS = 16
MAX_BYTES = 65536
ASSET_FIELDS = {'displayModel', 'cpuModel', 'cores', 'threads', 'memoryMiB', 'gpus'}
GPU_FIELDS = {'model', 'count', 'dedicatedMemoryMiB', 'unifiedMemoryMiB'}


class HardwareError(ValueError):
    """Fixed reasons never contain local commands, credentials or raw output."""


def text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise HardwareError('HARDWARE_TEXT_INVALID')
    return value.strip()


def positive(value, maximum=9007199254740991):
    if type(value) is not int or not 1 <= value <= maximum:
        raise HardwareError('HARDWARE_NUMBER_INVALID')
    return value


def validate_asset(value):
    if not isinstance(value, dict) or set(value) != ASSET_FIELDS:
        raise HardwareError('HARDWARE_ASSET_INVALID')
    result = {key: text(value[key]) for key in ('displayModel', 'cpuModel')}
    result.update({key: positive(value[key], MAX_CPUS) for key in ('cores', 'threads')})
    if result['cores'] > result['threads']:
        raise HardwareError('HARDWARE_CPU_INVALID')
    result['memoryMiB'] = positive(value['memoryMiB'])
    gpus = value['gpus']
    if not isinstance(gpus, list) or len(gpus) > MAX_GPUS:
        raise HardwareError('HARDWARE_GPU_INVALID')
    result['gpus'] = []
    for gpu in gpus:
        if not isinstance(gpu, dict) or set(gpu) != GPU_FIELDS:
            raise HardwareError('HARDWARE_GPU_INVALID')
        item = {'model': text(gpu['model']), 'count': positive(gpu['count'], MAX_GPUS)}
        for key in ('dedicatedMemoryMiB', 'unifiedMemoryMiB'):
            item[key] = None if gpu[key] is None else positive(gpu[key])
        result['gpus'].append(item)
    if sum(gpu['count'] for gpu in result['gpus']) > MAX_GPUS:
        raise HardwareError('HARDWARE_GPU_INVALID')
    return result


def unavailable(now):
    return {'version': 1, 'observed_at': now, 'status': 'UNAVAILABLE'}


def validate_observation(value, now, max_age=900):
    if (not isinstance(value, dict) or value.get('version') != 1
            or type(value.get('version')) is not int
            or value.get('status') not in ('OBSERVED', 'UNAVAILABLE')):
        raise HardwareError('HARDWARE_OBSERVATION_INVALID')
    expected = {'version', 'observed_at', 'status'}
    if value['status'] == 'OBSERVED':
        expected.add('asset')
    stamp = value.get('observed_at')
    if (set(value) != expected or type(stamp) not in (int, float)
            or not -30 <= now - stamp <= max_age):
        raise HardwareError('HARDWARE_OBSERVATION_STALE_OR_INVALID')
    result = {key: value[key] for key in ('version', 'observed_at', 'status')}
    if value['status'] == 'OBSERVED':
        result['asset'] = validate_asset(value['asset'])
    return result


def read(path, maximum=MAX_BYTES):
    with Path(path).open('rb') as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise HardwareError('HARDWARE_INPUT_TOO_LARGE')
    return value.decode('utf-8').strip('\x00\n ')


def cpu_set(value):
    result = set()
    if not value or len(value) > MAX_BYTES:
        raise HardwareError('HARDWARE_CPU_INVALID')
    for part in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', part):
            raise HardwareError('HARDWARE_CPU_INVALID')
        start, _, end = part.partition('-')
        start, end = int(start), int(end or start)
        if not 0 <= start <= end < MAX_CPUS:
            raise HardwareError('HARDWARE_CPU_INVALID')
        result.update(range(start, end + 1))
    return result


def cpu_models(value):
    found = []
    def visit(item):
        if isinstance(item, dict):
            if item.get('field', '').rstrip(':') == 'Model name' and item.get('data'):
                name = text(item['data'])
                if name not in found:
                    found.append(name)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return text(' / '.join(found))


def pci_id(value):
    match = re.fullmatch(r'([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])', value)
    if not match:
        raise HardwareError('HARDWARE_PCI_INVALID')
    return f'{int(match[1], 16):04x}:{match[2].lower()}:{match[3].lower()}.{match[4]}'


def probe(*, sys_root=Path('/sys'), proc_root=Path('/proc'), runner=bounded_process):
    cpu_root = sys_root / 'devices/system/cpu'
    present = cpu_set(read(cpu_root / 'present'))
    groups = {tuple(sorted(cpu_set(read(cpu_root / f'cpu{cpu}/topology/thread_siblings_list'))
                           & present)) for cpu in present}
    if not present or not groups or () in groups:
        raise HardwareError('HARDWARE_CPU_INVALID')
    model = cpu_models(json.loads(runner(['lscpu', '-J'], timeout=3, maximum=MAX_BYTES)))
    memory = dict(line.split(':', 1) for line in read(proc_root / 'meminfo').splitlines() if ':' in line)
    match = re.fullmatch(r'\s*(\d+) kB', memory.get('MemTotal', ''))
    if not match:
        raise HardwareError('HARDWARE_MEMORY_INVALID')
    memory_mib = positive(int(match[1]) // 1024)
    product_root = sys_root / 'class/dmi/id'
    product = text(read(product_root / 'product_name'))
    try:
        vendor = text(read(product_root / 'sys_vendor'))
    except (OSError, HardwareError):
        vendor = ''
    display = text(f'{vendor} {product}'.strip() if vendor and vendor not in product else product)

    pci_root = sys_root / 'bus/pci/devices'
    devices = list(pci_root.iterdir())
    if len(devices) > 4096:
        raise HardwareError('HARDWARE_PCI_INVALID')
    gpu_ids = []
    for device in devices:
        if int(read(device / 'class'), 16) >> 16 == 3:
            vendor_id = int(read(device / 'vendor'), 16)
            if vendor_id not in (0x102b, 0x1a03):
                gpu_ids.append(pci_id(device.name))
    if len(gpu_ids) > MAX_GPUS:
        raise HardwareError('HARDWARE_GPU_INVALID')
    names = {}
    if gpu_ids:
        output = runner(['lspci', '-Dmm'], timeout=3, maximum=MAX_BYTES).decode('utf-8')
        for line in output.splitlines():
            parts = shlex.split(line)
            if len(parts) >= 4:
                names[pci_id(parts[0])] = text(parts[3])
    nvml = {}
    if gpu_ids:
        try:
            output = runner(['nvidia-smi', '--query-gpu=pci.bus_id,name,memory.total',
                             '--format=csv,noheader,nounits'], timeout=3, maximum=MAX_BYTES).decode('utf-8')
            for line in output.splitlines():
                parts = [item.strip() for item in line.split(',')]
                if len(parts) != 3:
                    raise HardwareError('HARDWARE_NVML_INVALID')
                raw = parts[2]
                memory_value = positive(int(raw)) if raw.isdigit() else None
                nvml[pci_id(parts[0])] = (text(parts[1]), memory_value)
        except Exception:
            # PCI enumeration remains authoritative when a GPU is assigned to a guest.
            nvml = {}
    gpus = []
    for address in sorted(gpu_ids):
        if address not in nvml and address not in names:
            raise HardwareError('HARDWARE_GPU_MODEL_UNAVAILABLE')
        name, vram = nvml.get(address, (names.get(address), None))
        gpus.append({'model': name, 'count': 1, 'dedicatedMemoryMiB': vram,
                     'unifiedMemoryMiB': None})
    return validate_asset({'displayModel': display, 'cpuModel': model, 'cores': len(groups),
                           'threads': len(present), 'memoryMiB': memory_mib, 'gpus': gpus})


def observe(*, now=None, observer=probe):
    stamp = time.time() if now is None else now
    try:
        return {'version': 1, 'observed_at': stamp, 'status': 'OBSERVED', 'asset': observer()}
    except Exception:
        return unavailable(stamp)
