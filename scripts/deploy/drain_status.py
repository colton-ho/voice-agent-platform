#!/usr/bin/env python3
"""U-405 drain-status helper — FC-7 lease census + announced forced-BYE delivery.

Companion to scripts/deploy/drain-runtime.sh (blue/green release drain tooling;
scheme documented in docs/azure/blue-green-release.md). Two layers:

* pure functions (live_rows / attribute_rows / census / is_drain_complete /
  build_hangup_command / remaining_seconds) — the drain-complete predicate,
  tested by tests/test_drain.py;
* a thin CLI (census / force-end) that fetches rows through the FC-7 store
  interface and delivers the forced-drain command.

Data source of truth is the FC-7 call-to-owner lease (storage/ownership.py).
Attribution here mirrors ``_CosmosStore.list_leases`` (storage/ownership.py
query): kind == "lease" AND lease_expires_at > now AND (owner_id == owner OR
(owner_id absent AND reserved_by == owner)). The owner set for a revision is
the set of Container Apps replica names (owner_id = replica name — the binding
convention for the U-305 runtime wiring).

force-end delivers ``Command(name="hangup", payload={"reason", "call_id"})``
(vocabulary per tests/test_ownership.py) through
``CosmosSessionDirectory.route_command`` — the owner-addressable command path
of FC-7 (storage/ownership.py). This is the "announced forced BYE" posture of
the design doc: the owner ends the call through its normal end-of-call path,
so the caller hears an early but normal ending, never a silent drop.

Transport seam: route_command needs the owner-addressable ``send`` callable
``(owner_id, Command) -> bool``. Set DRAIN_SEND_FACTORY="module:function"
(function returning the send callable) to wire the U-305 transport. When it
is unset, route_command honestly reports accepted=False
("no owner-addressable transport wired", storage/ownership.py) and force-end
exits 3 without killing anything — the tool never hard-kills replicas.

Authentication mirrors storage/cosmos.py (DefaultAzureCredential +
COSMOS_ENDPOINT / COSMOS_DATABASE). The operator principal needs the Cosmos
DB Built-in Data Contributor role on the account (u-401-deployment-plan.md
RBAC row 4).

Usage:
  python3 scripts/deploy/drain_status.py census --owners a,b,c [--format text|json|count] [--now EPOCH]
  python3 scripts/deploy/drain_status.py force-end --owners a,b,c --reason "forced-drain U-405" [--now EPOCH]

Exit codes:
  0  ok (census printed / all force-end commands accepted)
  1  usage or runtime error
  3  force-end incomplete (at least one command not accepted)
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import uuid

LEASE_KIND = "lease"
HANGUP_NAME = "hangup"
DRAIN_TOOL_OWNER = "drain-tool"


# ---------------------------------------------------------------------------
# pure functions (tested by tests/test_drain.py)
# ---------------------------------------------------------------------------

def live_rows(rows, now: float) -> list:
    """Lease rows still live at ``now`` (kind == 'lease' and lease not expired)."""
    out = []
    for row in rows:
        if row.get("kind") != LEASE_KIND:
            continue
        if float(row.get("lease_expires_at") or 0) > float(now):
            out.append(row)
    return out


def attribute_rows(rows, owners) -> list:
    """Rows held by one of ``owners`` — mirror of storage/ownership.list_leases:
    bound leases count against owner_id; unbound reservations against
    reserved_by."""
    owner_set = set(owners)
    out = []
    for row in rows:
        holder = row.get("owner_id") or None
        if holder is None:
            holder = row.get("reserved_by") or None
        if holder in owner_set:
            out.append(row)
    return out


def census(rows, owners, now: float) -> dict:
    """Drain census: live lease rows attributed to ``owners``."""
    attributed = attribute_rows(live_rows(rows, now), owners)
    per_owner: dict = {}
    calls = []
    for row in sorted(attributed, key=lambda r: str(r.get("call_id"))):
        owner = row.get("owner_id") or None
        if owner is None:
            holder = "reserved:%s" % (row.get("reserved_by") or "?")
            owner_key = row.get("reserved_by") or "?"
        else:
            holder = owner
            owner_key = owner
        per_owner[owner_key] = per_owner.get(owner_key, 0) + 1
        calls.append({
            "call_id": row.get("call_id"),
            "holder": holder,
            "lease_expires_at": float(row.get("lease_expires_at") or 0),
            "remaining_seconds": remaining_seconds(
                now, float(row.get("lease_expires_at") or 0)
            ),
        })
    return {"total": len(calls), "per_owner": per_owner, "calls": calls}


def is_drain_complete(rows, owners, now: float) -> bool:
    """Drain-complete criterion: zero live lease rows attributed to the
    revision's replica (owner) set."""
    return len(attribute_rows(live_rows(rows, now), owners)) == 0


def build_hangup_command(call_id: str, reason: str, command_id: str):
    """The announced forced-BYE command (FC-7 vocabulary: name='hangup')."""
    from storage.base import Command

    return Command(
        command_id=command_id,
        name=HANGUP_NAME,
        payload={"reason": reason, "call_id": call_id},
    )


