import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ClarityModule } from '@clr/angular';
import { forkJoin } from 'rxjs';
import { ApiService, VrfRuleRow } from '../api.service';

export interface VrfRule {
  id: number;
  fromVrf: string;
  toVrf: string;
  action: 'PASS' | 'FAIL';
  comment: string;
}

@Component({
  selector: 'app-vrf-rules',
  standalone: true,
  imports: [CommonModule, FormsModule, ClarityModule],
  templateUrl: './vrf-rules.component.html',
  styleUrl: './vrf-rules.component.scss'
})
export class VrfRulesComponent implements OnInit {
  rules: VrfRule[] = [];
  vrfOptions: string[] = [];
  saveMsg = '';
  private _nextId = 1;

  constructor(private api: ApiService) {}

  ngOnInit() {
    forkJoin({
      rules: this.api.getVrfRules(),
      input: this.api.getInput(),
    }).subscribe({
      next: ({ rules: res, input: r }) => {
        // Populate vrfOptions first so the <select> renders with the correct
        // options before rule rows are added, avoiding the race condition where
        // the select defaults to the first option instead of the saved value.
        this.vrfOptions = [...new Set(r.rows.map(row => row['vrf'] || '').filter(Boolean))].sort();
        this.rules = res.rules.map(r => ({
          id: this._nextId++,
          fromVrf: r.from_vrf,
          toVrf: r.to_vrf,
          action: r.action,
          comment: r.comment,
        }));
      },
      error: () => {}
    });
  }

  addRule() { this.rules.push({ id: this._nextId++, fromVrf: '', toVrf: '', action: 'PASS', comment: '' }); }
  removeRule(id: number) { this.rules = this.rules.filter(r => r.id !== id); }
  clearAll() { this.rules = []; }

  save() {
    const payload: VrfRuleRow[] = this.rules.map(r => ({
      from_vrf: r.fromVrf,
      to_vrf: r.toVrf,
      action: r.action,
      comment: r.comment,
    }));
    this.api.saveVrfRules(payload).subscribe({
      next: () => { this.saveMsg = '✓ Saved'; setTimeout(() => this.saveMsg = '', 3000); },
      error: () => { this.saveMsg = '✗ Save failed'; },
    });
  }

  getVrfLinks(): string[] {
    return this.rules
      .filter(r => r.fromVrf && r.toVrf)
      .map(r => r.action === 'FAIL' ? r.fromVrf + ':' + r.toVrf + ':FAIL' : r.fromVrf + ':' + r.toVrf);
  }

  get previewLines(): Array<{ from: string; to: string; allow: boolean }> {
    return this.rules.filter(r => r.fromVrf && r.toVrf).map(r => ({ from: r.fromVrf, to: r.toVrf, allow: r.action === 'PASS' }));
  }
}

