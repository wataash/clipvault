#!/usr/bin/python3
"""Storage, protocol and permission-boundary regression tests; synthetic data only."""
import base64
import json
import io
import os
import pty
import select
from pathlib import Path
import socket
import sqlite3
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import clipvault as cv


def event(data=b'Japanese: \xe6\x97\xa5\xe6\x9c\xac\xe8\xaa\x9e\n\x00', mime='text/plain'):
    return {'id': str(uuid.uuid4()), 'copied_ms': 1788652800000,
            'formats': [{'mime': mime, 'data': base64.b64encode(data).decode()}], 'errors': []}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.db = cv.database(self.state, 8 * 1024 * 1024)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def exchange(self, body, allowed_uid=None, size=None):
        server, client = socket.socketpair()
        with server, client:
            client.sendall(struct.pack('!I', len(body) if size is None else size) + body)
            cv.handle(server, self.db, os.getuid() if allowed_uid is None else allowed_uid)
            return json.loads(client.recv(1024))

    def test_binary_multiple_mime_and_idempotent_retry(self):
        item = event()
        item['formats'].append({'mime': 'image/png', 'data': base64.b64encode(bytes(range(256))).decode()})
        for _ in range(2):
            self.assertTrue(self.exchange(json.dumps(item).encode())['ok'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        self.assertEqual(dict(cv.get_formats(self.db, item['id']))['image/png'], bytes(range(256)))

    def test_deduplication_and_delete(self):
        a, b = event(), event()
        cv.append(self.db, a)
        cv.append(self.db, b)
        self.assertEqual(self.db.execute('SELECT count(*) FROM blobs').fetchone()[0], 1)
        cv.delete(self.db, a['id'])
        self.assertEqual(len(cv.get_formats(self.db, b['id'])), 1)
        cv.delete(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM blobs').fetchone()[0], 0)

    def test_search_parameterized_and_preview_escaped(self):
        cv.append(self.db, event())
        self.assertEqual(len(cv.search(self.db, '日本語')), 1)
        self.assertEqual(cv.search(self.db, "' OR 1=1 --"), [])
        self.assertEqual(cv.safe('\x1b]52;x\x07\n\u202e'), '?]52;x?\\n?')

    def test_forbidden_uid_cannot_append(self):
        self.assertEqual(self.exchange(b'{}', os.getuid() + 1)['code'], 'forbidden')
        self.assertEqual(cv.search(self.db, ''), [])

    def test_protocol_has_no_read_or_delete_operation(self):
        cv.append(self.db, event())
        for operation in ('read', 'list', 'delete', 'search'):
            self.assertEqual(self.exchange(json.dumps({'op': operation}).encode())['code'], 'invalid')
        self.assertEqual(len(cv.search(self.db, '')), 1)

    def test_malformed_and_oversized_input(self):
        for body in (b'[]', b'{', b'null', b'{}', b'"x"', b'[' * 1100):
            self.assertEqual(self.exchange(body)['code'], 'invalid')
        self.assertEqual(self.exchange(b'', size=cv.MAX_FRAME + 1)['code'], 'invalid')
        item = event()
        item['formats'][0]['data'] = '**'
        self.assertEqual(self.exchange(json.dumps(item).encode())['code'], 'invalid')
        item = event()
        item['formats'][0]['mime'] = '\x1b'
        self.assertEqual(self.exchange(json.dumps(item).encode())['code'], 'invalid')

    def test_partial_capture_persisted(self):
        item = event()
        item['errors'] = ['format-unavailable-or-size-limit']
        cv.append(self.db, item)
        self.assertIn('format-unavailable', cv.search(self.db, '')[0][2])

    def test_capacity_failure_rolls_back_without_deleting_history(self):
        cv.append(self.db, event(b'keep'))
        pages = self.db.execute('PRAGMA page_count').fetchone()[0]
        self.db.execute(f'PRAGMA max_page_count={pages}')
        with self.assertRaises(sqlite3.OperationalError):
            cv.append(self.db, event(os.urandom(256000)))
        self.assertEqual(len(cv.search(self.db, '')), 1)
        self.assertEqual(self.db.execute('SELECT data FROM blobs').fetchone()[0], b'keep')

    def test_persists_after_reopen(self):
        item = event(b'after restart')
        cv.append(self.db, item)
        self.db.close()
        self.db = cv.database(self.state, 8 * 1024 * 1024)
        self.assertEqual(cv.get_formats(self.db, item['id'])[0][1], b'after restart')

    def test_browse_search_selection_emits_only_chosen_bytes(self):
        item = event('日本語\n\x00'.encode())
        cv.append(self.db, item)
        config = self.state / 'config.json'
        config.write_text(json.dumps({'max_db_bytes': 8 * 1024 * 1024}))

        output = io.StringIO()
        master, slave = pty.openpty()
        terminal_path = os.ttyname(slave)
        real_open = open

        def open_terminal(path, *args, **kwargs):
            return real_open(terminal_path if path == '/dev/tty' else path, *args, **kwargs)

        try:
            # Real non-seekable terminal I/O; stdout remains the restoration pipe.
            os.write(master, '/日本語\n1\n1\n'.encode())
            with patch.object(cv, 'CONFIG', config), patch.object(cv, 'STATE', self.state), patch('builtins.open', side_effect=open_terminal), patch('sys.stdout', output):
                self.assertEqual(cv.browse(None), 0)
            os.set_blocking(master, False)
            self.assertIn('ClipVault'.encode(), os.read(master, 65536))
        finally:
            os.close(master)
            os.close(slave)
        restored = json.loads(output.getvalue())
        self.assertEqual(restored, item['formats'][0])

    def test_empty_text_entry_lists_without_crashing(self):
        # substr() over a zero-length blob yields NULL, not an empty blob.
        cv.append(self.db, event(b'', 'text/plain'))
        config = self.state / 'config.json'
        config.write_text(json.dumps({'max_db_bytes': 8 * 1024 * 1024}))

        master, slave = pty.openpty()
        terminal_path = os.ttyname(slave)
        real_open = open

        def open_terminal(path, *args, **kwargs):
            return real_open(terminal_path if path == '/dev/tty' else path, *args, **kwargs)

        try:
            os.write(master, 'q\n'.encode())
            with patch.object(cv, 'CONFIG', config), patch.object(cv, 'STATE', self.state), patch('builtins.open', side_effect=open_terminal):
                self.assertEqual(cv.browse(None), 0)
            os.set_blocking(master, False)
            # The terminal delivers the listing in chunks; drain until it arrives.
            shown, deadline = b'', time.monotonic() + 5
            while b'text/plain' not in shown and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    shown += os.read(master, 65536)
            self.assertIn(b'text/plain', shown)
        finally:
            os.close(master)
            os.close(slave)

    def test_no_partial_transaction_when_later_format_is_invalid(self):
        item = event()
        item['formats'].append({'mime': 'text/html', 'data': 'invalid!!'})
        self.assertEqual(self.exchange(json.dumps(item).encode())['code'], 'invalid')
        self.assertEqual(cv.search(self.db, ''), [])

    def test_invalid_timestamp_cannot_break_browser(self):
        item = event()
        item['copied_ms'] = 2**52
        self.assertEqual(self.exchange(json.dumps(item).encode())['code'], 'invalid')


if __name__ == '__main__':
    unittest.main()
