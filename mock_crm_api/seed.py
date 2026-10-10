"""Generate a synthetic EdTech CRM dataset and write it to data/crm.json.

    python -m mock_crm_api.seed [--accounts 150] [--seed 42] [--out data/crm.json]

The data has a few deliberate patterns so the analytics are not flat:
- two buying motions: professor-led deals are small and quick, instructional-
  designer-led deals are larger and linger in Proposal / Negotiation
- some customer institutions have declining weekly usage (the "needs follow-up" set)
- deals carry a full stage_history so time-in-stage can be modeled in dbt
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

from .store import (
    BUYING_MOTIONS,
    DEFAULT_DATA_PATH,
    CRMStore,
    fmt_ts,
    utcnow,
)

PREFIX = ["Northern", "Southern", "Eastern", "Western", "Lakeside", "Riverside",
          "Summit", "Pioneer", "Evergreen", "Coastal", "Prairie", "Highland"]
PLACE = ["State", "Valley", "Pacific", "Atlantic", "Central", "Metro",
         "Heritage", "Bay", "Mountain", "Capital"]
KINDS = ["University", "Community College", "State University", "College"]
KIND_WEIGHTS = [0.4, 0.3, 0.2, 0.1]
STATES = ["WA", "OR", "CA", "TX", "NY", "PA", "OH", "IL", "MA", "GA", "FL", "CO"]
OWNERS = ["Avery", "Jordan", "Riley", "Casey"]
FIRST = ["Alex", "Sam", "Priya", "Wei", "Maria", "James", "Fatima", "Chen", "Olivia", "Noah", "Aisha", "Luis"]
LAST = ["Nguyen", "Patel", "Garcia", "Kim", "Johnson", "Okafor", "Silva", "Brown", "Zhang", "Cohen", "Lopez", "Wright"]
ROLES = ["professor", "instructional_designer", "administrator"]
ROLE_WEIGHTS = [0.5, 0.3, 0.2]
CHANNELS = ["Webinar", "Conference", "Paid Search", "Email", "LinkedIn", "Referral", "Content/SEO", "Partner"]

# (min_days, max_days) spent in each open stage
STAGE_DAYS = {"Lead": (3, 14), "Discovery": (5, 21), "Demo": (5, 18),
              "Proposal": (7, 30), "Negotiation": (7, 35)}
SLOW_STAGES = {"Proposal", "Negotiation"}  # instructional-designer-led deals dwell longer here


def _between(rng: random.Random, start: datetime, end: datetime) -> datetime:
    span = max(int((end - start).total_seconds()), 0)
    return start + timedelta(seconds=rng.randint(0, span))


def build_dataset(seed: int = 42, n_accounts: int = 150, now: datetime | None = None) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    now = now or utcnow()
    max_names = len(PREFIX) * len(PLACE) * len(KINDS)
    if n_accounts > max_names * 0.8:
        raise ValueError(f"n_accounts too large for the name pool (max ~{int(max_names * 0.8)})")

    # ---- campaigns -------------------------------------------------------
    campaigns = []
    for i in range(1, 13):
        channel = CHANNELS[(i - 1) % len(CHANNELS)]
        start = now - timedelta(days=rng.randint(30, 300))
        campaigns.append({
            "id": f"cmp_{i:03d}",
            "name": f"{channel} Campaign {i}",
            "channel": channel,
            "spend": float(rng.randint(2, 40) * 1000),
            "start_date": start.date().isoformat(),
            "updated_at": fmt_ts(_between(rng, start, now)),
        })

    # ---- accounts + contacts ---------------------------------------------
    accounts, contacts, used_names = [], [], set()
    contact_n = 0
    for i in range(1, n_accounts + 1):
        while True:
            kind = rng.choices(KINDS, KIND_WEIGHTS)[0]
            name = f"{rng.choice(PREFIX)} {rng.choice(PLACE)} {kind}"
            if name not in used_names:
                used_names.add(name)
                break
        created = now - timedelta(days=rng.randint(200, 700), seconds=rng.randint(0, 86399))
        accounts.append({
            "id": f"acc_{i:04d}",
            "name": name,
            "institution_type": kind,
            "state": rng.choice(STATES),
            "enrollment": rng.randint(20, 450) * 100,
            "owner": rng.choice(OWNERS),
            "created_at": fmt_ts(created),
            "updated_at": fmt_ts(_between(rng, created, now)),
        })
        domain = name.lower().replace(" ", "") + ".edu"
        for _ in range(rng.randint(1, 4)):
            contact_n += 1
            first, last = rng.choice(FIRST), rng.choice(LAST)
            c_created = _between(rng, created, now - timedelta(days=1))
            contacts.append({
                "id": f"con_{contact_n:05d}",
                "account_id": accounts[-1]["id"],
                "first_name": first,
                "last_name": last,
                "email": f"{first}.{last}@{domain}".lower(),
                "role": rng.choices(ROLES, ROLE_WEIGHTS)[0],
                "created_at": fmt_ts(c_created),
                "updated_at": fmt_ts(_between(rng, c_created, now)),
            })

    # ---- deals -----------------------------------------------------------
    deals = []
    deal_n = 0
    for account in accounts:
        account_created = datetime.strptime(account["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        account_created = account_created.replace(tzinfo=now.tzinfo)
        for _ in range(rng.choices([0, 1, 2, 3], [0.2, 0.4, 0.3, 0.1])[0]):
            deal_n += 1
            motion = rng.choices(BUYING_MOTIONS, [0.6, 0.4])[0]
            slow = motion == "instructional_designer_led"
            created = _between(rng, max(account_created, now - timedelta(days=260)), now - timedelta(days=10))

            path = ["Lead"]
            for stage in ["Discovery", "Demo", "Proposal", "Negotiation"]:
                if rng.random() < 0.82:
                    path.append(stage)
                else:
                    path.append("Closed Lost")
                    break
            else:  # reached Negotiation without dropping out
                path.append("Closed Won" if rng.random() < 0.55 else "Closed Lost")

            history, t = [], created
            for stage in path:
                if t > now:
                    break
                history.append({"stage": stage, "entered_at": fmt_ts(t)})
                lo, hi = STAGE_DAYS.get(stage, (0, 0))
                days = rng.randint(lo, hi)
                if slow and stage in SLOW_STAGES:
                    days = int(days * 1.6)
                t += timedelta(days=days, seconds=rng.randint(0, 3600))

            current = history[-1]
            closed = current["stage"].startswith("Closed")
            amount = rng.randint(80, 1200) * 100 if slow else rng.randint(20, 250) * 100
            deals.append({
                "id": f"deal_{deal_n:04d}",
                "account_id": account["id"],
                "campaign_id": rng.choice(campaigns)["id"] if rng.random() < 0.85 else None,
                "name": f"{account['name']} - {motion}",
                "buying_motion": motion,
                "stage": current["stage"],
                "amount": float(amount),
                "created_at": fmt_ts(created),
                "close_date": current["entered_at"][:10] if closed else None,
                "stage_history": history,
                "updated_at": current["entered_at"],
            })

    # ---- weekly product usage for customer (closed-won) accounts ----------
    customer_ids = sorted({d["account_id"] for d in deals if d["stage"] == "Closed Won"})
    last_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
    usage = []
    for account_id in customer_ids:
        users = float(rng.randint(20, 400))
        trend = rng.choice([-0.07, -0.04, -0.01, 0.0, 0.02, 0.04])  # a third decline: the follow-up set
        for k in range(12):
            week_start = last_monday - timedelta(weeks=11 - k)
            users = max(1.0, users * (1 + trend) + rng.uniform(-0.05, 0.05) * users)
            active = int(users)
            usage.append({
                "id": f"{account_id}_{week_start.date().isoformat()}",
                "account_id": account_id,
                "week_start": week_start.date().isoformat(),
                "active_users": active,
                "sessions": int(active * rng.uniform(2.5, 5.0)),
                "updated_at": fmt_ts(min(week_start + timedelta(days=7), now)),
            })

    return {"accounts": accounts, "contacts": contacts, "deals": deals,
            "campaigns": campaigns, "usage": usage}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", type=int, default=150)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=DEFAULT_DATA_PATH)
    args = parser.parse_args(argv)

    data = build_dataset(seed=args.seed, n_accounts=args.accounts)
    CRMStore(data).save(args.out)
    print(f"Wrote {args.out}")
    for name, rows in data.items():
        print(f"  {name:<10} {len(rows):>6}")


if __name__ == "__main__":
    main()
