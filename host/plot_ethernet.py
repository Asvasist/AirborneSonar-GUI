#!/usr/bin/env python3
"""Plot one of 16 channels from a Stage 2 capture and its JSON sidecar."""
import argparse
import json
from pathlib import Path
from sonar_audio import is_wav, read_pcm_wav


def channel_bits(payload, channel):
    if not 0 <= channel < 16 or len(payload) % 4:
        raise ValueError("Use channel 0..15 and a whole number of 32-bit words")
    # DDR packing: byte 0 rising, byte 1 falling; repeated for next PDM cycle.
    return [((payload[i + (channel // 8)] >> (channel % 8)) & 1)
            for i in range(0, len(payload), 2)]


def capture_waveform(payload, meta, channel=0, decimate=50):
    """Return times in ms, normalized samples, and an axis label."""
    import numpy as np
    if not 0 <= channel < 16:
        raise ValueError("Use channel 0..15")
    if is_wav(payload, meta):
        pcm, channels, rate, frames = read_pcm_wav(payload, meta.get("channels", 16))
        samples = np.frombuffer(pcm, dtype="<i2").reshape(frames, channels)
        waveform = samples[:, channel].astype(float) / 32768.0
        return np.arange(frames) * 1000.0 / rate, waveform, "PCM amplitude (full scale)"
    if decimate < 1:
        raise ValueError("--decimate must be positive")
    bits = np.asarray(channel_bits(payload, channel), dtype=float)
    n = len(bits) // decimate
    if n == 0:
        raise ValueError("Capture too short for chosen decimation")
    # Boxcar density remains a diagnostic view of legacy raw PDM only.
    waveform = bits[:n * decimate].reshape(n, decimate).mean(axis=1) * 2 - 1
    times = (np.arange(n) + .5) * decimate / meta["pdm_hz"] * 1000
    return times, waveform, "PDM density (diagnostic)"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="WAV or legacy raw PDM capture")
    parser.add_argument("--channel", type=int, choices=range(16), default=0)
    parser.add_argument("--decimate", type=int, default=50,
                        help="Legacy raw PDM diagnostic only; WAV is already PCM")
    parser.add_argument("--save", type=Path)
    args = parser.parse_args()
    import matplotlib.pyplot as plt
    try:
        meta = json.loads(args.capture.with_suffix(".json").read_text())
        times, waveform, label = capture_waveform(
            args.capture.read_bytes(), meta, args.channel, args.decimate)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    plt.plot(times, waveform)
    plt.xlabel("Time from capture start (ms)")
    plt.ylabel(label)
    plt.title(f"Cycle {meta['cycle']}, microphone channel {args.channel}, flags 0x{meta['flags']:x}")
    plt.grid(True)
    plt.tight_layout()
    if args.save:
        plt.savefig(args.save)
    else:
        plt.show()


if __name__ == "__main__":
    main()
