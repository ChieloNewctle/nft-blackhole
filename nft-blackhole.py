#!/usr/bin/env python3

"""Script to blocking IP in nftables by country and black lists"""

__author__ = "Tomasz Cebula <tomasz.cebula@gmail.com>"
__license__ = "MIT"
__version__ = "1.1.0"

import argparse
import os.path
import re
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from string import Template
from subprocess import run
from sys import stderr

from yaml import safe_load

ROOT = os.path.dirname(os.path.realpath(__file__))

# Get config
with open("/etc/nft-blackhole/nft-blackhole.conf") as cnf:
    config = safe_load(cnf)

WHITELIST = config["WHITELIST"]
BLACKLIST = config["BLACKLIST"]
COUNTRY_LIST = config["COUNTRY_LIST"]
BLOCK_OUTPUT = config["BLOCK_OUTPUT"]
BLOCK_FORWARD = config["BLOCK_FORWARD"]
GH_BASE_URL = config["GH_BASE_URL"]
TIMEOUT = config["TIMEOUT"]
RETRIES = config["RETRIES"]
STATUS_SKIP_RETRYING = frozenset(config["STATUS_SKIP_RETRYING"])


# Correct incorrect YAML parsing of NO (Norway)
# It should be the string 'no', but YAML interprets it as False
# This is a hack due to the lack of YAML 1.2 support by PyYAML
while False in COUNTRY_LIST:
    COUNTRY_LIST[COUNTRY_LIST.index(False)] = "no"

SET_TEMPLATE = (
    "table inet blackhole {\n\tset ${set_name} {\n\t\ttype ${ip_ver}_addr\n"
    "\t\tflags interval\n\t\tauto-merge\n\t\telements = { ${ip_list} }\n\t}\n}"
).expandtabs(4)

FORWARD_TEMPLATE = (
    "\tchain forward {\n\t\ttype filter hook forward priority -1; policy ${default_policy};\n"
    "\t\tct state established,related accept\n"
    "\t\tip saddr @whitelist-v4 counter accept\n"
    "\t\tip6 saddr @whitelist-v6 counter accept\n"
    "\t\tip saddr @blacklist-v4 counter ${block_policy}\n"
    "\t\tip6 saddr @blacklist-v6 counter ${block_policy}\n"
    "\t\t${country_ex_ports_rule}\n"
    "\t\tip saddr @country-v4 counter ${country_policy}\n"
    "\t\tip6 saddr @country-v6 counter ${country_policy}\n"
    "\t\tcounter\n\t}"
).expandtabs(4)

OUTPUT_TEMPLATE = (
    "\tchain output {\n\t\ttype filter hook output priority -1; policy accept;\n"
    "\t\tip daddr @whitelist-v4 counter accept\n"
    "\t\tip6 daddr @whitelist-v6 counter accept\n"
    "\t\tip daddr @blacklist-v4 counter ${block_policy}\n"
    "\t\tip6 daddr @blacklist-v6 counter ${block_policy}\n\t}"
).expandtabs(4)

COUNTRY_EX_PORTS_TEMPLATE = (
    "meta l4proto { tcp, udp } th dport { ${country_ex_ports} } counter accept"
)

IP_VER = []
for ip_v in ["v4", "v6"]:
    if config["IP_VERSION"][ip_v]:
        IP_VER.append(ip_v)

BLOCK_POLICY = "reject" if config["BLOCK_POLICY"] == "reject" else "drop"
COUNTRY_POLICY = "accept" if config["COUNTRY_POLICY"] == "accept" else "block"
COUNTRY_EXCLUDE_PORTS = config["COUNTRY_EXCLUDE_PORTS"]

if COUNTRY_POLICY == "block":
    default_policy = "accept"
    block_policy = BLOCK_POLICY
    country_policy = BLOCK_POLICY
else:
    default_policy = BLOCK_POLICY
    block_policy = BLOCK_POLICY
    country_policy = "accept"

