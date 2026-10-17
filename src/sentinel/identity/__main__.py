"""Enroll and remove household members from the command line.

  python -m sentinel.identity consent
  python -m sentinel.identity add --config C --name "Alex" --given-by "Alex" --relationship self --method in_person --agreed
  python -m sentinel.identity enroll --config C --member m_1234abcd photo1.jpg photo2.jpg photo3.jpg
  python -m sentinel.identity list --config C
  python -m sentinel.identity remove --config C --member m_1234abcd

Works whether or not Sentinel is running (the running service picks changes up
within a few seconds). Photos are read, turned into templates and not kept."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ..config import load_config
from .gallery import CONSENT_STATEMENT, CONSENT_STATEMENT_VERSION, METHODS, RELATIONSHIPS, EnrollmentError, FaceGallery
from .settings import IdentitySettings


def _storage(config: dict) -> Path:
    return Path(config.get("storage", {}).get("dir", "data/sentinel"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sentinel.identity", description="Household-member enrollment (consent required)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("consent", help="Print the consent statement to show the person")
    for name in ("list", "add", "enroll", "remove"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", default="config/sentinel.example.json")
        if name == "add":
            sub.add_argument("--name", required=True)
            sub.add_argument("--given-by", required=True, help="who agreed (the member, or their parent or guardian)")
            sub.add_argument("--relationship", choices=RELATIONSHIPS, required=True)
            sub.add_argument("--method", choices=METHODS, required=True)
            sub.add_argument("--agreed", action="store_true", help="confirm the person was shown the statement and agreed")
            sub.add_argument("--note", default="")
        if name in ("enroll", "remove"):
            sub.add_argument("--member", required=True)
        if name == "enroll":
            sub.add_argument("photos", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "consent":
        print(f"Consent statement (version {CONSENT_STATEMENT_VERSION}):\n\n{CONSENT_STATEMENT}")
        return 0
    config = load_config(args.config, resolve_secrets=False)
    settings = IdentitySettings.from_dict(config.get("identity"))
    storage = _storage(config)
    gallery = FaceGallery(storage / "identity" / "gallery.dat")
    actor = "cli"
    from ..store import EventStore

    store = EventStore(storage / "sentinel.db")
    try:
        now = int(time.time() * 1000)
        if args.command == "list":
            print(json.dumps(gallery.members(), indent=2))
        elif args.command == "add":
            consent = {"agreed": args.agreed, "given_by": args.given_by, "relationship": args.relationship, "method": args.method,
                       "statement_version": CONSENT_STATEMENT_VERSION, "note": args.note}
            member = gallery.add_member(args.name, consent, actor, now)
            store.audit(actor, "identity_member_added", {"member_id": member["member_id"], "display_name": member["display_name"],
                                                         "given_by": args.given_by, "relationship": args.relationship, "method": args.method,
                                                         "statement_version": CONSENT_STATEMENT_VERSION})
            print(f"Added {member['display_name']} as {member['member_id']}. Next: enroll at least {settings.enroll_min_photos} photos.")
        elif args.command == "enroll":
            from .faces import decode_image
            from .service import Enrollment

            images = [decode_image(Path(photo).read_bytes()) for photo in args.photos]
            member = gallery.add_templates(args.member, Enrollment(settings).templates(images), settings, now)
            store.audit(actor, "identity_photos_enrolled", {"member_id": args.member, "photos": len(images), "source": "photos"})
            print(f"{member['display_name']}: {member['photos_enrolled']} photo(s) enrolled "
                  f"({'complete' if member['photos_enrolled'] >= settings.enroll_min_photos else 'needs more photos'}).")
        elif args.command == "remove":
            removed = gallery.remove_member(args.member)
            scrubbed = store.scrub_identity(args.member)
            store.audit(actor, "identity_member_removed", {"member_id": args.member, "templates_deleted": removed["templates_deleted"],
                                                           "events_scrubbed": scrubbed})
            print(f"Removed {removed['display_name']}: {removed['templates_deleted']} template(s) deleted, {scrubbed} stored event(s) scrubbed.")
    except EnrollmentError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    except KeyError as error:
        print(f"Unknown member {error}", file=sys.stderr)
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
