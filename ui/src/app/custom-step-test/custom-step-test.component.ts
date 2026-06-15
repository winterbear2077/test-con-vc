import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ClarityModule } from '@clr/angular';
import { ApiService, CustomStepRuleRow } from '../api.service';

@Component({
  selector: 'app-custom-step-test',
  standalone: true,
  imports: [CommonModule, FormsModule, ClarityModule],
  templateUrl: './custom-step-test.component.html',
  styleUrl: './custom-step-test.component.scss',
})
export class CustomStepTestComponent implements OnInit {
  customRules: CustomStepRuleRow[] = [];
  customSaveMsg = '';
  private _nextCustomId = 1;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.api.getCustomStepRules().subscribe({
      next: ({ rules }) => {
        this.customRules = (rules || []).map(r => ({
          id: this._nextCustomId++,
          src_subnet: r.src_subnet || '',
          protocol: (r.protocol || 'tcp') as 'tcp' | 'udp' | 'icmp',
          dest: r.dest || '',
          port: Number(r.port || 80),
          comment: r.comment || '',
        }));
      },
      error: () => {},
    });
  }

  addCustomRule() {
    this.customRules.push({
      id: this._nextCustomId++,
      src_subnet: '',
      protocol: 'tcp',
      dest: '',
      port: 80,
      comment: '',
    });
  }

  removeCustomRule(id: number | undefined) {
    this.customRules = this.customRules.filter(r => r.id !== id);
  }

  clearCustomRules() {
    this.customRules = [];
  }

  saveCustomRules() {
    const payload: CustomStepRuleRow[] = this.customRules.map(r => ({
      src_subnet: (r.src_subnet || '').trim(),
      protocol: (r.protocol || 'tcp') as 'tcp' | 'udp' | 'icmp',
      dest: (r.dest || '').trim(),
      port: Number(r.port || 80),
      comment: (r.comment || '').trim(),
    }));
    this.api.saveCustomStepRules(payload).subscribe({
      next: () => { this.customSaveMsg = '✓ Saved'; setTimeout(() => this.customSaveMsg = '', 3000); },
      error: () => { this.customSaveMsg = '✗ Save failed'; },
    });
  }
}
