#!/usr/bin/env python3
"""Entry points for trusted CI/release workflows (no local test runner)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from release_chain.automation import finish, receipt, reconcile
from release_chain.contract import (
    hub_manifest,
    object_json,
    verify_artifacts,
    verify_local,
    verify_manifest,
)
from release_chain.evidence import GateRejected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("local", "reconcile", "manual", "rollback"))
    parser.add_argument("--bump", choices=("patch", "minor", "major"), default="patch")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--package", help="previous verified data package for a rollback patch PR")
    args = parser.parse_args()
    if args.command == "rollback" and not args.package:
        parser.error("rollback requires --package identifying a verified previous package")
    if args.command == "local":
        lock = verify_local(Path.cwd())
        if lock:
            verify_manifest(lock["event"], hub_manifest(lock["event"]["release_version"]))
            verify_artifacts(lock["event"])
            if lock.get("rollback"):
                from release_chain.publishing import verify_rollback

                verify_rollback(lock, os.environ["GITHUB_REPOSITORY"])
        return
    report = receipt(None, args.command)
    try:
        trigger = object_json(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
        reconcile(
            os.environ["GITHUB_REPOSITORY"],
            trigger,
            report,
            os.environ["RELEASE_BOT_LOGIN"],
            bump=args.bump if args.command == "manual" else None,
            rollback_package=args.package if args.command == "rollback" else None,
        )
    except Exception as exc:
        report["state"] = "failed"
        report["verification"]["detail"] = str(exc)
        if isinstance(exc, GateRejected):
            report["verification"]["ci"] = exc.evidence
        raise
    finally:
        assert args.receipt is not None
        finish(report, args.receipt)


if __name__ == "__main__":
    main()
