from __future__ import annotations

from itertools import combinations
from typing import Dict, List, Optional, Sequence, Set, Tuple, cast

from nettest.models import NetworkRow, TestCase, TestPhase, TestResult, VrfLink


# ── Parsing helpers ────────────────────────────────────────────────────────────

def parse_allowlist(allow_pairs: Sequence[str]) -> Set[Tuple[str, str]]:
    """Parse legacy allow_vrf strings ('VRF_A:VRF_B') into a normalised set."""
    out: Set[Tuple[str, str]] = set()
    for item in allow_pairs:
        if ":" not in item:
            continue
        a, b = [x.strip() for x in item.split(":", 1)]
        if not a or not b:
            continue
        key = cast(Tuple[str, str], tuple(sorted((a, b))))
        out.add(key)
    return out


def parse_vrf_links(vrf_links_raw: Sequence[dict]) -> List[VrfLink]:
    """Parse vrf_links from config (list of dicts) into VrfLink objects.

    Accepted dict keys: ``from``/``from_vrf``, ``to``/``to_vrf``,
    ``expected`` (default "PASS"), ``comment``.
    """
    links: List[VrfLink] = []
    for item in vrf_links_raw:
        from_vrf = str(item.get("from") or item.get("from_vrf") or "").strip()
        to_vrf   = str(item.get("to")   or item.get("to_vrf")   or "").strip()
        expected = str(item.get("expected", "PASS")).strip().upper()
        comment  = str(item.get("comment", "")).strip()
        if from_vrf and to_vrf and expected in ("PASS", "FAIL"):
            links.append(VrfLink(from_vrf=from_vrf, to_vrf=to_vrf,
                                 expected=expected, comment=comment))
    return links


def parse_vrf_links_cli(vrf_links_cli: Sequence[str]) -> List[VrfLink]:
    """Parse CLI --vrf-links strings: ``'FROM:TO'`` or ``'FROM:TO:PASS|FAIL'``."""
    links: List[VrfLink] = []
    for item in vrf_links_cli:
        parts = [p.strip() for p in item.split(":")]
        if len(parts) < 2:
            continue
        from_vrf = parts[0]
        to_vrf   = parts[1]
        expected = parts[2].upper() if len(parts) >= 3 and parts[2].upper() in ("PASS", "FAIL") else "PASS"
        if from_vrf and to_vrf:
            links.append(VrfLink(from_vrf=from_vrf, to_vrf=to_vrf, expected=expected))
    return links


# ── Internal helpers ──────────────────────────────────────────────────────────

def _build_effective_sets(
    allowlist: Set[Tuple[str, str]],
    vrf_links: Sequence[VrfLink],
) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    """Return (pass_set, explicit_fail_set) of normalised sorted VRF pairs.

    vrf_links with expected=FAIL take precedence over pass_set.
    """
    pass_set: Set[Tuple[str, str]] = set(allowlist)
    fail_set: Set[Tuple[str, str]] = set()
    for link in vrf_links:
        key = cast(Tuple[str, str], tuple(sorted((link.from_vrf, link.to_vrf))))
        if link.expected == "PASS":
            pass_set.add(key)
            fail_set.discard(key)
        elif link.expected == "FAIL":
            fail_set.add(key)
            pass_set.discard(key)
    return pass_set, fail_set


_PHASE_NAMES: Dict[str, str] = {
    "intra-subnet":        "Intra-Subnet Connectivity",
    "intra-vrf":           "Intra-VRF Cross-Subnet",
    "cross-vrf-allowlist": "Cross-VRF Allowlisted",
    "cross-vrf-block":     "Cross-VRF Block Verification",
}
_PHASE_DESC: Dict[str, str] = {
    "intra-subnet":        "ICMP between VMs in the same subnet — expected PASS",
    "intra-vrf":           "ICMP between different subnets in the same VRF — expected PASS",
    "cross-vrf-allowlist": "ICMP between explicitly allowlisted VRF pairs — expected PASS",
    "cross-vrf-block":     "ICMP between VRF pairs that should be blocked — expected FAIL",
}

ALL_PHASE_IDS = ["intra-subnet", "intra-vrf", "cross-vrf-allowlist", "cross-vrf-block"]


def assign_case_ids(cases: Sequence[TestCase], prefix: str = "tc") -> None:
    """Assign stable sequential IDs to cases that do not already have one."""
    for idx, case in enumerate(cases, start=1):
        if not getattr(case, "case_id", ""):
            case.case_id = f"{prefix}-{idx:04d}"


# ── Public API ────────────────────────────────────────────────────────────────

