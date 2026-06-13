import { Injectable, NgZone } from '@angular/core';
import { BehaviorSubject } from 'rxjs';
import { filter, take } from 'rxjs/operators';

@Injectable({ providedIn: 'root' })
export class PluginContextService {
  /** True when loaded inside the vSphere Client (iframe or ?plugin=1). */
  readonly isPlugin: boolean;

  /** vCenter session ID — set from URL param, SDK getSessionId(), or postMessage. */
  sessionId: string | null;

  /** One-time clone ticket from vsphereClient.auth.acquireCloneTicket().
   *  When set, the backend uses CloneSession(ticket) instead of the REST session path.
   *  This is the Broadcom-recommended "delegate session authority" mechanism. */
  cloneTicket: string | null = null;
  readonly cloneTicket$ = new BehaviorSubject<string | null>(null);

  /** vCenter host inferred from the referrer. */
  readonly vcenterHost: string | null;

  /** Emits each time sessionId is updated (useful for deferred auth). */
  readonly sessionId$ = new BehaviorSubject<string | null>(null);

  constructor(private zone: NgZone) {
    const params = new URLSearchParams(window.location.search);
    const inFrame = window.self !== window.top;
    this.isPlugin = inFrame || params.get('plugin') === '1';

    // URL-param session:
    //   vCenter 7.x substitutes {vmwareApiSessionId} directly into the manifest URI.
    //   vCenter 8.x injects it via the vsphereClient SDK (polled below).
    //   Both paths are supported so a single build works on all versions.
    //   Guard: if vCenter did NOT perform URL substitution the param value is the
    //   literal string "{vmwareApiSessionId}" — discard it so we don't forward
    //   the placeholder as an auth token.
    const RAW_PLACEHOLDER = '{vmwareApiSessionId}';
    const rawFromUrl =
      params.get('vmwareApiSessionId') ||
      params.get('sessionId') ||
      params.get('vmwSessionId') ||
      null;
    const fromUrl = (rawFromUrl === RAW_PLACEHOLDER) ? null : rawFromUrl;
    this.sessionId = fromUrl;
    if (fromUrl) this.sessionId$.next(fromUrl);

    // Derive the vCenter host — prefer document.referrer, fall back to the
    // ?vcHost=<host> param that the backend injects into the manifest URI from
    // the saved Config (handles cases where referrer is missing or empty).
    let host: string | null = null;
    try {
      if (document.referrer) host = new URL(document.referrer).hostname;
    } catch { host = null; }
    if (!host) host = params.get('vcHost') || null;
    this.vcenterHost = host;

    if (this.isPlugin) {
      if (this.vcenterHost) {
        // Per Broadcom vSphere Client SDK 8.0 docs:
        //   "How to Delegate Session Authority to the Plug-in Server"
        // window.vsphereClient is NOT injected automatically — the plugin page must
        // explicitly load the SDK JavaScript from the vCenter host first.
        // Only after the SDK script loads does vsphereClient become available.
        this._loadVsphereSdk(this.vcenterHost);
      } else {
        // No referrer available — optimistically poll in case SDK was pre-loaded
        this._pollVsphereClientSdk();
      }

      // 2. postMessage listener (fallback for SDK versions that push context proactively)
      window.addEventListener('message', (event: MessageEvent) => {
        const data = event.data;
        if (!data || typeof data !== 'object') return;
        const sid: string | undefined =
          data.sessionId ||
          data.vmwareApiSessionId ||
          data.vmwSessionId ||
          data.vmware_soap_session ||
          (data.type === 'pluginContext' && data.sessionId) ||
          (data.type === 'SESSION' && data.token) ||
          undefined;
        if (sid) {
          this.zone.run(() => {
            this.sessionId = sid;
            this.sessionId$.next(sid);
          });
        }
      });

      // 3. Initiate handshake — request context from parent frame
      try {
        window.parent.postMessage({ type: 'pluginReady' }, '*');
        window.parent.postMessage({ type: 'getPluginContext' }, '*');
        window.parent.postMessage({ type: 'vsphere-ui:getContext' }, '*');
      } catch { /* cross-origin parent may block */ }
    }
  }

