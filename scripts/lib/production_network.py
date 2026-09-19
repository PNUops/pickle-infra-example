"""Pure validation and scoped firewall plans for the two-node guest network."""
import ipaddress
import re
import shlex

if not __debug__:
    raise RuntimeError("production network safety checks require normal Python mode")

CHAINS = {
    "raw": "PKL-PROD-RAW",
    "mangle": "PKL-PROD-MARK",
    "input": "PKL-PROD-IN",
    "forward": "PKL-PROD-FWD",
    "nat": "PKL-PROD-NAT",
}


class NetBirdMarkNotReady(AssertionError):
    """NetBird has not recreated one of the two required mark rules yet."""


def validate_config(config):
    assert config["schema"] == 1 and len(config["nodes"]) == 2
    assert config["gateway_owner"] in config["nodes"]
    assert set(config["vnets"]) == {"pinfra", "pguest"}
    assert config["host_mtu"] == 1420 and config["guest_mtu"] == 1370
    for value in [config["cluster"], config["zone"], config["uplink"], config["mesh_interface"], *config["nodes"]]:
        assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,30}", value)
    networks = []
    addresses = set()
    for node in config["nodes"].values():
        for key in ("campus", "mesh"):
            address = ipaddress.IPv4Address(node[key])
            assert str(address) not in addresses
            addresses.add(str(address))
        if node.get("bmc_interface"):
            assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,14}", node["bmc_interface"])
            ipaddress.IPv4Address(node["bmc_address"])
    for vnet in config["vnets"].values():
        network = ipaddress.IPv4Network(vnet["cidr"])
        assert 16 <= network.prefixlen <= 24 and ipaddress.IPv4Address(vnet["gateway"]) in network
        assert 1 <= vnet["vni"] <= 16777215
        assert not any(network.overlaps(other) for other in networks)
        networks.append(network)
    for node in config["nodes"].values():
        for key in ("campus", "mesh", "bmc_address"):
            if node.get(key):
                assert not any(ipaddress.IPv4Address(node[key]) in network for network in networks), "guest ranges overlap host management"
    assert len({v["vni"] for v in config["vnets"].values()}) == 2
    for value in config["service_sources"].values():
        ipaddress.IPv4Address(value)
    if "backup_service" in config:
        backup = config["backup_service"]
        assert isinstance(backup, dict) and set(backup) == {"source", "destination", "port"}
        source = ipaddress.IPv4Address(backup["source"])
        destination = ipaddress.IPv4Address(backup["destination"])
        assert type(backup["port"]) is int and backup["port"] == 8007
        networks = [ipaddress.IPv4Network(vnet["cidr"]) for vnet in config["vnets"].values()]
        infra = ipaddress.IPv4Network(config["vnets"]["pinfra"]["cidr"])
        assert source in infra and source not in (infra.network_address, infra.broadcast_address)
        assert source != ipaddress.IPv4Address(config["vnets"]["pinfra"]["gateway"])
        assert all(destination not in network for network in networks)
        host_addresses = {ipaddress.IPv4Address(value) for node in config["nodes"].values()
                          for key, value in node.items() if key in ("campus", "mesh", "bmc_address") and value}
        assert destination not in host_addresses
    return config


def parse_netbird_accept_mark(filter_rules, mangle_rules, pve_source):
    """Refuse unknown mark layouts rather than clearing unrelated skb bits."""
    accepted = set()
    mark_accept_lines = []
    for line in filter_rules.splitlines():
        tokens = shlex.split(line)
        if (tokens[:2] == ["-A", "FORWARD"] and tokens[-2:] == ["-j", "ACCEPT"]
                and ("mark" in tokens or "--mark" in tokens)):
            mark_accept_lines.append(tokens)
        if len(tokens) == 8 and tokens[:5] == ["-A", "FORWARD", "-m", "mark", "--mark"] and tokens[-2:] == ["-j", "ACCEPT"]:
            value, _, mask = tokens[5].partition("/")
            accepted.add((int(value, 0), int(mask or "0xffffffff", 0)))
    assert len(mark_accept_lines) == len(accepted), "unknown global accept mark layout"
    setters = set()
    for line in mangle_rules.splitlines():
        tokens = shlex.split(line)
        if tokens[:2] != ["-A", "NETBIRD-RT-PRE"] or "--set-xmark" not in tokens:
            continue
        assert "-i" in tokens and tokens[tokens.index("-i") + 1] == "wt0", "unknown NetBird ingress"
        assert "--dst-type" in tokens and tokens[tokens.index("--dst-type") + 1] == "LOCAL", "NetBird routing marks are not permitted"
        value, _, mask = tokens[tokens.index("--set-xmark") + 1].partition("/")
        setters.add((int(value, 0), int(mask or "0xffffffff", 0)))
    assert len(accepted) <= 1, "ambiguous global accept mark"
    assert len(setters) <= 1, "ambiguous NetBird mark setter"
    for value, mask in accepted | setters:
        assert mask == 0xffffffff and value != 0, "unknown NetBird mark setter/mask"
    match = re.search(r'\$FWACCEPTMARK_ON\s*=\s*"(0x[0-9a-fA-F]+)/(0x[0-9a-fA-F]+)"', pve_source)
    assert match, "unknown PVE acceptance mark"
    pve_mask = int(match.group(2), 0)
    for value, _mask in accepted | setters:
        assert value & pve_mask == 0, "NetBird and PVE mark bits overlap"
    if not accepted:
        raise NetBirdMarkNotReady("NetBird global accept mark is not ready")
    if not setters:
        raise NetBirdMarkNotReady("NetBird mark setter is not ready")
    candidate = next(iter(accepted))
    assert setters == {candidate}, "unknown NetBird mark setter/mask"
    return candidate[0], candidate[1], pve_mask


