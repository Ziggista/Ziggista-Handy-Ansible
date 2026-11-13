# Ubuntu 26.04 golden image (cloud-init on vSphere)

Builds an Ubuntu 26.04 template from the official cloud image, deploys VMs from
it with cloud-init, then runs the post-deploy stage (Rapid7, CrowdStrike, AD join).

| Playbook | What it does |
|---|---|
| `build-template.yml` | Deploys the Ubuntu 26.04 cloud image OVA as a build VM, patches it, bakes in the cloud-init datasource config, users and SSH keys, seals it and converts it to the template `ubuntu-2604-golden`. |
| `deploy-vm.yml` | Clones VM(s) from the template. Hostname, static IP, DNS and timezone are passed to cloud-init through VMware `guestinfo` (no VMware customization spec). |
| `post-deploy.yml` | On the new VM(s): apt upgrade, Rapid7 Insight Agent, CrowdStrike Falcon sensor, AD join with realmd/sssd, sudo and SSH access groups, reboot if needed. |
| `site.yml` | `deploy-vm.yml` followed by `post-deploy.yml` in one run. |

## Setup

```bash
ansible-galaxy collection install -r golden/ubuntu-2604/requirements.yml
cp golden/ubuntu-2604/vars/vault.yml.example golden/ubuntu-2604/vars/vault.yml
# fill it in, then:
ansible-vault encrypt golden/ubuntu-2604/vars/vault.yml
```

`vars/vault.yml` is git-ignored and loaded automatically when it exists. In AWX,
provide the same variable names from a custom credential type or extra vars
instead. Non-secret defaults are in `vars/main.yml`.

Variables with no default that you must supply (vault, `-e`, or inventory):
`vcenter_hostname`, `vcenter_username`, `vcenter_password`, `vcenter_datacenter`,
`vcenter_cluster`, `vcenter_datastore`, `vsphere_network`, `ubuntu_netmask`,
`ubuntu_gateway`, `ubuntu_dns_servers`, `golden_admin_pubkeys`, `rapid7_token`,
`falcon_cid`, `ad_join_user`, `ad_join_password`, plus an installer location for
each agent (`*_installer_url` or `*_installer_src`).
`golden/vsphere/vsphere-discover-defaults.yml` lists the real vCenter names.

## Run

```bash
# 1. Build the template (once, and again whenever you want a fresh image)
ansible-playbook golden/ubuntu-2604/build-template.yml --ask-vault-pass \
  -e golden_build_ip=172.19.2.250

# 2. Deploy a VM and configure it
ansible-playbook golden/ubuntu-2604/site.yml --ask-vault-pass \
  -e vm_name=p-example-01 -e vm_ip=172.19.2.50
```

Several VMs at once: pass `vm_list` as JSON extra vars, e.g.
`-e '{"vm_list":[{"name":"a-01","ip":"172.19.2.51"},{"name":"a-02","ip":"172.19.2.52","cpu":8}]}'`.

Skip a post-deploy stage with `-e golden_install_rapid7=false`,
`-e golden_install_crowdstrike=false` or `-e golden_domain_join=false`.

## How the image works

- **Datasource**: `/etc/cloud/cloud.cfg.d/99-rrc-golden.cfg` pins
  `datasource_list: [VMware, None]`, so clones only read `guestinfo.metadata`
  and `guestinfo.userdata`.
- **Users and keys**: the same file creates `template_admin` (keys from
  `golden_admin_pubkeys`) and `svc_awx_linux` (key from
  `golden/ubuntu/files/svc_awx_linux_ed25519.pub`) on every clone. Both are
  key-only with passwordless sudo. Password SSH and root login are off.
- **Sealing**: `cloud-init clean --logs --seed --machine-id` and SSH host key
  removal, so each clone gets a new machine-id, new host keys and a fresh
  cloud-init run.
- **Disk**: the cloud image has a single root partition (no LVM). cloud-init
  grows it to `vm_disk_gb` on first boot, so `_setup/grow-hdd.yml` is not
  needed (and would fail) on these VMs.
- **SSH access after AD join**: `AllowGroups` permits the local group
  `local-ssh-allowed` (template_admin, svc_awx_linux), `ad_ssh_groups` and
  `ad_sudo_group`.

## Not yet tested

These playbooks pass `ansible-lint` (production profile) and a syntax check but
have not been run against vCenter or a real 26.04 guest. Check on the first run:

- the OVA network name (`golden_ova_network_name`, default `VM Network`)
- that the build VM is reachable (DHCP, or `golden_build_ip`)
- time sync before the AD join (26.04 uses chrony, not systemd-timesyncd)
- the Rapid7 and CrowdStrike `.deb` versions you host support 26.04
