import { Component, Input, OnChanges, SimpleChanges } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ClarityModule } from '@clr/angular';

const PHASE_NAMES: Record<string, string> = {
  'intra-subnet': 'Intra-Subnet',
  'intra-vrf': 'Intra-VRF',
  'cross-vrf-allowlist': 'Cross-VRF Allow',
  'cross-vrf-block': 'Cross-VRF Block',
};

const PHASE_COLORS: Record<string, string> = {
  'intra-subnet': 'label-blue',
  'intra-vrf': 'label-success',
  'cross-vrf-allowlist': 'label-warning',
  'cross-vrf-block': 'label-light-blue',
};

function parseCustomPhaseId(phaseId: string): { row: number; step: number } | null {
  const m = /^custom-(\d+)-step(\d+)$/.exec(phaseId || '');
  if (!m) return null;
  return { row: Number(m[1]), step: Number(m[2]) };
}

function phaseMeta(phaseId: string): { label: string; color: string } {
  if (PHASE_NAMES[phaseId]) {
    return {
      label: PHASE_NAMES[phaseId],
      color: PHASE_COLORS[phaseId] || 'label-light-blue',
    };
  }
  const custom = parseCustomPhaseId(phaseId);
  if (custom) {
    const stepLabel = custom.step === 1 ? 'Ping' : 'Protocol';
    return {
      label: `Custom Row ${custom.row + 1} - ${stepLabel}`,
      color: custom.step === 1 ? 'label-info' : 'label-warning',
    };
  }
  return { label: phaseId, color: 'label-light-blue' };
}

@Component({
  selector: 'app-result-panel',
  standalone: true,
  imports: [CommonModule, ClarityModule],
  templateUrl: './result-panel.component.html',
  styleUrl: './result-panel.component.scss'
})
export class ResultPanelComponent implements OnChanges {
  @Input() result: any = null;
  processed: any = null;

  ngOnChanges(changes: SimpleChanges) {
    if (changes['result']) {
      this.processed = this.result ? this.processResult(this.result) : null;
    }
  }

  processResult(result: any): any {
    if (!result) return null;
    const details: any[] = Array.isArray(result.Results) ? result.Results : (result.Results?.details || []);
    const subnetVrf: Record<string, string> = {};
    const subnetVlan: Record<string, string> = {};
    [...(result.ParsedInput?.vm_provisioned || []), ...(result.ParsedInput?.mngt_esxi_skipped || [])].forEach((r: any) => {
      if (r.subnet) { subnetVrf[r.subnet] = r.vrf || ''; subnetVlan[r.subnet] = r.vlan || ''; }
    });
    const defaultPhaseOrder = ['intra-subnet', 'intra-vrf', 'cross-vrf-allowlist', 'cross-vrf-block'];
    const detailsByPhase: Record<string, any[]> = {};
    details.forEach(d => {
      const ph = d.phase || 'intra-vrf';
      if (!detailsByPhase[ph]) detailsByPhase[ph] = [];
      detailsByPhase[ph].push(d);
    });
    const presentPhases = Object.keys(detailsByPhase);
    const extraPhases = presentPhases
      .filter(ph => !defaultPhaseOrder.includes(ph))
      .sort((a, b) => {
        const pa = parseCustomPhaseId(a);
        const pb = parseCustomPhaseId(b);
        if (pa && pb) {
          if (pa.row !== pb.row) return pa.row - pb.row;
          return pa.step - pb.step;
        }
        if (pa) return 1;
        if (pb) return -1;
        return a.localeCompare(b);
      });
    const phaseOrder = [...defaultPhaseOrder, ...extraPhases];
    const matrices = phaseOrder.filter(ph => (detailsByPhase[ph] || []).length > 0).map(ph => {
      const pds = detailsByPhase[ph];
      const meta = phaseMeta(ph);
      return {
        ph, pds,
        srcSubnets: [...new Set(pds.map((d: any) => d.src_subnet))],
        dstSubnets: [...new Set(pds.map((d: any) => d.dst_subnet))],
        phaseLabel: meta.label,
        phaseColor: meta.color,
      };
    });
    return {
      ...result, details, subnetVrf, subnetVlan, matrices,
      total: details.length,
      passed: details.filter((d: any) => d.status === 'pass').length,
      failed: details.filter((d: any) => d.status !== 'pass').length,
    };
  }

  cellsFor(matrix: any, src: string, dst: string): any[] {
    return matrix.pds.filter((d: any) => d.src_subnet === src && d.dst_subnet === dst);
  }

  cellClass(d: any): string {
    const { expected, actual } = d;
    if (actual === 'UNKNOWN') return 'c-uk';
    if (expected === 'PASS' && actual === 'PASS') return 'c-pp';
    if (expected === 'FAIL' && actual === 'FAIL') return 'c-ff';
    if (expected === 'PASS' && actual === 'FAIL') return 'c-pf';
    return 'c-fp';
  }

  cellIcon(d: any): string {
    const { expected, actual } = d;
    if (actual === 'UNKNOWN') return '?';
    if (expected === 'PASS' && actual === 'PASS') return '\u2713';
    if (expected === 'FAIL' && actual === 'FAIL') return '\uD83D\uDD12';
    if (expected === 'PASS' && actual === 'FAIL') return '\u2717';
    return '\u26A0';
  }

  phaseName(ph: string): string { return phaseMeta(ph).label; }
  phaseColor(ph: string): string { return phaseMeta(ph).color; }
}
