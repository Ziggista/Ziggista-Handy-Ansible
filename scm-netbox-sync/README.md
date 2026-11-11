# Strata Cloud Manager → NetBox sync

Ansible playbook that reads Palo Alto firewalls from Strata Cloud Manager (SCM) and
creates/updates them in NetBox: device, serial, model, PAN-OS version, HA state,
interfaces, and IP addresses.

**The HA rule:** in an active/passive pair both firewalls get the device and all
interfaces, but **only the active firewall gets IP addresses**. When the pair
fails over, the next run moves the same NetBox IP objects to the new active
(ids, tags, journal and history are kept) and releases anything left on the
passive.

Tested against NetBox 4.7.1 (docker) and netbox.netbox 3.23.1 with a mock SCM: first run, idempotent re-run (changed=0), failover,
failback, `--check`, and both management-IP policies. It has **not** been run
against a real SCM tenant yet; see [Unverified](#unverified-scm-api-details).

## Flow

```
scm_auth      POST auth.apps.paloaltonetworks.com/oauth2/access_token  (client creds, scope tsg_id:<TSG>)
scm_collect   GET  /config/setup/v1/devices                   serial, model, version, mgmt IP, ha_state, ha_peer_serial
              POST /operations/v1/jobs/device-interfaces      live "show interface" (5 serials per job)
              GET  /operations/v1/device/jobs/{id}            poll until complete
              GET  /config/network/v1/<kind>-interfaces       fallback: configured intent per folder + device
netbox_prereqs  manufacturer, role, platform, tags, custom fields, device types
netbox_sync   plan (filter_plugins/scm_netbox.py) -> devices -> interfaces -> IPs (active only)
              -> release IPs left on passive -> primary/OOB IP
```

## Files

| Path | What |
|---|---|
| `scm_to_netbox.yml` | Main playbook (runs on localhost / AWX EE) |
| `group_vars/all/main.yml` | All settings: site mapping, HA/mgmt-IP policy, interface typing |
| `group_vars/all/vault.yml.example` | Credentials for running outside AWX |
| `roles/scm_auth` | OAuth2 token (re-run before each phase; tokens last 15 min) |
| `roles/scm_collect` | Devices, live interfaces, configured interfaces |
| `roles/netbox_prereqs` | Objects the sync needs; idempotent |
| `roles/netbox_sync` | Devices, interfaces, IPs, passive cleanup, primary IP, AWX `set_stats` summary |
| `filter_plugins/scm_netbox.py` | Parsing, HA role resolution and the NetBox plan |
| `awx/credential_type_*.yml` | AWX custom credential types for SCM and NetBox |
| `collections/requirements.yml`, `requirements.txt`, `execution-environment.yml` | AWX project / EE deps |

## Setup

1. **SCM service account**: Common Services → Identity & Access → add a service
   account with a read-only role on the TSG that holds the firewalls (the NGFW
   Operations jobs may need a role that can run operational commands; check this
   on the first run). Note the client ID, secret and TSG ID.
2. **NetBox token** with write access to dcim, ipam, extras (tags, custom fields).
   v1 (`Token`) and v2 (`nbt_…`, Bearer) tokens both work.
3. Edit `group_vars/all/main.yml`: at minimum `netbox_default_site`,
   `netbox_site_map` (SCM folder → site slug) and `netbox_mgmt_networks` (SCM
   gives the mgmt IP without a mask).

### Run locally

```bash
ansible-galaxy collection install -r collections/requirements.yml
pip install -r requirements.txt
export SCM_CLIENT_ID=... SCM_CLIENT_SECRET=... SCM_TSG_ID=...
export NETBOX_API=https://netbox.example NETBOX_TOKEN=...
ansible-playbook scm_to_netbox.yml --tags collect -e scm_debug_dump_dir=./dump   # SCM only, dump raw JSON
ansible-playbook scm_to_netbox.yml --check --diff                                # dry run against NetBox
ansible-playbook scm_to_netbox.yml
```

### AWX

1. Credential types: create two from `awx/credential_type_scm.yml` and
   `awx/credential_type_netbox.yml` (input config + the injector block in each
   file). Create one credential of each.
2. Execution environment: build `execution-environment.yml` with
   `ansible-builder`, or use any EE that has `netbox.netbox` and `pynetbox`
   (AWX also installs `collections/requirements.yml` at project sync).
3. Job template: playbook `scm_to_netbox.yml`, inventory with just `localhost`,
   both credentials, "Prompt on launch" for Check and Variables. Schedule it
   (e.g. every 15–30 minutes so failovers show up quickly).
4. The job's artifacts carry a `scm_netbox_sync` summary (active, passive,
   held, released IPs, warnings) for notifications or a follow-on job.

## How the HA rule works

Roles come from SCM's `ha_state`, `ha_peer_state` and `ha_peer_serial` on each device:

