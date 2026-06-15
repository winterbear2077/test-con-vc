from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


REQUIRED_COLUMNS = {"vlan", "subnet", "gw", "vrf", "cluster", "datacenter"}


@dataclass
class NetworkRow:
    vlan: str
    subnet: str
    gw: str
    vrf: str
    cluster: str
    datacenter: str
    mode: str
    source_line: int


@dataclass
class VrfLink:
    """Explicit VRF-to-VRF connectivity expectation (PASS or FAIL).

    Supplements the legacy ``allow_vrf`` allowlist and provides finer control:
    you can mark a pair as expected-FAIL to force a blocking-verification case.
    """
    from_vrf: str
    to_vrf: str
    expected: str = "PASS"   # "PASS" or "FAIL"
    comment: str = ""


@dataclass
class TestCase:
    src_subnet: str
    dst_subnet: str
    src_vrf: str
    dst_vrf: str
    expected: str
    reason: str
    # Phase classification
    phase: str = "intra-vrf"
    # VM index within the subnet (for multi-VM intra-subnet scenarios)
    src_vm_index: int = 0
    dst_vm_index: int = 0
    # Probe type: "icmp" (default) or "tcp"
    probe_type: str = "icmp"
    # TCP ports to probe when probe_type == "tcp" (empty = use port 80)
    tcp_ports: List[int] = field(default_factory=list)
    # Optional direct destination IP for custom point-to-point TCP tests.
    dst_ip: str = ""


@dataclass
class TestPhase:
    """A self-contained group of test cases sharing one VM lifecycle.

    Execution order in phased mode:
      1. intra-subnet        – VMs in same subnet ping each other (needs vms_per_subnet >= 2)
      2. intra-vrf           – Same VRF, different subnets (expected PASS)
      3. cross-vrf-allowlist – Explicitly allowlisted cross-VRF pairs (expected PASS)
      4. cross-vrf-block     – All remaining cross-VRF pairs (expected FAIL)
    """
    phase_id: str        # one of the four values above
    name: str
    description: str
    cases: List[TestCase]
    vms_per_subnet: int = 1   # VMs to provision per subnet for this phase
    batch_index: int = 0      # batch number when a phase is split into multiple batches


@dataclass
class TestResult:
    src_subnet: str
    dst_subnet: str
    expected: str
    actual: str
    status: str
    reason: str
    phase: str = "intra-vrf"
    src_vm_index: int = 0
    dst_vm_index: int = 0
    # Probe type used to produce this result
    probe_type: str = "icmp"
    # TCP port probed (0 = ICMP or multi-port summary)
    tcp_port: int = 0


@dataclass
class PlacementChoice:
    datacenter: str
    cluster: str
    host: str
    datastore: str
    selection_policy: str


@dataclass
class NetworkBinding:
    datacenter: str
    cluster: str
    vlan: str
    dpg_name: str
    dpg_key: str
    resolution_policy: str
