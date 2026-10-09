# chatmail-discard-list

A shared list of mail domains that [chatmail relays](https://github.com/chatmail/relay)
cannot deliver to (dead servers, NXDOMAIN, broken TLS certificates, closed port 25),
in postfix `transport` format, plus a script that keeps it up to date on a relay.

Without such an entry, every message to a dead domain sits in the postfix queue
for days (`maximal_queue_lifetime`), is retried again and again and, depending on
settings, comes back as a bounce. With `discard:` postfix drops it right away.

`discard:` and not `error:`: a bounce for an unreachable domain was tested and
broke sending, so the list uses `discard:` only.

## Files

| File | What it is |
|---|---|
| `transport` | The list. One `<domain> discard:` per line, full-line `#` comments. |
| `install.sh` | Installs, updates or removes everything below on a relay. |
| `sync-discard-list.sh` | Runs on the relay from cron: downloads the list, checks it, installs it as a postfix map. |
| `scripts/check.py` | Probes every entry and shows which ones look alive again. |
| `scripts/validate.py` | Format check, run by CI on every push and PR. |

## Install on a relay

```sh
curl -fsSL https://raw.githubusercontent.com/mrgluek/chatmail-discard-list/main/install.sh | sudo bash
```

This installs `/usr/local/sbin/sync-discard-list`, adds
`/etc/cron.d/chatmail-discard-list` (every 6 hours, at a random minute so that
relays do not all fetch at once) and runs the first sync.

Run the same command again to update the sync script; the cron schedule is kept.
The sync script never updates itself, only the list: self-updating code would
let anyone with write access to this repo run commands as root on every relay.

The script:

- writes the list to `/etc/postfix/transport_discard_shared` and runs `postmap`;
- **appends** `hash:/etc/postfix/transport_discard_shared` to `transport_maps`.
  Your own maps stay first, and postfix uses the first match, so your entries
  always win. It re-adds the map on every run, because `cmdeploy run` rewrites
  `main.cf` and drops `transport_maps`. If it finds `transport_maps` empty and
  `/etc/postfix/transport.db` exists, it restores your local map in front too;
- reloads postfix only when something changed, and logs changes to syslog
  (`journalctl -t chatmail-discard-sync`) and to stdout (so cron mails them).

It refuses a list (and keeps the previous one) if:

- a line is anything other than a comment or `<domain|[IPv4]> discard:`, so
  the list can never reroute mail somewhere else (`smtp:[host]`);
- it contains your own `mail_domain`, `myhostname` or `nine.testrun.org`
  (extra domains: `DISCARD_LIST_PROTECT="a.org b.org"`);
- one update adds more than 10 domains (`DISCARD_LIST_MAX_ADD`). Review the list
  and run `sync-discard-list --force` to accept it.

### Exceptions

To keep delivering to a listed domain, add it to your own `/etc/postfix/transport`
with an empty action, which means "use the default transport", then `postmap` it:

```
example.org   :
```

### Removing it

```sh
curl -fsSL https://raw.githubusercontent.com/mrgluek/chatmail-discard-list/main/install.sh | sudo bash -s -- --uninstall
```

It removes the script, the cron file and the map, and takes only the shared map
out of `transport_maps`.

## Adding or removing a domain

Open a pull request against `transport` (the PR template lists what to include):
a `# YYYY-MM-DD reason` comment above the new line, and a short note on how you
checked it (error from the postfix log, `check.py --smtp` output). CI checks the
format, and `main` only accepts PRs with a passing `validate` check.

Keep a PR to at most 10 new domains: relays refuse larger updates until their
admin reviews the list and runs `sync-discard-list --force`.

### Domains that answer but stay listed

If a listed domain answers again but should stay discarded (for example its mail
now goes to Cloudflare Email Routing, not to a chatmail relay), add a line

```
# keep: example.org <reason>
```

The daily check still probes it and shows it as `keep` in the report, but it does
not keep the `revived` issue open. CI fails on a `keep:` line without a reason or
for a domain that is not in the list.

## Daily check

The `check domains` workflow probes every entry once a day with a real SMTP
check on port 25 (see below). If the runner cannot reach port 25, it falls back
to TLS on IMAP port 993, which is a much weaker signal. If
some entries answer, it keeps one open issue labelled `revived` with the report
and closes it when nothing answers any more (entries marked `keep` do not count). A domain that answers is only a
candidate, so check it from a relay before removing it:

```sh
python3 scripts/check.py --smtp transport
```

This does what chatmail's postfix does: SMTP to the MX on port 25, STARTTLS,
and a certificate check against the MX hostname (`smtp_tls_security_level = verify`).
It also requires a valid certificate on `https://<domain>/`: a chatmail relay
serves its website with the same certificate, and clients use port 443 too.
The report shows how many days both certificates have left and warns when
fewer than 7 remain.

A chatmail relay uses one certificate for mail and for the website. When the
two differ, an alive entry is marked "probably not a chatmail relay" (for example
a domain whose mail goes to Cloudflare Email Routing). That alone is no reason to
discard a domain, but it is worth a closer look before removing it from the list.
