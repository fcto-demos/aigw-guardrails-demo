# Infrastructure — provisioning a VM for WSO2 API Platform

Applies when deploying to a **new** AWS VM. Skip if the user already has a host.

## Preflight — check quotas BEFORE committing to a region

Getting this wrong costs a full rebuild. Check all four in the target region
before creating anything:

```bash
R=<region>
# 1. Elastic IP headroom — the one people miss
echo "EIPs: $(aws ec2 describe-addresses --region $R --query 'length(Addresses)' --output text)"
aws service-quotas get-service-quota --region $R --service-code ec2 \
  --quota-code L-0263D0A3 --query 'Quota.Value' --output text

# 2. instance type offered here
aws ec2 describe-instance-type-offerings --region $R \
  --filters Name=instance-type,Values=<type> --query 'InstanceTypeOfferings[].InstanceType' --output text

# 3. default VPC + subnets
aws ec2 describe-vpcs --region $R --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text

# 4. RDS class, only if using an external database
aws rds describe-orderable-db-instance-options --region $R --engine postgres \
  --db-instance-class <class> --query 'OrderableDBInstanceOptions[0].DBInstanceClass' --output text
```

**A region can be at or over its EIP quota because of other teams' resources.**
Default is 5. If `describe-addresses` returns a count at or above the quota,
either request an increase (not instant — it goes to a queue) or pick another
region. Discovering this after provisioning a database means tearing it down.

Also check nothing already exists under the chosen name prefix, so a retry
doesn't half-adopt someone else's resources:

```bash
aws ec2 describe-security-groups --region $R --filters Name=group-name,Values='<prefix>-*' --query 'SecurityGroups[].GroupName' --output text
aws resourcegroupstaggingapi get-resources --region $R --tag-filters Key=<tagkey>,Values=<tagval> --query 'ResourceTagMappingList[].ResourceARN' --output text
```

## Two AWS CLI traps

**zsh does not word-split unquoted variables.** `R="--region eu-west-1"; aws ec2 ... $R`
passes `--region eu-west-1` as a *single* argument and fails with
`Unknown options`. Set `AWS_DEFAULT_REGION` / `AWS_REGION` in the environment
instead of building flag strings.

**Boolean CLI options are flags, not values.** `--deletion-protection false` is
an error; it is `--no-deletion-protection`. Same for `--publicly-accessible`,
`--multi-az`, `--auto-minor-version-upgrade`.

## Sizing

| Deployment | Type | Why |
|---|---|---|
| Control plane only | t3.large (2 vCPU / 8 GB) | three light containers |
| Gateway only | t3.xlarge (4 vCPU / 16 GB) | the docs size the runtime alone at 2 vCPU / 2 Gi requested, controller 0.5–1 vCPU / 1–2 Gi |
| Both on one host | t3.xlarge | comfortable; t3.large works for light PoC traffic |

50 GB gp3 root is ample — a full deployment uses about 5 GB. Encrypt it: the
SQLite databases and the at-rest encryption keys live there.

## Instance settings that matter

```bash
--metadata-options 'HttpTokens=required,HttpEndpoint=enabled'
--block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":50,"VolumeType":"gp3","DeleteOnTermination":true,"Encrypted":true}}]'
```

`HttpTokens=required` enforces IMDSv2. Important when the instance carries an
IAM role for DNS-01 certificate issuance.

**Tag the volume, not just the instance.** Snapshot lifecycle policies target
*volumes* by tag — tag only the instance and nothing gets backed up:

```bash
--tag-specifications \
  'ResourceType=instance,Tags=[{Key=Name,Value=<prefix>-server},...]' \
  'ResourceType=volume,Tags=[{Key=Name,Value=<prefix>-root},...]'
```

## Security group

One hostname serves every component; **ports** distinguish them. Open only what
is actually consumed:

| Port | Component | Open to |
|---|---|---|
| 22 | SSH | your IP, or `0.0.0.0/0` if the host is key-only (verify first) |
| 9643 | AI Workspace UI | public |
| 9543 | API Portal | public |
| 8443 | Gateway data plane | public |
| 9243 | Platform API | **only if gateways run on other hosts** |

