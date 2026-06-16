import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ClarityModule } from '@clr/angular';
import { ApiService, RunTestcase, TestSuite } from '../api.service';

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

  editingName = '';
  selectedCaseIds = new Set<string>();
  saveMsg = '';

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.refresh();
  }

  refresh() {
    this.api.getRunCatalog().subscribe({
      next: (catalog) => {
        this.testcases = catalog.testcases || [];
      },
      error: () => {
        this.testcases = [];
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
    this.saveMsg = '';
  }

  editSuite(s: TestSuite) {
    this.editingName = s.name;
    this.selectedCaseIds = new Set((s.testcase_keys || []).filter(Boolean));
    this.saveMsg = '';
  }

  isCaseSelected(caseId: string): boolean {
    return this.selectedCaseIds.has(caseId);
  }

  toggleCase(caseId: string, checked: boolean) {
    if (checked) this.selectedCaseIds.add(caseId);
    else this.selectedCaseIds.delete(caseId);
  }

  selectAllCases() {
    this.testcases.forEach(tc => this.selectedCaseIds.add(tc.id));
  }

  clearCases() {
    this.selectedCaseIds.clear();
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
    const row: TestSuite = {
      name,
      testcase_keys: [...this.selectedCaseIds],
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
