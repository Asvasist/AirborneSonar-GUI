"""Fixed 96 kHz playback and microphone PCM constants."""
TX_SAMPLE_HZ = 96000
RATES = (TX_SAMPLE_HZ,)
MAX_TX_US = 49000
RX_PCM_HZ = 96000


def rate_hz(value):
    text = str(value).strip()
    if not text.isascii() or not text.isdecimal() or int(text) not in RATES:
        raise ValueError("Playback is fixed at 96,000 Hz.")
    return int(text)


def max_tx_samples(hz, bram_capacity=None):
    n = rate_hz(hz) * MAX_TX_US // 1000000
    return n if bram_capacity is None else min(n, bram_capacity)

