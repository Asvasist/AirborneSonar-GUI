"""Store WAV or raw PDM frames by their board capture-time second."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from sonar_audio import is_wav, read_pcm_wav


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_atomic(path, data):
    """Publish a complete file only after its content has been flushed."""
    temporary = path.with_name(path.name + ".part")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def write_json(path, value):
    write_atomic(path, (json.dumps(value, indent=2) + "\n").encode("utf-8"))


class CaptureStore:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.batch_us = None

    def save(self, meta, payload):
        if not payload:
            self.batch_us = meta["trigger_request_us"]
            return None
        wav_payload = is_wav(payload, meta)
        pcm_info = None
        if wav_payload:
            # Validate before persistence/ACK; preserve the original bytes exactly.
            _, channels, rate, frames = read_pcm_wav(payload, meta.get("channels", 16))
            pcm_info = dict(pcm_sample_hz=rate, pcm_channels=channels, pcm_frames=frames)
        second = meta["trigger_request_us"] // 1_000_000
        folder = self.folder / f"second_{second:010d}"
        folder.mkdir(parents=True, exist_ok=True)
        stem = folder / f"capture_{meta['sequence']:010d}"
        extension = ".wav" if wav_payload else ".bin"
        binary, description = stem.with_suffix(extension), stem.with_suffix(".json")
        alternate = stem.with_suffix(".bin" if wav_payload else ".wav")
        if alternate.exists() and alternate.read_bytes() != payload:
            raise ValueError(f"Conflicting duplicate: {alternate}")
        if binary.exists() and binary.read_bytes() != payload:
            raise ValueError(f"Conflicting duplicate: {binary}")
        if description.exists():
            old = json.loads(description.read_text(encoding="utf-8"))
            if any(old.get(key) != value for key, value in meta.items()):
                raise ValueError(f"Conflicting metadata: {description}")
        else:
            old = dict(meta, pc_received_utc=utc_now(), board_batch_release_us=self.batch_us)
            if pcm_info is not None:
                old.update(pcm_info)
            config_id = meta.get("config_id", 0)
            old["configuration_status"] = "unavailable"
            if config_id:
                manifest = self.folder / "configurations" / f"config_{config_id:05d}.json"
                try:
                    saved = json.loads(manifest.read_text(encoding="utf-8"))
                    c = saved["configuration"]
                    mode = "WAV" if c["mode"] == 1 else "GENERATE"
                    if (c["config_id"] == config_id and mode == meta.get("tx_mode")
                            and c["duration_us"] == meta.get("tx_duration_us")
                            and c.get("boot_id") == meta.get("boot_id")):
                        old["transmit_configuration"] = saved
                        old["configuration_status"] = "matched"
                        old["configuration_manifest"] = str(manifest.relative_to(self.folder))
                    else:
                        old["configuration_status"] = "mismatch"
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # Preserve/ACK audio even when configuration provenance is unavailable.

        # Rewriting an identical retry is safe and re-flushes both files before ACK.
        write_atomic(binary, payload)
        write_json(description, old)
        return binary
