#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Authenticated clipboard archive. Network protocol exposes append only."""
import argparse
import base64
import hashlib
import json
import logging
import os
import resource
from pathlib import Path
import socket
import sqlite3
import struct
import sys
import time
import unicodedata
import uuid

logger = logging.getLogger(__name__)
CONFIG = Path('/etc/clipvault.json')
STATE = Path('/var/lib/clipvault/')
MAX_ITEM = 8 * 1024 * 1024
MAX_EVENT = 24 * 1024 * 1024
MAX_FRAME = 34 * 1024 * 1024
MAX_TYPES = 16
SCHEMA = '''
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, copied_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL,
 errors TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS formats (
 event TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
 mime TEXT NOT NULL, hash TEXT NOT NULL REFERENCES blobs(hash),
 PRIMARY KEY(event, mime));
CREATE INDEX IF NOT EXISTS events_time ON events(copied_ms DESC);
'''


class Rejected(Exception):
    pass


def safe(text, limit=240):
    """Never emit clipboard-supplied terminal escapes or bidi controls."""
    return ''.join(c if not unicodedata.category(c).startswith('C') else
                   {'\n': '\\n', '\r': '\\r', '\t': '\\t'}.get(c, '?')
                   for c in str(text)[:limit])


def database(state, max_bytes):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(state / 'history.sqlite3', timeout=5)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA secure_delete=ON')
    page_size = db.execute('PRAGMA page_size').fetchone()[0]
    db.execute(f'PRAGMA max_page_count={max(16, max_bytes // page_size)}')
    db.executescript(SCHEMA)
    return db


def validate(event):
    if not isinstance(event, dict) or set(event) != {'id', 'copied_ms', 'formats', 'errors'}:
        raise Rejected('invalid event')
    if not isinstance(event['id'], str) or str(uuid.UUID(event['id'])) != event['id']:
        raise Rejected('invalid id')
    if type(event['copied_ms']) is not int or not 0 <= event['copied_ms'] < 4102444800000:
        raise Rejected('invalid timestamp')
    formats, errors = event['formats'], event['errors']
    if not isinstance(formats, list) or len(formats) > MAX_TYPES:
        raise Rejected('too many formats')
    if (not isinstance(errors, list) or len(errors) > MAX_TYPES + 2 or
            any(not isinstance(e, str) or len(e) > 256 for e in errors)):
        raise Rejected('invalid errors')
    result, seen, size = [], set(), 0
    for item in formats:
        if not isinstance(item, dict) or set(item) != {'mime', 'data'}:
            raise Rejected('invalid format')
        mime = item['mime']
        if (not isinstance(mime, str) or not 1 <= len(mime) <= 256 or mime in seen or
                any(ord(c) < 32 or ord(c) > 126 for c in mime)):
            raise Rejected('invalid MIME')
        if not isinstance(item['data'], str):
            raise Rejected('invalid data')
        raw = base64.b64decode(item['data'], validate=True)
        size += len(raw)
        if len(raw) > MAX_ITEM or size > MAX_EVENT:
            raise Rejected('size limit')
        seen.add(mime)
        result.append((mime, raw))
    if not result and not errors:
        raise Rejected('empty event')
    return result


def append(db, event):
    formats = validate(event)
    # UUID makes retries safe when COMMIT succeeded but acknowledgement was lost.
    with db:
        if db.execute('SELECT 1 FROM events WHERE id=?', (event['id'],)).fetchone():
            return
        db.execute('INSERT INTO events VALUES (?,?,?,?)',
                   (event['id'], event['copied_ms'], time.time_ns() // 1_000_000,
                    json.dumps(event['errors'], ensure_ascii=True)))
        for mime, raw in formats:
            digest = hashlib.sha256(raw).hexdigest()
            db.execute('INSERT OR IGNORE INTO blobs VALUES (?,?)', (digest, raw))
            db.execute('INSERT INTO formats VALUES (?,?,?)', (event['id'], mime, digest))


def receive_exact(conn, size):
    chunks = bytearray()
    deadline = time.monotonic() + 10
    while len(chunks) < size:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        conn.settimeout(left)
        chunk = conn.recv(min(65536, size - len(chunks)))
        if not chunk:
            raise Rejected('incomplete frame')
        chunks.extend(chunk)
    return bytes(chunks)


