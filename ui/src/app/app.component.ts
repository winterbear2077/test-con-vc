import { Component } from '@angular/core';
import { ClarityModule } from '@clr/angular';
import { ConfigComponent } from './config/config.component';
import { InputNetworksComponent } from './input-networks/input-networks.component';
import { VrfRulesComponent } from './vrf-rules/vrf-rules.component';
import { CustomStepTestComponent } from './custom-step-test/custom-step-test.component';
import { RunTestComponent } from './run-test/run-test.component';
import { HistoryComponent } from './history/history.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    ClarityModule,
    ConfigComponent, InputNetworksComponent, VrfRulesComponent,
    CustomStepTestComponent, RunTestComponent, HistoryComponent,
  ],
  templateUrl: './app.component.html',
  styleUrl: './app.component.scss'
})
export class AppComponent {
  activeTab = 'config';
  navTo(tab: string) { this.activeTab = tab; }
}
