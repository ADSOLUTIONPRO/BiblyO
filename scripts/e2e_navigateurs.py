#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Lance les parcours de bout en bout sur Chromium, WebKit et Firefox.

Trois issues par moteur, jamais confondues : « réussi », « échec », « indisponible » (le navigateur ne démarre pas ici,
donc aucun test n'a été exécuté). Le code de sortie n'est nul que si tous les moteurs ont réussi.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINES = ("chromium", "webkit", "firefox")
OUTPUT = ROOT / ".cache" / "e2e-navigateurs"
LAUNCH_TIMEOUT_MS = 45_000
PROBE = """
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = getattr(playwright, sys.argv[1]).launch(timeout=int(sys.argv[2]))
    print(browser.version)
    browser.close()
"""


@dataclass
class Result:
    engine: str
    status: str
    version: str = ""
    tests: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    detail: str = ""


def probe(engine: str) -> tuple[bool, str]:
    """Le navigateur démarre-t-il ? Sans démarrage, aucun test ne doit être présenté comme exécuté."""
    command = [sys.executable, "-c", PROBE, engine, str(LAUNCH_TIMEOUT_MS)]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=LAUNCH_TIMEOUT_MS / 1000 + 30
        )
    except subprocess.TimeoutExpired:
        return False, "délai dépassé au lancement"
    if done.returncode == 0:
        return True, done.stdout.strip().splitlines()[-1]
    lines = [line for line in done.stderr.splitlines() if "Error" in line or "Timeout" in line]
    return False, (lines[-1] if lines else done.stderr.strip()[-300:])[:300]


def read_report(result: Result, report: Path) -> None:
    suite = ElementTree.parse(report).getroot().find("testsuite")  # noqa: S314 : fichier produit à l'instant
    assert suite is not None
    errors = int(suite.get("errors", "0"))
    failures = int(suite.get("failures", "0"))
    result.tests = int(suite.get("tests", "0"))
    result.skipped = int(suite.get("skipped", "0"))
    result.failed = errors + failures
    result.passed = result.tests - result.failed - result.skipped
    for case in suite.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            result.failures.append(f"{case.get('classname')}::{case.get('name')}")


def run_engine(engine: str, selection: list[str]) -> Result:
    started, detail = probe(engine)
    if not started:
        return Result(engine, "indisponible", detail=detail)
    report = OUTPUT / f"{engine}.xml"
    log = OUTPUT / f"{engine}.log"
    command = [
        sys.executable,
        "-m",
        "pytest",
        *selection,
        "-m",
        "e2e",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]
    with log.open("w", encoding="utf-8") as handle:
        done = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "BIBLYO_E2E_NAVIGATEUR": engine},
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    result = Result(engine, "échec", version=detail)
    if report.is_file():
        read_report(result, report)
    result.status = "réussi" if done.returncode == 0 and result.failed == 0 and result.tests > 0 else "échec"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--moteurs", default=",".join(ENGINES), help="Moteurs, séparés par des virgules.")
    parser.add_argument("selection", nargs="*", default=["tests/e2e"], help="Tests à lancer.")
    arguments = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results = [run_engine(engine, arguments.selection) for engine in arguments.moteurs.split(",")]
    print(f"{'Moteur':<10} {'Version':<16} {'Issue':<13} {'Tests':>6} {'Réussis':>8} {'Échecs':>7} {'Ignorés':>8}")
    for result in results:
        print(
            f"{result.engine:<10} {result.version:<16} {result.status:<13} "
            f"{result.tests:>6} {result.passed:>8} {result.failed:>7} {result.skipped:>8}"
        )
        for failure in result.failures:
            print(f"  échec : {failure}")
        if result.status == "indisponible":
            print(f"  AUCUN TEST EXÉCUTÉ sur {result.engine} : {result.detail}")
    (OUTPUT / "resultats.json").write_text(
        json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if all(result.status == "réussi" for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
