// SPDX-License-Identifier: Apache-2.0
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

export const MAX_ITEM = 8 * 1024 * 1024;
export const MAX_EVENT = 24 * 1024 * 1024;
export const MAX_TYPES = 16;
const MAX_QUEUE = 64 * 1024 * 1024;
const MAX_PENDING = 128;
// The unsent queue lives in module scope, so disable()/enable() - an extension
// reload, a session mode switch - keeps it. Only the process boundary, logout
// or a shell exit, loses the copies still in it.
export const queue = {pending: [], bytes: 0, lost: 0};
const PRIORITY = GLib.PRIORITY_DEFAULT;
// X11 selection targets can be commands, not data. Never request side effects
// such as DELETE, or protocol-management targets advertised through XWayland.
const CONTROL_TARGETS = new Set(['TARGETS', 'TIMESTAMP', 'MULTIPLE', 'SAVE_TARGETS',
    'DELETE', 'INSERT_SELECTION', 'INSERT_PROPERTY']);

function readSource(source, mime, cancellable) {
    return new Promise((resolve, reject) => {
        source.read_async(mime, cancellable, (obj, result) => {
            try { resolve(obj.read_finish(result)); } catch (e) { reject(e); }
        });
    });
}

function readBytes(stream, cancellable, size = 65536) {
    return new Promise((resolve, reject) => {
        stream.read_bytes_async(size, PRIORITY, cancellable, (obj, result) => {
            try { resolve(obj.read_bytes_finish(result).get_data()); } catch (e) { reject(e); }
        });
    });
}

export async function capture(source, cancellable) {
    const event = {id: GLib.uuid_string_random(), copied_ms: Date.now(), formats: [], errors: []};
    const types = [...new Set(source.get_mimetypes())].filter(type => !CONTROL_TARGETS.has(type));
    if (types.length > MAX_TYPES)
        event.errors.push('format-count-limit');
    let total = 0;
    // Start every read while this particular source still owns the clipboard.
    await Promise.all(types.slice(0, MAX_TYPES).map(async mime => {
        let stream = null;
        try {
            if (mime.length < 1 || mime.length > 256 || /[^\x20-\x7e]/.test(mime))
                throw new Error('mime');
            stream = await readSource(source, mime, cancellable);
            const chunks = [];
            let size = 0;
            while (true) {
                const bytes = await readBytes(stream, cancellable);
                if (bytes.length === 0)
                    break;
                size += bytes.length;
                total += bytes.length;
                if (size > MAX_ITEM || total > MAX_EVENT)
                    throw new Error('limit');
                chunks.push(bytes);
            }
            const all = new Uint8Array(size);
            let offset = 0;
            for (const chunk of chunks) {
                all.set(chunk, offset);
                offset += chunk.length;
            }
            event.formats.push({mime, data: GLib.base64_encode(all)});
        } catch (_) {
            // MIME and exception messages may contain clipboard data; don't log them.
            event.errors.push('format-unavailable-or-size-limit');
        } finally {
            if (stream) {
                stream.close_async(PRIORITY, null, (obj, result) => {
                    try { obj.close_finish(result); } catch (_) { /* already closed */ }
                });
            }
        }
    }));
    if (!event.formats.length && !event.errors.length)
        event.errors.push('no-formats');
    return event;
}

export async function send(path, payload, cancellable) {
    const client = new Gio.SocketClient();
    client.set_timeout(12);
    const address = new Gio.UnixSocketAddress({path});
    const conn = await new Promise((resolve, reject) => {
        client.connect_async(address, cancellable, (obj, result) => {
            try { resolve(obj.connect_finish(result)); } catch (e) { reject(e); }
        });
    });
    try {
        const header = new Uint8Array(4);
        new DataView(header.buffer).setUint32(0, payload.length, false);
        for (const data of [header, payload]) {
            await new Promise((resolve, reject) => {
                conn.get_output_stream().write_all_async(data, PRIORITY, cancellable, (obj, result) => {
                    try { obj.write_all_finish(result); resolve(); } catch (e) { reject(e); }
                });
            });
        }
        // Fixed maximum: a server bug must not grow the shell's memory indefinitely.
        const input = conn.get_input_stream();
        const bytes = [];
        while (bytes.length < 256) {
            const chunk = await readBytes(input, cancellable, 1);
            if (!chunk.length)
                throw new Error('missing acknowledgement');
            if (chunk[0] === 10)
                return JSON.parse(new TextDecoder().decode(new Uint8Array(bytes)));
            bytes.push(chunk[0]);
        }
        throw new Error('oversized acknowledgement');
    } finally {
        conn.close_async(PRIORITY, null, (obj, result) => {
            try { obj.close_finish(result); } catch (_) { /* disconnected */ }
        });
    }
}

