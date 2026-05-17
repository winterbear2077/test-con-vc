import { Injectable } from '@angular/core';
import { HttpClient, HttpHeaders, HttpParams } from '@angular/common/http';
import { Observable, map, switchMap, tap } from 'rxjs';
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
  cleaned?: number;
  vms_cleaned?: number;
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

  /** True when vCenter credentials are available (plugin session OR saved session token). */
  hasAuth(): boolean {
    return (this.plugin.isPlugin && !!this.plugin.sessionId) ||
           !!sessionStorage.getItem('vcenter_session_token');
  }

  /**
   * Returns auth headers for vCenter API calls:
   * - X-Vcenter-Session + X-Vcenter-Host in plugin mode
   * - X-Session-Token (opaque, not the password) in standalone mode
   */
  private authHeaders(): { headers?: HttpHeaders } {
    const h: Record<string, string> = {};
    if (this.plugin.isPlugin && this.plugin.sessionId) {
      h['X-Vcenter-Session'] = this.plugin.sessionId;
      if (this.plugin.vcenterHost) h['X-Vcenter-Host'] = this.plugin.vcenterHost;
    }
    const token = sessionStorage.getItem('vcenter_session_token');
    if (token) h['X-Session-Token'] = token;
    return Object.keys(h).length ? { headers: new HttpHeaders(h) } : {};
  }

  /** Fetch config from server (password is never returned; enter it fresh each session). */
  getConfig(): Observable<AppConfig> {
    return this.http.get<AppConfig>(this.base + '/config');
  }

  /**
   * Save config. If a password is provided it is exchanged for a server-side session
   * token stored in sessionStorage — the password itself is never sent beyond this call.
   */
  saveConfig(c: AppConfig): Observable<any> {
    const password = c.vcenter_password || '';
    const safe: AppConfig = { ...c };
    delete safe['vcenter_password'];
    const save$ = this.http.put<any>(this.base + '/config', safe);
    if (!password) return save$;
    return save$.pipe(
      switchMap(() =>
        this.http.post<{ session_token: string }>(this.base + '/session', { vcenter_password: password })
      ),
      tap(r => {
        const old = sessionStorage.getItem('vcenter_session_token');
        if (old) this.http.delete(this.base + '/session/' + old).subscribe();
        sessionStorage.setItem('vcenter_session_token', r.session_token);
      }),
      map(() => ({ ok: true }))
    );
  }

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

  getInventory(): Observable<VcInventory> { return this.http.get<VcInventory>(this.base + '/vcenter/inventory', this.authHeaders()); }

  getPluginStatus(key: string): Observable<PluginStatus> {
    return this.http.get<PluginStatus>(this.base + '/vcenter/plugin/status', { params: new HttpParams().set('key', key) });
  }

  getThumbprint(url: string): Observable<{ thumbprint: string }> {
    return this.http.get<{ thumbprint: string }>(this.base + '/vcenter/plugin/thumbprint', { params: new HttpParams().set('url', url) });
  }

  registerPlugin(req: { plugin_url: string; plugin_key: string; ssl_thumbprint: string }): Observable<any> {
    return this.http.post<any>(this.base + '/vcenter/plugin/register', req, this.authHeaders());
  }

  unregisterPlugin(key: string): Observable<any> {
    return this.http.post<any>(this.base + '/vcenter/plugin/unregister', { key }, this.authHeaders());
  }

  startRun(req: RunRequest): Observable<{ run_id: string }> {
    return this.http.post<{ run_id: string }>(this.base + '/run', req, this.authHeaders());
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
    return this.http.post<any>(this.base + '/run/' + runId + '/cleanup', {}, this.authHeaders());
  }

  getVrfRules(): Observable<{ rules: VrfRuleRow[] }> {
    return this.http.get<{ rules: VrfRuleRow[] }>(this.base + '/vrf-rules');
  }

  saveVrfRules(rules: VrfRuleRow[]): Observable<any> {
    return this.http.put<any>(this.base + '/vrf-rules', { rules });
  }
}