def remaining_seconds(now: float, deadline_epoch: float) -> float:
    """Seconds left until ``deadline_epoch``, clamped at 0."""
    return max(0.0, float(deadline_epoch) - float(now))


# ---------------------------------------------------------------------------
# I/O layer (CLI)
# ---------------------------------------------------------------------------

def resolve_send():
    """Owner-addressable transport seam (U-305). DRAIN_SEND_FACTORY='module:function'
    names a factory returning the async ``(owner_id, Command) -> bool`` callable.
    Unset -> None (route_command reports 'no owner-addressable transport wired')."""
    spec = os.environ.get("DRAIN_SEND_FACTORY", "").strip()
    if not spec:
        return None
    mod_name, sep, fn_name = spec.partition(":")
    if not sep or not mod_name or not fn_name:
        raise ValueError(
            "DRAIN_SEND_FACTORY must look like 'module:function', got %r" % spec
        )
    factory = getattr(importlib.import_module(mod_name), fn_name)
    return factory()


def fetch_rows(owners, now: float) -> list:
    """Union of FC-7 list_leases per owner (storage/ownership.py semantics)."""
    from storage import ownership

    store = ownership.get_store()
    rows: list = []
    for owner in owners:
        rows.extend(store.list_leases(owner, now))
    return rows


async def force_end(directory, rows, owners, now: float, reason: str,
                    command_id_factory=None) -> list:
    """Deliver one announced forced-BYE per attributed live call via
    ``directory.route_command`` (async FC-7 directory). Returns one result
    dict per call: {call_id, command_id, accepted, detail}."""
    if command_id_factory is None:
        def command_id_factory(call_id):  # noqa: F811 — default generator
            return "forced-drain-%s-%s" % (call_id, uuid.uuid4().hex[:12])
    attributed = attribute_rows(live_rows(rows, now), owners)
    results = []
    for row in attributed:
        call_id = row["call_id"]
        cid = command_id_factory(call_id)
        command = build_hangup_command(call_id, reason, cid)
        ack = await directory.route_command(call_id=call_id, command=command)
        results.append({
            "call_id": call_id,
            "command_id": cid,
            "accepted": bool(ack.accepted),
            "detail": ack.detail,
        })
    return results


def _parse_owners(raw: str) -> list:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _cmd_census(args) -> int:
    now = float(args.now) if args.now is not None else time.time()
    owners = _parse_owners(args.owners)
    rows = fetch_rows(owners, now)
    result = census(rows, owners, now)
    if args.format == "count":
        print(result["total"])
    elif args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("live leases attributed to %d owner(s): %d" %
              (len(owners), result["total"]))
        for owner in sorted(result["per_owner"]):
            print("  %-48s %d" % (owner, result["per_owner"][owner]))
        for call in result["calls"]:
            print("  call %-24s holder=%-28s expires_in=%.0fs" %
                  (call["call_id"], call["holder"], call["remaining_seconds"]))
    return 0


def _cmd_force_end(args) -> int:
    import asyncio

    from storage.ownership import CosmosSessionDirectory

    now = float(args.now) if args.now is not None else time.time()
    owners = _parse_owners(args.owners)
    rows = fetch_rows(owners, now)
    directory = CosmosSessionDirectory(
        owner_id=DRAIN_TOOL_OWNER,
        max_concurrent_calls=10 ** 9,
        send=resolve_send(),
    )
    results = asyncio.run(
        force_end(directory, rows, owners, now, args.reason)
    )
    accepted = 0
    for res in results:
        status = "accepted" if res["accepted"] else "NOT accepted"
        print("call %-24s %s (%s)" % (res["call_id"], status, res["detail"]))
        if res["accepted"]:
            accepted += 1
    print("force-end: %d/%d announced hangup command(s) accepted" %
          (accepted, len(results)))
    if accepted == len(results):
        return 0
    print("force-end incomplete: the owner-addressable transport is not wired "
          "or unreachable (see DRAIN_SEND_FACTORY / docs/azure/blue-green-release.md "
          "forced-drain table). No replica was killed and no call was dropped "
          "by this tool.")
    return 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="drain_status.py",
        description="U-405 drain-status helper (FC-7 lease census + announced forced-BYE)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_census = sub.add_parser("census", help="report live leases attributed to owners")
    p_census.add_argument("--owners", required=True,
                          help="comma-separated replica (owner) names")
    p_census.add_argument("--format", choices=("text", "json", "count"),
                          default="text")
    p_census.add_argument("--now", type=float, default=None,
                          help="epoch override (deterministic checks)")
    p_census.set_defaults(func=_cmd_census)

    p_force = sub.add_parser(
        "force-end",
        help="announced forced-BYE per live call (FC-7 hangup command)",
    )
    p_force.add_argument("--owners", required=True,
                         help="comma-separated replica (owner) names")
    p_force.add_argument("--reason", default="forced-drain U-405")
    p_force.add_argument("--now", type=float, default=None,
                         help="epoch override (deterministic checks)")
    p_force.set_defaults(func=_cmd_force_end)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
