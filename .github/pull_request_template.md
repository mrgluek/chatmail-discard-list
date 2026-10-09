<!-- Thank you! Please fill in what applies and delete the rest. -->

### Change

- [ ] Add domain(s)
- [ ] Remove domain(s) (revived)
- [ ] Add or remove a `# keep:` mark
- [ ] Scripts / docs

### Why

<!--
Adding: a "# YYYY-MM-DD reason" comment above the new lines, and here the evidence,
e.g. a postfix log line ("Host or domain name not found", "certificate verification
failed", "Connection refused") or the output of:
    python3 scripts/check.py --smtp transport
Removing: the check.py --smtp output showing it answers again, ideally a test
message that arrived.
-->

### Checklist

- [ ] At most 10 new domains (relays refuse bigger updates until an admin runs `sync-discard-list --force`)
- [ ] Only `discard:` entries, lowercase, one per line
- [ ] The domain is really dead, not just slow or briefly down
