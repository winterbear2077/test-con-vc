import { Injectable } from '@angular/core';
import { HttpClient, HttpHeaders, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { tap } from 'rxjs/operators';
import { PluginContextService } from './plugin-context.service';

export interface AppConfig {
  vcenter_host?: string;
  vcenter_user?: string;
  vcenter_password?: string;
  ovf_path?: string;
  boot_method?: 'ovf' | 'memboot' | string;
  memboot_iso_path?: string;
  input?: string;
  resource_pool?: string;
  vm_prefix?: string;
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
}

export interface CustomStepRunRequest {
  rules: Array<{
    src_subnet: string;
    protocol: 'tcp' | 'udp' | 'icmp';
    dest: string;
    port?: number;
    comment?: string;
  }>;
  execute_vcenter?: boolean;
  probe_mode?: string;
}

export interface CustomStepRuleRow {
  id?: number;
  src_subnet: string;
  protocol: 'tcp' | 'udp' | 'icmp';
  dest: string;
  port: number;
  comment: string;
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
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private base = '/api';
  /** Absolute base derived from <base href> so EventSource works inside vCenter iframes. */
  private absBase: string;
  private sessionToken: string | null = null;

  constructor(private http: HttpClient, private plugin: PluginContextService) {
    // document.baseURI respects the <base href> tag (which web_app.py rewrites for plugin mode).
    // Strip trailing slash then append /api so EventSource uses the correct backend origin.
    const base = (document.baseURI || window.location.href).replace(/\/+$/, '');
    this.absBase = base + '/api';
    if (typeof window !== 'undefined') {
      this.sessionToken = window.sessionStorage.getItem('nettest_session_token');
    }
  }

  /** Returns headers including plugin session/host or standalone session token. */
  private sessionHeaders(): { headers?: HttpHeaders } {
    let headers = new HttpHeaders();
    if (this.plugin.isPlugin) {
      if (this.plugin.sessionId) {
        headers = headers.set('X-Vcenter-Session', this.plugin.sessionId);
      }
      if (this.plugin.vcenterHost) {
        headers = headers.set('X-Vcenter-Host', this.plugin.vcenterHost);
      }
    } else if (this.sessionToken) {
      headers = headers.set('X-Session-Token', this.sessionToken);
    }
    if (headers.keys().length > 0) {
      return { headers };
    }
    return {};
  }

  createSession(password: string): Observable<{ session_token: string }> {
    return this.http.post<{ session_token: string }>(this.base + '/session', { vcenter_password: password }).pipe(
      tap((r) => {
        this.sessionToken = r.session_token;
        if (typeof window !== 'undefined') {
          window.sessionStorage.setItem('nettest_session_token', r.session_token);
        }
      })
    );
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

  startCustomStepRun(req: CustomStepRunRequest): Observable<{ run_id: string }> {
    return this.http.post<{ run_id: string }>(this.base + '/run/custom-steps', req, this.sessionHeaders());
  }

  cancelRun(runId: string): Observable<{ ok: boolean; run_id?: string; detail?: string }> {
    return this.http.post<{ ok: boolean; run_id?: string; detail?: string }>(
      this.base + '/run/' + runId + '/cancel',
      {},
      this.sessionHeaders()
    );
  }

  hasAuth(): boolean {
    if (this.plugin.isPlugin) {
      return !!(this.plugin.sessionId || this.plugin.cloneTicket);
    }
    return !!this.sessionToken;
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
    return this.http.post<any>(
      this.base + '/run/' + runId + '/cleanup',
      {},
      this.sessionHeaders()
    );
  }

  getVrfRules(): Observable<{ rules: VrfRuleRow[] }> {
    return this.http.get<{ rules: VrfRuleRow[] }>(this.base + '/vrf-rules');
  }

  saveVrfRules(rules: VrfRuleRow[]): Observable<any> {
    return this.http.put<any>(this.base + '/vrf-rules', { rules });
  }

  getCustomStepRules(): Observable<{ rules: CustomStepRuleRow[] }> {
    return this.http.get<{ rules: CustomStepRuleRow[] }>(this.base + '/custom-step-rules');
  }

  saveCustomStepRules(rules: CustomStepRuleRow[]): Observable<any> {
    return this.http.put<any>(this.base + '/custom-step-rules', { rules });
  }
}