  /**
   * Dynamically load the VMware vSphere Client SDK JavaScript from the vCenter host.
   *
   * Per Broadcom vSphere Client SDK 8.0 docs (How to Delegate Session Authority):
   *   The SDK must be loaded from: https://{vcenter}/ui/resources/sdk/js/plugin-api.min.js
   * Only after this script loads does window.vsphereClient become available.
   *
   * Tries the 8.x/9.x path first, then the 7.x path, then falls back to blind polling
   * in case the SDK was somehow loaded by another mechanism.
   */
  private _loadVsphereSdk(vcHost: string): void {
    const paths = [
      `https://${vcHost}/ui/resources/sdk/js/plugin-api.min.js`,     // 8.x / 9.x
      `https://${vcHost}/ui/resources/sdk/js/vsphere-plugin-api.js`, // 7.x fallback
    ];

    const tryLoad = (index: number): void => {
      if (index >= paths.length) {
        // All SDK paths failed (cert, 404, etc.) — poll anyway as last resort
        this._pollVsphereClientSdk();
        return;
      }
      const script = document.createElement('script');
      script.src = paths[index];
      script.onload = () => this._pollVsphereClientSdk(20, 100); // poll immediately after SDK loads
      script.onerror = () => tryLoad(index + 1);
      document.head.appendChild(script);
    };

    tryLoad(0);
  }

  /**
   *
   * Priority order per Broadcom vSphere Client SDK 8.0 docs
   * "Communication Paths for Authentication in the Remote Plug-in Server":
   *   Path A (REST session forwarding — what our backend uses):
   *     vsphereClient.auth.getSessionId()  → returns vmware-api-session-id
   *   Path B (bearer / SAML token — different backend handling needed):
   *     vsphereClient.auth.getToken()      → opaque bearer / SAML token
   *   Legacy / older SDK shapes:
   *     vsphereClient.getSessionId()
   *     vsphereClient.auth.sessionId       (direct property)
   *     vsphereClient.sessionId
   */
  private _pollVsphereClientSdk(attemptsLeft = 40, intervalMs = 150): void {
    const tryNow = async () => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const vc = (window as any).vsphereClient;
      if (vc) {
        try {
          // ── Broadcom "ticket clone" path (recommended for backend session delegation) ──
          // acquireCloneTicket() returns a one-time ticket the backend exchanges for a
          // full SOAP session via SessionManager.CloneSession(ticket).
          if (typeof vc.auth?.acquireCloneTicket === 'function') {
            const ticket: string = await vc.auth.acquireCloneTicket();
            if (ticket) {
              this.zone.run(() => {
                this.cloneTicket = ticket;
                this.cloneTicket$.next(ticket);
                // Also signal sessionId$ so waitForSession() resolves
                this.sessionId$.next(ticket);
              });
              return;
            }
          }

          // ── Path A fallback: REST vmware-api-session-id forwarding ────────────────
          let token: string | null = null;
          if      (typeof vc.auth?.getSessionId === 'function') token = await vc.auth.getSessionId();
          else if (typeof vc.getSessionId === 'function')       token = await vc.getSessionId();
          else if (typeof vc.auth?.getToken === 'function')     token = await vc.auth.getToken();
          else if (typeof vc.getToken === 'function')           token = await vc.getToken();
          else if (typeof vc.auth?.sessionId === 'string')      token = vc.auth.sessionId;
          else if (typeof vc.sessionId === 'string')            token = vc.sessionId;

          if (token) {
            this.zone.run(() => { this.sessionId = token!; this.sessionId$.next(token!); });
            return;
          }
        } catch { /* SDK call failed — keep polling */ }
      }
      if (attemptsLeft > 1) {
        setTimeout(() => this._pollVsphereClientSdk(attemptsLeft - 1, intervalMs), intervalMs);
      }
    };
    setTimeout(tryNow, intervalMs);
  }

  /** Wait up to `timeoutMs` for a clone ticket or session ID. */
  waitForSession(timeoutMs = 2000): Promise<string | null> {
    if (this.cloneTicket) return Promise.resolve(this.cloneTicket);
    if (this.sessionId)   return Promise.resolve(this.sessionId);
    return new Promise(resolve => {
      const timer = setTimeout(() => resolve(this.cloneTicket || this.sessionId), timeoutMs);
      this.sessionId$.pipe(filter(s => !!s), take(1)).subscribe(s => {
        clearTimeout(timer);
        resolve(this.cloneTicket || s);
      });
    });
  }
}
