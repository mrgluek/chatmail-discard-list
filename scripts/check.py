#!/usr/bin/env python3
"""Probe every domain in the discard list and report which ones look alive again.

Two modes:

  default   For hosts that cannot connect out to port 25: checks DNS and TLS
            on 443 (web/ALPN) and 993 (IMAP). Weak signal: a relay can serve
            its website and still have port 25 closed.
  --smtp    Run on a relay: SMTP to the MX on port 25, EHLO, STARTTLS and a
            certificate check against the MX hostname. This is what chatmail's
            postfix does (smtp_tls_security_level = verify, cert must match the
            MX hostname; IP literals only need encryption).

Only the Python standard library is used. DNS goes through Cloudflare DoH
because the stdlib cannot look up MX records.
"""
import argparse
import concurrent.futures
import json
import smtplib
import socket
import ssl
import sys
import urllib.parse
import urllib.request

TIMEOUT = 15
RTYPES = {"A": 1, "MX": 15, "AAAA": 28}


def read_entries(path):
    keys = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                keys.append(line.split()[0])
    return keys


def doh(name, rtype):
    """Return (rcode, [record data]) for one DNS query."""
    url = "https://cloudflare-dns.com/dns-query?" + urllib.parse.urlencode({"name": name, "type": rtype})
    req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
    # CNAMEs also show up in Answer, keep only the requested type.
    return data["Status"], [a["data"] for a in data.get("Answer", []) if a["type"] == RTYPES[rtype]]


def mail_hosts(domain):
    """MX hosts in preference order, or a string explaining why there are none."""
    rcode, mx = doh(domain, "MX")
    if rcode == 3:
        return "NXDOMAIN"
    if rcode != 0:
        return f"DNS error rcode={rcode}"
    if mx:
        # "10 mx.example.org." -> lowest preference first. "0 ." is a null MX
        # (RFC 7505): the domain says it accepts no mail at all.
        hosts = [r.split()[1].rstrip(".") for r in sorted(mx, key=lambda r: int(r.split()[0]))]
        hosts = [h for h in hosts if h]
        return hosts or "null MX"
    # No MX: SMTP falls back to the domain's own address (RFC 5321 implicit MX).
    if doh(domain, "A")[1] or doh(domain, "AAAA")[1]:
        return [domain]
    return "no MX and no address"


def tls_connect(host, port, verify_name):
    ctx = ssl.create_default_context()
    if verify_name is None:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=verify_name):
            pass


def smtp_starttls(host, verify_name):
    ctx = ssl.create_default_context()
    if verify_name is None:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with smtplib.SMTP(host, 25, timeout=TIMEOUT, local_hostname=socket.getfqdn()) as s:
        s.ehlo()
        if not s.has_extn("starttls"):
            raise RuntimeError("no STARTTLS")
        s.starttls(context=ctx)
        s.ehlo()


def short(exc):
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"TLS cert: {exc.verify_message}"
    return f"{type(exc).__name__}: {exc}"[:120]


def probe(key, smtp):
    """Return (alive, detail) for one transport key."""
    if key.startswith("["):
        hosts, verify = [key.strip("[]")], False   # IP literal: no name to verify
    else:
        try:
            hosts = mail_hosts(key)
        except Exception as e:
            return False, f"DNS lookup failed: {short(e)}"
        if isinstance(hosts, str):
            return False, hosts
        verify = True

    errors = []
    for host in hosts:
        name = host if verify else None
        if smtp:
            try:
                smtp_starttls(host, name)
                return True, f"{host}:25 STARTTLS ok"
            except Exception as e:
                errors.append(f"{host}:25 {short(e)}")
        else:
            for port in (443, 993):
                try:
                    tls_connect(host, port, name)
                    return True, f"{host}:{port} TLS ok"
                except Exception as e:
                    errors.append(f"{host}:{port} {short(e)}")
    return False, "; ".join(errors)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transport", nargs="?", default="transport")
    ap.add_argument("--smtp", action="store_true", help="probe port 25 (run on a relay)")
    ap.add_argument("--markdown", help="write a markdown report to this file")
    ap.add_argument("--alive", help="write the alive keys to this file, one per line")
    args = ap.parse_args()

    keys = read_entries(args.transport)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(keys, pool.map(lambda k: probe(k, args.smtp), keys)))

    alive = [k for k in keys if results[k][0]]
    mode = "SMTP port 25" if args.smtp else "DNS + TLS on 443/993"
    lines = [f"Checked {len(keys)} entries ({mode}): **{len(alive)} look alive**.", "",
             "| Entry | Status | Detail |", "|---|---|---|"]
    for k in keys:
        ok, detail = results[k]
        lines.append(f"| `{k}` | {'alive' if ok else 'dead'} | {detail.replace('|', '/')} |")
    report = "\n".join(lines) + "\n"

    print(report)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(report)
    if args.alive:
        with open(args.alive, "w", encoding="utf-8") as f:
            f.writelines(k + "\n" for k in alive)


if __name__ == "__main__":
    sys.exit(main())
