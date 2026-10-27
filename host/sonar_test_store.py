"""Durable routing by (board boot UUID, test UUID), never sequence ranges."""
import base64
from datetime import datetime
import json
from pathlib import Path
import re
import struct
import threading
import uuid
from capture_store import CaptureStore, utc_now, write_atomic, write_json
from sonar_audio import is_wav, read_pcm_wav

def identity(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value) or value == '0' * 32:
        raise ValueError('A nonzero 128-bit hexadecimal identity is required.')
    return value

def asset_name(value):
    if not value or value in ('.', '..'):
        raise ValueError('Invalid recording asset filename.')
    if not isinstance(value, str) or Path(value).name != value or '/' in value or ('\\' in value):
        raise ValueError('Invalid recording asset filename.')
    return value

class TestCaptureStore:

    def __init__(self, output, emit=lambda *_: None):
        self.output = Path(output).resolve()
        self.emit = emit
        self.lock = threading.RLock()
        self.index = self.output / '.sonar_recording_index.json'
        self.runs = {}
        self.active_key = None
        self.batch_us = None
        self.connection = None
        if self.index.exists():
            document = json.loads(self.index.read_text(encoding='utf-8'))
            if document.get('version') != 1:
                raise ValueError('Unknown recording index version.')
            for entry in document['tests']:
                key = (identity(entry['boot_id']), identity(entry['test_id']))
                folder = (self.output / asset_name(entry['folder'])).resolve()
                if not folder.is_relative_to(self.output):
                    raise ValueError('Recording index destination is outside the output directory.')
                if key in self.runs:
                    raise ValueError('Duplicate recording identity in durable index.')
                self.runs[key] = dict(folder=folder, bundle={asset_name(n): base64.b64decode(v, validate=True) for n, v in entry['bundle'].items()}, metadata=entry['metadata'], store=None, notified=False)

    def _persist_index(self):
        """Persist routing and configuration snapshots before the board can start."""
        self.output.mkdir(parents=True, exist_ok=True)
        entries = [dict(boot_id=k[0], test_id=k[1], folder=r['folder'].name, bundle={n: base64.b64encode(data).decode('ascii') for n, data in r['bundle'].items()}, metadata=r['metadata']) for k, r in self.runs.items()]
        write_json(self.index, dict(version=1, tests=entries))

    def set_connection(self, info):
        with self.lock:
            self.connection = dict(info)

    def begin_test(self, first_sequence, positions, configuration_folder, config_id, *, boot_id, test_id):
        key = (identity(boot_id), identity(test_id))
        if first_sequence < 1 or positions == 0:
            raise ValueError('Invalid test sequence or capture count.')
        source = Path(configuration_folder) / 'configurations'
        name = f'config_{config_id:05d}.json'
        data = (source / name).read_bytes()
        manifest = json.loads(data.decode('utf-8'))
        c = manifest['configuration']
        if c['config_id'] != config_id or c.get('boot_id') != boot_id:
            raise ValueError('Cached configuration belongs to a different board boot/configuration.')
        c['test_id'] = test_id
        for i, word in enumerate(struct.unpack('<4I', bytes.fromhex(test_id))):
            c[f'test_word_{i}'] = word
        manifest['recording_identity'] = dict(boot_id=boot_id, test_id=test_id)
        data = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
        bundle = {name: data}
        for field in ('source_copy', 'playback_copy'):
            if manifest.get(field):
                asset = asset_name(manifest[field])
                bundle[asset] = (source / asset).read_bytes()
        with self.lock:
            if key in self.runs:
                raise ValueError('Test identity already exists; prepare a fresh test before Start.')
            folder = self.output / (datetime.now().strftime('test_%Y%m%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:8])
            self.runs[key] = dict(folder=folder, bundle=bundle, store=None, notified=False, metadata=dict(boot_id=boot_id, test_id=test_id, start_requested_utc=utc_now(), first_sequence=first_sequence, requested_positions=positions, config_id=config_id, status='recording', board=self.connection))
            try:
                self._persist_index()
            except OSError:
                del self.runs[key]
                raise
            self.active_key = key
            return folder

    def request_stop(self):
        with self.lock:
            if self.active_key is not None:
                self.runs[self.active_key]['metadata']['stop_requested'] = True

    def finish_test(self, last_sequence):
        with self.lock:
            if self.active_key is None:
                return
            run = self.runs[self.active_key]
            if run['metadata']['status'] != 'recording':
                return
            count = len(list(run['folder'].glob('second_*/capture_*.json')))
            run['metadata'].update(finished_utc=utc_now(), last_sequence=last_sequence, saved_captures=count, status='stopped' if run['metadata'].get('stop_requested') or count < abs(run['metadata']['requested_positions']) else 'complete')
            self._persist_index()
            if run['store'] is not None:
                write_json(run['folder'] / 'test.json', run['metadata'])

    def reset_board(self):
        """Retain historical routes: a reset must never erase recording identity."""
        with self.lock:
            self.active_key = None

    def save(self, meta, payload):
        """Route by explicit packet IDs; unknown IDs never borrow active settings."""
        with self.lock:
            if not payload:
                self.batch_us = meta['trigger_request_us']
                return None
            if is_wav(payload, meta):
                read_pcm_wav(payload, meta.get('channels', 16))
            key = (identity(meta.get('boot_id')), identity(meta.get('test_id')))
            run = self.runs.get(key)
            if run is None:
                folder = self.output / (datetime.now().strftime('recovered_%Y%m%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:8])
                run = dict(folder=folder, bundle={}, store=None, notified=False, metadata=dict(boot_id=key[0], test_id=key[1], status='recovered', received_utc=utc_now(), board=self.connection))
                self.runs[key] = run
                try:
                    self._persist_index()
                except OSError:
                    del self.runs[key]
                    raise
            if run['store'] is None:
                folder = run['folder']
                folder.mkdir(parents=True, exist_ok=True)
                if run['bundle']:
                    configs = folder / 'configurations'
                    configs.mkdir(exist_ok=True)
                    for name, data in run['bundle'].items():
                        write_atomic(configs / name, data)
                write_json(folder / 'test.json', run['metadata'])
                run['store'] = CaptureStore(folder)
            run['store'].batch_us = self.batch_us
            path = run['store'].save(meta, payload)
            notify = not run['notified'] and (self.active_key is None or key == self.active_key)
            run['notified'] = True
        if notify:
            self.emit('folder', str(run['folder']))
        return path
