import { Component, OnInit, Output, EventEmitter, isDevMode } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ClarityModule } from '@clr/angular';
import { ApiService, CleanupAllResponse, CleanupRunResponse, HistoryEntry } from '../api.service';
import { ResultPanelComponent } from '../result-panel/result-panel.component';

@Component({
  selector: 'app-history',
  standalone: true,
  imports: [CommonModule, ClarityModule, ResultPanelComponent],
  templateUrl: './history.component.html',
  styleUrl: './history.component.scss'
})
export class HistoryComponent implements OnInit {
  @Output() navigate = new EventEmitter<string>();

  runs: HistoryEntry[] = [];
  loading = false;
  error = '';
  cleaningId: string | null = null;
  cleaningAll = false;
  cleanErrMsg: Record<string, string> = {};
  deletingId: string | null = null;
  readonly devMode = isDevMode();

  selectedRunId: string | null = null;
  selectedResult: any = null;
  loadingResult = false;
  drawerOpen = false;

  // Pagination
  readonly pageSize = 10;
  currentPage = 1;

  get totalPages(): number { return Math.max(1, Math.ceil(this.runs.length / this.pageSize)); }
  get pageRuns(): HistoryEntry[] {
    const start = (this.currentPage - 1) * this.pageSize;
    return this.runs.slice(start, start + this.pageSize);
  }
  goPage(p: number) { this.currentPage = Math.max(1, Math.min(p, this.totalPages)); }

  constructor(private api: ApiService) {}

  ngOnInit() { this.load(); }

  load() {
    this.loading = true;
    this.api.getHistory().subscribe({
      next: r => { this.runs = r; this.loading = false; this.currentPage = 1; },
      error: err => { this.error = 'Failed to load: ' + (err.error?.detail || err.message); this.loading = false; }
    });
  }

  selectRun(run: HistoryEntry) {
    if (this.selectedRunId === run.run_id) {
      this.closeDrawer();
      return;
    }
    this.selectedRunId = run.run_id;
    this.selectedResult = null;
    this.loadingResult = true;
    this.drawerOpen = true;
    this.api.getRunResult(run.run_id).subscribe({
      next: r => { this.selectedResult = r; this.loadingResult = false; },
      error: () => { this.loadingResult = false; }
    });
  }

  closeDrawer() {
    this.drawerOpen = false;
    this.selectedRunId = null;
    this.selectedResult = null;
  }

  cleanup(run: HistoryEntry) {
    if (!this.canCleanup(run)) return;
    if (!confirm('Run cleanup for ' + run.run_id + '? This will delete tracked test VMs/ISOs for this run.')) return;
    this.cleaningId = run.run_id;
    this.api.cleanupRun(run.run_id).subscribe({
      next: (res: CleanupRunResponse) => {
        const state = this.normalizeCleanupState(res.cleanup_state);
        run.cleanup_state = state;
        run.cleaned = state === 'cleanup' ? 1 : 0;
        run.vms_cleaned = run.cleaned;
        const icon = state === 'cleanup' ? '\u2713' : state === 'partial' ? '\u26A0' : '\u2717';
        this.cleanErrMsg[run.run_id] = `${icon} cleanup=${state} (vm cleaned:${res.cleaned}, vm failed:${res.failed}, vm skipped:${res.skipped}, iso cleaned:${res.iso_cleaned || 0}, iso failed:${res.iso_failed || 0}, remaining:${res.remaining || 0})`;
        this.cleaningId = null;
      },
      error: err => { this.cleanErrMsg[run.run_id] = '\u2717 ' + (err.error?.detail || err.message); this.cleaningId = null; }
    });
  }

  cleanupAll() {
    if (!confirm('Delete all possible test artifacts across all runs? This will remove VMs by vm_prefix and tracked uploaded ISOs.')) return;
    this.cleaningAll = true;
    this.error = '';
    this.api.cleanupAllRuns().subscribe({
      next: (res: CleanupAllResponse) => {
        const state = this.normalizeCleanupState(res.cleanup_state);
        this.error = '';
        this.cleaningAll = false;
        this.load();
        this.cleanErrMsg['__global__'] =
          `\u2713 cleanup-all=${state} (vm cleaned:${res.vm_cleaned}, vm failed:${res.vm_failed}, vm skipped:${res.vm_skipped}, iso cleaned:${res.iso_cleaned}, iso failed:${res.iso_failed}, remaining:${res.remaining})`;
      },
      error: err => {
        this.cleaningAll = false;
        this.error = 'Cleanup-all failed: ' + (err.error?.detail || err.message);
      }
    });
  }

  deleteRecord(runId: string) {
    if (!confirm('Remove run ' + runId + ' from history?')) return;
    this.deletingId = runId;
    this.api.deleteHistory(runId).subscribe({
      next: () => {
        this.runs = this.runs.filter(r => r.run_id !== runId);
        if (this.selectedRunId === runId) { this.selectedRunId = null; this.selectedResult = null; }
        this.deletingId = null;
      },
      error: err => { this.cleanErrMsg[runId] = '\u2717 ' + (err.error?.detail || err.message); this.deletingId = null; }
    });
  }

  statusClass(s: string): string {
    return s === 'PASS' ? 'label-success' : s === 'FAIL' ? 'label-danger' : 'label-light-blue';
  }

  normalizeCleanupState(s?: string): 'pending' | 'cleanup' | 'partial' | 'failed' {
    const v = String(s || '').trim().toLowerCase();
    if (v === 'cleanup' || v === 'partial' || v === 'failed') return v;
    return 'pending';
  }

  canCleanup(run: HistoryEntry): boolean {
    const state = this.normalizeCleanupState(run.cleanup_state);
    return state !== 'cleanup';
  }

  cleanupStateLabel(run: HistoryEntry): string {
    const state = this.normalizeCleanupState(run.cleanup_state);
    if (state === 'cleanup') return 'cleanup';
    if (state === 'partial') return 'partial clean (failed)';
    if (state === 'failed') return 'failed';
    return 'pending';
  }

  cleanupIcon(run: HistoryEntry): string {
    const state = this.normalizeCleanupState(run.cleanup_state);
    if (state === 'cleanup') return 'check-circle';
    if (state === 'partial') return 'warning-standard';
    if (state === 'failed') return 'error-standard';
    return 'vm';
  }

  cleanupIconColor(run: HistoryEntry): string {
    const state = this.normalizeCleanupState(run.cleanup_state);
    if (state === 'cleanup') return '#1d7a0a';
    if (state === 'partial') return '#d97a00';
    if (state === 'failed') return '#c92100';
    return '#666';
  }

  formatTime(runId: string): string {
    // run_id format: 20250514T123456Z
    const m = runId.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
    if (!m) return runId;
    return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]} UTC`;
  }
}

