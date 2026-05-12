import { Component, OnInit, Output, EventEmitter, isDevMode } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ClarityModule } from '@clr/angular';
import { ApiService, HistoryEntry } from '../api.service';

@Component({
  selector: 'app-history',
  standalone: true,
  imports: [CommonModule, ClarityModule],
  templateUrl: './history.component.html',
  styleUrl: './history.component.scss'
})
export class HistoryComponent implements OnInit {
  @Output() navigate = new EventEmitter<string>();

  runs: HistoryEntry[] = [];
  loading = false;
  error = '';
  cleaningId: string | null = null;
  cleanMsg: Record<string, string> = {};
  deletingId: string | null = null;
  readonly devMode = isDevMode();

  constructor(private api: ApiService) {}

  ngOnInit() { this.load(); }

  load() {
    this.loading = true;
    this.api.getHistory().subscribe({
      next: r => { this.runs = r; this.loading = false; },
      error: err => { this.error = 'Failed to load: ' + (err.error?.detail || err.message); this.loading = false; }
    });
  }

  cleanup(runId: string) {
    if (!confirm('Delete all test VMs for run ' + runId + '?')) return;
    this.cleaningId = runId;
    this.api.cleanupRun(runId).subscribe({
      next: r => { this.cleanMsg[runId] = 'Cleaned ' + r.cleaned + ', skipped ' + r.skipped + ', failed ' + r.failed; this.cleaningId = null; },
      error: err => { this.cleanMsg[runId] = '\u2717 ' + (err.error?.detail || err.message); this.cleaningId = null; }
    });
  }

  deleteRecord(runId: string) {
    if (!confirm('Remove run ' + runId + ' from history?')) return;
    this.deletingId = runId;
    this.api.deleteHistory(runId).subscribe({
      next: () => { this.runs = this.runs.filter(r => r.run_id !== runId); this.deletingId = null; },
      error: err => { this.cleanMsg[runId] = '\u2717 ' + (err.error?.detail || err.message); this.deletingId = null; }
    });
  }

  statusClass(s: string): string {
    return s === 'PASS' ? 'label-success' : s === 'FAIL' ? 'label-danger' : 'label-light-blue';
  }
}
