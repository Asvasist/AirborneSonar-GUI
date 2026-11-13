import io, sys, unittest, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import numpy as np
from sonar_wav_format import chirp_wav


def samples(data):
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 96000)
        return np.frombuffer(w.readframes(w.getnframes()), "<i2")


class ChirpWav(unittest.TestCase):
    def test_matches_pl_lut_at_quarter_rate(self):
        # 12 kHz = phase step 2^29: LUT[0], LUT[32], LUT[63-0], LUT[63-32], ... scaled by 32767 >> 15 (floor).
        s = samples(chirp_wav(12000, 12000, 8, 100))
        self.assertEqual(s.tolist(), [0, 23169, 32756, 22593, 0, -23170, -32757, -22594])

    def test_sweep_and_length(self):
        s = samples(chirp_wav(2500, 5600, 3360, 40))
        self.assertEqual(len(s), 3360)
        self.assertLessEqual(abs(int(s.max())), (40 * 32767 + 50) // 100)
        zc = lambda x: np.count_nonzero(np.diff(np.signbit(x)))
        self.assertGreater(zc(s[-500:]), 1.8 * zc(s[:500]))  # ~2.2x frequency at the end


if __name__ == "__main__":
    unittest.main()
