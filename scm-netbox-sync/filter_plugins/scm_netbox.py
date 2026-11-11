# -*- coding: utf-8 -*-
"""Filters that turn Strata Cloud Manager (SCM) API data into a NetBox plan.

All the logic that is awkward in Jinja lives here so it can be unit tested
(see tests/test_scm_netbox.py):

  scm_select_devices        filter the raw /config/setup/v1/devices list
  scm_parse_op_interfaces   parse NGFW Operations "device-interfaces" job results
  scm_parse_cfg_interfaces  parse NGFW Configuration *-interfaces list results
  scm_netbox_plan           build the device / interface / IP plan, applying
                            the HA rule: only the ACTIVE member of an HA pair
                            gets IP addresses in NetBox.
"""
from __future__ import absolute_import, division, print_function

__metaclass__ = type

import ipaddress
import re

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_NOT_AN_IP = {"", "n/a", "na", "none", "null", "-", "0.0.0.0", "0.0.0.0/0", "unknown"}


def _s(value):
    """None-safe, stripped string."""
    if value is None:
        return ""
    return str(value).strip()


def _as_list(value):
    """PAN-OS JSON often gives a dict for one entry and a list for many."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and "entry" in value:
        return _as_list(value["entry"])
    return [value]


def _clean_ip(value):
    """Return a normalised 'a.b.c.d/len' (or v6) string, or None."""
    text = _s(value)
    if text.lower() in _NOT_AN_IP:
        return None
    try:
        iface = ipaddress.ip_interface(text)
    except ValueError:
        return None
    if iface.ip.is_unspecified:
        return None
    return iface.with_prefixlen


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d.get(k)
    return None


# --------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------


def scm_select_devices(devices, model_regex=r"^PA-", folders=None,
                       exclude_names=None, require_serial=True):
    """Keep only the SCM devices we want in NetBox (firewalls by default)."""
    out = []
    model_re = re.compile(model_regex or ".*", re.I)
    folders = [f for f in (folders or []) if f]
    exclude_names = set(exclude_names or [])
    for dev in devices or []:
        if not isinstance(dev, dict):
            continue
        serial = _s(dev.get("serial_number"))
        if require_serial and not serial:
            continue
        if not model_re.search(_s(dev.get("model"))):
            continue
        if folders and _s(dev.get("folder")) not in folders:
            continue
        names = {_s(dev.get(k)) for k in ("name", "hostname", "display_name")}
        if names & exclude_names:
            continue
        out.append(dev)
    return out


# --------------------------------------------------------------------------
# interfaces: operational (live) data
# --------------------------------------------------------------------------


def _walk_lists(obj, depth=0):
    """Yield every list of dicts (with a 'name' key) found in obj."""
    if depth > 6:
        return
    if isinstance(obj, list):
        if obj and all(isinstance(i, dict) for i in obj) and any("name" in i for i in obj):
            yield obj
        for item in obj:
            for found in _walk_lists(item, depth + 1):
                yield found
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if key == "entry":
                lst = _as_list(value)
                if lst and all(isinstance(i, dict) for i in lst):
                    yield lst
                    continue
            for found in _walk_lists(value, depth + 1):
                yield found


def _parse_op_result(result):
    """Parse one device's 'show interface all'-style result into a dict by name.

    The SCM Operations API does not publish the result schema for the
    device-interfaces job (see README, "Unverified"). PAN-OS itself returns
    'ifnet' (logical: name, ip, zone, tag, fwd) and 'hw' (physical: name, mac,
    state, speed, duplex) tables, so those are handled explicitly and any other
    list-of-dicts-with-name is merged in as a fallback.
    """
    by_name = {}

    def merge(entry):
        name = _s(_first(entry, "name", "interface", "interfaceName", "ifname"))
        if not name:
            return
        cur = by_name.setdefault(name, {"name": name, "ipv4": [], "ipv6": []})
        for key in ("ip", "ip_address", "ipaddr", "address", "ipv4"):
            for raw in _as_list(entry.get(key)):
                ip = _clean_ip(raw if not isinstance(raw, dict) else _first(raw, "name", "ip"))
                if ip and ip not in cur["ipv4"] + cur["ipv6"]:
                    (cur["ipv6"] if ":" in ip else cur["ipv4"]).append(ip)
        for key in ("ipv6", "ip6", "addr6"):
            for raw in _as_list(entry.get(key)):
                ip = _clean_ip(raw if not isinstance(raw, dict) else _first(raw, "name", "ip"))
                if ip and ":" in ip and ip not in cur["ipv6"]:
                    cur["ipv6"].append(ip)
        for src, dst in (("mac", "mac"), ("mac_address", "mac"), ("state", "state"),
                         ("speed", "speed"), ("duplex", "duplex"), ("zone", "zone"),
                         ("vsys", "vsys"), ("tag", "tag"), ("fwd", "fwd"),
                         ("mode", "mode"), ("type", "kind"), ("comment", "description"),
                         ("ae", "lag"), ("aggregate_group", "lag")):
            val = entry.get(src)
            if val not in (None, "", "N/A", "n/a", "[n/a]", "ukn") and not isinstance(val, (dict, list)):
                cur.setdefault(dst, _s(val))

    if isinstance(result, dict):
        for table in ("hw", "ifnet"):
            if table in result:
                for e in _as_list(result[table]):
                    if isinstance(e, dict):
                        merge(e)
        if by_name:
            return by_name
    for lst in _walk_lists(result):
        for e in lst:
            merge(e)
    return by_name


def scm_parse_op_interfaces(job_payloads):
    """job_payloads: list of GET /operations/v1/device/jobs/{id} JSON bodies.

    Returns {serial: [interface, ...]} for every device whose result
    completed. Devices that failed are returned under the key '_failed'.
    """
    out = {"_failed": []}
    for job in job_payloads or []:
        if not isinstance(job, dict):
            continue
        for res in job.get("results") or []:
            serial = _s(res.get("device") or res.get("serial") or res.get("serial_number"))
            if not serial:
                continue
            state = _s(res.get("state")).lower()
            details = res.get("details") or {}
            payload = details.get("result") if isinstance(details, dict) else None
            if state not in ("complete", "completed", "success") or payload is None:
                out["_failed"].append(serial)
                continue
            parsed = _parse_op_result(payload)
            out[serial] = sorted(parsed.values(), key=lambda i: _sort_key(i["name"]))
    return out


# --------------------------------------------------------------------------
# interfaces: configuration (intent) data
# --------------------------------------------------------------------------


def _cfg_ips(block):
    v4, v6, unresolved = [], [], []
    if not isinstance(block, dict):
        return v4, v6, unresolved
    for entry in _as_list(block.get("ip")):
        raw = entry.get("name") if isinstance(entry, dict) else entry
        ip = _clean_ip(raw)
        if ip:
            (v6 if ":" in ip else v4).append(ip)
        elif _s(raw):
            unresolved.append(_s(raw))  # address object or $variable
    ipv6 = block.get("ipv6") or {}
    for entry in _as_list(ipv6.get("address") if isinstance(ipv6, dict) else None):
        raw = entry.get("name") if isinstance(entry, dict) else entry
        ip = _clean_ip(raw)
        if ip:
            v6.append(ip)
        elif _s(raw):
            unresolved.append(_s(raw))
    return v4, v6, unresolved


def scm_parse_cfg_interfaces(responses):
    """responses: list of dicts {kind, container_type, container, json}.

    container_type is 'folder' or 'device'. Folder entries apply to every
    device in that folder; device entries override by interface name.
    Returns {"folder": {folder: [iface]}, "device": {serial: [iface]}}.
    """
    out = {"folder": {}, "device": {}}
    for resp in responses or []:
        body = resp.get("json") or {}
        items = body.get("data") if isinstance(body, dict) else body
        bucket = out.setdefault(resp.get("container_type", "folder"), {})
        lst = bucket.setdefault(_s(resp.get("container")), [])
        for item in _as_list(items):
            if not isinstance(item, dict):
                continue
            name = _s(item.get("name"))
            if name.startswith("$"):
                # interface variable: resolve to its default physical name
                name = _s(item.get("default_value"))
            if not name:
                continue
            block = item.get("layer3") or item
            v4, v6, unresolved = _cfg_ips(block)
            iface = {"name": name, "ipv4": v4, "ipv6": v6, "kind": resp.get("kind")}
            if unresolved:
                iface["unresolved"] = unresolved
            if item.get("comment"):
                iface["description"] = _s(item["comment"])
            mtu = block.get("mtu") if isinstance(block, dict) else None
            if mtu:
                iface["mtu"] = int(mtu)
            if item.get("link_state") in ("down",):
                iface["enabled"] = False
            if item.get("parent_interface"):
                iface["parent"] = _s(item["parent_interface"])
            # ethernet members of an AE carry aggregate_group at top level or
            # under the 'aggregate_group' / 'layer3' blocks depending on mode
            agg = item.get("aggregate_group")
            if isinstance(agg, dict):
                agg = agg.get("name")
            if agg:
                iface["lag"] = _s(agg)
            if item.get("tag") not in (None, ""):
                iface["tag"] = _s(item["tag"])
            lst.append(iface)
    return out


def scm_merge_cfg_for_device(parsed_cfg, device):
    """Folder interfaces overlaid with device-level interfaces (by name)."""
    merged = {}
    for iface in parsed_cfg.get("folder", {}).get(_s(device.get("folder")), []):
        merged[iface["name"]] = dict(iface)
    for iface in parsed_cfg.get("device", {}).get(_s(device.get("serial_number")), []):
        merged[iface["name"]] = dict(iface)
    return sorted(merged.values(), key=lambda i: _sort_key(i["name"]))


# --------------------------------------------------------------------------
# HA role resolution
# --------------------------------------------------------------------------

_STANDALONE = {"", "disabled", "standalone", "none", "n/a", "not-configured", "ha-not-enabled"}
_PASSIVE_LIKE = {"passive", "suspended", "non-functional", "initial", "tentative"}


def _norm_state(value):
    return _s(value).lower().replace("_", "-").replace(" ", "-")


def _classify(state, aa_policy):
    """Map a raw ha_state to active / passive / standalone / unknown."""
    if state in _STANDALONE:
        return "standalone"
    if state == "active":
        return "active"
    if state == "active-primary":
        return "active"
    if state == "active-secondary":
        return "active" if aa_policy == "both" else "passive"
    if state in _PASSIVE_LIKE:
        return "passive"
    return "unknown"


def _resolve_ha(devices, aa_policy="primary"):
    """Return {serial: {'role': ..., 'peer': serial|None, 'reason': str}}."""
    by_serial = {_s(d.get("serial_number")): d for d in devices}
    roles = {}
    for serial, dev in by_serial.items():
        own = _norm_state(dev.get("ha_state"))
        peer = _s(dev.get("ha_peer_serial"))
        own_c = _classify(own, aa_policy)
        if not peer and own_c in ("standalone", "unknown"):
            roles[serial] = {"role": "standalone", "peer": None, "reason": "no HA peer"}
            continue
        peer_dev = by_serial.get(peer)
        if peer_dev is None:
            # Peer not in SCM (or filtered out): trust our own view, and the
            # peer state we report, if they agree.
            peer_c = _classify(_norm_state(dev.get("ha_peer_state")), aa_policy)
            if own_c == "active" and peer_c != "active":
                role = "active"
            elif own_c == "passive" and peer_c in ("active", "unknown"):
                role = "passive"
            else:
                role = "hold"
            roles[serial] = {"role": role, "peer": peer or None,
                             "reason": "peer %s not in SCM list; own=%s peer=%s" % (peer, own, dev.get("ha_peer_state"))}
            continue
        peer_c = _classify(_norm_state(peer_dev.get("ha_state")), aa_policy)
        own_up = bool(dev.get("is_connected", True))
        peer_up = bool(peer_dev.get("is_connected", True))
        if own_c == "active" and peer_c != "active":
            role = "active"
        elif peer_c == "active" and own_c != "active":
            role = "passive"
        elif own_c == "active" and peer_c == "active":
            if aa_policy == "both":
                role = "active"
            elif own_up and not peer_up:
                role = "active"
            elif peer_up and not own_up:
                role = "passive"
            else:
                role = "hold"
        else:
            role = "hold"
        roles[serial] = {"role": role, "peer": peer,
                         "reason": "own=%s peer=%s" % (own or "-", _norm_state(peer_dev.get("ha_state")) or "-")}
    # A pair is only trusted if both halves agree; otherwise hold both.
    for serial, info in roles.items():
        peer = info["peer"]
        if peer and peer in roles and info["role"] == "hold":
            roles[peer]["role"] = "hold"
    return roles


# --------------------------------------------------------------------------
# NetBox interface typing
# --------------------------------------------------------------------------

_SPEED_TYPES = {
    "10": "10base-t", "100": "100base-tx", "1000": "1000base-t",
    "2500": "2.5gbase-t", "5000": "5gbase-t", "10000": "10gbase-x-sfpp",
    "25000": "25gbase-x-sfp28", "40000": "40gbase-x-qsfpp",
    "50000": "50gbase-x-sfp56", "100000": "100gbase-x-qsfp28",
    "400000": "400gbase-x-qsfpdd",
}

_VIRTUAL_RE = re.compile(r"^(tunnel|loopback|vlan|sdwan)(\.\d+)?$", re.I)
_LAG_RE = re.compile(r"^ae\d+$", re.I)
_SUBIF_RE = re.compile(r"^(?P<parent>(ethernet\d+/\d+(/\d+)?|ae\d+))\.(?P<tag>\d+)$", re.I)
_MGMT_RE = re.compile(r"^(mgmt|management)$", re.I)


def _sort_key(name):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def _nb_type(iface, model, settings):
    name = iface["name"]
    for rule in settings.get("interface_type_rules") or []:
        if re.search(rule.get("model_regex", ".*"), model or "", re.I) and \
                re.search(rule.get("name_regex", ".*"), name, re.I):
            return rule["type"]
    if _LAG_RE.match(name):
        return "lag"
    if _SUBIF_RE.match(name) or _VIRTUAL_RE.match(name):
        return "virtual"
    speed = re.sub(r"[^0-9]", "", _s(iface.get("speed")))
    if speed in _SPEED_TYPES:
        return _SPEED_TYPES[speed]
    return settings.get("default_physical_type", "1000base-t")


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def _mgmt_cidr(ip, networks, default_len):
    ip = _s(ip)
    if not ip or ip.lower() in _NOT_AN_IP:
        return None
    if "/" in ip:
        return _clean_ip(ip)
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for net in networks or []:
        try:
            n = ipaddress.ip_network(net, strict=False)
        except ValueError:
            continue
        if addr in n:
            return "%s/%d" % (addr, n.prefixlen)
    return "%s/%d" % (addr, default_len if addr.version == 4 else 128)


def scm_netbox_plan(devices, op_interfaces=None, cfg_interfaces=None,
                    existing=None, settings=None):
    """Build the NetBox plan.

    devices         output of scm_select_devices
    op_interfaces   output of scm_parse_op_interfaces (or None)
    cfg_interfaces  output of scm_parse_cfg_interfaces (or None)
    existing        list of NetBox devices [{id, name, serial}] to match on serial
    settings        dict, see group_vars/all/main.yml (netbox_plan_settings)
    """
    settings = settings or {}
    op_interfaces = op_interfaces or {}
    cfg_interfaces = cfg_interfaces or {"folder": {}, "device": {}}
    existing_by_serial = {}
    for e in existing or []:
        if isinstance(e, dict) and e.get("serial"):
            existing_by_serial[_s(e["serial"])] = e

    name_source = settings.get("name_source", ["hostname", "display_name", "name"])
    site_map = settings.get("site_map") or {}
    mgmt_policy = settings.get("mgmt_ip_policy", "active_only")
    per_device_re = re.compile(settings.get("per_device_ip_interfaces_regex", r"^(mgmt|management)$"), re.I)
    ignore_re = re.compile(settings.get("ignore_interfaces_regex", r"^$a"), re.I)
    source = settings.get("interface_source", "operational")
    roles = _resolve_ha(devices, settings.get("ha_active_active_policy", "primary"))

    plan = {"devices": [], "interfaces": [], "ip_addresses": [],
            "passive_devices": [], "held_devices": [], "warnings": []}
    serial_to_name = {}

    for dev in sorted(devices, key=lambda d: _s(d.get("serial_number"))):
        serial = _s(dev.get("serial_number"))
        if serial in existing_by_serial and settings.get("keep_existing_names", True):
            name = _s(existing_by_serial[serial].get("name"))
        else:
            name = next((_s(dev.get(k)) for k in name_source if _s(dev.get(k))), serial)
        serial_to_name[serial] = name

    seen_ips = {}
    for dev in sorted(devices, key=lambda d: _s(d.get("serial_number"))):
        serial = _s(dev.get("serial_number"))
        name = serial_to_name[serial]
        model = _s(dev.get("model"))
        folder = _s(dev.get("folder"))
        ha = roles.get(serial, {"role": "standalone", "peer": None, "reason": ""})
        role = ha["role"]
        peer_name = serial_to_name.get(ha["peer"] or "", ha["peer"] or "")

        site = site_map.get(folder) or settings.get("default_site")
        for rule in settings.get("site_name_rules") or []:
            if re.search(rule["name_regex"], name, re.I):
                site = rule["site"]
                break

        ex = existing_by_serial.get(serial)
        if ex and not settings.get("update_site", False):
            ex_site = ex.get("site")
            if isinstance(ex_site, dict):
                ex_site = ex_site.get("slug") or ex_site.get("name")
            site = ex_site or site

        tags = list(settings.get("device_tags") or [])
        if role in ("active", "passive", "hold"):
            tags.append("ha-%s" % role)
        if ex:
            # keep tags people added by hand; only the ha-* tags are ours to change
            for t in ex.get("tags") or []:
                slug = _s(t.get("slug") if isinstance(t, dict) else t)
                if slug and not slug.startswith("ha-") and slug not in tags:
                    tags.append(slug)

        nb_dev = {
            "name": name,
            "serial": serial,
            "model": model,
            "site": site,
            "ha_role": role,
            "ha_reason": ha["reason"],
            "tags": tags,
            "custom_fields": {
                "scm_ha_state": _s(dev.get("ha_state")) or None,
                "scm_ha_peer": peer_name or None,
                "scm_folder": folder or None,
                "scm_software_version": _s(dev.get("software_version")) or None,
                "scm_connected": bool(dev.get("is_connected")) if dev.get("is_connected") is not None else None,
                "scm_device_id": _s(dev.get("id")) or None,
            },
        }
        if serial in existing_by_serial:
            nb_dev["netbox_id"] = existing_by_serial[serial].get("id")
        plan["devices"].append(nb_dev)
        if role == "passive":
            plan["passive_devices"].append(name)
        if role == "hold":
            plan["held_devices"].append(name)
            plan["warnings"].append("%s: HA state indeterminate (%s) - IPs left untouched" % (name, ha["reason"]))

        # ---- interfaces
        ifaces = None
        used_source = None
        if source in ("operational", "auto") and serial in op_interfaces:
            ifaces, used_source = op_interfaces[serial], "operational"
        if ifaces is None and source in ("config", "auto", "operational") and \
                (source != "operational" or settings.get("fallback_to_config", True)):
            cfg = scm_merge_cfg_for_device(cfg_interfaces, dev)
            if cfg:
                ifaces, used_source = cfg, "config"
        if ifaces is None:
            ifaces = []
            plan["warnings"].append("%s: no interface data from SCM" % name)
        nb_dev["interface_source"] = used_source

        names_present = set()
        for iface in ifaces:
            iname = iface["name"]
            if ignore_re.search(iname):
                continue
            names_present.add(iname)
            nb_if = {
                "device": name,
                "name": iname,
                "type": _nb_type(iface, model, settings),
                "mgmt_only": bool(_MGMT_RE.match(iname)),
                "enabled": iface.get("enabled", True),
                "description": iface.get("description") or (
                    ("zone: %s" % iface["zone"]) if iface.get("zone") and settings.get("zone_in_description", True) else None),
                "mtu": iface.get("mtu"),
                "parent": None,
                "lag": None,
            }
            sub = _SUBIF_RE.match(iname)
            if sub:
                nb_if["parent"] = iface.get("parent") or sub.group("parent")
            if iface.get("lag") and _LAG_RE.match(iface["lag"]):
                nb_if["lag"] = iface["lag"]
            plan["interfaces"].append(nb_if)
            for unresolved in iface.get("unresolved", []):
                plan["warnings"].append("%s %s: IP '%s' is an object/variable, not imported" % (name, iname, unresolved))

            # ---- IPs (the HA rule)
            per_device = bool(per_device_re.search(iname))
            if per_device:
                allowed = (mgmt_policy == "both" and role != "hold") or \
                          (mgmt_policy == "active_only" and role in ("active", "standalone"))
            else:
                allowed = role in ("active", "standalone")
            if not allowed:
                continue
            for addr in iface.get("ipv4", []) + iface.get("ipv6", []):
                key = (addr.split("/")[0], settings.get("vrf"))
                if key in seen_ips:
                    plan["warnings"].append("%s %s: %s already planned on %s - skipped" % (name, iname, addr, seen_ips[key]))
                    continue
                seen_ips[key] = "%s %s" % (name, iname)
                plan["ip_addresses"].append({"address": addr, "device": name, "interface": iname,
                                             "per_device": per_device, "primary": False})

        # ---- management IP from the devices endpoint
        mgmt = _mgmt_cidr(dev.get("ip_address"), settings.get("mgmt_networks"),
                          int(settings.get("mgmt_default_prefix_length", 32)))
        mgmt_name = settings.get("mgmt_interface_name", "mgmt")
        mgmt_allowed = (mgmt_policy == "both" and role != "hold") or \
                       (mgmt_policy == "active_only" and role in ("active", "standalone"))
        if mgmt and mgmt_name not in names_present and settings.get("create_mgmt_interface", True):
            plan["interfaces"].append({"device": name, "name": mgmt_name, "type": settings.get("mgmt_interface_type", "1000base-t"),
                                       "mgmt_only": True, "enabled": True, "description": None,
                                       "mtu": None, "parent": None, "lag": None})
        if mgmt and mgmt_allowed and mgmt_policy != "none":
            key = (mgmt.split("/")[0], settings.get("vrf"))
            if key not in seen_ips:
                seen_ips[key] = "%s %s" % (name, mgmt_name)
                plan["ip_addresses"].append({"address": mgmt, "device": name, "interface": mgmt_name,
                                             "per_device": True, "primary": True})
            nb_dev["primary_ip4"] = mgmt if ":" not in mgmt else None
        else:
            nb_dev["primary_ip4"] = None

    # parents and LAGs must exist before children reference them
    plan["interfaces"].sort(key=lambda i: (i["device"], 1 if (i["parent"] or i["lag"]) else 0, _sort_key(i["name"])))
    return plan


def scm_ips_to_release(nb_ips, plan, device_name):
    """IPs currently on a passive/standby device in NetBox that must go.

    nb_ips: nb_lookup('ip-addresses', api_filter='device_id=N') values.
    Keeps per-device IPs that the plan still assigns to this device.
    """
    # Anything the plan assigns (to this device or, after a failover, to the
    # new active) is handled by the assign task, which moves it in place.
    keep = {ip["address"].split("/")[0] for ip in plan.get("ip_addresses", [])}
    out = []
    for ip in nb_ips or []:
        val = ip.get("value", ip) if isinstance(ip, dict) else {}
        addr = _s(val.get("address"))
        if addr and addr.split("/")[0] not in keep:
            out.append({"id": val.get("id"), "address": addr, "device": device_name})
    return out


def scm_cfg_containers(devices):
    """Containers to read config from: each folder once, then each device."""
    out, seen = [], set()
    for dev in devices or []:
        folder = _s(dev.get("folder"))
        if folder and folder not in seen:
            seen.add(folder)
            out.append({"type": "folder", "name": folder})
    for dev in devices or []:
        out.append({"type": "device", "name": _s(dev.get("serial_number"))})
    return out


def scm_cfg_collect(uri_results):
    """Turn a looped uri register (item = [kind, container]) into parse input."""
    out = []
    for r in uri_results or []:
        if r.get("status") != 200 or not isinstance(r.get("json"), (dict, list)):
            continue
        kind, container = r["item"][0], r["item"][1]
        out.append({"kind": kind, "container_type": container["type"],
                    "container": container["name"], "json": r["json"]})
    return out


class FilterModule(object):
    def filters(self):
        return {
            "scm_select_devices": scm_select_devices,
            "scm_parse_op_interfaces": scm_parse_op_interfaces,
            "scm_parse_cfg_interfaces": scm_parse_cfg_interfaces,
            "scm_netbox_plan": scm_netbox_plan,
            "scm_ips_to_release": scm_ips_to_release,
            "scm_cfg_containers": scm_cfg_containers,
            "scm_cfg_collect": scm_cfg_collect,
        }
