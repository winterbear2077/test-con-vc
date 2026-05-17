import { Injectable } from '@angular/core';
import { HttpClient, HttpHeaders, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { PluginContextService } from './plugin-context.service';

export interface AppConfig {
  vcenter_host?: string;
  vcenter_user?: string;
  vcenter_password?: string;
  ovf_path?: string;
  memboot_iso_path?: string;
  boot_method?: string;
  input?: string;
  resource_pool?: string;
  vm_prefix?: string;
  [key: string]: any;
}

export interface PluginStatus {
  registered: boolean;
  key: string;
  version?: string;
  url?: string;
}

export interface NetworkRow {
  datacenter?: string;
  cluster?: string;
  vlan?: string;
  subnet?: string;
  gw?: string;
  vrf?: string;
  [key: string]: any;
}

export interface PortGroup {
  name: string;
  vlan: string;
}

export interface VcInventory {
  datacenters?: string[];
  clusters?: Record<string, string[]>;
  portgroups?: Record<string, Record<string, PortGroup[]>>;
}

export interface RunRequest {
  execute_vcenter?: boolean;
  probe_mode?: string;
  max_retries?: number;
  cleanup_on_failure?: boolean;
  phased_testing?: boolean;
  vms_per_subnet?: number;
  max_vms_per_phase?: number;
  phases?: string;
  vrf_links?: string[];
  [key: string]: any;
}

export interface VrfRuleRow {
  id?: number;
  from_vrf: string;
  to_vrf: string;
  action: 'PASS' | 'FAIL';
  comment: string;
}

export interface HistoryEntry {
  run_id: string;
  status: string;
  probe_mode?: string;
  passed?: number;
  failed?: number;
  total?: number;
  [key: string]: any;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private base = '/api';
  /** Absolute base derived from <base href> so EventSource works inside vCenter iframes. */
  private absBase: string;

  constructor(private http: HttpClient, private plugin: PluginContextService) {
    // document.baseURI respects the <base href> tag (which web_app.py rewrites for plugin mode).
    // Strip trailing slash then append /api so EventSource uses the correct backend origin.
    const base = (document.baseURI || window.location.href).replace(/\/+$/, '');
    this.absBase = base + '/api';
  }

  /** Returns headers including X-Vcenter-Session when in plugin mode and session is available. */
  private sessionHeaders(): { headers?: HttpHeaders } {
    if (this.plugin.isPlugin && this.plugin.sessionId) {
      return { headers: new HttpHeaders({ 'X-Vcenter-Session': this.plugin.sessionId }) };
    }
    return {};
  }

  getConfig(): Observable<AppConfig> { return this.http.get<AppConfig>(this.base + '/config'); }
  saveConfig(c: AppConfig): Observable<any> { return this.http.put<any>(this.base + '/config', c); }

  uploadOvf(files: FileList): Observable<{ path: string }> {
    const fd = new FormData();
    for (let i = 0; i < files.length; i++) fd.append('files', files[i]);
    return this.http.post<{ path: string }>(this.base + '/upload/ovf', fd);
  }

  uploadIso(file: File): Observable<{ path: string }> {
    const fd = new FormData();
    fd.append('file', file);
    return this.http.post<{ path: string }>(this.base + '/upload/iso', fd);
  }

  uploadInput(file: File): Observable<{ path: string }> {
    const fd = new FormData();
    fd.append('file', file);
    return this.http.post<{ path: string }>(this.base + '/upload/input', fd);
  }

  previewInput(file: File): Observable<{ rows: NetworkRow[]; count: number; rejected: Array<{ line: number; reason: string; row: Record<string, string> }> }> {
    const fd = new FormData();
    fd.append('file', file);
    return this.http.post<{ rows: NetworkRow[]; count: number; rejected: Array<{ line: number; reason: string; row: Record<string, string> }> }>(this.base + '/upload/input/preview', fd);
  }

  getInput(): Observable<{ rows: NetworkRow[] }> { return this.http.get<{ rows: NetworkRow[] }>(this.base + '/input'); }
  saveInput(rows: NetworkRow[]): Observable<any> { return this.http.put<any>(this.base + '/input', { rows }); }

  getInventory(): Observable<VcInventory> { return this.http.get<VcInventory>(this.base + '/vcenter/inventory', this.sessionHeaders()); }

  getPluginStatus(key: string): Observable<PluginStatus> {
    return this.http.get<PluginStatus>(this.base + '/vcenter/plugin/status', { params: new HttpParams().set('key', key) });
  }

  getThumbprint(url: string): Observable<{ thumbprint: string }> {
    return this.http.get<{ thumbprint: string }>(this.base + '/vcenter/plugin/thumbprint', { params: new HttpParams().set('url', url) });
  }

  registerPlugin(req: { plugin_url: string; plugin_key: string; ssl_thumbprint: string }): Observable<any> {
    return this.http.post<any>(this.base + '/vcenter/plugin/register', req);
  }

  unregisterPlugin(key: string): Observable<any> {
    return this.http.post<any>(this.base + '/vcenter/plugin/unregister', { key });
  }

  startRun(req: RunRequest): Observable<{ run_id: string }> {
    return this.http.post<{ run_id: string }>(this.base + '/run', req, this.sessionHeaders());
  }

  /** Returns the absolute URL to use with EventSource (must be absolute to work in vCenter iframes). */
  streamRunUrl(runId: string): string {
    return this.absBase + '/run/' + runId + '/stream';
  }

  getRunResult(runId: string): Observable<any> {
    return this.http.get<any>(this.base + '/run/' + runId + '/result');
  }

  getHistory(): Observable<HistoryEntry[]> { return this.http.get<HistoryEntry[]>(this.base + '/history'); }
  deleteHistory(runId: string): Observable<{ ok: boolean }> { return this.http.delete<{ ok: boolean }>(this.base + '/history/' + runId); }

  cleanupRun(runId: string): Observable<{ cleaned: number; skipped: number; failed: number }> {
    return this.http.post<any>(this.base + '/run/' + runId + '/cleanup', {});
  }

  getVrfRules(): Observable<{ rules: VrfRuleRow[] }> {
    return this.http.get<{ rules: VrfRuleRow[] }>(this.base + '/vrf-rules');
  }

  saveVrfRules(rules: VrfRuleRow[]): Observable<any> {
    return this.http.put<any>(this.base + '/vrf-rules', { rules });
  }
}
