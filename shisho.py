#!/usr/bin/python3

import argparse
import logging
import os
import socket
import sqlite3
import sys
from enum import Enum
from getpass import getpass
from pathlib import Path
from subprocess import check_output, CalledProcessError
from time import time, sleep


CGRAY = '\x1b[90m'
CBRED = '\x1b[91m'
CBGREEN = '\x1b[92m'
CBBLUE = '\x1b[94m'
CRESET = '\x1b[0m'


class SocketState(Enum):
    IDLE = 1
    SENT = 2

class SocketNotReadyException(Exception):
    pass

class APINotLoggedInException(Exception):
    pass


class AniDBAPI:
    def __init__(self, prompt_login):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(('0.0.0.0', 9999))
        self._socket.settimeout(10)
        self._socket_state = SocketState.IDLE
        self._api_command = None
        self._session_id = None
        self._last_message = 0

        self._load_database(prompt_login)

    def _login(self):
        logging.info('Logging into the API')
        user = self._db_cursor.execute('SELECT value FROM meta WHERE name = "user"').fetchone()
        pass_ = self._db_cursor.execute('SELECT value FROM meta WHERE name = "pass"').fetchone()
        if user is None or pass_ is None:
            logging.error('Login info missing from database')
            self._force_quit()

        self._send('AUTH', {
            'user': user[0],
            'pass': pass_[0],
            'protover': '3',
            'client': 'anidbrenamepy',
            'clientver': '2',
            'enc': 'UTF-8'
        })
        if not self._handle_response():
            self._force_quit()

    def logout(self):
        self._send('LOGOUT')
        self._handle_response()
        self._close_database()

    def _force_quit(self):
        self._close_database()
        sys.exit(1)

    def _store_login_info(self):
        self._db_cursor.execute('INSERT OR REPLACE INTO meta VALUES ("user", ?)', (input('AniDB username: '),))
        self._db_cursor.execute('INSERT OR REPLACE INTO meta VALUES ("pass", ?)', (getpass('AniDB password: '),))

    def _load_database(self, prompt_login):
        if 'XDG_DATA_HOME' in os.environ:
            data_path = Path(os.environ['XDG_DATA_HOME']) / 'shisho'
        else:
            data_path = Path('~/.local/share/shisho').expanduser()

        if not data_path.exists():
            data_path.mkdir(parents=True)

        database_path = data_path / 'db.sqlite3'
        db_exists = database_path.exists()
        self._db_connection = sqlite3.connect(database_path)
        self._db_cursor = self._db_connection.cursor()

        # structure needs to be created
        if not db_exists:
            print('Creating new database')
            self._db_cursor.execute('CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT)')
            self._db_cursor.execute('''
                CREATE TABLE file_cache (
                    ed2k TEXT PRIMARY KEY,
                    anime_name TEXT,
                    episode_number TEXT,
                    episode_name TEXT,
                    group_name TEXT
                )
            ''')
            self._store_login_info()
            self._db_connection.commit()

        # user wants to re-enter their login info
        elif prompt_login:
            self._store_login_info()
            self._db_connection.commit()

    def _close_database(self):
        self._db_cursor.close()
        self._db_connection.close()

    def _pack_tags(self, tags):
        if tags is None:
            return ''
        return ' ' + '&'.join([ f'{name}={value}' for name, value in tags.items() ])

    def _send(self, command, tags=None):
        if self._socket_state != SocketState.IDLE:
            self._close_database()
            raise SocketNotReadyException
        if command != 'AUTH' and self._session_id is None:
            self._login()

        last_message_delta = time() - self._last_message
        if last_message_delta < 3:
            sleep(3 - last_message_delta)

        if command != 'AUTH':
            if tags is None:
                tags = {}
            else:
                tags = tags.copy()
            tags['s'] = self._session_id

        logging.info('Sending command %s', command)
        self._socket.sendto(f'{command}{self._pack_tags(tags)}\n'.encode('utf-8'), ('api.anidb.net', 9000))
        self._socket_state = SocketState.SENT
        self._api_command = command
        self._last_message = time()

    def _handle_response(self):
        if self._socket_state != SocketState.SENT or self._api_command is None:
            self._close_database()
            raise SocketNotReadyException

        try:
            response = self._socket.recv(1400).rstrip()
        except TimeoutError:
            return None
        lines = response.decode('utf-8').split('\n')
        ret_code, data = lines[0].split(' ', maxsplit=1)
        logging.info('Got response %s', ret_code)

        result = None
        handle_function = getattr(self, '_handle_' + self._api_command)
        if handle_function is not None:
            result = handle_function(int(ret_code), data.rstrip(), lines[1:])

        self._socket_state = SocketState.IDLE
        self._api_command = None
        return result

    def _handle_generic_error(self, ret_code, data):
        match ret_code:
            case 505:
                logging.error('API: Illegal input or access denied')
            case 555:
                logging.error('API: Banned')
            case 598:
                logging.error('API: Unknown command')
            case 600:
                logging.error('API error')
            case 601 | 602 | 604:
                logging.error('API busy')
            case _:
                return False
        return True

    def _handle_AUTH(self, ret_code, data, _):
        if self._handle_generic_error(ret_code, data):
            return False

        result = False
        match (ret_code, *(data.split(' '))):
            case (200, session_id, *_) | (201, session_id, *_):
                self._session_id = session_id
                logging.info('Successfully logged into the API')
                result = True
            case (500, *_):
                logging.error('API: Login failed')
            case (503, *_) | (504, *_):
                logging.error('API: Login failed - outdated or banned client')
            case (505, *_):
                logging.error('API: Login failed - access denied')
            case _:
                logging.error(f'API: Unknown AUTH response {ret_code}; data="{data}"')
        return result

    def _handle_LOGOUT(self, ret_code, data, _):
        self._handle_generic_error(ret_code, data)
        match ret_code:
            case 203:
                logging.info('Successfully logged out')
            case _:
                logging.error(f'API: Error logging out; {ret_code} {data}')

    def _handle_FILE(self, ret_code, data, lines):
        if self._handle_generic_error(ret_code, data):
            return None

        match ret_code:
            case 220:
                anime_name, episode_number, episode_name, group_name = lines[0].split('|', maxsplit=4)[1:]
                return anime_name, episode_number, episode_name, group_name
            case 320:
                logging.error('No file found')
            case 322:
                logging.error('Multiple files found')
        return None

    def get_file_info(self, file_size, ed2k_hash):
        # check if the hash is in our cache already
        cached_data = self._db_cursor.execute('''
            SELECT anime_name, episode_number, episode_name, group_name FROM file_cache WHERE ed2k = ?
        ''', (ed2k_hash,)).fetchone()
        if cached_data is not None:
            return cached_data

        # fetch new data otherwise
        self._send('FILE', {
            'size': file_size,
            'ed2k': ed2k_hash,
            'fmask': '0000000000',
            'amask': '0080C040'
        })
        result = self._handle_response()
        if result is not None:
            anime_name, episode_number, episode_name, group_name = result
            self._db_cursor.execute('INSERT INTO file_cache VALUES (?, ?, ?, ?, ?)', (ed2k_hash, anime_name, episode_number, episode_name, group_name))
            self._db_connection.commit()
        return result


