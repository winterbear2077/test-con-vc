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

  ngOnInit() {
    this.load();
    this.syncInventory();   // auto-trigger on page enter
  }

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
        this._validateRowsAgainstInventory(inv);
        this.msg = '✓ Loaded ' + (inv.datacenters || []).length + ' DC(s) from vCenter';
        setTimeout(() => this.msg = '', 4000);
      },
      error: err => { this.syncing = false; this.msg = '✗ Sync failed: ' + (err.error?.detail || err.message); }
    });
  }

  /** For each row that has a pg set, verify it exists in inventory for that dc/cluster.
   *  If not found, clear both pg and vlan to avoid silent mismatches. */
  private _validateRowsAgainstInventory(inv: VcInventory) {
    for (const row of this.rows) {
      const pg = (row['pg'] || '').trim();
      const vlan = (row['vlan'] || '').trim();
      if (!pg && !vlan) continue;
      const dc = row['datacenter'] || '';
      const cl = row['cluster'] || '';
      const pgs = inv.portgroups?.[dc]?.[cl] || [];
      // If pg is set, check it exists in inventory
      if (pg) {
        const found = pgs.find(p => p.name === pg);
        if (!found) { row['pg'] = ''; row['vlan'] = ''; continue; }
        // pg found but vlan mismatch — re-sync vlan from inventory
        const expectedVlan = (found.vlan && found.vlan !== '0') ? found.vlan : '';
        if (vlan && vlan !== expectedVlan) { row['vlan'] = expectedVlan; }
      }
      // If only vlan set (no pg), check if any pg in this dc/cluster has that vlan
      if (!pg && vlan) {
        const matchPg = pgs.find(p => p.vlan === vlan);
        if (!matchPg) { row['vlan'] = ''; }
      }
    }
  }

  addRow() { this.rows.push({ datacenter: '', cluster: '', pg: '', vlan: '', subnet: '', gw: '', vrf: '', _mngt: false }); }

  deleteRow(i: number) { this.rows.splice(i, 1); }

  onClusterChange(row: EditableRow) {
    row._mngt = (row['cluster'] || '').toUpperCase() === 'MNGT';
    // clear pg when cluster changes
    row['pg'] = '';
    row['vlan'] = '';
  }

  /** When a portgroup is selected, auto-fill vlan from inventory. */
  onPgChange(row: EditableRow) {
    const pgName = row['pg'] || '';
    if (!pgName || !this.inventory) return;
    const dc = row['datacenter'] || '';
    const cl = row['cluster'] || '';
    const pgs = this.inventory.portgroups?.[dc]?.[cl] || [];
    const found = pgs.find(p => p.name === pgName);
    if (found) {
      row['vlan'] = (found.vlan && found.vlan !== '0') ? found.vlan : '';
    }
  }

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
        this.msg = `✓ ${res.count} row(s) accepted${rejNote} — click Save to confirm`;
        if (res.rejected.length) {
          console.warn('[import] Rejected rows:', res.rejected.map(r => `Line ${r.line}: ${r.reason}`).join('\n'));
        }
        setTimeout(() => this.msg = '', 8000);
      },
      error: err => {
        this.importing = false;
        this.msg = '✗ Parse failed: ' + (err.error?.detail || err.message);
      }
    });
  }

  save() {
    const cleanRows = this.rows
      .filter(r => Object.keys(r).filter(k => !k.startsWith('_')).some(k => (r as any)[k]))
      .map(({ _mngt, ...rest }) => rest);
    this.api.saveInput(cleanRows).subscribe({
      next: () => { this.msg = '✓ Saved'; setTimeout(() => this.msg = '', 3000); },
      error: err => { this.msg = '✗ ' + (err.error?.detail || err.message); }
    });
  }

  dcOptions(): string[] { return this.inventory?.datacenters || []; }

  clusterOptions(dc: string): string[] {
    const inv = this.inventory?.clusters?.[dc] || [];
    return ['MNGT', ...inv.filter(c => c !== 'MNGT')];
  }

  pgOptions(dc: string, cluster: string): Array<{ name: string; vlan: string; label: string }> {
    const pgs = this.inventory?.portgroups?.[dc]?.[cluster] || [];
    return pgs.map(p => {
      const hasVlan = p.vlan && p.vlan !== '0';
      return { name: p.name, vlan: hasVlan ? p.vlan : '', label: hasVlan ? p.name + ' [' + p.vlan + ']' : p.name };
    });
  }
}
