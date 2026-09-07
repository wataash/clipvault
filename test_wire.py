#!/usr/bin/python3
"""Exercise actual GJS -> UNIX socket -> SQLite, without a desktop or root."""
import argparse
import logging
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
import threading
import unittest

import clipvault as cv

logger = logging.getLogger(__name__)


class WireTest(unittest.TestCase):
    def test_gjs_append_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            path = str(state / 'append.sock')
            errors = []
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(path)
                listener.listen(2)
                listener.settimeout(15)

                def server():
                    try:
                        with cv.database(state, 1024 * 1024) as db:
                            for _ in range(2):
                                conn, _ = listener.accept()
                                with conn:
                                    cv.handle(conn, db, os.getuid())
                    except Exception as error:
                        errors.append(error)

                worker = threading.Thread(target=server)
                worker.start()
                command = ['/usr/bin/gjs', '-m', str(Path(__file__).with_name('test_send.js')), path]
                logger.info('%s', shlex.join(command))
                try:
                    process = subprocess.run(command, capture_output=True, timeout=20)
                finally:
                    worker.join(timeout=16)
                self.assertFalse(errors, errors)
                self.assertEqual(process.returncode, 0, process.stderr.decode())
            with cv.database(state, 1024 * 1024) as db:
                self.assertEqual(len(cv.search(db, '')), 1)
                self.assertEqual(cv.get_formats(db, cv.search(db, '')[0][0])[0][1], 'wire 日本語\n\x00'.encode())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--dry_run', action='store_true')
    args = parser.parse_args()
    if args.dry_run:
        print('/usr/bin/gjs -m ' + shlex.quote(str(Path(__file__).with_name('test_send.js'))) + ' TEST_SOCKET')
    else:
        unittest.main(argv=[__file__], verbosity=2)
