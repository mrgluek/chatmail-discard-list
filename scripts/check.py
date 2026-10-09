#!/usr/bin/env python3
"""Probe every domain in the discard list and report which ones look alive again.

Two modes:

  --smtp    SMTP to the MX on port 25, EHLO, STARTTLS and a certificate check
            against the MX hostname. This is what chatmail's postfix does
            (smtp_tls_security_level = verify, cert must match the MX hostname;
            IP literals only need encryption).
  default   For hosts that cannot connect out to port 25: TLS on the MX's
            IMAP port 993 instead. Weak signal: port 25 may still be closed.

In both modes a domain also needs a valid certificate for https://<domain>/,
which every chatmail relay serves with the same certificate. Days until the
certificates expire are shown, with a warning below EXPIRY_WARN_DAYS.

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
import time
import urllib.parse
import urllib.request

TIMEOUT = 15
EXPIRY_WARN_DAYS = 7
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


def tls_context(verify_name):
    ctx = ssl.create_default_context()
    if verify_name is None:   # IP literal: postfix only requires encryption
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def days_left(tls_sock):
    """Days until the peer certificate expires, None if it was not verified."""
    cert = tls_sock.getpeercert()   # {} when verification is off
    if not cert:
        return None
    return int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) // 86400)


def tls_connect(host, port, verify_name):
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        with tls_context(verify_name).wrap_socket(sock, server_hostname=verify_name) as tls:
            return days_left(tls)


def smtp_starttls(host, verify_name):
    with smtplib.SMTP(host, 25, timeout=TIMEOUT, local_hostname=socket.getfqdn()) as s:
        s.ehlo()
        if not s.has_extn("starttls"):
            raise RuntimeError("no STARTTLS")
        s.starttls(context=tls_context(verify_name))
        days = days_left(s.sock)
        s.ehlo()
        return days


def cert_note(days):
    if days is None:
        return ""
    return f", cert {days}d left" + (" (expires soon!)" if days < EXPIRY_WARN_DAYS else "")


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

    # 1. Mail: the first MX that works, the way postfix tries them.
    port = 25 if smtp else 993
    mail, errors = None, []
    for host in hosts:
        name = host if verify else None
        try:
            days = smtp_starttls(host, name) if smtp else tls_connect(host, port, name)
            mail = f"{host}:{port} ok{cert_note(days)}"
            break
        except Exception as e:
            errors.append(f"{host}:{port} {short(e)}")
    if mail is None:
        return False, "; ".join(errors)
    if not verify:
        return True, mail

    # 2. Website: a chatmail relay serves https://<mail_domain>/ with the same
    #    certificate. Clients use it too (account creation, webxdc, ALPN on 443),
    #    so a broken certificate there means the relay is not really usable.
    try:
        days = tls_connect(key, 443, key)
    except Exception as e:
        return False, f"{mail}; https://{key} {short(e)}"
    return True, f"{mail}; https ok{cert_note(days)}"


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
    mode = "SMTP port 25 + HTTPS" if args.smtp else "IMAP 993 + HTTPS, no port 25"
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
