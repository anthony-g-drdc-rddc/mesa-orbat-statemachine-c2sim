"""Evaluate only delivered evidence, never simulated physical truth."""

from dataclasses import dataclass


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reasons: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


def evaluate(predicate: dict, state: dict) -> GuardResult:
    op, ref = predicate["op"], predicate.get("ref")
    if op in {"all_of", "any_of"}:
        results = [evaluate(p, state) for p in predicate["args"]]
        allowed = (
            all(r.allowed for r in results) if op == "all_of" else any(r.allowed for r in results)
        )
        return GuardResult(
            allowed,
            tuple(sorted({x for r in results if not r.allowed for x in r.reasons}))
            if not allowed
            else (),
            tuple(sorted({x for r in results if r.allowed for x in r.evidence_ids})),
        )
    reports = state["reports"]
    matches = []
    if op == "task_completed":
        task = state["tasks"].get(ref)
        if task and task["state"] == "COMPLETED":
            matches = task["evidence_ids"]
    elif op == "report_confirmed":
        matches = [
            r["id"] for r in reports if r["confirmed"] and r["payload"].get("finding") == ref
        ]
    elif op == "supply_received":
        receipts = {
            r["payload"]["transfer_id"]: r
            for r in reports
            if r["kind"] == "receipt" and r["confirmed"] and r["payload"]["recipient"] == ref
        }
        if sum(r["payload"]["amount"] for r in receipts.values()) >= predicate["amount"]:
            matches = [r["id"] for r in receipts.values()]
    elif op == "element_accounted_for":
        matches = [
            r["id"]
            for r in reports
            if r["kind"] == "accountability" and r["source"] == ref and r["confirmed"]
        ]
    else:
        raise ValueError(f"Unknown predicate {op}")
    return GuardResult(
        bool(matches), () if matches else (f"UNMET_{op.upper()}:{ref}",), tuple(matches)
    )
