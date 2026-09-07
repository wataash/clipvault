// Run using gjs -m test_collector.js. Synthetic data; no desktop access.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import System from 'system';
import {capture, Collector, MAX_ITEM, queue} from './clipvault@local/collector.js';

function assert(ok, message) {
    if (!ok)
        throw new Error(message);
}

function source(formats) {
    return {
        get_mimetypes: () => Object.keys(formats),
        read_async(mime, cancellable, callback) {
            GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                callback({read_finish() {
                    if (formats[mime] === null)
                        throw new Error('source gone');
                    return Gio.MemoryInputStream.new_from_bytes(new GLib.Bytes(formats[mime]));
                }}, null);
                return GLib.SOURCE_REMOVE;
            });
        },
    };
}

function tick() {
    return new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, 10, () => {
        resolve();
        return GLib.SOURCE_REMOVE;
    }));
}

async function tests() {
    const text = new TextEncoder().encode('日本語\n\x00\x1b');
    const binary = new Uint8Array([0, 255, 128, 10]);
    const event = await capture(source({'text/plain': text, 'image/png': binary}), new Gio.Cancellable());
    assert(event.formats.length === 2 && event.errors.length === 0, 'multi MIME');
    assert(event.formats.find(f => f.mime === 'text/plain').data === GLib.base64_encode(text), 'byte preservation');
    const controls = await capture(source({'text/plain': text, 'DELETE': null, 'TARGETS': null}), new Gio.Cancellable());
    assert(controls.formats.length === 1 && controls.errors.length === 0, 'never read X11 control targets');
    const partial = await capture(source({'text/plain': text, 'image/png': null}), new Gio.Cancellable());
    assert(partial.formats.length === 1 && partial.errors.length === 1, 'partial capture');
    const large = await capture(source({'image/png': new Uint8Array(MAX_ITEM + 1)}), new Gio.Cancellable());
    assert(large.formats.length === 0 && large.errors.length === 1, 'size cap');
    const cancel = new Gio.Cancellable();
    cancel.cancel();
    const cancelled = await capture(source({'text/plain': text}), cancel);
    assert(cancelled.formats.length === 0 && cancelled.errors.length === 1, 'cancellation');

    let online = false;
    const received = [];
    const notices = [];
    const collector = new Collector(message => notices.push(message), '/unused', async (_path, payload) => {
        if (!online)
            throw new Error('offline');
        received.push(JSON.parse(new TextDecoder().decode(payload)));
        return {ok: true};
    });
    try {
        collector.enqueue(event);
        await tick();
        assert(queue.pending.length === 1 && notices.length === 1, 'offline retention');
        online = true;
        await collector.flush();
        assert(queue.pending.length === 0 && received[0].id === event.id, 'retry keeps UUID');
        online = false;
        for (let i = 0; i < 140; i++)
            collector.enqueue(event);
        await tick();
        assert(queue.pending.length === 128 && queue.lost === 12, 'queue cap');
        online = true;
        await collector.flush();
        await collector.flush();
        assert(received.some(e => e.errors.includes('collector-dropped-events:12')), 'loss record');
        online = false;
        collector.enqueue(event);
        await tick();
        assert(queue.pending.length === 1, 'offline retention before stop');
    } finally {
        collector.stop();
    }
    assert(notices.at(-1).includes('Unsaved'), 'stop notice');

    // Disabling the extension (extension reload, session mode switch) must keep
    // unsent copies for the next Collector instead of dropping them.
    const carried = [];
    const resumed = new Collector(message => notices.push(message), '/unused', async (_path, payload) => {
        carried.push(JSON.parse(new TextDecoder().decode(payload)));
        return {ok: true};
    });
    await tick();
    assert(carried.length === 1 && carried[0].id === event.id, 'unsent copies survive stop');
    assert(queue.pending.length === 0 && queue.bytes === 0, 'resend clears the queue');
    resumed.stop();

    // Extensions without unlock-dialog are disabled while the screen is locked,
    // which stops collection exactly when the copies still sit in memory.
    const [, meta] = GLib.file_get_contents('./clipvault@local/metadata.json');
    const modes = JSON.parse(new TextDecoder().decode(meta))['session-modes'];
    assert(modes?.includes('user') && modes?.includes('unlock-dialog'), 'session modes');
    print('PASS: MIME, bytes, partial capture, size limit, cancellation, retry, queue limit, loss record, restart carryover, session modes');
}

const loop = new GLib.MainLoop(null, false);
let status = 0;
tests().catch(error => { printerr(error.stack); status = 1; }).finally(() => loop.quit());
loop.run();
System.exit(status);