def generate_test_cases(
    rows: Sequence[NetworkRow],
    allowlist: Set[Tuple[str, str]],
    vrf_links: Sequence[VrfLink] = (),
) -> List[TestCase]:
    """Generate observation test cases for all cross-subnet VM pairs.

    Legacy arguments are accepted for API compatibility but ignored.
    Connectivity expectation is assigned by testsuite allow/deny selections.
    """
    _ = allowlist
    _ = vrf_links
    vm_rows = [r for r in rows if r.mode == "vm-provisioned"]
    testcases: List[TestCase] = []

    for src, dst in combinations(vm_rows, 2):
        if src.subnet == dst.subnet:
            continue
        reason = "same-vrf" if src.vrf == dst.vrf else "cross-vrf"

        testcases.append(TestCase(
            src_subnet=src.subnet,
            dst_subnet=dst.subnet,
            src_vrf=src.vrf,
            dst_vrf=dst.vrf,
            expected="OBSERVE",
            reason=reason,
            phase="network-connectivity",
        ))
    assign_case_ids(testcases)
    return testcases


def generate_phased_cases(
    rows: Sequence[NetworkRow],
    allowlist: Set[Tuple[str, str]],
    vrf_links: Sequence[VrfLink],
    vms_per_subnet: int = 1,
    max_vms_per_phase: int = 20,
    enabled_phases: Optional[List[str]] = None,
) -> List[TestPhase]:
    """Generate test cases grouped into self-contained phases.

    Phase execution order:
      1. **intra-subnet**        – VM pairs within the same subnet (needs vms_per_subnet >= 2)
      2. **intra-vrf**           – Cross-subnet, same VRF (expected PASS)
      3. **cross-vrf-allowlist** – Allowlisted cross-VRF (expected PASS)
      4. **cross-vrf-block**     – All other cross-VRF (expected FAIL)

    If a phase's unique subnet count exceeds ``max_vms_per_phase // vms_per_subnet``,
    it is automatically split into sequential batches so the number of live VMs
    never exceeds ``max_vms_per_phase``.

    Args:
        rows:              Normalised NetworkRow list (mngt-esxi rows are ignored).
        allowlist:         Legacy allow_vrf set (from :func:`parse_allowlist`).
        vrf_links:         Richer VRF connectivity rules (from :func:`parse_vrf_links`).
        vms_per_subnet:    VMs to provision per subnet.  Must be >= 2 for intra-subnet.
        max_vms_per_phase: Safety cap; triggers batching when exceeded.
        enabled_phases:    Subset of ALL_PHASE_IDS to run; ``None`` means all.
    """
    if enabled_phases is None:
        enabled_phases = list(ALL_PHASE_IDS)

    pass_set, _ = _build_effective_sets(allowlist, vrf_links)
    vm_rows = [r for r in rows if r.mode == "vm-provisioned"]
    phases: List[TestPhase] = []

    # ── Phase 1: intra-subnet ─────────────────────────────────────────────────
    if "intra-subnet" in enabled_phases and vms_per_subnet >= 2:
        intra_cases: List[TestCase] = []
        for row in vm_rows:
            for i in range(vms_per_subnet):
                for j in range(i + 1, vms_per_subnet):
                    intra_cases.append(TestCase(
                        src_subnet=row.subnet,
                        dst_subnet=row.subnet,
                        src_vrf=row.vrf,
                        dst_vrf=row.vrf,
                        expected="PASS",
                        reason="intra-subnet-direct",
                        phase="intra-subnet",
                        src_vm_index=i,
                        dst_vm_index=j,
                    ))
        if intra_cases:
            phases.extend(_batch_phase(
                intra_cases, "intra-subnet", vms_per_subnet, max_vms_per_phase))

    # ── Phase 2: intra-VRF ────────────────────────────────────────────────────
    if "intra-vrf" in enabled_phases:
        intra_vrf_cases: List[TestCase] = []
        for src, dst in combinations(vm_rows, 2):
            if src.subnet == dst.subnet or src.vrf != dst.vrf:
                continue
            intra_vrf_cases.append(TestCase(
                src_subnet=src.subnet,
                dst_subnet=dst.subnet,
                src_vrf=src.vrf,
                dst_vrf=dst.vrf,
                expected="PASS",
                reason="same-vrf",
                phase="intra-vrf",
            ))
        if intra_vrf_cases:
            phases.extend(_batch_phase(
                intra_vrf_cases, "intra-vrf", 1, max_vms_per_phase))

    # ── Phase 3: cross-VRF allowlisted ────────────────────────────────────────
    if "cross-vrf-allowlist" in enabled_phases:
        cross_allow_cases: List[TestCase] = []
        for src, dst in combinations(vm_rows, 2):
            if src.subnet == dst.subnet or src.vrf == dst.vrf:
                continue
            pair = cast(Tuple[str, str], tuple(sorted((src.vrf, dst.vrf))))
            if pair in pass_set:
                cross_allow_cases.append(TestCase(
                    src_subnet=src.subnet,
                    dst_subnet=dst.subnet,
                    src_vrf=src.vrf,
                    dst_vrf=dst.vrf,
                    expected="PASS",
                    reason="cross-vrf-allowlist",
                    phase="cross-vrf-allowlist",
                ))
        if cross_allow_cases:
            phases.extend(_batch_phase(
                cross_allow_cases, "cross-vrf-allowlist", 1, max_vms_per_phase))

    # ── Phase 4: cross-VRF blocked ────────────────────────────────────────────
    if "cross-vrf-block" in enabled_phases:
        cross_block_cases: List[TestCase] = []
        for src, dst in combinations(vm_rows, 2):
            if src.subnet == dst.subnet or src.vrf == dst.vrf:
                continue
            pair = cast(Tuple[str, str], tuple(sorted((src.vrf, dst.vrf))))
            if pair not in pass_set:
                cross_block_cases.append(TestCase(
                    src_subnet=src.subnet,
                    dst_subnet=dst.subnet,
                    src_vrf=src.vrf,
                    dst_vrf=dst.vrf,
                    expected="FAIL",
                    reason="cross-vrf-default-block",
                    phase="cross-vrf-block",
                ))
        if cross_block_cases:
            phases.extend(_batch_phase(
                cross_block_cases, "cross-vrf-block", 1, max_vms_per_phase))

    running_idx = 1
    for ph in phases:
        for case in ph.cases:
            if not getattr(case, "case_id", ""):
                case.case_id = f"tc-{running_idx:04d}"
            running_idx += 1

    return phases


