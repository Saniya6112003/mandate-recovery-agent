"""Cancel open Payment Links on the Razorpay TEST account.

Every batch run creates real links, so the test dashboard fills up with
artifacts. Run this before recording a demo to start from a clean slate.

    python scripts/cancel_test_links.py            # dry run, lists what it would cancel
    python scripts/cancel_test_links.py --apply    # actually cancel them

Only ever touches links in `created` state — a `paid` link is evidence of a
real recovery and is never cancelled. Refuses to run against live keys.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import razorpay  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from razorpay.errors import BadRequestError, GatewayError, ServerError  # noqa: E402

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cancel open test-mode payment links.")
    parser.add_argument("--apply", action="store_true", help="actually cancel (default: dry run)")
    args = parser.parse_args()

    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        sys.exit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set in .env")

    if not key_id.startswith("rzp_test_"):
        sys.exit(f"refusing to run: {key_id[:12]}... is not a test-mode key")

    client = razorpay.Client(auth=(key_id, key_secret))
    links = client.payment_link.all({"count": 100}).get("payment_links", [])
    open_links = [link for link in links if link["status"] == "created"]

    print(f"{len(links)} links on account, {len(open_links)} open and cancellable")
    if not open_links:
        return

    if not args.apply:
        for link in open_links[:10]:
            print(f"  would cancel {link['id']}  ref={link.get('reference_id')}")
        if len(open_links) > 10:
            print(f"  ... and {len(open_links) - 10} more")
        print("\nre-run with --apply to cancel them")
        return

    cancelled, failed = 0, 0
    for link in open_links:
        try:
            client.payment_link.cancel(link["id"])
            cancelled += 1
        except (BadRequestError, GatewayError, ServerError) as exc:
            failed += 1
            print(f"  could not cancel {link['id']}: {exc}")
            if "too many requests" in str(exc).lower():
                time.sleep(2)

    print(f"cancelled {cancelled}, failed {failed}")


if __name__ == "__main__":
    main()