Keep closed: `8080`, `8081`, `9090`, `9094`, `9901`, `9011`, `9002`, `9003`,
`9200`, `5601`, `3000`, `9092`. Reach admin interfaces over an SSH tunnel.

Before opening SSH widely, confirm the host is key-only:

```bash
sudo sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitemptypasswords)'
# all three must be "no"
```

**9243 exposes the whole Platform API**, including the file-auth login endpoint.
Only open it when gateways genuinely run elsewhere; a gateway on the same host
reaches it over the Docker bridge instead (see `control-plane.md`).

**A `/32` SSH rule breaks whenever the user's IP changes.** If a team needs
access, use a corporate CIDR or accept `0.0.0.0/0` with key-only auth. When SSH
starts timing out, this is almost always why — `i/o timeout` means dropped
packets (firewall), whereas a closed port gives "connection refused".

## Sharing one key pair across regions

Key pairs are regional. Import the same public key rather than distributing a
second private key:

```bash
ssh-keygen -y -f existing-key.pem > /tmp/pub
aws ec2 import-key-pair --region <new-region> --key-name <name> \
  --public-key-material fileb:///tmp/pub
```

Matching fingerprints confirm it worked.

## Host bootstrap

```bash
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl gnupg unzip jq openssl acl \
    postgresql-client            # only when using an external Postgres
```

`acl` provides `setfacl`, needed to make TLS private keys readable by the
container user. Not always on minimal cloud images.

**Docker's GPG key must be stored ARMORED on Ubuntu 24.04.** The widely-copied
`curl … | gpg --dearmor -o /etc/apt/keyrings/docker.asc` produces
`NO_PUBKEY 7EA0A9C3F273FCD8` even though the key is present in the file:

```bash
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

Then the repo, then `docker-ce docker-ce-cli containerd.io docker-buildx-plugin
docker-compose-plugin`, and `usermod -aG docker ubuntu`.

**Docker CE's engine is Apache 2.0.** The paid-subscription requirement is
Docker *Desktop*, a separate macOS/Windows product. Rancher Desktop and Colima
are desktop tools; the documented Linux runtime is Docker Engine + Compose
plugin. Avoid Podman here — these stacks rely on `env_file` with `format: raw`,
Compose profiles, and `service_healthy` conditions, which `podman-compose`
supports unevenly.

## Backups

Everything worth keeping is on the one root volume — Docker named volumes under
`/var/lib/docker/volumes/`, both at-rest encryption keys, the certificates, and
the env files. A single EBS snapshot captures the databases *and* the keys that
decrypt them, which a database-only backup would not.

A DLM policy targeting volumes by tag, daily, retaining 7, is enough. Create a
dedicated role rather than the shared `AWSDataLifecycleManagerDefaultRole` if
the user wants everything under their own name prefix.

Snapshots of a live SQLite file are crash-consistent, not application-consistent
— usually recoverable in WAL mode. Add a `docker compose stop` pre-hook only if
the user wants certainty.

## Teardown

Order matters; each step frees the next.

1. DLM policy — stops new snapshots
2. **Existing snapshots** — deleting the policy does *not* remove them
3. RDS instance (`--skip-final-snapshot --delete-automated-backups`), then wait
4. EC2 instance, then **wait for `terminated`** — volume and ENI go with it
5. Release the Elastic IP — an unattached one bills the same as attached
6. DB subnet group (only after RDS is gone)
7. Security groups — the database SG first if it references the instance SG
8. Key pair
9. IAM: remove role from instance profile → delete profile → detach policy →
   delete role → delete policy

Confirm with `resourcegroupstaggingapi get-resources`. A **terminated instance
lingers in that API for about an hour** — that is a stale record, not a
resource.

Not AWS, and easy to forget: the DNS records, and any gateway still registered
in a control plane (it will show "Not Active" forever). Delete the DNS record
promptly — a released Elastic IP returns to the pool and may be reassigned,
leaving the hostname pointing at someone else's host.
