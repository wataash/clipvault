// SPDX-License-Identifier: Apache-2.0
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {MAX_ITEM} from './collector.js';

export function watchSelection(selection, collector) {
    const type = Meta.SelectionType.SELECTION_CLIPBOARD;
    const signal = selection.connect('owner-changed', (_selection, changedType, source) => {
        if (changedType === type)
            collector.changed(source);
    });
    // Meta has no public get_owner(); adapt the current selection to the source API.
    if (selection.get_mimetypes(type).length) {
        collector.changed({
            get_mimetypes: () => selection.get_mimetypes(type),
            read_async(mime, cancel, callback) {
                const output = Gio.MemoryOutputStream.new_resizable();
                selection.transfer_async(type, mime, MAX_ITEM + 1, output, cancel, (obj, result) => {
                    callback({read_finish() {
                        obj.transfer_finish(result);
                        output.close(null);
                        return Gio.MemoryInputStream.new_from_bytes(output.steal_as_bytes());
                    }}, result);
                });
            },
        });
    }
    return signal;
}
