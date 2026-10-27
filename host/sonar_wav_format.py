"""WAV container checks shared by the GUI and command-line upload client."""
import hashlib
import struct
import zlib
from sonar_sample_rates import max_tx_samples, rate_hz

MAX_FILE_BYTES = 65536

def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def inspect_wav(data, *, max_samples=None, sample_hz=None):
    """Validate the firmware's supported WAV container; do not convert samples."""
    if not 44 <= len(data) <= MAX_FILE_BYTES:
        raise ValueError("WAV file must be 44..65,536 bytes, including its header.")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE" or struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
        raise ValueError("Select a complete little-endian RIFF/WAVE file.")
    at, fmt, frames = 12, None, None
    while at < len(data):
        if at + 8 > len(data):
            raise ValueError("Truncated WAV chunk header.")
        name, length = struct.unpack_from("<4sI", data, at)
        start, end = at + 8, at + 8 + length
        at = end + (length & 1)
        if at > len(data):
            raise ValueError("Truncated WAV data or chunk padding.")
        if name == b"fmt ":
            if fmt is not None or length < 16:
                raise ValueError("WAV must contain one valid fmt chunk.")
            encoding, channels, rate, byte_rate, block, bits = struct.unpack_from("<HHIIHH", data, start)
            if encoding == 65534:
                if (length < 40 or struct.unpack_from("<H", data, start + 16)[0] < 22
                        or struct.unpack_from("<H", data, start + 18)[0] != bits
                        or data[start + 28:start + 40] != bytes.fromhex("00001000800000aa00389b71")):
                    raise ValueError("Unsupported WAVE_FORMAT_EXTENSIBLE layout.")
                encoding = struct.unpack_from("<I", data, start + 24)[0]
            if not 8000 <= rate <= 192000:
                raise ValueError("WAV source rate must be 8,000..192,000 Hz.")
            if sample_hz is not None and rate != sample_hz:
                raise ValueError(f"WAV is {rate:,} Hz, but selected TX rate is {sample_hz:,} Hz. Resample the samples first.")
            if (channels not in (1, 2) or encoding not in (1, 3)
                    or bits not in (8, 16, 24, 32) or (encoding == 3 and bits != 32)
                    or block != channels * (bits // 8) or byte_rate != rate * block):
                raise ValueError("Use mono/stereo PCM8/16/24/32 or float32 WAV.")
            fmt = dict(channels=channels, sample_hz=rate, bits=bits, encoding=encoding, block=block)
        elif name == b"data":
            if fmt is None or frames is not None or not length or length % fmt["block"]:
                raise ValueError("WAV requires one complete data chunk after fmt.")
            frames = length // fmt["block"]
            limit = fmt["sample_hz"] * 49000 // 1000000
            if max_samples is not None:
                limit = min(limit, max_samples)
            if not 1 <= frames <= limit:
                raise ValueError(f"WAV duration is {frames*1000/fmt['sample_hz']:g} ms; maximum is {limit*1000/fmt['sample_hz']:g} ms ({limit:,} samples).")
            fmt["data_offset"], fmt["data_bytes"] = start, length
            if fmt["encoding"] == 3:
                for (bits,) in struct.iter_unpack("<I", data[start:end]):
                    if bits & 0x7f800000 == 0x7f800000:
                        raise ValueError("Float WAV contains NaN or infinity.")
    if fmt is None or frames is None:
        raise ValueError("WAV is missing fmt or data.")
    return {**fmt, "samples": frames, "duration_us": (frames * 1000000 + fmt["sample_hz"] - 1) // fmt["sample_hz"],
            "file_bytes": len(data), "file_crc32": crc32(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def prepare_wav(data, sample_hz, *, max_samples=None):
    """Preserve original bytes at matching Fs; otherwise filter/resample to mono PCM16.

    This changes the samples, not just the WAV header. Resampling rounds length
    up to the next destination frame; duration changes by less than one frame.
    No trimming, stretching, peak normalization or amplitude control is applied.
    Vitis still verifies the file and converts its frames directly into BRAM.
    """
    import io
    import math
    import wave
    target = rate_hz(sample_hz)
    original = inspect_wav(data)
    if original["sample_hz"] == target:
        info = inspect_wav(data, sample_hz=target, max_samples=max_samples)
        return data, {**info, "resampled": False, "source_sample_hz": target}
    source = original["sample_hz"]
    divisor = math.gcd(source, target)
    up, down = target // divisor, source // divisor
    if max(up, down) > 4096:
        raise ValueError("This WAV uses an unusual source rate; export it at a standard audio rate first.")
    frames = (original["samples"] * up + down - 1) // down
    limit = max_tx_samples(target, max_samples)
    if frames > limit:
        raise ValueError(f"Resampled WAV needs {frames:,} samples; maximum is {limit:,} samples at {target:,} Hz. Shorten the source WAV.")
    try:
        import numpy as np
        from scipy.signal import resample_poly
    except ImportError as error:
        raise ValueError("WAV resampling needs NumPy and SciPy. Run: py -m pip install -r gui/requirements.txt") from error
    payload = data[original["data_offset"]:original["data_offset"] + original["data_bytes"]]
    bits = original["bits"]
    if original["encoding"] == 3:
        pcm = np.clip(np.frombuffer(payload, dtype="<f4").astype(np.float64), -1, 1) * 32768
    elif bits == 8:
        pcm = (np.frombuffer(payload, dtype=np.uint8).astype(np.float64) - 128) * 256
    elif bits == 24:
        b = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        values = (values ^ 0x800000) - 0x800000
        pcm = values.astype(np.float64) / 256
    else:
        pcm = np.frombuffer(payload, dtype="<i2" if bits == 16 else "<i4").astype(np.float64)
        pcm /= 2 ** (bits - 16)
    mono = pcm.reshape(-1, original["channels"]).mean(axis=1)
    converted = resample_poly(mono, up, down, window=("kaiser", 8.6), padtype="constant")
    if converted.size != frames or not np.isfinite(converted).all():
        raise ValueError("WAV resampling returned invalid samples.")
    clipped = int(np.count_nonzero((converted < -32768) | (converted > 32767)))
    samples = np.rint(np.clip(converted, -32768, 32767)).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, target, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    result = output.getvalue()
    info = inspect_wav(result, sample_hz=target, max_samples=max_samples)
    return result, {**info, "resampled": True, "source_sample_hz": source,
                    "resampler": "scipy.signal.resample_poly; Kaiser beta 8.6; zero padding",
                    "clipped_samples": clipped}
