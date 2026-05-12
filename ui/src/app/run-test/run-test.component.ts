import { Component, OnDestroy, ViewChild, ElementRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ClarityModule } from '@clr/angular';
import { ApiService } from '../api.service';

interface LogLine { text: string; cls: string; }

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

@Component({
  selector: 'app-run-test',
  standalone: true,
  imports: [CommonModule, FormsModule, ClarityModule],
  templateUrl: './run-test.component.html',
  styleUrl: './run-test.component.scss'
})
export class RunTestComponent implements OnDestroy {
  @ViewChild('logEl') logEl!: ElementRef<HTMLDivElement>;

  executeVcenter = false;
  probeMode = 'in-guest-ping';
  maxRetries = 0;
  cleanupOnFailure = false;
  phasedTesting = false;
  vmsPerSubnet = 1;
  maxVmsPerPhase = 20;
  phaseIntraSubnet = false;
  phaseIntraVrf = true;
  phaseCrossVrfAllow = true;
  phaseCrossVrfBlock = true;

  running = false;
  indicator: 'idle' | 'running' | 'pass' | 'fail' = 'idle';
  logLines: LogLine[] = [];
  result: any = null;

  private _sse: EventSource | null = null;

  constructor(private api: ApiService) {}

  ngOnDestroy() { this._sse?.close(); }

  get intraSubnetWarn(): boolean { return this.phaseIntraSubnet && this.vmsPerSubnet < 2; }

  get selectedPhases(): string[] {
    const p: string[] = [];
    if (this.phaseIntraSubnet) p.push('intra-subnet');
    if (this.phaseIntraVrf) p.push('intra-vrf');
    if (this.phaseCrossVrfAllow) p.push('cross-vrf-allowlist');
    if (this.phaseCrossVrfBlock) p.push('cross-vrf-block');
    return p;
  }

  startRun() {
    if (this.intraSubnetWarn) return;
    this.running = true; this.result = null; this.logLines = []; this.indicator = 'running';
    const req = {
      execute_vcenter: this.executeVcenter,
      probe_mode: this.probeMode,
      max_retries: this.maxRetries,
      cleanup_on_failure: this.cleanupOnFailure,
      phased_testing: this.phasedTesting,
      vms_per_subnet: this.vmsPerSubnet,
      max_vms_per_phase: this.maxVmsPerPhase,
      phases: this.selectedPhases.join(','),
      vrf_links: [],
    };
    this.api.startRun(req).subscribe({
      next: ({ run_id }) => { this.appendLog('\u25B6 Run started: ' + run_id, 'hdr'); this.stream(run_id); },
      error: err => { this.appendLog('Error: ' + (err.error?.detail || err.message), 'err'); this.running = false; this.indicator = 'idle'; }
    });
  }

  private stream(runId: string) {
    this._sse?.close();
    this._sse = new EventSource('/api/run/' + runId + '/stream');
    this._sse.onmessage = (e) => {
      const raw = e.data;
      if (raw.startsWith('__DONE__:')) {
        this._sse!.close();
        const rc = parseInt(raw.split(':')[1]);
        this.running = false;
        this.indicator = rc === 0 ? 'pass' : 'fail';
        this.appendLog('\u25B6 Finished (exit ' + rc + ')', rc === 0 ? 'ok' : 'err');
        this.api.getRunResult(runId).subscribe({ next: r => this.result = this.processResult(r), error: () => {} });
        return;
      }
      let line: string;
      try { line = JSON.parse(raw); } catch { line = raw; }
      const cls = line.includes('PASS') ? 'ok'
                : (line.includes('fail') || line.includes('Error') || line.includes('failed')) ? 'err'
                : (line.includes('Warning') || line.includes('warn')) ? 'warn' : '';
      this.appendLog(line, cls);
    };
    this._sse.onerror = () => {
      this.running = false; this.indicator = 'idle';
      this.appendLog('Connection lost.', 'warn');
    };
  }

  clearLog() { this.logLines = []; }

  private appendLog(text: string, cls = '') {
    this.logLines.push({ text, cls });
    setTimeout(() => {
      if (this.logEl) this.logEl.nativeElement.scrollTop = this.logEl.nativeElement.scrollHeight;
    });
  }

  processResult(result: any): any {
    if (!result) return null;
    const details: any[] = Array.isArray(result.Results) ? result.Results : (result.Results?.details || []);
    const subnetVrf: Record<string, string> = {};
    const subnetVlan: Record<string, string> = {};
    [...(result.ParsedInput?.vm_provisioned || []), ...(result.ParsedInput?.mngt_esxi_skipped || [])].forEach((r: any) => {
      if (r.subnet) { subnetVrf[r.subnet] = r.vrf || ''; subnetVlan[r.subnet] = r.vlan || ''; }
    });
    const phaseOrder = ['intra-subnet', 'intra-vrf', 'cross-vrf-allowlist', 'cross-vrf-block'];
    const detailsByPhase: Record<string, any[]> = {};
    details.forEach(d => {
      const ph = d.phase || 'intra-vrf';
      if (!detailsByPhase[ph]) detailsByPhase[ph] = [];
      detailsByPhase[ph].push(d);
    });
    const matrices = phaseOrder.filter(ph => (detailsByPhase[ph] || []).length > 0).map(ph => {
      const pds = detailsByPhase[ph];
      return {
        ph, pds,
        srcSubnets: [...new Set(pds.map((d: any) => d.src_subnet))],
        dstSubnets: [...new Set(pds.map((d: any) => d.dst_subnet))],
        phaseLabel: PHASE_NAMES[ph] || ph,
        phaseColor: PHASE_COLORS[ph] || 'label-light-blue',
      };
    });
    return { ...result, details, subnetVrf, subnetVlan, matrices,
      total: details.length, passed: details.filter((d: any) => d.status === 'pass').length,
      failed: details.filter((d: any) => d.status !== 'pass').length };
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

  phaseName(ph: string): string { return PHASE_NAMES[ph] || ph; }
  phaseColor(ph: string): string { return PHASE_COLORS[ph] || 'label-light-blue'; }
}