def _batch_phase(
    cases: List[TestCase],
    phase_id: str,
    vms_per_subnet: int,
    max_vms_per_phase: int,
) -> List[TestPhase]:
    """Split *cases* into TestPhase objects, each fitting within max_vms_per_phase."""
    max_subnets = max(1, max_vms_per_phase // max(1, vms_per_subnet))

    # Collect ordered unique subnets preserving first-appearance order
    seen: List[str] = []
    for c in cases:
        for s in (c.src_subnet, c.dst_subnet):
            if s not in seen:
                seen.append(s)

    if len(seen) <= max_subnets:
        return [TestPhase(
            phase_id=phase_id,
            name=_PHASE_NAMES.get(phase_id, phase_id),
            description=_PHASE_DESC.get(phase_id, ""),
            cases=cases,
            vms_per_subnet=vms_per_subnet,
            batch_index=0,
        )]

    # Split into batches
    batches: List[TestPhase] = []
    for batch_idx, i in enumerate(range(0, len(seen), max_subnets)):
        batch_subnets = set(seen[i: i + max_subnets])
        batch_cases = [
            c for c in cases
            if c.src_subnet in batch_subnets and c.dst_subnet in batch_subnets
        ]
        if batch_cases:
            batches.append(TestPhase(
                phase_id=phase_id,
                name=f"{_PHASE_NAMES.get(phase_id, phase_id)} (batch {batch_idx + 1})",
                description=f"{_PHASE_DESC.get(phase_id, '')} — batch {batch_idx + 1}",
                cases=batch_cases,
                vms_per_subnet=vms_per_subnet,
                batch_index=batch_idx,
            ))
    return batches


def summarize_expected(cases: Sequence[TestCase]) -> Dict[str, int]:
    out = {
        "observed": 0,
        "intra_subnet_pass": 0,
        "same_vrf_pass": 0,
        "cross_vrf_fail": 0,
        "cross_vrf_allowlist_pass": 0,
    }
    for c in cases:
        if c.expected == "OBSERVE":
            out["observed"] += 1
            continue
        if c.reason == "intra-subnet-direct":
            out["intra_subnet_pass"] += 1
        elif c.reason == "same-vrf":
            out["same_vrf_pass"] += 1
        elif c.reason == "cross-vrf-default-block":
            out["cross_vrf_fail"] += 1
        elif c.reason == "cross-vrf-allowlist":
            out["cross_vrf_allowlist_pass"] += 1
    return out


def merge_retry_results(
    prior: List[TestResult],
    new_results: List[TestResult],
) -> List[TestResult]:
    """Merge two result lists; newer results win on key collision."""
    def _key(r: TestResult) -> Tuple:
        return (r.src_subnet, r.dst_subnet,
                getattr(r, "src_vm_index", 0), getattr(r, "dst_vm_index", 0),
                getattr(r, "phase", "intra-vrf"),
                getattr(r, "probe_type", "icmp"),
                getattr(r, "expected", ""),
                getattr(r, "reason", ""),
                getattr(r, "tcp_port", 0))

    index: Dict[Tuple, TestResult] = {_key(r): r for r in prior}
    for r in new_results:
        index[_key(r)] = r
    return list(index.values())


def select_retry_cases(
    all_cases: Sequence[TestCase],
    latest_results: Sequence[TestResult],
    mode: str,
) -> List[TestCase]:
    if mode == "all":
        return list(all_cases)

    failed_keys = {
        (r.src_subnet, r.dst_subnet,
         getattr(r, "src_vm_index", 0), getattr(r, "dst_vm_index", 0),
         getattr(r, "phase", "intra-vrf"),
         getattr(r, "probe_type", "icmp"),
         getattr(r, "expected", ""),
         getattr(r, "reason", ""))
        for r in latest_results if r.status != "pass"
    }
    return [
        c for c in all_cases
        if (
            c.src_subnet,
            c.dst_subnet,
            c.src_vm_index,
            c.dst_vm_index,
            getattr(c, "phase", "intra-vrf"),
            getattr(c, "probe_type", "icmp"),
            getattr(c, "expected", ""),
            getattr(c, "reason", ""),
        ) in failed_keys
    ]
