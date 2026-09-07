// SPDX-License-Identifier: Apache-2.0
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Collector} from './collector.js';
import {watchSelection} from './selection.js';

export default class ClipVaultExtension extends Extension {
    enable() {
        this.collector = new Collector(message => Main.notify('ClipVault', message));
        this.selection = global.display.get_selection();
        this.signal = watchSelection(this.selection, this.collector);
    }

    disable() {
        this.selection?.disconnect(this.signal);
        this.collector?.stop();
        this.collector = null;
        this.selection = null;
    }
}
