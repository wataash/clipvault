#!/usr/bin/python3 -I
# SPDX-License-Identifier: Apache-2.0
"""GTK clipboard browser. Archive access uses an authenticated, private pipe."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import resource
import shlex
import subprocess
import threading
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk

logger = logging.getLogger(__name__)
BACKEND = ['/usr/bin/sudo', '-A', '-u', 'clipvault', '/usr/bin/python3', '-I',
           '/usr/local/lib/clipvault/clipvault.py', 'session']
MAX_RESPONSE = 12 * 1024 * 1024
# Identical bytes differ only in the advertised target name; offer the most portable one.
RESTORE_PREFERENCE = ('text/plain;charset=utf-8', 'text/plain', 'UTF8_STRING', 'TEXT', 'STRING')


class Backend:
    def __init__(self):
        self.process = None
        self.closed = False
        self.lock = threading.Lock()

    def request(self, request):
        with self.lock:
            if self.closed:
                raise RuntimeError('The history session is closed.')
            if self.process is None:
                env = os.environ.copy()
                env['SUDO_ASKPASS'] = os.path.realpath(__file__)
                env['CLIPVAULT_ASKPASS'] = '1'
                logger.info('%s', shlex.join(BACKEND))
                self.process = subprocess.Popen(BACKEND, stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            process = self.process
        try:
            payload = json.dumps(request).encode() + b'\n'
            if len(payload) > 16384:
                raise RuntimeError('The search term is too long.')
            process.stdin.write(payload)
            process.stdin.flush()
            line = process.stdout.readline(MAX_RESPONSE + 1)
            if not line:
                raise RuntimeError('Authentication was cancelled, or the history could not be opened. Check the installation.')
            if len(line) > MAX_RESPONSE or not line.endswith(b'\n'):
                raise RuntimeError('The response exceeded the size limit.')
            response = json.loads(line)
            if not response.get('ok'):
                raise RuntimeError(response.get('error', 'Could not read the history.'))
            return response['result']
        except (BrokenPipeError, OSError, ValueError) as error:
            raise RuntimeError('Lost the connection to the history. Close this window and open it again.') from error

    def close(self):
        with self.lock:
            self.closed = True
            process = self.process
        if process is not None:
            # Closing the only request pipe ends the authenticated session.
            try:
                process.stdin.close()
            except OSError:
                pass
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            threading.Thread(target=process.wait, daemon=True).start()


def copy_item(item):
    command = ['/usr/bin/wl-copy', '--type', item['mime']]
    logger.info('%s', shlex.join(command))
    subprocess.run(command, input=base64.b64decode(item['data'], validate=True),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)


def label(text='', css=None, **kwargs):
    widget = Gtk.Label(label=text, xalign=0, **kwargs)
    if css:
        widget.add_css_class(css)
    return widget


def margins(widget, size):
    for edge in ('top', 'bottom', 'start', 'end'):
        getattr(widget, f'set_margin_{edge}')(size)
    return widget


def size_text(size):
    return f'{size / 1048576:.1f} MiB' if size >= 1048576 else f'{size / 1024:.1f} KiB'


def hex_dump(raw, width=16):
    """xxd-style dump for formats without a text or image preview."""
    lines = []
    for offset in range(0, len(raw), width):
        chunk = raw[offset:offset + width]
        groups = ' '.join(chunk[i:i + 2].hex() for i in range(0, len(chunk), 2))
        printable = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{offset:08x}: {groups:<{width * 5 // 2 - 1}}  {printable}')
    return '\n'.join(lines)


def merge_formats(formats):
    """One entry per stored blob; formats holding the same bytes share a button."""
    groups = {}
    for fmt in formats:
        groups.setdefault(fmt.get('same_as', fmt['mime']), []).append(fmt)
    merged = []
    for group in groups.values():
        mimes = [fmt['mime'] for fmt in group]
        mime = sorted(mimes, key=lambda m: RESTORE_PREFERENCE.index(m)
                      if m in RESTORE_PREFERENCE else len(RESTORE_PREFERENCE))[0]
        merged.append({'mime': mime, 'size': group[0]['size'],
                       'aliases': [m for m in mimes if m != mime]})
    return merged


class Window(Gtk.ApplicationWindow):
    def __init__(self, application, backend=None, copier=copy_item):
        super().__init__(application=application, title='ClipVault', default_width=1050, default_height=700)
        self.backend = backend or Backend()
        self.copier = copier
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='clipvault')
        self.closed = False
        self.generation = 0
        self.preview_generation = 0
        self.search_timer = 0
        self.selected = None
        self.formats = []
        self.loading_formats = False

        header = Gtk.HeaderBar()
        header.set_title_widget(label('ClipVault', 'title'))
        self.set_titlebar(header)

        pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=350, wide_handle=True)
        pane.set_resize_start_child(False)
        pane.set_shrink_start_child(False)
        pane.set_shrink_end_child(False)
        self.set_child(pane)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=280)
        self.search = Gtk.SearchEntry(placeholder_text='Search history')
        self.search.connect('search-changed', self.search_changed)
        sidebar.append(margins(self.search, 12))
        list_header = Gtk.Box(spacing=6)
        list_header.set_margin_start(16)
        list_header.set_margin_end(12)
        self.count = label('Recent', 'dim-label', hexpand=True)
        list_header.append(self.count)
        self.refresh = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text='Reload')
        self.refresh.add_css_class('flat')
        self.refresh.connect('clicked', lambda *_: self.load_history())
        list_header.append(self.refresh)
        sidebar.append(list_header)
        self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.rows.add_css_class('navigation-sidebar')
        self.rows.connect('row-selected', self.row_selected)
        self.placeholder = margins(label('Loading…', 'dim-label', halign=Gtk.Align.CENTER, wrap=True), 24)
        self.rows.set_placeholder(self.placeholder)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.rows)
        sidebar.append(scroll)
        pane.set_start_child(sidebar)

        detail = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14), 24)
        self.title_label = label('Select an entry', 'title-2')
        detail.append(self.title_label)
        # Every stored format stays visible; one click switches the preview.
        self.formats_view = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.formats_view.add_css_class('boxed-list')
        self.formats_view.connect('row-selected', self.format_selected)
        formats_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            propagate_natural_height=True, max_content_height=360)
        formats_scroll.set_child(self.formats_view)
        detail.append(formats_scroll)
        self.preview = Gtk.Stack(vexpand=True)
        self.text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 left_margin=16, right_margin=16, top_margin=16, bottom_margin=16)
        text_scroll = Gtk.ScrolledWindow()
        text_scroll.add_css_class('card')
        text_scroll.set_child(self.text)
        self.preview.add_named(text_scroll, 'text')
        self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        self.preview.add_named(self.picture, 'image')
        self.empty = label('Select an entry from the list on the left.', 'dim-label', halign=Gtk.Align.CENTER, wrap=True)
        self.preview.add_named(self.empty, 'empty')
        self.preview.set_visible_child_name('empty')
        detail.append(self.preview)
        self.status = label('', wrap=True, selectable=True)
        detail.append(self.status)
        actions = Gtk.Box(spacing=12)
        self.delete_button = Gtk.Button(label='Delete')
        self.delete_button.add_css_class('destructive-action')
        self.delete_button.set_sensitive(False)
        self.delete_button.connect('clicked', self.confirm_delete)
        actions.append(self.delete_button)
        actions.append(Gtk.Box(hexpand=True))
        self.copy_button = Gtk.Button(label='Restore to clipboard')
        self.copy_button.add_css_class('suggested-action')
        self.copy_button.set_sensitive(False)
        self.copy_button.connect('clicked', self.restore)
        actions.append(self.copy_button)
        detail.append(actions)
        pane.set_end_child(detail)
        self.connect('close-request', self.on_close)
        self.startup_handler = self.connect('map', self.start)

    def start(self, *_):
        self.disconnect(self.startup_handler)
        self.load_history()

    def task(self, work, success, failure=None):
        if self.closed:
            return
        future = self.executor.submit(work)

        def completed(result):
            def deliver():
                if self.closed:
                    return False
                try:
                    value = result.result()
                except Exception as error:
                    if failure:
                        failure(str(error))
                    else:
                        self.status.set_text(str(error))
                else:
                    success(value)
                return False
            GLib.idle_add(deliver)
        future.add_done_callback(completed)

    def search_changed(self, *_):
        if self.search_timer:
            GLib.source_remove(self.search_timer)
        def run():
            self.search_timer = 0
            if self.search.get_sensitive():
                self.load_history()
            return False
        self.search_timer = GLib.timeout_add(250, run)

    def load_history(self):
        self.generation += 1
        generation = self.generation
        backend = self.backend
        query = self.search.get_text()[:256]
        self.refresh.set_sensitive(False)
        self.search.set_sensitive(False)
        self.placeholder.set_text('Loading…')
        self.status.set_text('Loading history…')
        def loaded(items):
            if generation != self.generation:
                return
            self.refresh.set_sensitive(True)
            self.search.set_sensitive(True)
            self.placeholder.set_text('No entries')
            self.rows.remove_all()
            self.clear_preview()
            self.count.set_text(f'{len(items)} entr{"y" if len(items) == 1 else "ies"}'
                                + (' · newest 100' if len(items) == 100 else ''))
            for item in items:
                row = Gtk.ListBoxRow()
                row.item = item
                box = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6), 12)
                text = item['summary'] or (item['formats'][0]['mime'] if item['formats'] else 'copy that could not be captured')
                box.append(label(text[:100], wrap=True, max_width_chars=30, lines=2, ellipsize=3))
                stamp = time.strftime('%m/%d %H:%M', time.localtime(item['copied_ms'] / 1000))
                box.append(label(f'{stamp}  ·  {len(item["formats"])} format{"" if len(item["formats"]) == 1 else "s"}', 'dim-label'))
                row.set_child(box)
                self.rows.append(row)
            if items:
                self.rows.select_row(self.rows.get_row_at_index(0))
        def failed(message):
            if generation != self.generation:
                return
            self.refresh.set_sensitive(True)
            backend.close()
            self.backend = Backend()
            self.rows.remove_all()
            self.clear_preview()
            self.count.set_text('')
            self.placeholder.set_text('Use the reload button to try again.')
            self.status.set_text(message)
        self.task(lambda: backend.request({'op': 'list', 'query': query}), loaded, failed)

    def clear_preview(self):
        self.preview_generation += 1
        self.selected = None
        self.formats = []
        self.text.get_buffer().set_text('')
        self.picture.set_paintable(None)
        self.formats_view.remove_all()
        self.copy_button.set_sensitive(False)
        self.delete_button.set_sensitive(False)
        self.status.set_text('')
        self.title_label.set_text('Select an entry')
        self.preview.set_visible_child_name('empty')

    def row_selected(self, _box, row):
        self.clear_preview()
        if row is None:
            return
        self.selected = row.item
        self.formats = merge_formats(row.item['formats'])
        self.title_label.set_text(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row.item['copied_ms'] / 1000)))
        self.delete_button.set_sensitive(True)
        self.loading_formats = True
        for fmt in self.formats:
            meta = size_text(fmt['size'])
            if fmt['aliases']:
                meta += ' · same as ' + ', '.join(fmt['aliases'])
            box = margins(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2), 8)
            box.append(label(fmt['mime'], ellipsize=3))
            box.append(label(meta, 'dim-label', ellipsize=3))
            chip = Gtk.ListBoxRow()
            chip.set_child(box)
            self.formats_view.append(chip)
        if self.formats:
            default = next((i for i, f in enumerate(self.formats) if f['mime'].startswith('text/plain')), 0)
            self.formats_view.select_row(self.formats_view.get_row_at_index(default))
        self.loading_formats = False
        self.format_selected()

    def format_index(self):
        row = self.formats_view.get_selected_row()
        return row.get_index() if row else None

    def format_selected(self, *_):
        if self.loading_formats or not self.selected or not self.formats:
            return
        index = self.format_index()
        if index is None or index >= len(self.formats):
            return
        self.preview_generation += 1
        generation = self.preview_generation
        item = self.selected
        mime = self.formats[index]['mime']
        backend = self.backend
        self.copy_button.set_sensitive(False)
        self.picture.set_paintable(None)
        self.text.get_buffer().set_text('')
        self.empty.set_text('Loading…')
        self.preview.set_visible_child_name('empty')
        self.status.set_text('')
        def show(result):
            if generation != self.preview_generation:
                return
            raw = base64.b64decode(result['data'], validate=True)
            note = 'Some formats could not be captured at copy time.' if item['errors'] else ''
            if mime.startswith('text/') or mime in ('UTF8_STRING', 'TEXT', 'STRING', 'COMPOUND_TEXT'):
                # Gtk text is plain text, never HTML or Pango markup.
                self.text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                self.text.get_buffer().set_text(raw.decode('utf-8', errors='replace').replace('\x00', '␀'))
                self.preview.set_visible_child_name('text')
                if result['truncated']:
                    note += ' The preview shows the first 64 KiB; restoring uses the whole content.'
            elif mime.startswith('image/'):
                try:
                    loader = GdkPixbuf.PixbufLoader.new()
                    def resize(obj, width, height):
                        scale = min(1, 1200 / max(width, height))
                        obj.set_size(max(1, int(width * scale)), max(1, int(height * scale)))
                    loader.connect('size-prepared', resize)
                    loader.write(raw)
                    loader.close()
                    self.picture.set_paintable(Gdk.Texture.new_for_pixbuf(loader.get_pixbuf()))
                    self.preview.set_visible_child_name('image')
                except GLib.Error:
                    note += ' This image format cannot be displayed, so a hex dump is shown.'
                    self.show_hex(raw)
            else:
                self.show_hex(raw)
                if result['truncated']:
                    note += ' The dump shows the first 4 KiB; restoring uses the whole content.'
            self.status.set_text(note)
            self.copy_button.set_sensitive(True)
        def failed(message):
            if generation == self.preview_generation:
                self.empty.set_text('Could not load the preview.')
                self.status.set_text(message)
        self.task(lambda: backend.request({'op': 'preview', 'id': item['id'], 'mime': mime}), show, failed)

    def show_hex(self, raw):
        # Fixed-width rows must not wrap, whatever the window size.
        self.text.set_wrap_mode(Gtk.WrapMode.NONE)
        self.text.get_buffer().set_text(hex_dump(raw))
        self.preview.set_visible_child_name('text')

    def restore(self, *_):
        index = self.format_index()
        if not self.selected or index is None:
            return
        request = {'op': 'get', 'id': self.selected['id'], 'mime': self.formats[index]['mime']}
        backend = self.backend
        self.copy_button.set_sensitive(False)
        self.status.set_text('Restoring…')
        generation = self.preview_generation
        def work():
            self.copier(backend.request(request))
        def done(_):
            if generation == self.preview_generation:
                self.copy_button.set_sensitive(True)
                self.status.set_text('Restored to the clipboard. Paste it in the target application.')
        def failed(_message):
            if generation == self.preview_generation:
                self.copy_button.set_sensitive(True)
                self.status.set_text('Could not restore. Check the connection to the clipboard.')
        self.task(work, done, failed)

    def confirm_delete(self, *_):
        if not self.selected:
            return
        item = self.selected
        dialog = Gtk.AlertDialog(message='Delete this entry?', detail='A deleted entry cannot be recovered.',
                                 buttons=['Cancel', 'Delete'], cancel_button=0, default_button=0)
        def chosen(obj, result):
            try:
                if obj.choose_finish(result) != 1:
                    return
            except GLib.Error:
                return
            backend = self.backend
            self.task(lambda: backend.request({'op': 'delete', 'id': item['id']}), lambda _: self.load_history())
        dialog.choose(self, None, chosen)

    def on_close(self, *_):
        self.closed = True
        if self.search_timer:
            GLib.source_remove(self.search_timer)
        self.backend.close()
        self.rows.remove_all()
        self.clear_preview()
        self.executor.shutdown(wait=False, cancel_futures=True)
        return False


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='io.github.wataash.ClipVault', flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            window = Window(self)
        window.present()


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if os.environ.get('CLIPVAULT_ASKPASS') == '1':
        command = ['/usr/bin/zenity', '--password', '--title=ClipVault', '--text=sudo authentication to open the history']
        logger.info('%s', shlex.join(command))
        return subprocess.run(command, check=False).returncode
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-n', '--dry_run', action='store_true')
    args = parser.parse_args()
    if args.dry_run:
        print('SUDO_ASKPASS=' + shlex.quote(os.path.realpath(__file__)) + ' CLIPVAULT_ASKPASS=1 ' + shlex.join(BACKEND))
        print('/usr/bin/wl-copy --type MIME < SELECTED_DATA_FROM_PIPE')
        return 0
    return Application().run([])


if __name__ == '__main__':
    raise SystemExit(main())