def handle(conn, db, allowed_uid):
    uid = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if uid != allowed_uid:
        conn.sendall(b'{"ok":false,"code":"forbidden"}\n')
        return
    try:
        size = struct.unpack('!I', receive_exact(conn, 4))[0]
        if not 0 < size <= MAX_FRAME:
            raise Rejected('frame limit')
        event = json.loads(receive_exact(conn, size))
        append(db, event)
        response = {'ok': True}
    except sqlite3.Error:
        # Never log database inputs, clipboard values, or attacker-controlled errors.
        logger.error('Archive write failed (capacity, storage or database busy)')
        response = {'ok': False, 'code': 'storage'}
    except (Rejected, ValueError, TypeError, RecursionError):
        response = {'ok': False, 'code': 'invalid'}
    conn.settimeout(2)
    conn.sendall(json.dumps(response).encode() + b'\n')


def serve(args):
    config = json.loads(CONFIG.read_text())
    with database(STATE, config['max_db_bytes']) as db:
        # Socket is created and access-controlled by systemd, never by the client.
        if os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
            raise RuntimeError('Start using clipvault.socket')
        listener = socket.socket(fileno=3)
        while True:
            conn, _ = listener.accept()
            with conn:
                try:
                    conn.settimeout(2)
                    handle(conn, db, config['allowed_uid'])
                except OSError:
                    pass
            # Bound append rate and CPU use without retaining clipboard data.
            time.sleep(0.05)


def search(db, query):
    return db.execute('''SELECT e.id, e.copied_ms, e.errors FROM events e
      WHERE ? = '' OR EXISTS (
        SELECT 1 FROM formats f JOIN blobs b ON b.hash=f.hash
        WHERE f.event=e.id AND (f.mime LIKE 'text/%' OR f.mime='UTF8_STRING')
        AND instr(CAST(b.data AS TEXT), ?) > 0)
      ORDER BY e.copied_ms DESC, e.rowid DESC LIMIT 100''', (query, query)).fetchall()


def get_formats(db, event_id):
    return db.execute('''SELECT f.mime, b.data FROM formats f JOIN blobs b ON b.hash=f.hash
                         WHERE f.event=? ORDER BY f.mime''', (event_id,)).fetchall()


def summarize(db, event_id):
    """Formats with sizes and hashes, plus one short text preview; blobs stay in the database."""
    rows = db.execute("""SELECT f.mime, length(b.data), f.hash,
        CASE WHEN f.mime LIKE 'text/plain%' OR f.mime='UTF8_STRING'
        THEN COALESCE(substr(b.data,1,400), X'') ELSE X'' END
        FROM formats f JOIN blobs b ON b.hash=f.hash
        WHERE f.event=? ORDER BY f.mime""", (event_id,)).fetchall()
    preview = next((safe(' '.join(raw.decode('utf-8', errors='replace').split()), 100)
                    for *_, raw in rows if raw), '')
    return [row[:3] for row in rows], preview


def delete(db, event_id=None):
    with db:
        if event_id is None:
            db.execute('DELETE FROM events')
        else:
            db.execute('DELETE FROM events WHERE id=?', (event_id,))
        db.execute('DELETE FROM blobs WHERE hash NOT IN (SELECT hash FROM formats)')


def gui_request(db, request):
    """Private pipe API, reachable only after sudo authentication; never on append.sock."""
    if not isinstance(request, dict):
        raise Rejected('invalid request')
    operation = request.get('op')
    if operation == 'list' and set(request) == {'op', 'query'}:
        query = request['query']
        if not isinstance(query, str) or len(query) > 256:
            raise Rejected('invalid query')
        result = []
        for event_id, copied_ms, errors in search(db, query):
            rows, preview = summarize(db, event_id)
            # Equality between formats only; the content hash itself stays server side.
            first, formats = {}, []
            for mime, size, digest in rows:
                entry = {'mime': mime, 'size': size}
                if first.setdefault(digest, mime) != mime:
                    entry['same_as'] = first[digest]
                formats.append(entry)
            result.append({'id': event_id, 'copied_ms': copied_ms, 'errors': json.loads(errors),
                           'formats': formats, 'summary': preview})
        return result
    if operation == 'delete' and set(request) == {'op', 'id'}:
        if not isinstance(request['id'], str):
            raise Rejected('invalid id')
        delete(db, request['id'])
        return None
    if operation in ('preview', 'get') and set(request) == {'op', 'id', 'mime'}:
        event_id, mime = request['id'], request['mime']
        if not isinstance(event_id, str) or not isinstance(mime, str):
            raise Rejected('invalid selection')
        row = db.execute('''SELECT b.data FROM formats f JOIN blobs b ON b.hash=f.hash
                            WHERE f.event=? AND f.mime=?''', (event_id, mime)).fetchone()
        if row is None:
            raise Rejected('entry not found')
        raw = row[0]
        size = len(raw)
        if operation == 'preview':
            if mime.startswith('text/') or mime in ('UTF8_STRING', 'TEXT', 'STRING', 'COMPOUND_TEXT'):
                raw = raw[:65536]
            elif not mime.startswith('image/'):
                # Binary formats are previewed as a short hex dump.
                raw = raw[:4096]
        return {'mime': mime, 'size': size, 'truncated': len(raw) < size,
                'data': base64.b64encode(raw).decode()}
    raise Rejected('invalid operation')


