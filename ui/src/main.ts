import { bootstrapApplication } from '@angular/platform-browser';
import { appConfig } from './app/app.config';
import { AppComponent } from './app/app.component';
import { ClarityIcons } from '@cds/core/icon';
import {
  cogIcon, floppyIcon, folderIcon, networkGlobeIcon, tableIcon,
  shieldCheckIcon, playIcon, historyIcon, trashIcon, refreshIcon,
  downloadCloudIcon, plusCircleIcon, timesCircleIcon, terminalIcon,
  barChartIcon, pluginIcon, connectIcon, timesIcon, checkCircleIcon,
  exclamationTriangleIcon, infoCircleIcon, warningStandardIcon, plusIcon,
} from '@cds/core/icon';

ClarityIcons.addIcons(
  cogIcon, floppyIcon, folderIcon, networkGlobeIcon, tableIcon,
  shieldCheckIcon, playIcon, historyIcon, trashIcon, refreshIcon,
  downloadCloudIcon, plusCircleIcon, timesCircleIcon, terminalIcon,
  barChartIcon, pluginIcon, connectIcon, timesIcon, checkCircleIcon,
  exclamationTriangleIcon, infoCircleIcon, warningStandardIcon, plusIcon,
);

bootstrapApplication(AppComponent, appConfig).catch(err => console.error(err));
