"""Recognize the firmware's WAV flag and validate its PCM audio payload."""
import io
import struct
import wave

SONAR_FRAME_FLAG_WAV_PCM16 = 0x00000100


def is_wav(payload, meta):
    # Signature fallback also supports WAV files saved as .bin by older GUIs.
    return bool(meta.get("flags", 0) & SONAR_FRAME_FLAG_WAV_PCM16) or (
        payload[:4] == b"RIFF" and payload[8:12] == b"WAVE")


def read_pcm_wav(payload, expected_channels=16):
    if (len(payload) < 12 or payload[:4] != b"RIFF" or
            payload[8:12] != b"WAVE" or
            struct.unpack_from("<I", payload, 4)[0] + 8 != len(payload)):
        raise ValueError("Invalid or truncated RIFF/WAVE payload")
    try:
        with wave.open(io.BytesIO(payload), "rb") as wav:
            channels, width, rate, frames, compression, _ = wav.getparams()
            if (channels != expected_channels or width != 2 or rate not in (48000, 96000) or
                    compression != "NONE" or frames == 0):
                raise ValueError("Expected 16-channel, 48/96 kHz, signed 16-bit PCM WAV")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * channels * width:
                raise ValueError("Truncated WAV audio samples")
    except (wave.Error, EOFError, struct.error) as error:
        raise ValueError(f"Invalid WAV payload: {error}") from error
    return pcm, channels, rate, frames
