// Test the actual installed Mutter SelectionSource API, without a desktop.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta?version=18';
import System from 'system';
import {capture} from './clipvault@local/collector.js';
import {watchSelection} from './clipvault@local/selection.js';
const bytes = new TextEncoder().encode('Meta 日本語\n\x00');
const source = Meta.SelectionSourceMemory.new('text/plain', new GLib.Bytes(bytes));
const loop = new GLib.MainLoop(null, false);
let status = 0;
async function tests() {
    const event = await capture(source, new Gio.Cancellable());
    if (event.errors.length || event.formats.length !== 1 || event.formats[0].data !== GLib.base64_encode(bytes))
        throw new Error('Meta capture mismatch');
    // Real GObject signal and enum values: a nonexistent enum must not silently
    // suppress every clipboard event or make startup read PRIMARY instead.
    const selection = new Meta.Selection();
    const captured = [];
    const collector = {changed: item => captured.push(item)};
    const signal = watchSelection(selection, collector);
    if (captured.length)
        throw new Error('empty startup should not create an entry');
    selection.emit('owner-changed', Meta.SelectionType.SELECTION_PRIMARY, source);
    selection.emit('owner-changed', Meta.SelectionType.SELECTION_DND, source);
    if (captured.length)
        throw new Error('PRIMARY and DND must be ignored');
    for (let i = 0; i < 3; i++)
        selection.emit('owner-changed', Meta.SelectionType.SELECTION_CLIPBOARD, source);
    if (captured.length !== 3 || captured.some(item => item !== source))
        throw new Error('clipboard updates were lost');
    selection.disconnect(signal);

    selection.set_owner(Meta.SelectionType.SELECTION_CLIPBOARD, source);
    selection.set_owner(Meta.SelectionType.SELECTION_PRIMARY,
        Meta.SelectionSourceMemory.new('text/plain', new GLib.Bytes(new TextEncoder().encode('PRIMARY'))));
    captured.length = 0;
    const startupSignal = watchSelection(selection, collector);
    if (captured.length !== 1)
        throw new Error('existing clipboard was not captured');
    const startup = await capture(captured[0], new Gio.Cancellable());
    if (startup.errors.length || startup.formats[0]?.data !== GLib.base64_encode(bytes))
        throw new Error('startup must read CLIPBOARD, not PRIMARY');
    selection.disconnect(startupSignal);
    print('PASS: Mutter source bytes, clipboard update signals, PRIMARY/DND exclusion, startup clipboard');
}
tests().catch(error => { printerr(error.stack); status = 1; }).finally(() => loop.quit());
loop.run();
System.exit(status);
