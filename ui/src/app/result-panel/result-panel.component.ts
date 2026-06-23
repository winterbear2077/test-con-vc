import { Component, ElementRef, Input, OnChanges, SimpleChanges, ViewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ClarityModule } from '@clr/angular';
import { ClrTooltipModule } from '@clr/angular/popover/tooltip';
import html2canvas from 'html2canvas';

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

function endpointKey(subnet: string, cluster?: string): string {
  const c = String(cluster || '').trim();
  return `${String(subnet || '')}@@${c}`;
}

function endpointLabel(subnet: string, cluster?: string): string {
  const s = String(subnet || '').trim();
  const c = String(cluster || '').trim();
  return c ? `${s} (${c})` : s;
}

function findClusterBySubnetVrf(rows: any[], subnet: string, vrf: string): string {
  const s = String(subnet || '');
  const v = String(vrf || '');
  const hit = (rows || []).find((r: any) => String(r?.subnet || '') === s && String(r?.vrf || '') === v);
  return String(hit?.cluster || '');
}

@Component({
  selector: 'app-result-panel',
  standalone: true,
  imports: [CommonModule, ClarityModule, ClrTooltipModule],
  templateUrl: './result-panel.component.html',
  styleUrl: './result-panel.component.scss'
})
export class ResultPanelComponent implements OnChanges {
  @Input() result: any = null;
  @ViewChild('matrixExportArea') matrixExportArea?: ElementRef<HTMLElement>;
  processed: any = null;
  exporting = false;
  exportMsg = '';
  private vmByKey = new Map<string, any>();
  private endpointMetaByKey = new Map<string, { subnet: string; cluster: string }>();

  private readonly EXPORT_MAX_PIXELS = 16_000_000;
  private readonly EXPORT_MIN_SCALE = 1;
  private readonly EXPORT_MAX_SCALE = 2.2;

  ngOnChanges(changes: SimpleChanges) {
    if (changes['result']) {
      this.processed = this.result ? this.processResult(this.result) : null;
    }
  }

