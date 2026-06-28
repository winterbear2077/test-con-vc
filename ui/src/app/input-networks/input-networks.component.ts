import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ClarityModule } from '@clr/angular';
import { ApiService, NetworkRow, VcInventory } from '../api.service';
import { PluginContextService } from '../plugin-context.service';

interface PgOption {
  name: string;
  vlan: string;
  label: string;
}

interface EditableRow extends NetworkRow {
  _id?: number;
  _mngt?: boolean;
  _pgOptions?: PgOption[];
}

interface InventoryRow extends EditableRow {}

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

  dcOptionsList: string[] = [];
  clusterOptionsByDc: Record<string, string[]> = {};
  pgOptionsByDcCluster: Record<string, PgOption[]> = {};

  private nextRowId = 1;

  constructor(private api: ApiService, private pluginCtx: PluginContextService) {}

  ngOnInit() {
    this.load();
    // Delegate entirely to syncInventory() — it knows how to wait for the plugin session.
    this.syncInventory();
  }

  load() {
    this.api.getInput().subscribe({
      next: r => { this.rows = this.prepareRows(r.rows); },
      error: err => console.error('getInput', err)
    });
  }

  async syncInventory() {
    this.syncing = true;
    this.msg = '';

    // In plugin mode, wait for the vsphereClient SDK to inject the session token
    // (it arrives asynchronously after the iframe is ready — typically < 1 s but
    // can take longer on slower vCenter hosts).
    if (this.pluginCtx.isPlugin && !this.api.hasAuth()) {
      await this.pluginCtx.waitForSession(3000);
    }

    // Guard: still no credentials after waiting — surface a clear, actionable message
    // instead of letting the server return a raw 401 JSON body.
    if (!this.api.hasAuth()) {
      this.syncing = false;
      if (this.pluginCtx.isPlugin) {
        this.msg =
          '✗ No vCenter session detected. ' +
          'Open the plugin server URL directly, save credentials in Config, then refresh this page.';
      } else {
        this.msg = '✗ Please save your vCenter credentials in the Config page first.';
      }
      return;
    }

    this.api.getInventory().subscribe({
      next: inv => {
        this.inventory = inv;
        this.updateInventoryOptionCaches(inv);
        this.rows = this.prepareRows(this._mergeRowsWithInventory(this.rows, this._rowsFromInventory(inv)));
        this.syncing = false;
        this.msg = '✓ Merged ' + this.rows.length + ' network row(s) from vCenter';
        setTimeout(() => this.msg = '', 4000);
      },
      error: err => {
        this.syncing = false;
        const detail: string = err.error?.detail || err.message || 'Unknown error';
        this.msg = '✗ Sync failed: ' + detail;
      }
    });
  }

  /** Merge existing rows with freshly synced inventory rows.
   *
   * Matching priority:
   *   1. datacenter + cluster + pg + vlan
   *   2. datacenter + cluster + vlan
   *   3. datacenter + cluster + pg
   *   4. datacenter + cluster
   *
   * Existing user-entered subnet/gw/vrf values are preserved; inventory values
   * fill blanks and normalize pg/vlan metadata.
   */
  private _mergeRowsWithInventory(existingRows: EditableRow[], inventoryRows: EditableRow[]): EditableRow[] {
    const used = new Set<number>();
    const merged: EditableRow[] = [];

    const existing = existingRows.map(row => ({
      ...row,
      _mngt: (row['cluster'] || '').toUpperCase() === 'MNGT',
    }));

    for (const invRow of inventoryRows) {
      const matchIndex = this._findBestMatchingRow(existing, used, invRow);
      if (matchIndex >= 0) {
        used.add(matchIndex);
        merged.push(this._mergeRow(existing[matchIndex], invRow));
      } else {
        merged.push({ ...invRow, _mngt: (invRow['cluster'] || '').toUpperCase() === 'MNGT' });
      }
    }

    // Keep user rows that do not exist in inventory so manual entries are not lost.
    existing.forEach((row, idx) => {
      if (!used.has(idx)) {
        merged.push(row);
      }
    });

    return merged;
  }

  private _findBestMatchingRow(existingRows: EditableRow[], used: Set<number>, invRow: EditableRow): number {
    const keys = [
      (row: EditableRow) => this._sameKey(row, invRow, ['datacenter', 'cluster', 'pg', 'vlan']),
      (row: EditableRow) => this._sameKey(row, invRow, ['datacenter', 'cluster', 'vlan']),
      (row: EditableRow) => this._sameKey(row, invRow, ['datacenter', 'cluster', 'pg']),
      (row: EditableRow) => this._sameKey(row, invRow, ['datacenter', 'cluster']),
    ];

    for (const predicate of keys) {
      const idx = existingRows.findIndex((row, rowIdx) => !used.has(rowIdx) && predicate(row));
      if (idx >= 0) return idx;
    }
    return -1;
  }

  private _sameKey(a: EditableRow, b: EditableRow, fields: Array<keyof EditableRow>): boolean {
    return fields.every((field) => this._norm(a[field]) === this._norm(b[field]));
  }

  private _norm(value: any): string {
    return String(value ?? '').trim().toLowerCase();
  }

  private _mergeRow(existing: EditableRow, invRow: EditableRow): EditableRow {
    const merged: EditableRow = {
      ...invRow,
      ...existing,
      datacenter: existing.datacenter || invRow.datacenter || '',
      cluster: existing.cluster || invRow.cluster || '',
      pg: existing['pg'] || invRow['pg'] || '',
      vlan: existing['vlan'] || invRow['vlan'] || '',
      subnet: existing.subnet || invRow.subnet || '',
      gw: existing.gw || invRow.gw || '',
      vrf: existing.vrf || invRow.vrf || '',
      _mngt: (existing.cluster || invRow.cluster || '').toUpperCase() === 'MNGT',
    };

    // Keep VLAN authoritative from inventory when the selected portgroup matches.
    // This avoids stale saved VLAN values after "Sync from vCenter".
    if (merged['pg'] && invRow['pg'] && merged['pg'] === invRow['pg']) {
      merged['vlan'] = invRow['vlan'] || '';
    }

    return merged;
  }

  /** Flatten the vCenter inventory into the current table structure. */
  private _rowsFromInventory(inv: VcInventory): InventoryRow[] {
    const rows: InventoryRow[] = [];
    const dcs = inv.datacenters || [];
    for (const dc of dcs) {
      const clusters = inv.clusters?.[dc] || [];
      for (const cluster of clusters) {
        const pgs = inv.portgroups?.[dc]?.[cluster] || [];
        if (!pgs.length) {
          rows.push({ datacenter: dc, cluster, pg: '', vlan: '', subnet: '', gw: '', vrf: '', _mngt: cluster.toUpperCase() === 'MNGT' });
          continue;
        }
        for (const pg of pgs) {
          const hasVlan = pg.vlan && pg.vlan !== '0';
          rows.push({
            datacenter: dc,
            cluster,
            pg: pg.name,
            vlan: hasVlan ? pg.vlan : '',
            subnet: '',
            gw: '',
            vrf: '',
            _mngt: cluster.toUpperCase() === 'MNGT',
          });
        }
      }
    }
    return rows;
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

  addRow() {
    this.rows.push(this.prepareRow({ datacenter: '', cluster: '', pg: '', vlan: '', subnet: '', gw: '', vrf: '' }));
  }

  deleteRow(i: number) { this.rows.splice(i, 1); }

  onDatacenterChange(row: EditableRow) {
    row.cluster = '';
    row['pg'] = '';
    row['vlan'] = '';
    this.refreshRowDerived(row);
  }

  onClusterChange(row: EditableRow) {
    row._mngt = (row['cluster'] || '').toUpperCase() === 'MNGT';
    // clear pg when cluster changes
    row['pg'] = '';
    row['vlan'] = '';
    this.refreshRowDerived(row);
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
        this.rows = this.prepareRows(res.rows);
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

  trackByRowId(_: number, row: EditableRow): number {
    return row._id || 0;
  }

  private prepareRows(rows: NetworkRow[]): EditableRow[] {
    return rows.map((row) => this.prepareRow(row));
  }

  private prepareRow(row: Partial<NetworkRow> & { _id?: number }): EditableRow {
    const prepared: EditableRow = {
      datacenter: row.datacenter || '',
      cluster: row.cluster || '',
      pg: row['pg'] || '',
      vlan: row.vlan || '',
      subnet: row.subnet || '',
      gw: row.gw || '',
      vrf: row.vrf || '',
      _id: row._id || this.nextRowId++,
    };
    this.refreshRowDerived(prepared);
    return prepared;
  }

  private refreshRowDerived(row: EditableRow) {
    row._mngt = (row.cluster || '').toUpperCase() === 'MNGT';
    row._pgOptions = this.getPgOptions(row.datacenter || '', row.cluster || '');
  }

  private getPgOptions(dc: string, cluster: string): PgOption[] {
    return this.pgOptionsByDcCluster[this.pgKey(dc, cluster)] || [];
  }

  private pgKey(dc: string, cluster: string): string {
    return dc + '::' + cluster;
  }

  private updateInventoryOptionCaches(inv: VcInventory) {
    this.dcOptionsList = inv.datacenters || [];
    this.clusterOptionsByDc = {};
    this.pgOptionsByDcCluster = {};

    for (const dc of this.dcOptionsList) {
      const clusters = inv.clusters?.[dc] || [];
      this.clusterOptionsByDc[dc] = ['MNGT', ...clusters.filter(c => c !== 'MNGT')];

      for (const cluster of clusters) {
        const pgs = inv.portgroups?.[dc]?.[cluster] || [];
        this.pgOptionsByDcCluster[this.pgKey(dc, cluster)] = pgs.map(p => {
          const hasVlan = p.vlan && p.vlan !== '0';
          return {
            name: p.name,
            vlan: hasVlan ? p.vlan : '',
            label: hasVlan ? p.name + ' [' + p.vlan + ']' : p.name,
          };
        });
      }
    }
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

}
