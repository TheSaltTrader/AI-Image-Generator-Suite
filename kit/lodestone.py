#!/usr/bin/env python3
"""The Lodestone Assessment: what is actually known about this release.

A portable runner. Every sweep is declared in lodestone.toml beside this file, so this
script knows nothing about your language, your test runner or your product — it runs
what you tell it to and reports each sweep separately.

    python lodestone.py                  every sweep
    python lodestone.py --quick          skip the ones marked slow
    python lodestone.py --sweep no-loss  just that one
    python lodestone.py --list           what is declared, without running it

Four verdicts, never averaged into a score:

    PASS       the sweep exists and it passed
    FAIL       the sweep exists and it found something
    NOT RUN    the sweep exists and this machine could not host it
    NOT BUILT  the sweep does not exist yet

The last two are different problems with different fixes — one needs somebody to change
the machine, the other needs somebody to write the check — and NEITHER IS A PASS. A
percentage would invite rounding a missing sweep up to "mostly fine", so there is no
percentage. The report names what it could not check, every single run.

Exit code is 0 unless a sweep FAILED. Sweeps that are NOT BUILT or NOT RUN are reported
and do not fail the run: they are debt, and the point is that the debt is visible.

Needs Python 3.11 or later for tomllib. No other dependency.

See LODESTONE-PORTABLE.md for what each sweep is and how to build one.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import time
import tomllib

HERE = pathlib.Path(__file__).resolve().parent

PASS = "PASS"
FAIL = "FAIL"
NOTRUN = "NOT RUN"
DEBT = "NOT BUILT"


def load(path: pathlib.Path) -> dict:
    """Reads the declaration, and says something useful when it cannot."""
    if not path.exists():
        raise SystemExit(
            f"No {path.name} beside {pathlib.Path(__file__).name}.\n"
            "Copy the one from the kit and describe your own sweeps in it.")

    with open(path, "rb") as handle:
        return tomllib.load(handle)


def run(command: list[str], root: pathlib.Path, timeout: int) -> tuple[int, str]:
    """Runs a command from the project root and returns its code and everything it said.

    Both streams are captured together. A tool that writes its refusals to standard
    error and its progress to standard output is the normal case, and reading only one
    of them is how a sweep comes to report a pass for a run that complained.
    """
    try:
        done = subprocess.run(
            command, cwd=root, capture_output=True, text=True, timeout=timeout)

        return done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        return 1, f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, f"{command[0]} is not on the PATH"


def judge(sweep: dict, code: int, said: str) -> tuple[str, str]:
    """Turns one command's outcome into a verdict and a sentence.

    The order matters. "Could not run here" is decided before "failed", because a
    machine without the thing a sweep needs has not proved anything either way, and
    reporting that as a failure sends somebody hunting for a defect that is not there.
    """
    # A sweep may name a phrase that means "this machine cannot host it". Anything that
    # prints it — a missing tool, a missing permission, a missing certificate — is NOT
    # RUN rather than a pass or a failure.
    for phrase in sweep.get("not_run_when", []):
        if phrase.lower() in said.lower():
            line = next(
                (l.strip() for l in said.splitlines() if phrase.lower() in l.lower()),
                phrase)

            return NOTRUN, line

    if code != 0:
        return FAIL, sweep.get("on_failure", "the sweep found something")

    # An optional pattern pulls a number out of the output, so the report says what was
    # actually checked rather than only that something was.
    detail = sweep.get("detail", "passed")

    if (pattern := sweep.get("detail_pattern")) and (found := re.search(pattern, said)):
        try:
            detail = detail.format(*found.groups())
        except (IndexError, KeyError):
            pass

    return PASS, detail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="skip the sweeps marked slow")
    parser.add_argument("--sweep", help="run only this one")
    parser.add_argument("--list", action="store_true", help="show what is declared and stop")
    parser.add_argument("--config", default="lodestone.toml", help="which declaration to read")
    args = parser.parse_args()

    declared = load(HERE / args.config)
    project = declared.get("project", {})
    sweeps = declared.get("sweeps", {})

    root = (HERE / project.get("root", "..")).resolve()

    version = "unknown"
    if (where := project.get("version_file")) and (root / where).exists():
        version = (root / where).read_text(encoding="utf-8").strip()

    if args.sweep and args.sweep not in sweeps:
        print(f"no sweep called {args.sweep}; declared: {', '.join(sweeps)}", file=sys.stderr)
        return 2

    chosen = [args.sweep] if args.sweep else [
        name for name, sweep in sweeps.items() if not (args.quick and sweep.get("slow"))]

    if args.list:
        for name in sweeps:
            sweep = sweeps[name]
            state = "declared" if sweep.get("command") else "NOT BUILT"
            print(f"{name:<16}{state:<12}{sweep.get('purpose', '')}")
        return 0

    print()
    print(f"LODESTONE ASSESSMENT - {project.get('name', root.name)} {version}")
    print("=" * 96)

    results: list[tuple[str, str, str]] = []

    for name in chosen:
        sweep = sweeps[name]
        title = sweep.get("title", name)
        purpose = sweep.get("purpose", "")

        started = time.time()

        if not sweep.get("command"):
            verdict, detail = DEBT, sweep.get(
                "why", "nobody has written this one yet; it is reported, never assumed")
        else:
            code, said = run(sweep["command"], root, sweep.get("timeout", 1800))
            verdict, detail = judge(sweep, code, said)

        took = time.time() - started
        results.append((title, verdict, detail))

        print(f"{verdict:<10}{title:<14}{purpose}")
        print(f"{'':<24}{detail}  [{took:.0f}s]")

    print("=" * 96)

    failed = [r for r in results if r[1] == FAIL]
    debt = [r for r in results if r[1] == DEBT]
    notrun = [r for r in results if r[1] == NOTRUN]

    if failed:
        print(f"NOT CERTIFIED. {len(failed)} sweep(s) failed: "
              + ", ".join(r[0] for r in failed))
    elif debt or notrun:
        if debt:
            print(f"{len(debt)} sweep(s) do not exist yet: " + ", ".join(r[0] for r in debt))
        if notrun:
            print(f"{len(notrun)} sweep(s) could not run on this machine: "
                  + ", ".join(r[0] for r in notrun))

        print("CERTIFIED as far as it was able to check.")
        print("Neither of those is a pass. They are the list of what is still on somebody "
              "to do by hand.")
    else:
        print("CERTIFIED. Every sweep exists, ran here, and passed.")

    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
