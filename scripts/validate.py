#!/usr/bin/env python3
"""Check the format of the transport file. Run by CI on every push and PR.

Uses the same rules as sync-discard-list.sh, so a list that passes here is
accepted by the relays: only comments, empty lines and "<domain> discard:".
"""
import re
import sys

DOMAIN = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"
IPV4 = r"\[[0-9]{1,3}(\.[0-9]{1,3}){3}\]"
ENTRY = re.compile(rf"^({DOMAIN}|{IPV4})\s+discard:\s*$")
# Discarding these would cut off a large part of the chatmail network.
PROTECTED = {"nine.testrun.org"}


def main(path):
    errors, seen, keeps = [], {}, {}
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if line.lstrip().startswith("#"):
                parts = line.lstrip()[1:].split(None, 2)
                if parts and parts[0] == "keep:":
                    if len(parts) < 3:
                        errors.append(f"{path}:{n}: expected '# keep: <domain> <reason>'")
                    else:
                        keeps[parts[1]] = n
                continue
            if not line.strip():
                continue
            if not ENTRY.match(line):
                errors.append(f"{path}:{n}: expected '<lowercase domain> discard:', got {line!r}")
                continue
            key = line.split()[0]
            if key in seen:
                errors.append(f"{path}:{n}: {key} duplicates line {seen[key]}")
            seen[key] = n
            if key in PROTECTED:
                errors.append(f"{path}:{n}: {key} is protected and must not be discarded")
    # A keep for a domain that is no longer listed is stale and would be confusing.
    for key, n in keeps.items():
        if key not in seen:
            errors.append(f"{path}:{n}: keep: {key} is not in the list")
    for e in errors:
        print(e)
    print(f"{len(seen)} entries, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "transport"))
