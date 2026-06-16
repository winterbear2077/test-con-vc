import { Component, NgZone, OnDestroy, ViewChild, ElementRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ClarityModule } from '@clr/angular';
import { ApiService, TestSuite } from '../api.service';
import { ResultPanelComponent } from '../result-panel/result-panel.component';
import { PluginContextService } from '../plugin-context.service';
import { Subscription } from 'rxjs';

interface LogLine { text: string; cls: string; }

@Component({
  selector: 'app-run-test',
  standalone: true,
  imports: [CommonModule, FormsModule, ClarityModule, ResultPanelComponent],
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
  selectedTestsuite = '';
  suites: TestSuite[] = [];

  running = false;
  indicator: 'idle' | 'running' | 'pass' | 'fail' = 'idle';
  logLines: LogLine[] = [];
  result: any = null;
  private _currentRunId: string | null = null;

  private _sse: EventSource | null = null;
  private _sseDone = false;
  private _suiteChangedSub: Subscription | null = null;

  constructor(private api: ApiService, private zone: NgZone, private pluginCtx: PluginContextService) {}

  ngOnInit() {
    this.refreshSuites();
    this._suiteChangedSub = this.api.testsuiteChanged$.subscribe(() => this.refreshSuites());
  }

  ngOnDestroy() {
    this._sse?.close();
    this._suiteChangedSub?.unsubscribe();
  }

  get intraSubnetWarn(): boolean { return this.phaseIntraSubnet && this.vmsPerSubnet < 2; }

  get selectedPhases(): string[] {
    const p: string[] = [];
    if (this.phaseIntraSubnet) p.push('intra-subnet');
    if (this.phaseIntraVrf) p.push('intra-vrf');
    if (this.phaseCrossVrfAllow) p.push('cross-vrf-allowlist');
    if (this.phaseCrossVrfBlock) p.push('cross-vrf-block');
    return p;
  }

  refreshSuites() {
    this.api.getTestSuites().subscribe({
      next: ({ suites }) => {
        this.suites = suites || [];
      },
      error: () => {
        this.suites = [];
      },
    });
  }

  async startRun() {
    if (this.intraSubnetWarn) return;

    if (this.executeVcenter && this.pluginCtx.isPlugin && !this.api.hasAuth()) {
      await this.pluginCtx.waitForSession(3000);
    }

    if (this.executeVcenter && !this.api.hasAuth()) {
      this.appendLog('Error: missing vCenter auth. Save credentials in Config first (or wait for plugin session).', 'err');
      this.indicator = 'idle';
      return;
    }

    this.running = true; this.result = null; this.logLines = []; this.indicator = 'running';
    this.api.getVrfRules().subscribe({
      next: ({ rules }) => {
        const vrf_links = rules
          .filter(r => r.from_vrf && r.to_vrf)
          .map(r => r.action === 'FAIL' ? `${r.from_vrf}:${r.to_vrf}:FAIL` : `${r.from_vrf}:${r.to_vrf}`);
        const req = {
          execute_vcenter: this.executeVcenter,
          probe_mode: this.probeMode,
          max_retries: this.maxRetries,
          cleanup_on_failure: this.cleanupOnFailure,
          phased_testing: this.phasedTesting,
          vms_per_subnet: this.vmsPerSubnet,
          max_vms_per_phase: this.maxVmsPerPhase,
          phases: this.phasedTesting ? this.selectedPhases.join(',') : '',
          testsuite: (this.selectedTestsuite || '').trim(),
          vrf_links,
        };
        this.api.startRun(req).subscribe({
          next: ({ run_id }) => { this._currentRunId = run_id; this.appendLog('\u25B6 Run started: ' + run_id, 'hdr'); this.stream(run_id); },
          error: err => { this.appendLog('Error: ' + (err.error?.detail || err.message), 'err'); this.running = false; this.indicator = 'idle'; }
        });
      },
      error: () => { this.appendLog('Error: failed to load VRF rules', 'err'); this.running = false; this.indicator = 'idle'; }
    });
  }

  cancelRun() {
    if (!this._currentRunId) return;
    this.api.cancelRun(this._currentRunId).subscribe({
      next: () => this.appendLog('\u25A0 Cancel requested…', 'warn'),
      error: () => this.appendLog('Cancel request failed', 'err'),
    });
  }

  private stream(runId: string) {
    this._sseDone = false;
    this._sse?.close();
    this._sse = new EventSource(this.api.streamRunUrl(runId));
    this._sse.onmessage = (e) => {
      this.zone.run(() => {
        const raw = e.data;
        if (raw.startsWith('__DONE__:')) {
          this._sseDone = true;
          this._sse!.close();
          const rc = parseInt(raw.split(':')[1]);
          this.running = false;
          this.indicator = rc === 0 ? 'pass' : (rc === 5 ? 'idle' : 'fail');
          const label = rc === 5 ? 'Cancelled' : (rc === 0 ? 'PASS' : 'FAIL');
          this.appendLog('\u25B6 Finished (exit ' + rc + ' — ' + label + ')', rc === 0 ? 'ok' : (rc === 5 ? 'warn' : 'err'));
          this.api.getRunResult(runId).subscribe({ next: r => this.result = r, error: () => {} });
          return;
        }
        let line: string;
        try { line = JSON.parse(raw); } catch { line = raw; }
        const cls = line.includes('PASS') ? 'ok'
                  : (line.includes('fail') || line.includes('Error') || line.includes('failed')) ? 'err'
                  : (line.includes('Warning') || line.includes('warn')) ? 'warn' : '';
        this.appendLog(line, cls);
      });
    };
    this._sse.onerror = () => {
      this.zone.run(() => {
        if (this._sseDone) return;  // stream closed normally after __DONE__ — ignore
        this.running = false; this.indicator = 'idle';
        this.appendLog('Connection lost.', 'warn');
      });
    };
  }

  clearLog() { this.logLines = []; }

  private appendLog(text: string, cls = '') {
    this.logLines.push({ text, cls });
    setTimeout(() => {
      if (this.logEl) this.logEl.nativeElement.scrollTop = this.logEl.nativeElement.scrollHeight;
    });
  }

}

