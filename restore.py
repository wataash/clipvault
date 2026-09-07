#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Unprivileged restore entry point; secrets travel only through pipes."""
import argparse
import base64
import json
import logging
import shlex
import subprocess

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-n', '--dry_run', action='store_true')
    parser.add_argument('--gui', action='store_true', help='open the graphical history browser')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if args.gui:
        command = ['/usr/local/bin/clipvault-gui']
        if args.dry_run:
            print(shlex.join(command))
            return 0
        logger.info('%s', shlex.join(command))
        return subprocess.run(command, check=False).returncode
    command = ['/usr/bin/sudo', '-u', 'clipvault', '/usr/bin/python3', '-I',
               '/usr/local/lib/clipvault/clipvault.py', 'browse']
    if args.dry_run:
        print(shlex.join(command))
        print('/usr/bin/wl-copy --type MIME < SELECTED_DATA_FROM_PIPE')
        return 0
    logger.info('%s', shlex.join(command))
    result = subprocess.run(command, stdout=subprocess.PIPE, check=False)
    if result.returncode or not result.stdout:
        return result.returncode
    item = json.loads(result.stdout)
    command = ['/usr/bin/wl-copy', '--type', item['mime']]
    logger.info('%s', shlex.join(command))
    return subprocess.run(command, input=base64.b64decode(item['data'], validate=True),
                          check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
