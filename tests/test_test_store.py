"""No hardware: verify idle storage, configuration provenance and test boundaries."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"host"))
from sonar_test_store import TestCaptureStore
from capture_store import write_json
from receive_ethernet import receive_record
from test_ethernet import Fragmented, record


def config(folder, config_id=1, mode=0):
    configs=Path(folder)/"configurations"
    configs.mkdir(parents=True,exist_ok=True)
    m=dict(configuration=dict(config_id=config_id,mode=mode,duration_us=35000,
        start_hz=8000,stop_hz=12000,amplitude_pct=40,sample_hz=96000))
    if mode:
        m.update(source_copy='source.wav',playback_copy='playback.wav')
        (configs/'source.wav').write_bytes(b'original source')
        (configs/'playback.wav').write_bytes(b'converted playback')
    write_json(configs/f'config_{config_id:05d}.json',m)
    return m


def meta(seq=1, stamp=100, mode=0):
    return dict(sequence=seq,cycle=seq,trigger_request_us=stamp,channels=16,
                config_id=1,tx_mode='WAV' if mode else 'GENERATE',tx_duration_us=35000)


class TestStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cache=self.root/'cache'
        config(self.cache)
        self.output=self.root/'recordings'
        self.events=[]
        self.store=TestCaptureStore(self.output,lambda *e:self.events.append(e))

    def test_idle_heartbeats_ack_without_any_recording_directory(self):
        for stamp in [30_000_000,60_000_000,90_000_000]:
            sock=Fragmented(record(b'',0,stamp))
            path,_=receive_record(sock,self.store)
            self.assertIsNone(path)
            self.assertEqual(len(sock.sent),12)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.events)

    def test_start_reserves_but_creates_nothing_until_valid_capture(self):
        folder=self.store.begin_test(1,3,self.cache,1)
        self.assertFalse(folder.exists())
        self.store.save(meta(0,30_000_000),b'')
        self.assertFalse(folder.exists())
        path=self.store.save(meta(),bytes(4))
        self.assertEqual(path.parents[1],folder)
        saved=json.loads(path.with_suffix('.json').read_text())
        self.assertEqual(saved['configuration_status'],'matched')
        self.assertEqual(saved['transmit_configuration']['configuration']['start_hz'],8000)
        self.assertEqual(saved['board_batch_release_us'],30_000_000)
        self.assertTrue((folder/'test.json').is_file())
        self.assertEqual(self.events,[('folder',str(folder))])

    def test_bad_checksum_or_malformed_wav_creates_no_folder_or_ack(self):
        folder=self.store.begin_test(1,3,self.cache,1)
        sock=Fragmented(record()[:-1]+b'\xff')
        with self.assertRaises(ValueError):receive_record(sock,self.store)
        self.assertFalse(sock.sent)
        with self.assertRaises(ValueError):self.store.save(dict(meta(),flags=0x100),b'not a wave')
        self.assertFalse(folder.exists())

    def test_successive_tests_keep_late_and_retried_records_in_original_test(self):
        first=self.store.begin_test(1,3,self.cache,1)
        a=self.store.save(meta(),bytes(4))
        second=self.store.begin_test(4,-3,self.cache,1)
        late=self.store.save(meta(2,200),bytes(4))
        b=self.store.save(meta(4,400),bytes(4))
        retry=self.store.save(meta(),bytes(4))
        self.assertNotEqual(first,second)
        self.assertEqual(a.parents[1],first)
        self.assertEqual(late.parents[1],first)
        self.assertEqual(b.parents[1],second)
        self.assertEqual(a,retry)
        self.assertEqual(len(list(first.rglob('capture_*.bin'))),2)

    def test_stopped_run_is_finalized_once_and_next_start_is_separate(self):
        first=self.store.begin_test(1,30,self.cache,1)
        self.store.save(meta(),bytes(4))
        self.store.request_stop()
        self.store.finish_test(1)
        data=json.loads((first/'test.json').read_text())
        self.assertEqual(data['status'],'stopped')
        self.assertEqual(data['saved_captures'],1)
        original=(first/'test.json').read_bytes()
        self.store.finish_test(1)
        self.assertEqual((first/'test.json').read_bytes(),original)
        second=self.store.begin_test(2,3,self.cache,1)
        self.assertEqual(self.store.save(meta(2,200),bytes(4)).parents[1],second)

    def test_snapshot_preserves_original_wav_assets_after_configuration_changes(self):
        config(self.cache,mode=1)
        folder=self.store.begin_test(1,3,self.cache,1)
        (self.cache/'configurations'/'playback.wav').write_bytes(b'changed later')
        self.store.save(meta(mode=1),bytes(4))
        self.assertEqual((folder/'configurations'/'playback.wav').read_bytes(),b'converted playback')
        self.assertEqual((folder/'configurations'/'source.wav').read_bytes(),b'original source')

    def test_recovered_frames_are_saved_without_being_assigned_to_next_start(self):
        path,_=receive_record(Fragmented(record()),self.store)
        self.assertTrue(path.parents[1].name.startswith('recovered_'))
        next_folder=self.store.begin_test(2,3,self.cache,1)
        self.assertFalse(next_folder.exists())
        self.assertEqual(self.store.save(meta(2,200),bytes(4)).parents[1],next_folder)

    def test_cancelled_start_can_be_replaced_without_empty_folder(self):
        first=self.store.begin_test(1,3,self.cache,1)
        second=self.store.begin_test(1,3,self.cache,1)
        self.assertFalse(first.exists())
        self.assertEqual(self.store.save(meta(),bytes(4)).parents[1],second)

    def test_board_reset_does_not_overwrite_previous_test(self):
        first=self.store.begin_test(1,3,self.cache,1)
        path=self.store.save(meta(),bytes(4))
        self.store.reset_board()
        second=self.store.begin_test(1,3,self.cache,1)
        new=self.store.save(meta(),b'\x01'*4)
        self.assertNotEqual(first,second)
        self.assertEqual(path.read_bytes(),bytes(4))
        self.assertEqual(new.read_bytes(),b'\x01'*4)

if __name__=='__main__':unittest.main()
