#!/usr/bin/env python3
"""Render a JUnit XML report as GitHub-flavored Markdown.

Usage:
    python junit_summary.py <junit.xml>

Writes to stdout so the caller can append it to $GITHUB_STEP_SUMMARY. A missing
or unparsable report is not an error: the summary is a reporting step and must
never mask the real test outcome.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET


def _suites(root: ET.Element) -> list[ET.Element]:
    return [root] if root.tag == "testsuite" else root.findall("testsuite")


def _failed_cases(suites: list[ET.Element]) -> list[str]:
    failed = []
    for suite in suites:
        for case in suite.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failed.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return failed


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "/tmp/junit.xml"

    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError:
        print(f"_no JUnit report at `{path}` (tests may not have run)_")
        print("")
        return 0
    except ET.ParseError as exc:
        print(f"_failed to parse JUnit report `{path}`: {exc}_")
        print("")
        return 0

    suites = _suites(root)
    total = sum(int(s.get("tests", 0)) for s in suites)
    failures = sum(int(s.get("failures", 0)) for s in suites)
    errors = sum(int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    seconds = sum(float(s.get("time", 0) or 0) for s in suites)
    passed = total - failures - errors - skipped

    print("### Test result")
    print("")
    print("| total | passed | failed | errors | skipped | duration |")
    print("|---|---|---|---|---|---|")
    print(f"| {total} | {passed} | {failures} | {errors} | {skipped} | {seconds:.1f} s |")
    print("")

    failed = _failed_cases(suites)
    if failed:
        print("### Failed cases")
        print("")
        for name in failed:
            print(f"- `{name}`")
        print("")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
