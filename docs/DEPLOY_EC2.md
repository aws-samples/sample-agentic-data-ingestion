# Deploying from an EC2 Build Host

Use this when you can't deploy from a local machine with Docker — for example
from AWS CloudShell, which **cannot deploy this project** (no Docker daemon,
~1 GB storage). A small Graviton EC2 instance is the cheapest reliable
alternative and builds the project's `linux/arm64` images natively.

## Known-good instance recipe

| Setting | Value | Why |
|---------|-------|-----|
| Instance type | `t4g.large` (2 vCPU, 8 GB, Graviton/ARM64) | Native arm64 builds — no QEMU emulation |
| Root volume | **100 GB gp3** | Docker image builds + layer bundling; the default 8 GB fails fast |
| AMI | Amazon Linux 2023 (arm64) | `dnf` has Node 20 and Python 3.12 packages |
| Access | SSM Session Manager (instance profile with `AmazonSSMManagedInstanceCore`) | No inbound ports, no SSH keys |
| IAM | Instance profile with permissions to deploy (CloudFormation, IAM, Lambda, S3, ECR, etc.) | CDK deploys ~8 stacks of mixed resource types |

## Setup

Launch the instance, connect via SSM, then:

```bash
# 1. Software
sudo dnf install -y docker nodejs20 python3.12 git

# 2. Start the daemon AND enable it at boot (one command does both)
sudo systemctl enable --now docker

# 3. Docker group — requires ending and reopening the SSM session afterwards
sudo usermod -aG docker "$USER"
exit   # end the SSM session, then start a new one
```

### Docker must be installed *and* started

`dnf install docker` only puts the daemon on disk — it does **not** start it, and
on AL2023 it is not enabled at boot either. A fresh instance therefore has a
`docker` binary and no daemon behind it, which is the most common reason
`./deploy.sh` fails its Docker check on a host that looks correctly set up.

`sudo systemctl enable --now docker` does both jobs: `--now` starts the daemon in
this boot, and `enable` makes it start on every subsequent boot. Run it once,
after the install.

### The docker-group change needs a new SSM session

`usermod -aG docker` changes your group membership, but group membership is
resolved when a session is created. Your current session keeps the old set, so
`docker info` will still fail in it no matter how long you wait.

**End the SSM session and open a new one** — `exit` the shell, then start a fresh
`aws ssm start-session`. Reconnecting is what applies the change; nothing you can
run inside the old session substitutes for it. (`newgrp docker` works for a single
shell if you need it immediately, but a clean reconnect is what these instructions
assume.)

Verify after reconnecting — these two commands separate the two failure modes:

```bash
systemctl is-active docker   # "active" = the daemon is running
docker info                  # must succeed WITHOUT sudo
```

If `is-active` says `inactive`, the daemon is not started — go back to
`sudo systemctl enable --now docker`. If it says `active` but `docker info` fails
with `permission denied` on `/var/run/docker.sock`, the group change has not taken
effect in this session — reconnect again.

> **Do not** work around a permission error with `chmod 666 /var/run/docker.sock`.
> Mode 666 makes the socket world-writable, and any process that can talk to the
> Docker daemon can start a privileged container that mounts the host filesystem —
> so it grants **every local user root-equivalent access to the host**. The group
> membership plus a fresh session is the fix; see
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md#permission-denied-on-varrundockersock).

### Node and Python versions on AL2023

If `node --version` shows something older than 20, point the alternatives at
the Node 20 install (`sudo alternatives --set node /usr/bin/node-20` on AL2023,
or use nvm).

`dnf install python3.12` installs `/usr/bin/python3.12` and **leaves `python3` at
3.9** — `python3 --version` reports 3.9 on a host that has 3.12 installed. That is
expected AL2023 behaviour, not a failed install.

Do **not** re-point the `python3` link to work around it. On AL2023 that link is
managed by the system package manager and `dnf` itself runs on the system Python,
so repointing it can break package management on the instance. `deploy.sh` probes
`python3` first and falls back to `python3.12`, and prints which interpreter it
selected.

## Get the code and deploy

```bash
# Clone (or transfer a zip via S3 if your network blocks git)
git clone <repo-url>          # this repository's HTTPS or SSH clone URL
cd agentic_data_ingestion

./deploy.sh
```

`deploy.sh` validates the environment (Node 20+, Python 3.12+, Docker, arm64
capability, disk space, AWS credentials), bootstraps CDK in the app region and
`us-east-1`, and deploys all stacks. The React dashboard is built automatically
during deploy. First deploy takes roughly 20–30 minutes on a `t4g.large`.

To only validate the environment first:

```bash
./deploy.sh --dry-run
```

## Growing the volume later

If you launched with a smaller volume and hit `No space left on device`:

```bash
aws ec2 modify-volume --volume-id <vol-id> --size 100
sudo growpart /dev/nvme0n1 1
sudo xfs_growfs -d /        # AL2023 root is XFS
```

## Cost note

A `t4g.large` with a 100 GB gp3 volume costs roughly $2/day on-demand plus ~$8/month
for the volume. Stop or terminate the instance when you're done — the deployed
application does not need it.
