#!/usr/bin/python3
"""GTK integration tests with synthetic data; no sudo or real clipboard access."""
import base64
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import shlex
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

import clipvault as cv
import gui
from gi.repository import GLib, Gtk

_image = gui.GdkPixbuf.Pixbuf.new(gui.GdkPixbuf.Colorspace.RGB, False, 8, 64, 48)
_image.fill(0x259d90ff)
PNG = _image.save_to_bufferv('png', [], [])[1]


def event(text, timestamp):
    return {'id': str(uuid.uuid4()), 'copied_ms': timestamp,
            'formats': [{'mime': 'text/plain;charset=utf-8', 'data': base64.b64encode(text.encode()).decode()}], 'errors': []}


class LocalBackend:
    def __init__(self, state):
        self.state = state
        self.closed = False

    def request(self, request):
        if self.closed:
            raise RuntimeError('closed')
        with contextlib.closing(cv.database(self.state, 1024 * 1024)) as db:
            return cv.gui_request(db, request)

    def close(self):
        self.closed = True


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.db = cv.database(self.state, 1024 * 1024)
        self.item = event('日本語\n' + 'a' * 70000, 1788660000000)
        cv.append(self.db, self.item)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_preview_truncated_restore_exact_and_delete(self):
        items = cv.gui_request(self.db, {'op': 'list', 'query': '日本語'})
        self.assertEqual(len(items), 1)
        request = {'id': items[0]['id'], 'mime': self.item['formats'][0]['mime']}
        preview = cv.gui_request(self.db, dict(request, op='preview'))
        self.assertTrue(preview['truncated'])
        self.assertEqual(len(base64.b64decode(preview['data'])), 65536)
        restored = cv.gui_request(self.db, dict(request, op='get'))
        self.assertEqual(restored['data'], self.item['formats'][0]['data'])
        cv.gui_request(self.db, {'op': 'delete', 'id': items[0]['id']})
        self.assertEqual(cv.gui_request(self.db, {'op': 'list', 'query': ''}), [])

    def test_list_marks_formats_with_identical_content(self):
        item = event('同一の中身', 1788662000000)
        item['formats'] += [{'mime': mime, 'data': item['formats'][0]['data']} for mime in ('STRING', 'TEXT')]
        item['formats'].append({'mime': 'image/png', 'data': base64.b64encode(PNG).decode()})
        cv.append(self.db, item)
        formats = cv.gui_request(self.db, {'op': 'list', 'query': '同一の中身'})[0]['formats']
        self.assertEqual([(f['mime'], f.get('same_as')) for f in formats],
                         [('STRING', None), ('TEXT', 'STRING'), ('image/png', None),
                          ('text/plain;charset=utf-8', 'STRING')])

    def test_binary_preview_is_a_truncated_hex_dump(self):
        item = event('', 1788662000000)
        item['formats'] = [{'mime': 'application/octet-stream',
                            'data': base64.b64encode(bytes(range(256)) * 32).decode()}]
        cv.append(self.db, item)
        preview = cv.gui_request(self.db, {'op': 'preview', 'id': item['id'],
                                           'mime': 'application/octet-stream'})
        self.assertEqual((preview['size'], preview['truncated']), (8192, True))
        self.assertEqual(len(base64.b64decode(preview['data'])), 4096)
        self.assertEqual(gui.hex_dump(bytes(range(16))),
                         '00000000: 0001 0203 0405 0607 0809 0a0b 0c0d 0e0f  ................')

    def test_invalid_requests_and_missing_records(self):
        for request in ([], {}, {'op': 'list', 'query': 3}, {'op': 'list', 'query': 'x' * 257},
                        {'op': 'delete', 'id': []}, {'op': 'get', 'id': 'missing', 'mime': 'text/plain'},
                        {'op': 'sql', 'query': 'DELETE FROM events'}, {'op': 'delete_all'}):
            with self.assertRaises(cv.Rejected):
                cv.gui_request(self.db, request)
        self.assertEqual(len(cv.search(self.db, '')), 1)

    def test_private_pipe_protocol_and_eof(self):
        config = self.state / 'config.json'
        config.write_text(json.dumps({'max_db_bytes': 1024 * 1024}))
        stdin = io.TextIOWrapper(io.BytesIO(b'{"op":"invalid"}\n{"op":"list","query":""}\n'))
        stdout = io.StringIO()
        with patch.object(cv, 'CONFIG', config), patch.object(cv, 'STATE', self.state), patch('sys.stdin', stdin), patch('sys.stdout', stdout):
            self.assertEqual(cv.session(None), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertFalse(responses[0]['ok'])
        self.assertTrue(responses[1]['ok'])
        self.assertEqual(responses[1]['result'][0]['id'], self.item['id'])

    def test_client_uses_real_pipes_and_releases_session(self):
        config = self.state / 'config.json'
        config.write_text(json.dumps({'max_db_bytes': 1024 * 1024}))
        code = 'import sys; from pathlib import Path; import clipvault as cv; cv.CONFIG=Path(sys.argv[1]); cv.STATE=Path(sys.argv[2]); raise SystemExit(cv.session(None))'
        command = [sys.executable, '-B', '-c', code, str(config), str(self.state)]
        real_popen = subprocess.Popen
        def launch(_command, **kwargs):
            logging.getLogger(__name__).info('%s', shlex.join(command))
            return real_popen(command, **kwargs)
        backend = gui.Backend()
        try:
            with patch('gui.subprocess.Popen', side_effect=launch):
                rows = backend.request({'op': 'list', 'query': '日本語'})
                result = backend.request({'op': 'get', 'id': rows[0]['id'], 'mime': self.item['formats'][0]['mime']})
            self.assertEqual(result['data'], self.item['formats'][0]['data'])
        finally:
            backend.close()
            if backend.process:
                backend.process.wait(timeout=5)
                backend.process.stdout.close()
        with self.assertRaises(RuntimeError):
            backend.request({'op': 'list', 'query': ''})


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Gtk.init()
        cls.app = gui.Application()
        cls.app.register(None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        state = Path(self.temp.name)
        self.backend = LocalBackend(state)
        with contextlib.closing(cv.database(state, 1024 * 1024)) as db:
            cv.append(db, event('会議メモ\n\n次回の打ち合わせは金曜日 14:00。\n画面の検索と復元を確認する。', 1788661000000))
            cv.append(db, event('https://example.com/design-notes', 1788660000000))
            image = event('', 1788659000000)
            image['formats'] = [{'mime': 'image/png', 'data': base64.b64encode(PNG).decode()}]
            cv.append(db, image)
        self.copied = []
        self.window = gui.Window(self.app, self.backend, copier=lambda item: self.copied.append(item))
        self.window.present()
        self.pump(lambda: self.window.get_mapped())

    def tearDown(self):
        self.window.close()
        self.pump(lambda: self.window.closed)
        self.window.executor.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()

    def pump(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            if condition():
                return
            time.sleep(0.01)
        self.fail('GUI operation timed out')

    def wait_for_history(self):
        self.pump(lambda: self.window.copy_button.get_sensitive())

    def test_startup_search_preview_restore_and_close(self):
        self.wait_for_history()
        self.assertEqual(self.window.count.get_text(), '3 entries')
        self.assertEqual(self.window.preview.get_visible_child_name(), 'text')
        self.window.restore()
        self.pump(lambda: bool(self.copied) and self.window.copy_button.get_sensitive())
        self.assertIn('会議メモ', base64.b64decode(self.copied[0]['data']).decode())
        self.window.search.set_text('example.com')
        self.pump(lambda: self.window.count.get_text() == '1 entry' and self.window.copy_button.get_sensitive())
        self.assertIn('example.com', self.window.selected['summary'])
        self.window.search.set_text('no match')
        self.pump(lambda: self.window.count.get_text() == '0 entries')
        self.assertFalse(self.window.copy_button.get_sensitive())
        self.window.close()
        self.pump(lambda: self.window.closed)
        self.assertTrue(self.backend.closed)
        self.assertIsNone(self.window.selected)

    def test_identical_formats_merge_into_one_button(self):
        self.wait_for_history()
        with contextlib.closing(cv.database(self.backend.state, 1024 * 1024)) as db:
            item = event('同一の中身', 1788662000000)
            item['formats'] += [{'mime': mime, 'data': item['formats'][0]['data']}
                                for mime in ('STRING', 'TEXT')]
            cv.append(db, item)
        self.window.search.set_text('同一の中身')
        self.pump(lambda: self.window.count.get_text() == '1 entry' and self.window.copy_button.get_sensitive())
        chips, index = [], 0
        while (chip := self.window.formats_view.get_row_at_index(index)) is not None:
            mime = chip.get_child().get_first_child()
            chips.append((mime.get_text(), mime.get_next_sibling().get_text()))
            index += 1
        self.assertEqual(chips, [('text/plain;charset=utf-8', '0.0 KiB · same as STRING, TEXT')])
        self.assertEqual(self.window.format_index(), 0)
        # The merged button restores the most portable of the identical formats.
        self.window.restore()
        self.pump(lambda: bool(self.copied))
        self.assertEqual(self.copied[0]['mime'], 'text/plain;charset=utf-8')

    def test_binary_entry_previews_as_a_hex_dump(self):
        self.wait_for_history()
        with contextlib.closing(cv.database(self.backend.state, 1024 * 1024)) as db:
            item = event('', 1788662000000)
            item['formats'] = [{'mime': 'application/octet-stream',
                                'data': base64.b64encode(b'\x00\x01clipvault' * 8).decode()}]
            cv.append(db, item)
        self.window.load_history()
        self.pump(lambda: self.window.count.get_text() == '4 entries' and self.window.copy_button.get_sensitive())
        self.assertEqual(self.window.preview.get_visible_child_name(), 'text')
        buffer = self.window.text.get_buffer()
        dump = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.assertTrue(dump.startswith('00000000: 0001 636c 6970 7661 756c 7400 0163 6c69  ..clipvault..cli'))

    def test_image_preview_and_screenshot(self):
        self.wait_for_history()
        screenshot = os.environ.get('CLIPVAULT_SCREENSHOT')
        if screenshot:
            # Snapshot only synthetic test data, never the real user's history.
            self.pump(lambda: self.window.get_width() > 0)
            frame_ready = time.monotonic() + 0.25
            self.pump(lambda: time.monotonic() >= frame_ready)
            from gi.repository import Gsk, Graphene
            snapshot = Gtk.Snapshot.new()
            Gtk.WidgetPaintable.new(self.window).snapshot(snapshot, self.window.get_width(), self.window.get_height())
            node = snapshot.to_node()
            renderer = Gsk.Renderer.new_for_surface(self.window.get_surface())
            try:
                bounds = Graphene.Rect().init(0, 0, self.window.get_width(), self.window.get_height())
                renderer.render_texture(node, bounds).save_to_png(screenshot)
            finally:
                renderer.unrealize()
        self.window.rows.select_row(self.window.rows.get_row_at_index(2))
        self.pump(lambda: self.window.copy_button.get_sensitive())
        self.assertEqual(self.window.preview.get_visible_child_name(), 'image')
        self.window.restore()
        self.pump(lambda: bool(self.copied))
        self.assertEqual(base64.b64decode(self.copied[0]['data']), PNG)

    def test_authentication_failure_does_not_show_history(self):
        self.window.close()
        self.window.executor.shutdown(wait=True, cancel_futures=True)
        self.backend = LocalBackend(Path(self.temp.name))
        self.backend.request = lambda _: (_ for _ in ()).throw(RuntimeError('Authentication cancelled.'))
        self.window = gui.Window(self.app, self.backend, copier=lambda item: self.copied.append(item))
        self.window.present()
        self.pump(lambda: 'cancelled' in self.window.status.get_text())
        self.assertTrue(self.window.refresh.get_sensitive())
        self.assertFalse(self.window.copy_button.get_sensitive())
        self.assertIsNone(self.window.rows.get_row_at_index(0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
