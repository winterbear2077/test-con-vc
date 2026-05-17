import { Component, OnInit, Output, EventEmitter, isDevMode } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ClarityModule } from '@clr/angular';
import { ApiService, HistoryEntry } from '../api.service';
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
    if (run.vms_cleaned) return;
    if (!confirm('Delete all test VMs for run ' + run.run_id + '?')) return;
    this.cleaningId = run.run_id;
    this.api.cleanupRun(run.run_id).subscribe({
      next: () => { run.vms_cleaned = 1; this.cleaningId = null; },
      error: err => { this.cleanErrMsg[run.run_id] = '\u2717 ' + (err.error?.detail || err.message); this.cleaningId = null; }
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

  formatTime(runId: string): string {
    // run_id format: 20250514T123456Z
    const m = runId.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/);
    if (!m) return runId;
    return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]} UTC`;
  }
}