  processResult(result: any): any {
    if (!result) return null;
    const rawDetails: any[] = Array.isArray(result.Results) ? result.Results : (result.Results?.details || []);
    const parsedRows: any[] = [
      ...(result.ParsedInput?.vm_provisioned || []),
      ...(result.ParsedInput?.mngt_esxi_skipped || []),
    ];
    const details: any[] = rawDetails.map((d: any) => {
      const srcCluster = String(d?.src_cluster || '').trim() || findClusterBySubnetVrf(parsedRows, d?.src_subnet, d?.src_vrf);
      const dstCluster = String(d?.dst_cluster || '').trim() || findClusterBySubnetVrf(parsedRows, d?.dst_subnet, d?.dst_vrf);
      return {
        ...d,
        src_cluster: srcCluster,
        dst_cluster: dstCluster,
      };
    });
    const createdVms: any[] = Array.isArray(result.Execution?.created_vms) ? result.Execution.created_vms : [];
    this.vmByKey = new Map(
      createdVms
        .filter((vm: any) => vm && vm.subnet)
        .map((vm: any) => [endpointKey(vm.subnet, vm.cluster) + `|${Number(vm.vm_index ?? 0)}`, vm])
    );
    const subnetVrf: Record<string, string> = {};
    const subnetVlan: Record<string, string> = {};
    [...(result.ParsedInput?.vm_provisioned || []), ...(result.ParsedInput?.mngt_esxi_skipped || [])].forEach((r: any) => {
      if (r.subnet) {
        const key = endpointKey(r.subnet, r.cluster);
        this.endpointMetaByKey.set(key, { subnet: String(r.subnet || ''), cluster: String(r.cluster || '') });
        subnetVrf[key] = r.vrf || '';
        subnetVlan[key] = r.vlan || '';
      }
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
    // Build axis order from sorted vm_provisioned rows (mirrors backend sort key:
    // subnet, cluster, datacenter, vlan, gw, vrf) so that for any combinations(i,j) pair
    // the src axis index is always < dst axis index.
    const sortedVmRows = [...(result.ParsedInput?.vm_provisioned || [])].sort((a: any, b: any) => {
      const cmp = (x: any, y: any) => String(x || '').localeCompare(String(y || ''));
      return cmp(a.subnet, b.subnet)
          || cmp(a.cluster, b.cluster)
          || cmp(a.datacenter, b.datacenter)
          || cmp(a.vlan, b.vlan)
          || cmp(a.gw, b.gw)
          || cmp(a.vrf, b.vrf);
    });
    const resultEndpointKeys = new Set<string>();
    details.forEach((d: any) => {
      resultEndpointKeys.add(endpointKey(d.src_subnet, d.src_cluster));
      resultEndpointKeys.add(endpointKey(d.dst_subnet, d.dst_cluster));
    });

    const globalAxisKeys: string[] = [];
    const globalAxisSeen = new Set<string>();
    sortedVmRows.forEach((r: any) => {
      const k = endpointKey(r.subnet, r.cluster);
      // Render only endpoints participating in this run's result set.
      if (resultEndpointKeys.size > 0 && !resultEndpointKeys.has(k)) return;
      if (!globalAxisSeen.has(k)) { globalAxisKeys.push(k); globalAxisSeen.add(k); }
      if (!this.endpointMetaByKey.has(k)) {
        this.endpointMetaByKey.set(k, { subnet: String(r.subnet || ''), cluster: String(r.cluster || '') });
      }
    });

    // Safety net: include result endpoints that may be absent from ParsedInput.
    resultEndpointKeys.forEach((k) => {
      if (globalAxisSeen.has(k)) return;
      globalAxisKeys.push(k);
      globalAxisSeen.add(k);
      if (!this.endpointMetaByKey.has(k)) {
        const p = k.split('@@');
        this.endpointMetaByKey.set(k, { subnet: p[0] || '', cluster: p[1] || '' });
      }
    });

    const matrices = phaseOrder.filter(ph => (detailsByPhase[ph] || []).length > 0).map(ph => {
      const pds = detailsByPhase[ph];
      const meta = phaseMeta(ph);
      const pairKeys = new Set<string>();
      // Ensure any endpoint seen in results but not in ParsedInput is still registered
      pds.forEach((d: any) => {
        const srcK = endpointKey(d.src_subnet, d.src_cluster);
        const dstK = endpointKey(d.dst_subnet, d.dst_cluster);
        pairKeys.add(`${srcK}=>${dstK}`);
        if (!this.endpointMetaByKey.has(srcK)) {
          const p = srcK.split('@@');
          this.endpointMetaByKey.set(srcK, { subnet: p[0] || '', cluster: p[1] || '' });
        }
        if (!this.endpointMetaByKey.has(dstK)) {
          const p = dstK.split('@@');
          this.endpointMetaByKey.set(dstK, { subnet: p[0] || '', cluster: p[1] || '' });
        }
      });

      const rowSubnets = globalAxisKeys.filter((src, srcIdx) =>
        globalAxisKeys.some((dst, dstIdx) => dstIdx > srcIdx && pairKeys.has(`${src}=>${dst}`))
      );

      return {
        ph, pds,
        rowSubnets,
        srcSubnets: globalAxisKeys,
        dstSubnets: globalAxisKeys,
        phaseLabel: meta.label,
        phaseColor: meta.color,
      };
    });
    const plannedVms = Array.isArray(result.Execution?.planned_vms) ? result.Execution.planned_vms : [];
    return {
      ...result, details, subnetVrf, subnetVlan, matrices, plannedVms,
      total: details.length,
      passed: details.filter((d: any) => d.status === 'pass').length,
      failed: details.filter((d: any) => d.status !== 'pass').length,
    };
  }

  endpointText(key: string): string {
    const meta = this.endpointMetaByKey.get(key);
    if (!meta) return key;
    return endpointLabel(meta.subnet, meta.cluster);
  }

  endpointVlan(key: string): string {
    return this.processed?.subnetVlan?.[key] || '';
  }

  endpointVrf(key: string): string {
    return this.processed?.subnetVrf?.[key] || '';
  }

  cellsFor(matrix: any, src: string, dst: string): any[] {
    const srcMeta = this.endpointMetaByKey.get(src);
    const dstMeta = this.endpointMetaByKey.get(dst);
    if (!srcMeta || !dstMeta) return [];
    const srcIdx = matrix?.srcSubnets?.indexOf(src) ?? -1;
    const dstIdx = matrix?.dstSubnets?.indexOf(dst) ?? -1;
    if (srcIdx < 0 || dstIdx < 0 || srcIdx >= dstIdx) return [];

    return matrix.pds.filter((d: any) =>
      d.src_subnet === srcMeta.subnet
      && String(d.src_cluster || '') === srcMeta.cluster
      && d.dst_subnet === dstMeta.subnet
      && String(d.dst_cluster || '') === dstMeta.cluster
    );
  }

  isUpperTriangleCell(matrix: any, src: string, dst: string): boolean {
    const srcIdx = matrix?.srcSubnets?.indexOf(src) ?? -1;
    const dstIdx = matrix?.dstSubnets?.indexOf(dst) ?? -1;
    return srcIdx >= 0 && dstIdx >= 0 && srcIdx < dstIdx;
  }

  cellTitle(cell: any): string {
    const key = endpointKey(cell.src_subnet, cell.src_cluster) + `|${Number(cell.src_vm_index ?? 0)}`;
    const vm = this.vmByKey.get(key);
    const host = vm?.host_name || 'unknown';
    const ip = vm?.ip_address || 'unknown';
    const vmLabel = vm?.vm_name || `vm-${Number(cell.src_vm_index ?? 0)}`;
    return [
      `src subnet: ${cell.src_subnet}`,
      `src vm: ${vmLabel}`,
      `src host: ${host}`,
      `src ip: ${ip}`,
      `dst subnet: ${cell.dst_subnet}`,
      `expected: ${cell.expected}`,
      `actual: ${cell.actual}`,
      cell.reason ? `reason: ${cell.reason}` : '',
    ].filter(Boolean).join('\n');
  }

  cellTooltipLines(cell: any): Array<{ label: string; value: string }> {
    const key = endpointKey(cell.src_subnet, cell.src_cluster) + `|${Number(cell.src_vm_index ?? 0)}`;
    const vm = this.vmByKey.get(key);
    return [
      { label: 'src subnet', value: String(cell.src_subnet || '') },
      { label: 'src vm', value: String(vm?.vm_name || `vm-${Number(cell.src_vm_index ?? 0)}`) },
      { label: 'src host', value: String(vm?.host_name || 'unknown') },
      { label: 'src ip', value: String(vm?.ip_address || 'unknown') },
      { label: 'dst subnet', value: String(cell.dst_subnet || '') },
      { label: 'expected', value: String(cell.expected || '') },
      { label: 'actual', value: String(cell.actual || '') },
      { label: 'reason', value: String(cell.reason || '') },
    ].filter((item) => item.value !== '');
  }

  cellClass(d: any): string {
    const { expected, actual } = d;
    if (expected === 'OBSERVE') {
      if (actual === 'PASS') return 'c-pp';
      if (actual === 'FAIL') return 'c-pf';
      return 'c-uk';
    }
    if (actual === 'UNKNOWN') return 'c-uk';
    if (expected === 'PASS' && actual === 'PASS') return 'c-pp';
    if (expected === 'FAIL' && actual === 'FAIL') return 'c-ff';
    if (expected === 'PASS' && actual === 'FAIL') return 'c-pf';
    return 'c-fp';
  }

  cellIcon(d: any): string {
    const { expected, actual } = d;
    if (expected === 'OBSERVE') {
      if (actual === 'PASS') return '\u2713';
      if (actual === 'FAIL') return '\u2717';
      return '?';
    }
    if (actual === 'UNKNOWN') return '?';
    if (expected === 'PASS' && actual === 'PASS') return '\u2713';
    if (expected === 'FAIL' && actual === 'FAIL') return '\uD83D\uDD12';
    if (expected === 'PASS' && actual === 'FAIL') return '\u2717';
    return '\u26A0';
  }

  phaseName(ph: string): string { return phaseMeta(ph).label; }
  phaseColor(ph: string): string { return phaseMeta(ph).color; }

  async exportMatrixHtml(): Promise<void> {
    const host = this.matrixExportArea?.nativeElement;
    if (!host) {
      this.exportMsg = 'Matrix area not ready yet';
      return;
    }
    if (!this.processed?.matrices?.length) {
      this.exportMsg = 'No matrix to export';
      return;
    }

    this.exporting = true;
    this.exportMsg = '';
    try {
      const documentText = this.buildExportHtmlDocument(host.outerHTML);
      const blob = new Blob([documentText], { type: 'text/html;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = this.exportHtmlFileName();
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      this.exportMsg = 'Exported HTML';
    } catch {
      this.exportMsg = 'Export failed';
    } finally {
      this.exporting = false;
    }
  }

  async exportMatrixPng(): Promise<void> {
    const host = this.matrixExportArea?.nativeElement;
    if (!host) {
      this.exportMsg = 'Matrix area not ready yet';
      return;
    }
    if (!this.processed?.matrices?.length) {
      this.exportMsg = 'No matrix to export';
      return;
    }

    this.exporting = true;
    this.exportMsg = '';
    const wrapEls = Array.from(host.querySelectorAll<HTMLElement>('.matrix-wrap'));
    const wrapStyleBackup = wrapEls.map((el) => ({
      el,
      overflow: el.style.overflow,
      maxWidth: el.style.maxWidth,
      maxHeight: el.style.maxHeight,
      width: el.style.width,
      height: el.style.height,
    }));

    const hostStyleBackup = {
      width: host.style.width,
      maxWidth: host.style.maxWidth,
      overflow: host.style.overflow,
    };

    try {
      // Export the full matrix content instead of only the visible scroll area.
      host.classList.add('exporting-image');
      for (const w of wrapEls) {
        w.style.overflow = 'visible';
        w.style.maxWidth = 'none';
        w.style.maxHeight = 'none';
        w.style.width = `${w.scrollWidth}px`;
        w.style.height = `${w.scrollHeight}px`;
      }
      host.style.overflow = 'visible';
      host.style.maxWidth = 'none';
      host.style.width = `${Math.max(host.scrollWidth, host.clientWidth)}px`;

      await new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      });

      const captureWidth = Math.max(host.scrollWidth, host.clientWidth, 1);
      const captureHeight = Math.max(host.scrollHeight, host.clientHeight, 1);
      const scale = this.computeExportScale(captureWidth, captureHeight);

      const canvas = await html2canvas(host, {
        backgroundColor: '#ffffff',
        scale,
        useCORS: true,
        logging: false,
        width: captureWidth,
        height: captureHeight,
        windowWidth: Math.max(window.innerWidth, captureWidth),
        windowHeight: Math.max(window.innerHeight, captureHeight),
        scrollX: 0,
        scrollY: 0,
      });

      const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'));
      if (!blob) {
        throw new Error('toBlob returned null');
      }

      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = this.exportFileName();
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      this.exportMsg = `Exported PNG (${Math.round(scale * 100)}%)`;
    } catch {
      this.exportMsg = 'Export failed';
    } finally {
      host.classList.remove('exporting-image');
      for (const item of wrapStyleBackup) {
        item.el.style.overflow = item.overflow;
        item.el.style.maxWidth = item.maxWidth;
        item.el.style.maxHeight = item.maxHeight;
        item.el.style.width = item.width;
        item.el.style.height = item.height;
      }
      host.style.width = hostStyleBackup.width;
      host.style.maxWidth = hostStyleBackup.maxWidth;
      host.style.overflow = hostStyleBackup.overflow;
      this.exporting = false;
    }
  }

  private computeExportScale(width: number, height: number): number {
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const preferred = Math.min(this.EXPORT_MAX_SCALE, dpr + 0.5);
    const area = Math.max(1, width * height);
    const limitByPixels = Math.sqrt(this.EXPORT_MAX_PIXELS / area);
    return Math.max(this.EXPORT_MIN_SCALE, Math.min(preferred, limitByPixels));
  }

  private buildExportHtmlDocument(contentHtml: string): string {
    const title = this.exportDocumentTitle();
    return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${this.escapeHtml(title)}</title>
  <style>
${this.exportStyles()}
  </style>
</head>
<body>
  <div class="export-shell">
    <div class="export-header">
      <div class="export-title">${this.escapeHtml(title)}</div>
      <div class="export-meta">
        <span class="export-chip ${this.processed?.FinalStatus === 'PASS' ? 'ok' : 'bad'}">${this.escapeHtml(String(this.processed?.FinalStatus || 'UNKNOWN'))}</span>
        <span class="export-chip">${this.escapeHtml(String(this.processed?.passed || 0))} passed</span>
        <span class="export-chip">${this.escapeHtml(String(this.processed?.failed || 0))} failed</span>
      </div>
    </div>
    ${contentHtml}
  </div>
</body>
</html>`;
  }

  private exportStyles(): string {
    return `
      :root { color-scheme: light; }
      html, body {
        margin: 0;
        padding: 0;
        background: #f4f7fb;
        color: #21333b;
        font-family: Arial, Helvetica, sans-serif;
      }
      body { padding: 20px; }
      .export-shell {
        background: #fff;
        border: 1px solid #d8e1ea;
        border-radius: 12px;
        box-shadow: 0 10px 30px rgba(17, 24, 39, 0.08);
        padding: 18px 18px 22px;
      }
      .export-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        margin-bottom: 14px;
        flex-wrap: wrap;
      }
      .export-title { font-size: 18px; font-weight: 700; }
      .export-meta { display: flex; gap: 8px; flex-wrap: wrap; }
      .export-chip {
        display: inline-flex;
        align-items: center;
        padding: 4px 10px;
        border-radius: 999px;
        background: #edf2f7;
        color: #334155;
        font-size: 12px;
        font-weight: 600;
      }
      .export-chip.ok { background: #dcfce7; color: #14532d; }
      .export-chip.bad { background: #fee2e2; color: #7f1d1d; }
      .matrix-export-area { background: #fff; }
      /* Static export has no Clarity runtime; hide inlined tooltip panels. */
      clr-tooltip-content,
      .cell-tooltip-content,
      .cell-tooltip {
        display: none !important;
      }
      .matrix-wrap { overflow: auto; max-width: 100%; max-height: 70vh; }
      .matrix-table {
        font-size: .75rem;
        border-collapse: separate;
        border-spacing: 2px;
      }
      .matrix-table th {
        font-size: .7rem;
        font-weight: 600;
        background: #f5f5f5;
        padding: .3rem .5rem;
        white-space: nowrap;
        position: sticky;
        top: 0;
        z-index: 2;
      }
      .matrix-table td {
        padding: .3rem .45rem;
        text-align: center;
        border-radius: 3px;
        white-space: nowrap;
      }
      .matrix-table td:first-child {
        position: sticky;
        left: 0;
        z-index: 1;
        background: #f5f5f5;
        font-weight: 600;
        text-align: left;
      }
      .matrix-table thead th:first-child {
        position: sticky;
        left: 0;
        top: 0;
        z-index: 3;
      }
      .c-pp  { background: #dcfce7; color: #14532d; }
      .c-ff  { background: #f1f5f9; color: #475569; }
      .c-pf  { background: #fee2e2; color: #7f1d1d; }
      .c-fp  { background: #fef9c3; color: #713f12; }
      .c-uk  { background: #fef3c7; color: #78350f; }
      .c-self{ background: #e2e8f0; color: #94a3b8; }
      .vrf-badge {
        display: inline-block;
        font-size: .62rem;
        font-weight: 700;
        padding: 1px 5px;
        border-radius: 3px;
        background: #dbeafe;
        color: #1e40af;
        vertical-align: middle;
        margin-left: 3px;
      }
      .label { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; font-weight: 600; }
      .label-success { background: #dcfce7; color: #14532d; }
      .label-danger { background: #fee2e2; color: #7f1d1d; }
      .label-info { background: #dbeafe; color: #1e40af; }
      .label-warning { background: #fef3c7; color: #78350f; }
      .label-light-blue { background: #e0f2fe; color: #075985; }
      .label-blue { background: #dbeafe; color: #1d4ed8; }
      .card {
        border: 1px solid #e5e7eb;
        border-radius: 10px;
        background: #fff;
      }
      .card-header {
        background: #f8fafc;
        border-bottom: 1px solid #e5e7eb;
        padding: 8px 10px;
        border-top-left-radius: 10px;
        border-top-right-radius: 10px;
      }
      .card-block { padding: 10px; }
      .p6 { font-size: 12px; }
      .text-muted { color: #64748b; }
      .text-success { color: #15803d; }
      .text-danger { color: #b91c1c; }
      .section-header {
        font-size: 1.05rem;
        font-weight: 700;
        margin-bottom: 10px;
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .matrix-export-area > div { margin-bottom: 16px; }
      @media print {
        body { background: #fff; padding: 0; }
        .export-shell { box-shadow: none; border: none; border-radius: 0; }
      }
    `;
  }

  private exportDocumentTitle(): string {
    return `Result Matrix ${String(this.processed?.Execution?.run_id || this.processed?.RunId || '').trim() || ''}`.trim();
  }

  private exportHtmlFileName(): string {
    const runId = String(this.processed?.Execution?.run_id || this.processed?.RunId || '').trim();
    const now = new Date();
    const stamp = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}-${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}${String(now.getSeconds()).padStart(2, '0')}`;
    const suffix = runId ? `-${runId}` : `-${stamp}`;
    return `result-matrix${suffix}.html`;
  }

  private escapeHtml(value: string): string {
    return value
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  private exportFileName(): string {
    const runId = String(this.processed?.Execution?.run_id || this.processed?.RunId || '').trim();
    const now = new Date();
    const stamp = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}-${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}${String(now.getSeconds()).padStart(2, '0')}`;
    const suffix = runId ? `-${runId}` : `-${stamp}`;
    return `result-matrix${suffix}.png`;
  }
}
