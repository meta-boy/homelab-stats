# homelab-stats

Daily snapshot of my homelab, rendered on [anurag.wtf](https://anurag.wtf) under `homelab`.

`stats.json` is written once a day by `collect.py`, which runs in a small container on one of the
Proxmox hosts. It reads each hypervisor through a read-only (`PVEAuditor`) API token and the
Raspberry Pi through an SSH key locked to `labstats-report`, then commits the result here and
triggers a site rebuild. Nothing reaches into the lab from outside.

No addresses, hostnames or exact kernel builds are published.