| Situation | Result in NetBox |
|---|---|
| Standalone (no peer) | device, interfaces, IPs |
| Active member | device, interfaces, IPs, tag `ha-active` |
| Passive member (`passive`, `suspended`, `non-functional`, `initial`) | device, interfaces, **no IPs**, tag `ha-passive` |
| Both claim active and both connected, or neither active | **hold**: interfaces updated, IPs left exactly as they were, tag `ha-hold`, warning in the job |
| Both claim active but one is disconnected from SCM | the connected one is active |
| Active/active (`active-primary`/`active-secondary`) | primary owns IPs (`scm_ha_active_active_policy: primary`), or `both` |

**Failover:** IPs are matched in NetBox on address (+ VRF if `netbox_vrf` is set), not
on address + interface, so the assign step re-points the existing IP to the new
active instead of creating a duplicate. Afterwards any IP still attached to a
passive member is released (`netbox_passive_ip_action: unassign | delete | keep`).
NetBox will not move an IP that is a device's primary/OOB IP, so the passive's
primary/OOB is cleared first.

**Management IPs** are unique per firewall, which sits awkwardly with "only the
active has IPs". `netbox_mgmt_ip_policy` chooses:

- `active_only` (default, the rule as you described it): the passive has no IPs at all, and the mgmt IP
  is unassigned from a box when it becomes passive.
- `both`: each firewall keeps its own mgmt IP (and primary/OOB IP); only the shared
  dataplane IPs follow the active. Recommended if anything (AWX, monitoring)
  needs to reach the passive via NetBox.
- `none`: never import mgmt IPs.

## What lands in NetBox

- **Device**: matched on **serial** (an existing device keeps its NetBox name and site
  unless `netbox_update_site: true`). Device type per model (created bare if missing;
  import from the devicetype-library for proper templates), role `Firewall`, platform
  `PAN-OS`, tags (`scm-managed`, `ha-*`; tags you added by hand are kept).
- **Custom fields** (group "Strata Cloud Manager"): `scm_ha_state`, `scm_ha_peer`,
  `scm_folder`, `scm_software_version`, `scm_connected`, `scm_device_id`.
- **Interfaces**: `aeN` → LAG, `ethernetX/Y.Z` → virtual with parent, `tunnel.N` /
  `loopback.N` / `vlan.N` → virtual, physical type from live speed (or
  `netbox_interface_type_rules`), `mgmt` (mgmt-only). Zone goes in the description.
- **IPs**: status active, description "Synced from Strata Cloud Manager", tag `scm-managed`.
- **Primary IPv4 and OOB IP** = mgmt IP (`netbox_set_primary_ip`).

## Unverified SCM API details

Checked against pan.dev and the `pan-scm-sdk` (0.15.1) source, but not against a live tenant:

1. **Devices list** `GET /config/setup/v1/devices`: the field names (`serial_number`, `model`,
   `software_version`, `ip_address`, `ha_state`, `ha_peer_state`, `ha_peer_serial`,
   `is_connected`) and the `data`/`total`/`limit`/`offset` paging come from the SDK, not a
   published Palo Alto schema. The exact `ha_state` strings are not documented; the
   mapping handles `active`, `passive`, `active-primary/secondary`, `suspended`,
   `non-functional`, `initial`, `tentative`, and holds anything else.
2. **Live interfaces** `POST /operations/v1/jobs/device-interfaces`: endpoint, 1–5 serials per
   job and polling via `GET /operations/v1/device/jobs/{id}` are documented (R2-2026 spec),
   but **the result body for this job is not**. The parser assumes PAN-OS
   `show interface all` style (`ifnet` + `hw` tables) and falls back to any list of
   objects with a `name`. Run once with `--tags collect -e scm_debug_dump_dir=./dump` and
   check `dump/op_jobs.json`; if the shape differs, `_parse_op_result` is the one function to adjust.
3. **Configured interfaces** `GET /config/network/v1/{ethernet,aggregate,layer3-sub,loopback,tunnel,vlan}-interfaces`:
   not sure whether `?folder=` returns inherited parent-folder objects, or whether `?device=`
   expects the serial or the device name. Interface names that are variables (`$wan`) are
   resolved via their `default_value` only; per-device variable overrides
   (`/config/setup/v1/variables`) are not read, and IPs given as address objects are
   reported as warnings, not imported. This is why live data is the default.
4. **Token URL**: pan.dev documents `/oauth2/access_token`; the SDK uses
   `/am/oauth2/access_token`. Both currently work; change `scm_auth_url` if one stops.
5. **Permissions**: which SCM role is needed for the Operations jobs is not stated.
6. No `X-PANW-Region` header is sent; these services take the region from the token.

## Not done (yet)

- Deleting NetBox interfaces that no longer exist in SCM (safe to add later as an opt-in prune).
- MAC addresses (NetBox 4.2+ stores them as separate objects; PAN HA dataplane MACs are
  virtual and also move on failover).
- VRF per logical/virtual router, security zones as objects, cables, licences.
