"""Stage 3 of spec §9.3: test-import — load transformed JSONL into a target.

Verifies accept/reject counts and shape validation by writing the transformed
envelopes through the storage-adapter container verbs. The default target is
the in-memory FakeCosmos (tests/fake_cosmos.py): the whole path is verifiable
WITHOUT touching real Azure. The real target is opt-in (``--target real``)
and writes only happen with ``--apply`` (D-8 gate: cutover-time, user
authorization required).

Usage::

    python -m scripts.migration.test_import --input runs/cutover/transformed/users.transformed.jsonl
    python -m scripts.migration.test_import --input ... --apply            # write into FakeCosmos
    python -m scripts.migration.test_import --input ... --target real --apply   # cutover only
"""
from __future__ import annotations

import argparse
import json
import os
import sys

if __package__ in (None, ""):  # plain `python scripts/migration/test_import.py`
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

from scripts.migration.common import read_jsonl, resolve_container_provider  # noqa: E402
from scripts.migration.transform import validate_envelope  # noqa: E402


def load_transformed(envelopes: list[dict], get_container, *, apply: bool = False) -> dict:
    """Validate + (optionally) upsert transformed envelopes.

    Returns ``{"accepted", "rejected", "wrote", "by_container", "rejections"}``.
    ``rejections`` carries ``{line, key, container, reason}`` per rejected
    envelope. Write failures (unexpected container errors) are NOT swallowed:
    a systemic write problem must stop the operator, not look like a clean run.
    """
    report: dict = {
        "accepted": 0,
        "rejected": 0,
        "wrote": 0,
        "by_container": {},
        "rejections": [],
    }
    for line_no, env in enumerate(envelopes, start=1):
        reason = validate_envelope(env)
        if reason:
            report["rejected"] += 1
            report["rejections"].append(
                {
                    "line": line_no,
                    "key": env.get("key") if isinstance(env, dict) else None,
                    "container": env.get("container") if isinstance(env, dict) else None,
                    "reason": reason,
                }
            )
            continue
        report["accepted"] += 1
        container = env["container"]
        per = report["by_container"].setdefault(container, {"accepted": 0, "wrote": 0})
        per["accepted"] += 1
        if apply:
            get_container(container).upsert_item(env["item"])
            report["wrote"] += 1
            per["wrote"] += 1
    return report


def render_report(report: dict) -> str:
    lines = [
        f"accepted: {report['accepted']}",
        f"rejected: {report['rejected']}",
        f"wrote:    {report['wrote']}",
    ]
    for container in sorted(report["by_container"]):
        per = report["by_container"][container]
        lines.append(f"  {container}: accepted={per['accepted']} wrote={per['wrote']}")
    for rej in report["rejections"]:
        lines.append(
            f"  reject line {rej['line']} key={rej.get('key')!r} "
            f"container={rej.get('container')!r}: {rej['reason']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="U-404 §9.3 stage 3: test-import transformed JSONL "
        "(default target = FakeCosmos, dry-run by default).",
    )
    parser.add_argument("--input", required=True, help="transformed JSONL (envelopes)")
    parser.add_argument("--target", choices=("fake", "real"), default="fake",
                        help="'fake' = in-memory FakeCosmos (default); 'real' = storage.cosmos")
    parser.add_argument("--apply", action="store_true",
                        help="actually upsert rows (without it: validate + count only)")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    envelopes = read_jsonl(args.input)
    get_container, label = resolve_container_provider(args.target)
    report = load_transformed(envelopes, get_container, apply=args.apply)
    report["target"] = label
    report["applied"] = bool(args.apply)

    if args.json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    else:
        print(f"target:   {label}")
        print(f"applied:  {args.apply}")
        print(render_report(report))
    return 0 if report["rejected"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