def session(args):
    config = json.loads(CONFIG.read_text())
    with database(STATE, config['max_db_bytes']) as db:
        while True:
            line = sys.stdin.buffer.readline(16385)
            if not line:
                return 0
            if len(line) > 16384 or not line.endswith(b'\n'):
                return 1
            try:
                response = {'ok': True, 'result': gui_request(db, json.loads(line))}
            except (Rejected, ValueError, TypeError, RecursionError):
                response = {'ok': False, 'error': 'Could not read the requested history.'}
            except sqlite3.Error:
                response = {'ok': False, 'error': 'Could not access the database.'}
            # Do not log requests, query terms, previews, or response bodies.
            try:
                sys.stdout.write(json.dumps(response) + '\n')
                sys.stdout.flush()
            except BrokenPipeError:
                return 0


def browse(args):
    config = json.loads(CONFIG.read_text())
    with (database(STATE, config['max_db_bytes']) as db,
          open('/dev/tty', 'r') as tty_input,
          open('/dev/tty', 'w') as tty_output):
        def say(text):
            tty_output.write(text + '\n')
            tty_output.flush()

        def ask(text):
            tty_output.write(text)
            tty_output.flush()
            line = tty_input.readline()
            if not line:
                raise EOFError
            return line.rstrip('\n')

        query = ''
        while True:
            rows = search(db, query)
            say('\nClipVault — newest 100. Number to restore, /search, d NUMBER, DELETE ALL, q')
            for i, (event_id, copied_ms, errors) in enumerate(rows, 1):
                formats, preview = summarize(db, event_id)
                stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(copied_ms / 1000))
                say(f'{i:3} {stamp} {safe(", ".join(m for m, _, _ in formats), 100)} {preview}')
                if errors != '[]':
                    say('    capture gaps: ' + safe(errors))
            command = ask('> ')
            if command == 'q':
                return 0
            if command.startswith('/'):
                query = command[1:]
                continue
            if command == 'DELETE ALL':
                if ask('Deleting the whole history. Type DELETE: ') == 'DELETE':
                    delete(db)
                continue
            deleting = command.startswith('d ')
            try:
                index = int(command[2:] if deleting else command) - 1
                if not 0 <= index < len(rows):
                    raise ValueError
            except ValueError:
                say('Enter a number from the list.')
                continue
            event_id = rows[index][0]
            if deleting:
                if ask('Deleting the selected entry. Type DELETE: ') == 'DELETE':
                    delete(db, event_id)
                continue
            fs = get_formats(db, event_id)
            if not fs:
                say('Nothing to restore.')
                continue
            for i, (mime, raw) in enumerate(fs, 1):
                say(f'{i}: {safe(mime)} ({len(raw)} bytes)')
            try:
                fmt = int(ask('Format number to restore: ')) - 1
                if not 0 <= fmt < len(fs):
                    raise ValueError
            except ValueError:
                continue
            mime, raw = fs[fmt]
            # One chosen representation only; no history exported to the user UI.
            sys.stdout.write(json.dumps({'mime': mime, 'data': base64.b64encode(raw).decode()}))
            return 0


class ArgumentDefaultsRawTextHelpFormatter(argparse.ArgumentDefaultsHelpFormatter,
                                          argparse.RawTextHelpFormatter):
    pass


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(formatter_class=ArgumentDefaultsRawTextHelpFormatter)
    parser.add_argument('-q', '--quiet', action='count', default=0)
    subs = parser.add_subparsers(dest='subcommand_name', required=True)
    subs.add_parser('serve').set_defaults(func=serve)
    subs.add_parser('browse').set_defaults(func=browse)
    subs.add_parser('session').set_defaults(func=session)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format='%(levelname)s: %(message)s')
    try:
        return args.func(args)
    except (EOFError, KeyboardInterrupt):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
