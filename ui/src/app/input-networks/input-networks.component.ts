import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ClarityModule } from '@clr/angular';
import { ApiService, NetworkRow, VcInventory } from '../api.service';

interface EditableRow extends NetworkRow { _mngt?: boolean; }

@Component({
  selector: 'app-input-networks',
  standalone: true,
  imports: [CommonModule, FormsModule, ClarityModule],
  templateUrl: './input-networks.component.html',
  styleUrl: './input-networks.component.scss'
})
export class InputNetworksComponent implements OnInit {
  rows: EditableRow[] = [];
  inventory: VcInventory | null = null;
  msg = '';
  syncing = false;
  importing = false;

  constructor(private api: ApiService) {}

  ngOnInit() { this.load(); }

  load() {
    this.api.getInput().subscribe({
      next: r => { this.rows = r.rows.map(row => ({ ...row, _mngt: (row['cluster'] || '').toUpperCase() === 'MNGT' })); },
      error: err => console.error('getInput', err)
    });
  }

  syncInventory() {
    this.syncing = true;
    this.api.getInventory().subscribe({
      next: inv => {
        this.inventory = inv; this.syncing = false;
        this.msg = '\u2713 Loaded ' + (inv.datacenters || []).length + ' DC(s) from vCenter';
        setTimeout(() => this.msg = '', 4000);
      },
      error: err => { this.syncing = false; this.msg = '\u2717 Sync failed: ' + (err.error?.detail || err.message); }
    });
  }

  addRow() { this.rows.push({ datacenter: '', cluster: '', vlan: '', subnet: '', gw: '', vrf: '', _mngt: false }); }

  deleteRow(i: number) { this.rows.splice(i, 1); }

  onClusterChange(row: EditableRow) { row._mngt = (row['cluster'] || '').toUpperCase() === 'MNGT'; }

  /** Send file to server for parsing; replace displayed rows with result. */
  onImportFile(event: Event) {
    const file = (event.target as HTMLInputElement).files?.[0];
    (event.target as HTMLInputElement).value = '';
    if (!file) return;
    this.importing = true;
    this.msg = 'Parsing ' + file.name + '…';
    this.api.previewInput(file).subscribe({
      next: res => {
        this.importing = false;
        this.rows = res.rows.map(r => ({ ...r, _mngt: (r['cluster'] || '').toUpperCase() === 'MNGT' }));
        const rejNote = res.rejected.length ? ` — ${res.rejected.length} row(s) rejected (see console)` : '';
        this.msg = `\u2713 ${res.count} row(s) accepted${rejNote} — click Save to confirm`;
        if (res.rejected.length) {
          console.warn('[import] Rejected rows:', res.rejected.map(r => `Line ${r.line}: ${r.reason}`).join('\n'));
        }
        setTimeout(() => this.msg = '', 8000);
      },
      error: err => {
        this.importing = false;
        this.msg = '\u2717 Parse failed: ' + (err.error?.detail || err.message);
      }
    });
  }

  save() {
    const cleanRows = this.rows
      .filter(r => Object.keys(r).filter(k => !k.startsWith('_')).some(k => (r as any)[k]))
      .map(({ _mngt, ...rest }) => rest);
    this.api.saveInput(cleanRows).subscribe({
      next: () => { this.msg = '\u2713 Saved'; setTimeout(() => this.msg = '', 3000); },
      error: err => { this.msg = '\u2717 ' + (err.error?.detail || err.message); }
    });
  }

  dcOptions(): string[] { return this.inventory?.datacenters || []; }

  clusterOptions(dc: string): string[] {
    const inv = this.inventory?.clusters?.[dc] || [];
    return ['MNGT', ...inv.filter(c => c !== 'MNGT')];
  }

  pgOptions(dc: string, cluster: string): Array<{ label: string; value: string }> {
    const pgs = this.inventory?.portgroups?.[dc]?.[cluster] || [];
    return pgs.map(p => {
      const hasVlan = p.vlan && p.vlan !== '0';
      return { label: hasVlan ? p.name + '  [VLAN ' + p.vlan + ']' : p.name, value: hasVlan ? p.vlan : p.name };
    });
  }
}
