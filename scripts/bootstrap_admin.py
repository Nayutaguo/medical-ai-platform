#!/usr/bin/env python3
"""Interactively create the first organization administrator.

The password is read with ``getpass`` so it is absent from shell history,
process arguments, logs, and environment examples. This command refuses to run
after any control-plane user exists.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from medical_ai.identity.errors import BootstrapAlreadyCompletedError
from medical_ai.repositories import IdentityRepository
from medical_ai.services import IdentityAdministrationService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create the one-time first organization administrator.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--organization-name", required=True)
    parser.add_argument("--organization-slug", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    password = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm administrator password: ")
    if password != confirmation:
        print("Passwords do not match; no changes were made.", file=sys.stderr)
        return 2

    service = IdentityAdministrationService(IdentityRepository())
    try:
        result = service.bootstrap_first_administrator(
            email=args.email,
            display_name=args.display_name,
            password=password,
            organization_name=args.organization_name,
            organization_slug=args.organization_slug,
        )
    except (BootstrapAlreadyCompletedError, ValueError) as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1
    finally:
        # Remove the strongest local references as soon as hashing completes.
        password = ""
        confirmation = ""

    print("Bootstrap completed.")
    print(f"user_id={result.user_id}")
    print(f"organization_id={result.organization_id}")
    print(f"membership_id={result.membership_id}")
    print("The administrator has governance permissions only; assign data access explicitly after scopes exist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
