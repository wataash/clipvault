// Wire interoperability fixture, called by test_wire.py.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import System from 'system';
import {send} from './clipvault@local/collector.js';
const event = {id: '12345678-1234-4234-8234-123456789abc', copied_ms: 1788652800000,
    formats: [{mime: 'text/plain', data: GLib.base64_encode(new TextEncoder().encode('wire 日本語\n\x00'))}], errors: []};
const loop = new GLib.MainLoop(null, false);
let status = 0;
(async () => {
    for (let i = 0; i < 2; i++) {
        const response = await send(ARGV[0], new TextEncoder().encode(JSON.stringify(event)), new Gio.Cancellable());
        if (response.ok !== true)
            throw new Error('append failed');
    }
})().catch(error => { printerr(error.stack); status = 1; }).finally(() => loop.quit());
loop.run();
System.exit(status);
