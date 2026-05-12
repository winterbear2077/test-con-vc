import { Injectable } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class PluginContextService {
  /** True when loaded inside the vSphere Client (iframe or ?plugin=1). */
  readonly isPlugin: boolean;

  /** vCenter session ID passed via URL param ?sessionId=<token>. */
  readonly sessionId: string | null;

  /** vCenter host inferred from the parent page URL (parent origin). */
  readonly vcenterHost: string | null;

  constructor() {
    const params = new URLSearchParams(window.location.search);
    const inFrame = window.self !== window.top;
    this.isPlugin = inFrame || params.get('plugin') === '1';
    this.sessionId = params.get('sessionId');

    // Try to derive the vCenter host from the referrer or parent origin
    let host: string | null = null;
    try {
      if (document.referrer) {
        host = new URL(document.referrer).hostname;
      }
    } catch {
      host = null;
    }
    this.vcenterHost = host;
  }
}