def ed2k(path):
    '''Return the size and ed2k hash for a given file'''
    if not path.exists():
        return None
    try:
        with path.open('rb') as fp:
            res = check_output(( 'ed2k' ), stdin=fp)
            size, hash_ = res.split(b' ', 1)
        return size.decode('ascii'), hash_.decode('ascii')
    except (OSError, CalledProcessError):
        return None

def replace_characters(name):
    '''Replace some characters in file names with better ones'''
    new_name = name.replace('`', "'")  # anidb uses backticks instead of apostrophes
    new_name = new_name.replace('/', '\u2215')  # replace real slashes with unicode "division slash"
    return new_name

def process_file(path, api_client, dry_run):
    print(f'{CGRAY}Hashing{CRESET} {path} {CGRAY}...{CRESET} ', end='', flush=True)
    ed2k_result = ed2k(path)
    if ed2k_result is None:
        print(f'{CBRED}Failed{CRESET}')
        return

    file_size, ed2k_hash = ed2k_result
    print(f'{CBBLUE}Done!{CRESET}')
    logging.info(f'{file_size} bytes; ed2k: {ed2k_hash}')
    print(f'{CGRAY}Querying AniDB...{CRESET} ', end='', flush=True)
    file_info_result = api_client.get_file_info(file_size, ed2k_hash)
    if file_info_result is None:
        print(f'{CBRED}Failed!{CRESET}')
    else:
        print(f'{CBGREEN}Success!{CRESET}')
        anime_name, episode_number, episode_name, group_name = file_info_result
        new_name = replace_characters(f'{anime_name} - {episode_number} - {episode_name} [{group_name}]')
        new_path = path.with_name(new_name + ''.join(path.suffixes))
        if new_path == path:
            print(f'{CBBLUE}No rename necessary{CRESET}')
        else:
            print(f'{CGRAY}{dry_run and "Would rename to" or "Renaming to"}{CRESET} {new_path}')
            if not dry_run:
                if new_path.exists():
                    print(f'{new_path} {CBRED}already exists. Failed to rename.{CRESET}')
                else:
                    path.rename(new_path)
                    print(f'{CBGREEN}Rename successful.{CRESET}')
        print('')


argument_parser = argparse.ArgumentParser('shisho', description='Opinionated AniDB rename utility. Renames files in a non-configurable format if they are known to AniDB.')
argument_parser.add_argument('-d', '--dry-run', action='store_true', help='do not actually rename any files')
argument_parser.add_argument('-v', '--verbose', action='store_true')
argument_parser.add_argument('--prompt-login', action='store_true', help='get prompted for your AniDB login info again')
argument_parser.add_argument('path', nargs='+', type=Path, help='file or folder to rename; not recursive when specifying a folder')
arguments = argument_parser.parse_args()

logging.basicConfig(format='%(message)s', level=arguments.verbose and logging.INFO or logging.WARNING)

files = []
for path in arguments.path:
    if not path.exists():
        logging.error(f'Path {path} does not exist')
        continue

    if path.is_dir():
        logging.info(f'Path {path} is a directory')
        files += [ file for file in path.iterdir() if file.is_file() and not file.is_symlink() ]
    else:
        logging.info(f'Path {path} is a file')
        files.append(path)
files.sort()

print(f'Found {len(files)} file(s)\n')
if len(files) == 0:
    sys.exit(0)

api_client = AniDBAPI(arguments.prompt_login)
try:
    for file in files:
        process_file(file, api_client, arguments.dry_run)
except KeyboardInterrupt:
    print('Aborting')
    pass
api_client.logout()