if COUNTRY_EXCLUDE_PORTS:
    country_ex_ports = ", ".join(map(str, config["COUNTRY_EXCLUDE_PORTS"]))
    country_ex_ports_rule = Template(COUNTRY_EX_PORTS_TEMPLATE).substitute(
        country_ex_ports=country_ex_ports
    )
else:
    country_ex_ports_rule = ""

if BLOCK_OUTPUT:
    chain_output = Template(OUTPUT_TEMPLATE).substitute(block_policy=block_policy)
else:
    chain_output = ""

if BLOCK_FORWARD:
    chain_forward = Template(FORWARD_TEMPLATE).substitute(
        default_policy=default_policy,
        block_policy=block_policy,
        country_policy=country_policy,
        country_ex_ports_rule=country_ex_ports_rule,
    )
else:
    chain_forward = ""

# Setting urllib
ctx = ssl.create_default_context()
IGNORE_CERTIFICATE = False
if IGNORE_CERTIFICATE:
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

https_handler = urllib.request.HTTPSHandler(context=ctx)

opener = urllib.request.build_opener(https_handler)
# opener.addheaders = [('User-agent', 'Mozilla/5.0 (X11; Linux x86_64)')]
opener.addheaders = [
    (
        "User-agent",
        f"Mozilla/5.0 (compatible; nft-blackhole/{__version__}; "
        "+https://github.com/tomasz-c/nft-blackhole)",
    )
]
urllib.request.install_opener(opener)


def stop():
    """Stopping nft-blackhole"""
    run(["nft", "delete", "table", "inet", "blackhole"], check=False)


def start():
    """Starting nft-blackhole"""
    nft_template = open(os.path.join(ROOT, "nft-blackhole.template")).read()
    nft_conf = Template(nft_template).substitute(
        default_policy=default_policy,
        block_policy=block_policy,
        country_ex_ports_rule=country_ex_ports_rule,
        country_policy=country_policy,
        chain_output=chain_output,
        chain_forward=chain_forward,
    )

    run(["nft", "-f", "-"], input=nft_conf.encode(), check=True)


def get_urls(urls):
    """Download url in threads"""
    ip_list_aggregated = []

    def get_cidr_or_url(url):
        if not any(map(url.startswith, ["http://", "https://"])):
            return [url]

        ip_list = []
        for _ in range(RETRIES):
            try:
                response = urllib.request.urlopen(url, timeout=TIMEOUT)
                content = response.read().decode("utf-8")
            except BaseException as exc:
                print("WARN", getattr(exc, "message", repr(exc)), url, file=stderr)
                if (
                    isinstance(exc, urllib.error.HTTPError)
                    and exc.code in STATUS_SKIP_RETRYING
                ):
                    break
                print("RETRY", url, file=stderr)
                continue
            content = re.sub(r"[#;].*", "", content, flags=re.MULTILINE)
            ip_list = list(filter(bool, map(str.strip, content.splitlines())))
            break
        else:
            print("ERROR", "failed to fetch:", url, file=stderr)
            return None
        return ip_list

    with ThreadPoolExecutor(max_workers=8) as executor:
        do_urls = [executor.submit(get_cidr_or_url, url) for url in urls]
        for out in as_completed(do_urls):
            ip_list = out.result()
            if ip_list is None:
                return None
            ip_list_aggregated.extend(ip_list)
    return sorted(frozenset(ip_list_aggregated))


def get_blacklist(ip_ver):
    """Get blacklists"""
    urls = []
    for bl_url in BLACKLIST[ip_ver]:
        urls.append(bl_url)
    ips = get_urls(urls)
    return ips


def get_whitelist(ip_ver):
    """Get blacklists"""
    urls = []
    for bl_url in WHITELIST[ip_ver]:
        urls.append(bl_url)
    ips = get_urls(urls)
    return ips