def validate_sdn_inventory(config, zones, vnets):
    """Only absent or exact owned objects may be changed by the installer."""
    assert all(z["zone"] == config["zone"] for z in zones), "unrelated SDN zone exists"
    assert all(v["vnet"] in config["vnets"] for v in vnets), "unrelated VNet exists"
    for zone in zones:
        assert zone["type"] == "vxlan" and int(zone["mtu"]) == config["guest_mtu"]
        assert set(zone["nodes"].split(",")) == set(config["nodes"])
        assert set(zone["peers"].split(",")) == {n["mesh"] for n in config["nodes"].values()}
        assert not any(zone.get(k) for k in ("dhcp", "controller", "fabric", "bridge", "dns", "dnszone")), "unrelated zone options"
    for vnet in vnets:
        assert vnet["zone"] == config["zone"]
        assert int(vnet["tag"]) == config["vnets"][vnet["vnet"]]["vni"]
        assert not vnet.get("vlanaware") and not vnet.get("isolate-ports"), "unexpected VNet policy"


def firewall_plan(config, node_name, accept_mark, active):
    """Generate only owned chains; RETURN leaves PVE guest filtering in force."""
    node = config["nodes"][node_name]
    peer = next(value for name, value in config["nodes"].items() if name != node_name)
    uplink, mesh = config["uplink"], config["mesh_interface"]
    v4 = {name: [] for name in CHAINS}
    v6 = {name: [] for name in CHAINS if name != "nat"}
    # Admit only the intended encrypted VTEP before host INPUT policy is evaluated.
    local_vxlan = ["-p", "udp", "--dport", "4789", "-m", "addrtype", "--dst-type", "LOCAL"]
    v4["raw"] += [["-i", mesh, "-s", peer["mesh"], *local_vxlan, "-j", "RETURN"],
                  [*local_vxlan, "-j", "DROP"]]
    v6["raw"] += [[*local_vxlan, "-j", "DROP"]]
    mark, mask, _pve_mask = accept_mark
    for family in (v4, v6):
        for vnet in config["vnets"]:
            # Exact mark match excludes packets carrying PVE or any other mark.
            family["mangle"].append(["-m", "physdev", "--physdev-in", "vxlan_" + vnet,
                                      "-m", "mark", "--mark", f"0x{mark:x}/0x{mask:x}",
                                      "-j", "MARK", "--set-xmark", f"0x0/0x{mark:x}"])
        if node.get("bmc_interface"):
            nic = node["bmc_interface"]
            family["input"] += [["-i", nic, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"],
                                ["-i", nic, "-j", "DROP"]]
            family["forward"] += [["-i", nic, "-j", "DROP"], ["-o", nic, "-j", "DROP"]]
    v4["input"] += [["-i", "pguest", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"],
                    ["-i", "pguest", "-p", "icmp", "--icmp-type", "echo-request", "-j", "RETURN"],
                    ["-i", "pguest", "-j", "DROP"],
                    ["-i", "pinfra", "-s", config["service_sources"]["api"], "-p", "tcp", "--dport", "8006", "-j", "RETURN"],
                    ["-i", "pinfra", "-p", "tcp", "-m", "multiport", "--dports", "22,8006,3128,111", "-j", "DROP"]]
    v6["input"] += [["-i", name, "-j", "DROP"] for name in config["vnets"]]
    for family in (v4, v6):
        family["forward"] += [["-i", "pguest", "-o", "pguest", "-j", "RETURN"],
                               ["-i", "pinfra", "-o", "pinfra", "-j", "RETURN"]]
    for host in config["nodes"].values():
        for destination in (host["campus"], host["mesh"]):
            v4["forward"].append(["-i", "pguest", "-d", destination, "-j", "DROP"])
    for name in config["vnets"]:
        v4["forward"].append(["-i", uplink, "-o", name, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"])
        if active:
            v4["forward"].append(["-i", name, "-o", uplink, "-j", "RETURN"])
            v4["nat"].append(["-s", config["vnets"][name]["cidr"], "-o", uplink, "-j", "MASQUERADE"])
    v4["forward"].append(["-i", "pguest", "-o", "pinfra", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"])
    for source in ("proxy", "sshgw", "relay"):
        v4["forward"].append(["-i", "pinfra", "-o", "pguest", "-s", config["service_sources"][source], "-j", "RETURN"])
    backup = config.get("backup_service")
    if active and backup is not None:
        v4["forward"] += [
            ["-i", "pinfra", "-o", mesh, "-s", backup["source"], "-d", backup["destination"],
             "-p", "tcp", "--dport", str(backup["port"]), "-j", "RETURN"],
            ["-i", mesh, "-o", "pinfra", "-s", backup["destination"], "-d", backup["source"],
             "-p", "tcp", "--sport", str(backup["port"]), "-m", "conntrack", "--ctstate",
             "ESTABLISHED,RELATED", "-j", "RETURN"],
        ]
        v4["nat"].append(["-s", backup["source"], "-d", backup["destination"], "-o", mesh,
                           "-p", "tcp", "--dport", str(backup["port"]), "-j", "MASQUERADE"])
    for family in (v4, v6):
        for name in config["vnets"]:
            family["forward"] += [["-i", name, "-j", "DROP"], ["-o", name, "-j", "DROP"]]
    return {"iptables": v4, "ip6tables": v6}


def nft_filter_rule(rule, family):
    """Translate the small owned filter vocabulary; never accept arbitrary input."""
    words, protocol = ["meta nfproto", family], None
    position = 0
    while position < len(rule):
        option, value = rule[position:position + 2]
        position += 2
        if option == "-m":
            assert value in ("conntrack", "multiport"), "unexpected match module"
        elif option in ("-i", "-o"):
            words += ["iifname" if option == "-i" else "oifname", '"' + value + '"']
        elif option in ("-s", "-d"):
            words += ["ip saddr" if option == "-s" else "ip daddr", value]
        elif option == "-p":
            protocol = value
            words += ["meta l4proto", value]
        elif option == "--ctstate":
            words += ["ct state", "{ " + ", ".join(value.lower().split(",")) + " }"]
        elif option in ("--dport", "--dports"):
            assert protocol in ("tcp", "udp")
            words += [protocol, "dport", "{ " + ", ".join(value.split(",")) + " }"]
        elif option == "--sport":
            assert protocol in ("tcp", "udp")
            words += [protocol, "sport", "{ " + ", ".join(value.split(",")) + " }"]
        elif option == "--icmp-type":
            assert protocol == "icmp" and value == "echo-request"
            words += ["icmp type", "echo-request"]
        elif option == "-j":
            assert value in ("RETURN", "DROP")
            words += ["counter", value.lower()]
        else:
            raise ValueError("unexpected owned filter option: " + option)
    return " ".join(words)


def nft_guard_text(config, node, mark, active, table):
    """Separate-priority guards cannot be bypassed by an earlier legacy ACCEPT."""
    plan = firewall_plan(config, node, mark, active)
    lines = [f"table inet {table} {{"]
    for hook, key in (("input", "input"), ("forward", "forward")):
        lines += [f" chain {hook} {{", f"  type filter hook {hook} priority -20; policy accept;"]
        if hook == "input":
            peer = next(row["mesh"] for name, row in config["nodes"].items() if name != node)
            lines += [f'  iifname "{config["mesh_interface"]}" ip saddr {peer} udp dport 4789 counter return',
                      "  udp dport 4789 counter drop"]
        for binary, family in (("iptables", "ipv4"), ("ip6tables", "ipv6")):
            lines += ["  " + nft_filter_rule(rule, family) for rule in plan[binary][key]]
        lines += [" }"]
    lines += ["}"]
    return "\n".join(lines) + "\n"
