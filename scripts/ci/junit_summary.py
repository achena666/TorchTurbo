#!/usr/bin/env python3
"""Render a JUnit XML report as GitHub-flavored Markdown.

Usage:
    python junit_summary.py <junit.xml>

Writes to stdout so the caller can append it to $GITHUB_STEP_SUMMARY. A missing
or unparsable report is not an error: the summary is a reporting step and must
never mask the real test outcome.
"""

from __future__ import annotations

import json
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


def parse(path: str) -> dict:
    """Counts from a JUnit report. Never raises -- a missing report is data.

    ``result.py`` imports this so the summary and the annotation cannot disagree
    about how many tests failed.
    """
    doc = {
        "path": path,
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "seconds": 0.0,
        "failed_cases": [],
        "error": "",
    }
    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError:
        doc["error"] = f"no JUnit report at {path} (tests may not have run)"
        return doc
    except ET.ParseError as exc:
        doc["error"] = f"unparsable JUnit report {path}: {exc}"
        return doc

    suites = _suites(root)
    doc["total"] = sum(int(s.get("tests", 0)) for s in suites)
    doc["failed"] = sum(int(s.get("failures", 0)) for s in suites)
    doc["errors"] = sum(int(s.get("errors", 0)) for s in suites)
    doc["skipped"] = sum(int(s.get("skipped", 0)) for s in suites)
    doc["seconds"] = sum(float(s.get("time", 0) or 0) for s in suites)
    doc["passed"] = doc["total"] - doc["failed"] - doc["errors"] - doc["skipped"]
    doc["failed_cases"] = _failed_cases(suites)
    return doc


def render(doc: dict) -> str:
    if doc["error"]:
        return f"_{doc['error']}_\n"

    lines = [
        "### Test result",
        "",
        "| total | passed | failed | errors | skipped | duration |",
        "|---|---|---|---|---|---|",
        f"| {doc['total']} | {doc['passed']} | {doc['failed']} | "
        f"{doc['errors']} | {doc['skipped']} | {doc['seconds']:.1f} s |",
        "",
    ]
    if doc["failed_cases"]:
        lines += ["### Failed cases", ""]
        lines += [f"- `{name}`" for name in doc["failed_cases"]]
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    path = "/tmp/pytest.xml"
    as_json = False
    for arg in argv[1:]:
        if arg == "--json":
            as_json = True
        else:
            path = arg

    doc = parse(path)
    if as_json:
        print(json.dumps(doc, ensure_ascii=False))
    else:
        sys.stdout.write(render(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
