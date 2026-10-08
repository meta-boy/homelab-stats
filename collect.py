#!/usr/bin/env python3
"""Collect homelab stats and publish them as stats.json for anurag.wtf."""

import hashlib
import http.client
import json
import math
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path(os.environ.get('LABSTATS_CONFIG', '/etc/labstats/config.json'))
REPO = Path(os.environ.get('LABSTATS_REPO', '/var/lib/labstats/repo'))
KEY = Path('/etc/labstats/id_ed25519')

# Certificates are self-signed, so trust comes from the pinned fingerprint instead.
UNVERIFIED = ssl._create_unverified_context()


def installed_gb(total_bytes: int) -> int:
    # The kernel sees less than what is installed (firmware and iGPU reservations), so round up
    # to the module size people actually quote: whole GB for small boards, multiples of 4 above.
    gib = total_bytes / 2**30
    return math.ceil(gib) if gib < 4 else math.ceil(gib / 4) * 4


def cpu_name(model: str) -> str:
    return model.split(' with ')[0].strip()


def pve_get(machine: dict, path: str):
    conn = http.client.HTTPSConnection(machine['host'], machine['port'], context=UNVERIFIED, timeout=20)
    try:
        conn.connect()
        fingerprint = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
        if fingerprint != machine['fingerprint']:
            raise RuntimeError(f"{machine['name']}: certificate fingerprint mismatch")
        conn.request('GET', f'/api2/json{path}', headers={'Authorization': f"PVEAPIToken={machine['token']}"})
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f"{machine['name']}: {path} responded {response.status}")
        return json.loads(body)['data']
    finally:
        conn.close()


def collect_pve(machine: dict) -> dict:
    node = pve_get(machine, '/nodes')[0]['node']
    status = pve_get(machine, f'/nodes/{node}/status')
    guests = pve_get(machine, f'/nodes/{node}/lxc') + pve_get(machine, f'/nodes/{node}/qemu')
    disks = pve_get(machine, f'/nodes/{node}/disks/list')
    release = status['pveversion'].split('/')[1]
    return {
        'os': f"Proxmox VE {'.'.join(release.split('.')[:2])}",
        'kernel': '.'.join(status['current-kernel']['release'].split('.')[:2]),
        'cpu': cpu_name(status['cpuinfo']['model']),
        'threads': status['cpuinfo']['cpus'],
        'memoryGb': installed_gb(status['memory']['total']),
        'storageGb': round(sum(disk['size'] for disk in disks) / 1e9),
        'bootTime': int(time.time() - status['uptime']),
        'guests': {
            'running': sum(1 for guest in guests if guest['status'] == 'running'),
            'total': len(guests),
        },
        'load': round(float(status['loadavg'][0]), 2),
    }


def collect_ssh(machine: dict) -> dict:
    output = subprocess.run(
        [
            'ssh', '-i', str(KEY), '-p', str(machine['port']),
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=/etc/labstats/known_hosts',
            f"{machine['user']}@{machine['host']}",
        ],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    # Login banners (fastfetch and friends) can precede the report, which is always the last line.
    report = json.loads(output.strip().splitlines()[-1])
    return {
        'os': report['os'],
        'kernel': '.'.join(report['kernel'].split('.')[:2]),
        'cpu': cpu_name(report['cpu']),
        'threads': report['threads'],
        'memoryGb': installed_gb(report['memKb'] * 1024),
        'storageGb': round(report['diskBytes'] / 1e9),
        'bootTime': report['bootTime'],
        'load': round(float(report['load']), 2),
        'tempC': round(report['tempMilli'] / 1000, 1),
    }


COLLECTORS = {'pve': collect_pve, 'ssh': collect_ssh}


def publish(config: dict, stats: dict) -> None:
    git_env = {**os.environ, 'GIT_SSH_COMMAND': f'ssh -i {KEY} -o UserKnownHostsFile=/etc/labstats/known_hosts'}

    def git(*args: str) -> None:
        subprocess.run(['git', '-C', str(REPO), *args], env=git_env, check=True)

    git('pull', '--quiet', '--rebase')
    (REPO / 'stats.json').write_text(json.dumps(stats, indent=2) + '\n')
    git('add', 'stats.json')
    git('commit', '--quiet', '-m', f"stats: {stats['generatedAt'][:10]}")
    git('push', '--quiet')
    if config.get('deployHook'):
        urllib.request.urlopen(urllib.request.Request(config['deployHook'], method='POST'), timeout=30).close()


def main() -> int:
    config = json.loads(CONFIG.read_text())
    machines = []
    for machine in config['machines']:
        try:
            measured = COLLECTORS[machine['kind']](machine)
        except Exception as error:
            print(f"labstats: skipping {machine['name']}: {error}", file=sys.stderr)
            continue
        public = {key: machine[key] for key in ('name', 'role', 'location', 'hardware')}
        machines.append({**public, **measured})

    if not machines:
        print('labstats: every machine failed, not publishing', file=sys.stderr)
        return 1

    stats = {'generatedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'machines': machines}
    if '--dry-run' in sys.argv:
        print(json.dumps(stats, indent=2))
        return 0
    publish(config, stats)
    print(f'labstats: published {len(machines)}/{len(config["machines"])} machines')
    return 0 if len(machines) == len(config['machines']) else 1


if __name__ == '__main__':
    sys.exit(main())