export class Collector {
    constructor(notify, path = '/run/clipvault/append.sock', transport = send) {
        this.notify = notify;
        this.path = path;
        this.transport = transport;
        this.active = new Set();
        this.stopped = false;
        this.sending = false;
        this.lastNotice = 0;
        this.retry = GLib.timeout_add_seconds(PRIORITY, 5, () => {
            this.flush();
            return GLib.SOURCE_CONTINUE;
        });
        if (queue.pending.length || queue.lost)
            this.flush();
    }

    warn(message) {
        if (this.stopped || Date.now() - this.lastNotice < 60000)
            return;
        this.lastNotice = Date.now();
        this.notify(message);
    }

    async changed(source) {
        if (this.stopped || !source)
            return;
        if (this.active.size >= 2) {
            queue.lost++;
            this.warn('Copies arrived too fast; some items could not be captured.');
            return;
        }
        const cancel = new Gio.Cancellable();
        this.active.add(cancel);
        const timer = GLib.timeout_add_seconds(PRIORITY, 5, () => {
            cancel.cancel();
            return GLib.SOURCE_CONTINUE;
        });
        try {
            const event = await capture(source, cancel);
            if (!this.stopped) {
                if (event.errors.length)
                    this.warn('Some formats could not be read. The gap is recorded in the history.');
                this.enqueue(event);
            }
        } catch (_) {
            queue.lost++;
            this.warn('Failed to capture a copy.');
        } finally {
            GLib.Source.remove(timer);
            this.active.delete(cancel);
        }
    }

    enqueue(event) {
        const payload = new TextEncoder().encode(JSON.stringify(event));
        if (queue.pending.length >= MAX_PENDING || queue.bytes + payload.length > MAX_QUEUE) {
            queue.lost++;
            this.warn('Unsaved data reached the memory limit. New copies cannot be stored.');
            return;
        }
        queue.pending.push(payload);
        queue.bytes += payload.length;
        this.flush();
    }

    async flush() {
        if (this.sending || this.stopped)
            return;
        this.sending = true;
        try {
            if (queue.lost && queue.pending.length < MAX_PENDING && queue.bytes < MAX_QUEUE - 4096) {
                const count = queue.lost;
                queue.lost = 0;
                this.enqueue({id: GLib.uuid_string_random(), copied_ms: Date.now(), formats: [],
                    errors: [`collector-dropped-events:${count}`]});
            }
            while (queue.pending.length && !this.stopped) {
                this.ioCancel = new Gio.Cancellable();
                const timeout = GLib.timeout_add_seconds(PRIORITY, 15, () => {
                    this.ioCancel?.cancel();
                    return GLib.SOURCE_CONTINUE;
                });
                let response;
                try {
                    response = await this.transport(this.path, queue.pending[0], this.ioCancel);
                } finally {
                    GLib.Source.remove(timeout);
                    this.ioCancel = null;
                }
                if (this.stopped)
                    break;
                if (response.ok !== true) {
                    if (response.code === 'invalid') {
                        queue.bytes -= queue.pending.shift().length;
                        queue.lost++;
                        this.warn('The archive service rejected the data.');
                        continue;
                    }
                    throw new Error('archive unavailable');
                }
                queue.bytes -= queue.pending.shift().length;
            }
        } catch (_) {
            this.warn('Cannot reach the archive. Copies are held in memory and retried. Check clipvault.socket/service.');
        } finally {
            this.sending = false;
        }
    }

    stop() {
        this.stopped = true;
        GLib.Source.remove(this.retry);
        for (const cancel of this.active)
            cancel.cancel();
        this.ioCancel?.cancel();
        // A cancelled send may have reached the archive; the event id makes the
        // resend idempotent. Interrupted captures cannot resume, so only their
        // count survives, to be recorded as a gap once collection restarts.
        queue.lost += this.active.size;
        if (queue.pending.length || queue.lost)
            this.notify('Collection stopped. Unsaved copies are stored when it resumes; logging out or restarting the shell loses them.');
    }
}
