import { Component, OnInit, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ClarityModule } from '@clr/angular';
import { ApiService, AppConfig, PluginStatus } from '../api.service';
import { PluginContextService } from '../plugin-context.service';

@Component({
  selector: 'app-config',
  standalone: true,
  imports: [CommonModule, FormsModule, ClarityModule],
  templateUrl: './config.component.html',
  styleUrl: './config.component.scss'
})
export class ConfigComponent implements OnInit {
  @Output() navigate = new EventEmitter<string>();

  cfg: AppConfig = {};
  saveMsg = '';

  pluginUrl = '';
  pluginKey = 'com.nettest.vcnet';
  pluginThumbprint = '';
  pluginStatus: PluginStatus | null = null;
  pluginMsg = '';
  pluginLoading = false;
  thumbprintLoading = false;

  constructor(private api: ApiService, public plugin: PluginContextService) {}

  ngOnInit() {
    this.api.getConfig().subscribe({
      next: c => {
        this.cfg = c;
        if (!this.cfg.boot_method) this.cfg.boot_method = 'ovf';
        if (!this.pluginUrl && typeof window !== 'undefined') {
          const loc = window.location;
          if (loc.protocol === 'https:') {
            this.pluginUrl = loc.protocol + '//' + loc.hostname + ':' + loc.port;
            setTimeout(() => this.fetchThumbprint(), 800);
          }
        }
      },
      error: err => console.error('getConfig', err)
    });
    this.checkPluginStatus();
  }

  save() {
    this.api.saveConfig(this.cfg).subscribe({
      next: () => { this.saveMsg = '\u2713 Saved'; setTimeout(() => this.saveMsg = '', 3000); },
      error: err => { this.saveMsg = '\u2717 ' + (err.error?.detail || err.message); }
    });
  }

  onOvfFiles(event: Event) {
    const files = (event.target as HTMLInputElement).files;
    if (!files || !files.length) return;
    this.api.uploadOvf(files).subscribe({
      next: r => { this.cfg.ovf_path = r.path; this.saveMsg = '\u2713 Uploaded ' + files.length + ' file(s)'; },
      error: err => { this.saveMsg = '\u2717 Upload failed: ' + (err.error?.detail || err.message); }
    });
  }

  onIsoFile(event: Event) {
    const files = (event.target as HTMLInputElement).files;
    if (!files || !files.length) return;
    this.api.uploadIso(files[0]).subscribe({
      next: r => { this.cfg.memboot_iso_path = r.path; this.saveMsg = '\u2713 Uploaded ' + files[0].name; },
      error: err => { this.saveMsg = '\u2717 Upload failed: ' + (err.error?.detail || err.message); }
    });
  }

  fetchThumbprint() {
    if (!this.pluginUrl || !this.pluginUrl.startsWith('https://')) return;
    this.thumbprintLoading = true;
    this.api.getThumbprint(this.pluginUrl).subscribe({
      next: r => { this.pluginThumbprint = r.thumbprint; this.thumbprintLoading = false; },
      error: err => { this.pluginMsg = 'Cannot fetch thumbprint: ' + (err.error?.detail || err.message); this.thumbprintLoading = false; }
    });
  }

  checkPluginStatus() {
    this.api.getPluginStatus(this.pluginKey).subscribe({
      next: s => this.pluginStatus = s,
      error: () => this.pluginStatus = null
    });
  }

  registerPlugin() {
    this.pluginLoading = true; this.pluginMsg = '';
    this.api.registerPlugin({ plugin_url: this.pluginUrl, plugin_key: this.pluginKey, ssl_thumbprint: this.pluginThumbprint }).subscribe({
      next: r => {
        this.pluginMsg = '\u2713 ' + r.action + ': ' + r.key;
        this.pluginThumbprint = r.thumbprint || this.pluginThumbprint;
        this.pluginLoading = false;
        this.checkPluginStatus();
      },
      error: err => { this.pluginMsg = '\u2717 ' + (err.error?.detail || err.message); this.pluginLoading = false; }
    });
  }

  unregisterPlugin() {
    if (!confirm('Unregister plugin "' + this.pluginKey + '" from vCenter?')) return;
    this.pluginLoading = true;
    this.api.unregisterPlugin(this.pluginKey).subscribe({
      next: r => { this.pluginMsg = '\u2713 ' + r.action + ': ' + r.key; this.pluginLoading = false; this.checkPluginStatus(); },
      error: err => { this.pluginMsg = '\u2717 ' + (err.error?.detail || err.message); this.pluginLoading = false; }
    });
  }
}