def get_country_ip_list(ip_ver):
    """Get country lists from multiple sources"""
    urls = []
    for country in COUNTRY_LIST:
        url = {
            "v4": f"https://www.ipdeny.com/ipblocks/data/aggregated/{country.lower()}-aggregated.zone",
            "v6": f"https://www.ipdeny.com/ipv6/ipaddresses/aggregated/{country.lower()}-aggregated.zone",
        }[ip_ver]
        urls.append(url)
        url = f"{GH_BASE_URL}/ipverse/rir-ip/master/country/{country.lower()}/ip{ip_ver.lower()}-aggregated.txt"
        urls.append(url)
        url = f"{GH_BASE_URL}/herrbischoff/country-ip-blocks/master/ip{ip_ver}/{country.lower()}.cidr"
        urls.append(url)
    ips = get_urls(urls)
    return ips


def whitelist_sets(reload=False, dry_run=False):
    """Create whitelist sets"""
    for ip_ver in IP_VER:
        set_name = f"whitelist-{ip_ver}"
        ip_list = get_whitelist(ip_ver)
        if ip_list is None:
            print(
                "ERROR",
                f"FAILED to build whitelist, skip update {set_name}",
                file=stderr,
            )
            continue
        set_list = ", ".join(ip_list)
        nft_set = Template(SET_TEMPLATE).substitute(
            ip_ver=f"ip{ip_ver}", set_name=set_name, ip_list=set_list
        )
        if dry_run:
            print(nft_set)
            continue
        if reload:
            run(["nft", "flush", "set", "inet", "blackhole", set_name], check=False)
        if WHITELIST[ip_ver]:
            run(["nft", "-f", "-"], input=nft_set.encode(), check=True)


def blacklist_sets(reload=False, dry_run=False):
    """Create blacklist sets"""
    for ip_ver in IP_VER:
        set_name = f"blacklist-{ip_ver}"
        ip_list = get_blacklist(ip_ver)
        if ip_list is None:
            print(
                "ERROR",
                f"FAILED to build blacklist, skip update {set_name}",
                file=stderr,
            )
            continue
        set_list = ", ".join(ip_list)
        nft_set = Template(SET_TEMPLATE).substitute(
            ip_ver=f"ip{ip_ver}", set_name=set_name, ip_list=set_list
        )
        if dry_run:
            print(nft_set)
            continue
        if reload:
            run(["nft", "flush", "set", "inet", "blackhole", set_name], check=False)
        if ip_list:
            run(["nft", "-f", "-"], input=nft_set.encode(), check=True)


def country_sets(reload=False, dry_run=False):
    """Create country sets"""
    for ip_ver in IP_VER:
        set_name = f"country-{ip_ver}"
        ip_list = get_country_ip_list(ip_ver)
        if ip_list is None:
            if COUNTRY_POLICY == "block":
                print(
                    "ERROR",
                    f"FAILED to build country sets, skip update {set_name}",
                    file=stderr,
                )
                continue
            else:
                print(
                    "ERROR",
                    "FAILED to build country sets, allowing everything",
                    file=stderr,
                )
                ip_list = {"v4": ["0.0.0.0/0"], "v6": ["::/0"]}[ip_ver]
        set_list = ", ".join(ip_list)
        nft_set = Template(SET_TEMPLATE).substitute(
            ip_ver=f"ip{ip_ver}", set_name=set_name, ip_list=set_list
        )
        if dry_run:
            print(nft_set)
            continue
        if reload:
            run(["nft", "flush", "set", "inet", "blackhole", set_name], check=False)
        if ip_list:
            run(["nft", "-f", "-"], input=nft_set.encode(), check=True)


def main():
    desc = "Script to blocking IP in nftables by country and black lists"
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument(
        "action",
        choices=("start", "stop", "restart", "reload", "dry-run"),
        help="Action to nft-blackhole",
    )
    args = parser.parse_args()
    action = args.action

    if action == "start":
        start()
        whitelist_sets()
        blacklist_sets()
        country_sets()
    elif action == "stop":
        stop()
    elif action == "restart":
        stop()
        start()
        whitelist_sets()
        blacklist_sets()
        country_sets()
    elif action == "reload":
        whitelist_sets(reload=True)
        blacklist_sets(reload=True)
        country_sets(reload=True)
    elif action == "dry-run":
        whitelist_sets(dry_run=True)
        blacklist_sets(dry_run=True)
        country_sets(dry_run=True)


# Main
if __name__ == "__main__":
    main()
