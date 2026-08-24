#!/usr/bin/env python3
"""Create one invitation-only account activation token.

The bearer token is printed exactly once. Operators must transfer it through
an approved secret channel and must never paste it into tickets or logs.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from uuid import uuid4

from medical_ai.audit import AuditActor
from medical_ai.identity.errors import InvitationCreationError
from medical_ai.repositories import IdentityRepository
from medical_ai.services import InvitationRegistrationService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create an invitation-bound user and organization membership."
    )
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--expires-hours",
        type=int,
        default=24,
        help="Invitation lifetime in hours (1-168, default: 24).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.expires_hours <= 168:
        print("Invitation creation failed: expires-hours must be 1-168.", file=sys.stderr)
        return 2

    service = InvitationRegistrationService(IdentityRepository())
    try:
        invitation = service.issue_invitation(
            organization_id=args.organization_id,
            email=args.email,
            actor=AuditActor.system(args.organization_id),
            lifetime=timedelta(hours=args.expires_hours),
            request_id=f"cli-{uuid4()}",
        )
    except (InvitationCreationError, ValueError) as exc:
        print(f"Invitation creation failed: {exc}", file=sys.stderr)
        return 1

    print("Invitation created. This bearer token will not be shown again:")
    print(f"invitation_token={invitation.token}")
    print(f"expires_at_utc={invitation.expires_at.isoformat()}")
    print(f"user_id={invitation.user_id}")
    print(f"organization_id={invitation.organization_id}")
    print(f"membership_id={invitation.membership_id}")
    print("No roles or facility scopes were assigned.")
    del invitation
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
