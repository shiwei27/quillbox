#!/usr/bin/env python3
"""Run the test scripts by tier.

    python run_tests.py                 # unit tier (offline: no server, browser, mailbox or model needed)
    python run_tests.py --tier vector   # needs the [vector] extra (chromadb)
    python run_tests.py --tier windows  # Windows-only (Outlook COM, wmic, DPAPI)
    python run_tests.py --tier integration   # needs a running instance on 127.0.0.1:8765, Playwright,
                                             # real mailboxes and/or a configured model
    python run_tests.py --list          # show every test and its tier
    python run_tests.py test_rule_match.py test_kbnav.js   # run named tests

Each test is a standalone script (exit code 0 = pass). Tiers are declared in test_tiers.txt; a test_* file
that is not listed there is reported as an error so new tests cannot silently skip CI.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TIERS = ("unit", "vector", "windows", "integration")


def load_tiers():
    tiers = {}
    for n, raw in enumerate((ROOT / "test_tiers.txt").read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tier, _, name = line.partition(" ")
        name = name.strip()
        if tier not in TIERS or not name:
            sys.exit("test_tiers.txt:%d: expected '<%s> <file>'" % (n, "|".join(TIERS)))
        tiers[name] = tier
    return tiers


def discover():
    return sorted(p.name for p in ROOT.glob("test_*") if p.suffix in (".py", ".js"))


def run_one(name, timeout):
    if name.endswith(".js"):
        node = shutil.which("node")
        if not node:
            return "SKIP", 0.0, "node not found"
        cmd = [node, name]
    else:
        cmd = [sys.executable, name]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return "FAIL", time.time() - t0, ((e.stdout or b"").decode("utf-8", "replace") + "\n[timeout after %ss]" % timeout)
    out = p.stdout.decode("utf-8", "replace")
    return ("PASS" if p.returncode == 0 else "FAIL"), time.time() - t0, out


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles default to cp1252
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tests", nargs="*")
    ap.add_argument("--tier", default="unit", choices=TIERS)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--timeout", type=int, default=120, help="seconds per test (default 120)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print output of passing tests too")
    a = ap.parse_args()

    tiers = load_tiers()
    found = discover()
    unlisted = [t for t in found if t not in tiers]
    missing = [t for t in tiers if t not in found]
    if a.list:
        for t in found:
            print("%-12s %s" % (tiers.get(t, "UNLISTED"), t))
    if unlisted or missing:
        for t in unlisted:
            print("ERROR: %s is not listed in test_tiers.txt" % t)
        for t in missing:
            print("ERROR: test_tiers.txt lists %s but the file does not exist" % t)
        return 2
    if a.list:
        return 0

    todo = a.tests or [t for t in found if tiers[t] == a.tier]
    print("running %d test(s)%s" % (len(todo), "" if a.tests else " in tier '%s'" % a.tier))
    results = []
    for t in todo:
        status, dt, out = run_one(t, a.timeout)
        results.append((t, status))
        print("%-4s %6.1fs  %s" % (status, dt, t), flush=True)
        if status == "FAIL" or a.verbose:
            tail = out.strip().splitlines()[-40:]
            print("\n".join("      | " + l for l in tail), flush=True)
    failed = [t for t, s in results if s == "FAIL"]
    skipped = [t for t, s in results if s == "SKIP"]
    print("\n%d passed, %d failed, %d skipped" % (len(results) - len(failed) - len(skipped), len(failed), len(skipped)))
    if failed:
        print("failed: " + " ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
