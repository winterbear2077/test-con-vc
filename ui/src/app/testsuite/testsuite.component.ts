import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ClarityModule } from '@clr/angular';
import { ApiService, RunTestcase, TestSuite, TestSuiteCaseRule } from '../api.service';

@Component({
  selector: 'app-testsuite',
  standalone: true,
  imports: [CommonModule, FormsModule, ClarityModule],
  templateUrl: './testsuite.component.html',
  styleUrl: './testsuite.component.scss',
})
export class TestsuiteComponent implements OnInit {
  suites: TestSuite[] = [];
  testcases: RunTestcase[] = [];
  filteredTestcases: RunTestcase[] = [];
  phaseOptions: string[] = [];

  editingName = '';
  selectedCaseIds = new Set<string>();
  caseActions: Record<string, 'ALLOW' | 'DENY'> = {};
  saveMsg = '';
  filterText = '';
  filterPhase = 'all';
  filterAction = 'all';
  onlySelected = false;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.refresh();
  }

  refresh() {
    this.api.getRunCatalog().subscribe({
      next: (catalog) => {
        this.testcases = catalog.testcases || [];
        this.phaseOptions = [...new Set(this.testcases.map(tc => String(tc.suite || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
        this.applyFilters();
      },
      error: () => {
        this.testcases = [];
        this.filteredTestcases = [];
        this.phaseOptions = [];
      },
    });

    this.api.getTestSuites().subscribe({
      next: ({ suites }) => {
        this.suites = suites || [];
      },
      error: () => {
        this.suites = [];
      },
    });
  }

  newSuite() {
    this.editingName = '';
    this.selectedCaseIds.clear();
    this.caseActions = {};
    this.saveMsg = '';
    this.applyFilters();
  }

  editSuite(s: TestSuite) {
    this.editingName = s.name;
    const rules = (s.testcase_rules || []).filter(r => !!r?.testcase_key);
    if (rules.length) {
      this.selectedCaseIds = new Set(rules.map(r => r.testcase_key));
      this.caseActions = {};
      rules.forEach(r => {
        this.caseActions[r.testcase_key] = r.action === 'DENY' ? 'DENY' : 'ALLOW';
      });
    } else {
      const keys = (s.testcase_keys || []).filter(Boolean);
      this.selectedCaseIds = new Set(keys);
      this.caseActions = {};
      keys.forEach(k => this.caseActions[k] = 'ALLOW');
    }
    this.saveMsg = '';
    this.applyFilters();
  }

  applyFilters() {
    const text = (this.filterText || '').trim().toLowerCase();
    this.filteredTestcases = this.testcases.filter((tc) => {
      if (this.onlySelected && !this.selectedCaseIds.has(tc.id)) return false;
      if (this.filterPhase !== 'all' && String(tc.suite || '') !== this.filterPhase) return false;
      if (this.filterAction !== 'all') {
        const expected = String(tc.expected || '').toUpperCase();
        if (this.filterAction === 'ALLOW' && expected === 'FAIL') return false;
        if (this.filterAction === 'DENY' && expected !== 'FAIL') return false;
      }
      if (!text) return true;
      const hay = [
        tc.id,
        tc.label,
        tc.src_subnet,
        tc.dst_subnet,
        tc.expected,
        tc.reason,
        tc.suite,
      ].map((v) => String(v || '').toLowerCase()).join(' ');
      return hay.includes(text);
    });
  }

  isCaseSelected(caseId: string): boolean {
    return this.selectedCaseIds.has(caseId);
  }

  toggleCase(caseId: string, checked: boolean) {
    if (checked) {
      this.selectedCaseIds.add(caseId);
      if (!this.caseActions[caseId]) this.caseActions[caseId] = 'ALLOW';
    } else {
      this.selectedCaseIds.delete(caseId);
      delete this.caseActions[caseId];
    }
    if (this.onlySelected) this.applyFilters();
  }

  actionFor(caseId: string): 'ALLOW' | 'DENY' {
    return this.caseActions[caseId] === 'DENY' ? 'DENY' : 'ALLOW';
  }

  setAction(caseId: string, action: 'ALLOW' | 'DENY') {
    if (!this.selectedCaseIds.has(caseId)) return;
    this.caseActions[caseId] = action === 'DENY' ? 'DENY' : 'ALLOW';
  }

  selectAllCases() {
    this.filteredTestcases.forEach(tc => {
      this.selectedCaseIds.add(tc.id);
      if (!this.caseActions[tc.id]) this.caseActions[tc.id] = 'ALLOW';
    });
    if (this.onlySelected) this.applyFilters();
  }

  clearFilteredCases() {
    this.filteredTestcases.forEach(tc => {
      this.selectedCaseIds.delete(tc.id);
      delete this.caseActions[tc.id];
    });
    if (this.onlySelected) this.applyFilters();
  }

  clearCases() {
    this.selectedCaseIds.clear();
    this.caseActions = {};
    if (this.onlySelected) this.applyFilters();
  }

  saveSuite() {
    const name = (this.editingName || '').trim();
    if (!name) {
      this.saveMsg = 'Suite name is required';
      return;
    }
    if (!this.selectedCaseIds.size) {
      this.saveMsg = 'Pick at least one testcase';
      return;
    }

    const updated: TestSuite[] = [...this.suites];
    const idx = updated.findIndex(s => s.name === name);
    const testcaseRules: TestSuiteCaseRule[] = [...this.selectedCaseIds]
      .map((id) => ({ testcase_key: id, action: this.actionFor(id) }))
      .sort((a, b) => a.testcase_key.localeCompare(b.testcase_key));

    const row: TestSuite = {
      name,
      testcase_keys: testcaseRules.map(r => r.testcase_key),
      testcase_rules: testcaseRules,
    };

    if (idx >= 0) updated[idx] = row;
    else updated.push(row);

    this.api.saveTestSuites(updated).subscribe({
      next: () => {
        this.suites = updated.sort((a, b) => a.name.localeCompare(b.name));
        this.saveMsg = 'Saved';
      },
      error: () => {
        this.saveMsg = 'Save failed';
      },
    });
  }

  deleteSuite(name: string) {
    const updated = this.suites.filter(s => s.name !== name);
    this.api.saveTestSuites(updated).subscribe({
      next: () => {
        this.suites = updated;
        if (this.editingName === name) this.newSuite();
        this.saveMsg = 'Deleted';
      },
      error: () => {
        this.saveMsg = 'Delete failed';
      },
    });
  }
}
