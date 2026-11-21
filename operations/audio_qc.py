from __future__ import annotations

import os
import struct
import sys
from array import array
from pathlib import Path
from typing import Optional

from operations.wav_core import read_wav_chunk, scan_wav_chunks


def _pcm_layout_from_fmt_and_data(wav_path: Path, fmt_data: Optional[bytes], data_offset: Optional[int], data_size: Optional[int]) -> dict[str, int]:
    if fmt_data is None or len(fmt_data) < 16 or data_offset is None:
        raise RuntimeError(f"WAV is missing fmt or data chunk: {wav_path}")
    format_tag, channels, sample_rate, _byte_rate, block_align, bits_per_sample = struct.unpack_from("<HHIIHH", fmt_data)
    if format_tag == 0xFFFE:
        if len(fmt_data) < 40:
            raise RuntimeError(f"unsupported WAVEFORMATEXTENSIBLE subtype: {wav_path}")
        audio_format = struct.unpack_from("<I", fmt_data, 24)[0]
    else:
        audio_format = format_tag
    if audio_format not in (1, 3):
        raise RuntimeError(f"unsupported WAV audio encoding: {wav_path}")
    if audio_format == 3 and bits_per_sample != 32:
        raise RuntimeError(f"unsupported IEEE Float bit depth: {bits_per_sample}")
    if bits_per_sample == 0 or bits_per_sample % 8:
        raise RuntimeError(f"unsupported PCM bit depth: {bits_per_sample}")
    bytes_per_sample = bits_per_sample // 8
    if channels < 1 or block_align != channels * bytes_per_sample or data_size % block_align:
        raise RuntimeError(f"invalid PCM block alignment: {wav_path}")
    return {
        "channels": channels,
        "audio_format": audio_format,
        "sample_rate": sample_rate,
        "bits_per_sample": bits_per_sample,
        "bytes_per_sample": bytes_per_sample,
        "block_align": block_align,
        "data_offset": data_offset,
        "data_size": data_size,
        "frames": data_size // block_align,
    }


def read_wav_pcm_layout(wav_path: Path) -> dict[str, int]:
    _header, chunks = scan_wav_chunks(wav_path)
    fmt_chunk = next((chunk for chunk in chunks if chunk.chunk_id == b"fmt "), None)
    data_chunk = next((chunk for chunk in chunks if chunk.chunk_id == b"data"), None)
    fmt_data = read_wav_chunk(wav_path, fmt_chunk) if fmt_chunk is not None else None
    return _pcm_layout_from_fmt_and_data(
        wav_path, fmt_data,
        data_chunk.data_offset if data_chunk is not None else None,
        data_chunk.size if data_chunk is not None else None,
    )


def read_wav_pcm_layout_fast(wav_path: Path) -> dict[str, int]:
    """Same result as `read_wav_pcm_layout()`, but stops scanning the moment
    both `fmt ` and `data` are found instead of walking every remaining
    chunk in the file the way `scan_wav_chunks()` does. `fmt ` always
    precedes `data` in a valid WAV, so this never needs to seek past a
    (potentially hundreds-of-MB) data payload just to enumerate chunks that
    happen to follow it — on a network-mounted file, that seek alone was
    measured costing real, visible per-click latency in Browse mode, which
    only ever needs channels/sample-rate/duration, never the full chunk
    list."""
    with wav_path.open("rb") as wav_file:
        header = wav_file.read(12)
        if len(header) != 12 or header[:4] not in (b"RIFF", b"RF64") or header[8:] != b"WAVE":
            raise RuntimeError(f"not a RIFF/WAVE file: {wav_path}")
        fmt_data: Optional[bytes] = None
        data_offset: Optional[int] = None
        data_size: Optional[int] = None
        while True:
            chunk_header = wav_file.read(8)
            if len(chunk_header) != 8:
                break
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
            if chunk_id == b"fmt ":
                fmt_data = wav_file.read(chunk_size)
                if chunk_size & 1:
                    wav_file.read(1)
                continue
            if chunk_id == b"data":
                data_offset = wav_file.tell()
                data_size = chunk_size
                break
            wav_file.seek(chunk_size + (chunk_size & 1), os.SEEK_CUR)
    return _pcm_layout_from_fmt_and_data(wav_path, fmt_data, data_offset, data_size)


def _peak_from_window(data: bytes, bits_per_sample: int, audio_format: int) -> float:
    """Max absolute normalized amplitude (0.0-1.0) across every sample in
    `data`, whatever channel each belongs to — a preview waveform doesn't
    need per-channel separation, just "how loud does this moment get"."""
    if not data:
        return 0.0
    if audio_format == 3:  # IEEE float32
        count = len(data) // 4
        if count == 0:
            return 0.0
        values = struct.unpack_from(f"<{count}f", data)
        return min(1.0, max(abs(value) for value in values))
    if bits_per_sample == 16:
        count = len(data) // 2
        if count == 0:
            return 0.0
        values = struct.unpack_from(f"<{count}h", data)
        return max(abs(value) for value in values) / 32768.0
    if bits_per_sample == 24:
        count = len(data) // 3
        if count == 0:
            return 0.0
        peak = 0
        for index in range(count):
            raw = data[index * 3:index * 3 + 3]
            value = raw[0] | (raw[1] << 8) | (raw[2] << 16)
            if value & 0x800000:
                value -= 0x1000000
            peak = max(peak, abs(value))
        return peak / 8388608.0
    if bits_per_sample == 32:
        count = len(data) // 4
        if count == 0:
            return 0.0
        values = struct.unpack_from(f"<{count}i", data)
        return max(abs(value) for value in values) / 2147483648.0
    return 0.0


def compute_wav_peaks(wav_path: Path, bucket_count: int = 600, sample_window_bytes: int = 4096) -> list[float]:
    """A fast, approximate waveform for a preview display — samples one
    small window per bucket (evenly spaced across the file) rather than
    reading every frame, so total I/O stays roughly
    `bucket_count * sample_window_bytes` (a few MB at most) regardless of
    how large the source file actually is. This is a preview, not a
    measurement — the app's real metadata/QC tooling doesn't use this."""
    layout = read_wav_pcm_layout(wav_path)
    total_frames = layout["frames"]
    if total_frames <= 0:
        return []
    block_align = layout["block_align"]
    data_offset = layout["data_offset"]
    data_size = layout["data_size"]
    bits_per_sample = layout["bits_per_sample"]
    audio_format = layout["audio_format"]
    bucket_count = max(1, min(bucket_count, total_frames))
    peaks: list[float] = []
    with wav_path.open("rb") as handle:
        for index in range(bucket_count):
            frame_start = (index * total_frames) // bucket_count
            byte_start = data_offset + frame_start * block_align
            window_size = min(sample_window_bytes, data_offset + data_size - byte_start)
            if window_size <= 0:
                peaks.append(0.0)
                continue
            # A whole number of sample frames, so a multi-channel
            # interleaved WAV never gets split mid-sample at the window edge.
            window_size -= window_size % block_align
            handle.seek(byte_start)
            window = handle.read(window_size)
            peaks.append(_peak_from_window(window, bits_per_sample, audio_format))
    return peaks


def _peaks_per_channel_from_window(data: bytes, bits_per_sample: int, audio_format: int, channels: int) -> list[float]:
    """Like `_peak_from_window`, but keeps each interleaved channel's max
    absolute amplitude separate instead of collapsing all of them into one
    value — used to draw one waveform lane per channel for a Poly source."""
    peaks = [0.0] * channels
    if not data or channels <= 0:
        return peaks
    if audio_format == 3:  # IEEE float32
        count = len(data) // 4
        if count == 0:
            return peaks
        values = struct.unpack_from(f"<{count}f", data)
        for index, value in enumerate(values):
            amplitude = min(1.0, abs(value))
            channel = index % channels
            if amplitude > peaks[channel]:
                peaks[channel] = amplitude
        return peaks
    if bits_per_sample == 16:
        count = len(data) // 2
        if count == 0:
            return peaks
        values = struct.unpack_from(f"<{count}h", data)
        for index, value in enumerate(values):
            amplitude = abs(value) / 32768.0
            channel = index % channels
            if amplitude > peaks[channel]:
                peaks[channel] = amplitude
        return peaks
    if bits_per_sample == 24:
        count = len(data) // 3
        if count == 0:
            return peaks
        for index in range(count):
            raw = data[index * 3:index * 3 + 3]
            value = raw[0] | (raw[1] << 8) | (raw[2] << 16)
            if value & 0x800000:
                value -= 0x1000000
            amplitude = abs(value) / 8388608.0
            channel = index % channels
            if amplitude > peaks[channel]:
                peaks[channel] = amplitude
        return peaks
    if bits_per_sample == 32:
        count = len(data) // 4
        if count == 0:
            return peaks
        values = struct.unpack_from(f"<{count}i", data)
        for index, value in enumerate(values):
            amplitude = abs(value) / 2147483648.0
            channel = index % channels
            if amplitude > peaks[channel]:
                peaks[channel] = amplitude
        return peaks
    return peaks


def compute_wav_channel_peaks(wav_path: Path, bucket_count: int = 600, sample_window_bytes: int = 4096) -> list[list[float]]:
    """Per-channel counterpart to `compute_wav_peaks()` — same sparse,
    fixed-I/O sampling strategy (one small window per bucket, evenly spaced
    across the file), but returns one peaks list per channel instead of
    collapsing them, so a Poly (multi-channel) source can be drawn as one
    waveform lane per channel."""
    layout = read_wav_pcm_layout(wav_path)
    channels = layout["channels"]
    total_frames = layout["frames"]
    if total_frames <= 0:
        return [[] for _ in range(channels)]
    block_align = layout["block_align"]
    data_offset = layout["data_offset"]
    data_size = layout["data_size"]
    bits_per_sample = layout["bits_per_sample"]
    audio_format = layout["audio_format"]
    bucket_count = max(1, min(bucket_count, total_frames))
    channel_peaks: list[list[float]] = [[] for _ in range(channels)]
    with wav_path.open("rb") as handle:
        for index in range(bucket_count):
            frame_start = (index * total_frames) // bucket_count
            byte_start = data_offset + frame_start * block_align
            window_size = min(sample_window_bytes, data_offset + data_size - byte_start)
            if window_size <= 0:
                for lane in channel_peaks:
                    lane.append(0.0)
                continue
            window_size -= window_size % block_align
            handle.seek(byte_start)
            window = handle.read(window_size)
            bucket_values = _peaks_per_channel_from_window(window, bits_per_sample, audio_format, channels)
            for lane, value in zip(channel_peaks, bucket_values):
                lane.append(value)
    return channel_peaks


def wav_duration_seconds(wav_path: Path) -> float:
    layout = read_wav_pcm_layout(wav_path)
    if layout["sample_rate"] <= 0:
        return 0.0
    return layout["frames"] / layout["sample_rate"]


def compare_wav_pcm_content(path_a: Path, path_b: Path) -> bool:
    """Byte-for-byte compare the PCM data of two same-layout WAVs. Callers
    are expected to have already confirmed sample rate, bit depth, channel
    count, and frame count match — this only looks at the actual sample
    bytes, which is what a plugin/render pass can silently alter (dropouts,
    gain, DSP) while every soxi-reported property stays identical."""
    layout_a = read_wav_pcm_layout(path_a)
    layout_b = read_wav_pcm_layout(path_b)
    if layout_a["data_size"] != layout_b["data_size"]:
        return False
    chunk_size = 1 << 20
    with path_a.open("rb") as file_a, path_b.open("rb") as file_b:
        file_a.seek(layout_a["data_offset"])
        file_b.seek(layout_b["data_offset"])
        remaining = layout_a["data_size"]
        while remaining:
            read_size = min(chunk_size, remaining)
            if file_a.read(read_size) != file_b.read(read_size):
                return False
            remaining -= read_size
    return True


def _int_float_chunk_equal(int_data: bytes, int_width: int, int_bits: int, float_data: bytes, float_width: int) -> Optional[bool]:
    """Same rule as the per-sample loop in compare_wav_pcm_stream
    (round(float * 2^(bits-1)) == integer sample), done a whole chunk at a
    time: the integer samples are widened to int32 by byte slicing and both
    sides are decoded with `array` instead of one struct/int.from_bytes call
    per sample (about 5x faster on a 24-bit vs 32-bit float file). Returns
    None when this layout isn't covered, so the caller falls back to the
    per-sample loop."""
    if float_width != 4 or int_width not in (1, 2, 3, 4) or sys.byteorder != "little" or not 1 < int_bits <= 32:
        return None
    if int_width == 1:
        return None  # 8-bit WAV is unsigned; keep the original loop's behaviour
    count = len(float_data) // 4
    if len(int_data) != count * int_width:
        return None
    pad = 4 - int_width
    if pad:
        widened = bytearray(count * 4)
        for byte_index in range(int_width):
            widened[pad + byte_index::4] = int_data[byte_index::int_width]
        int_data = bytes(widened)
    integers = array("i")
    integers.frombytes(int_data)
    floats = array("f")
    floats.frombytes(float_data)
    scale = float(1 << (int_bits - 1))
    shift = 8 * pad
    if shift:
        return not any(round(value * scale) != (integer >> shift) for value, integer in zip(floats, integers))
    return not any(round(value * scale) != integer for value, integer in zip(floats, integers))


def compare_wav_pcm_stream(poly_path: Path, stream_index: int, mono_path: Path) -> tuple[int, str]:
    poly = read_wav_pcm_layout(poly_path)
    mono = read_wav_pcm_layout(mono_path)
    if not 1 <= stream_index <= poly["channels"]:
        raise RuntimeError(f"Poly Stream {stream_index} is outside 1-{poly['channels']}")
    frames = poly["frames"]
    if mono["channels"] != 1:
        return frames, "DIFFERENT"
    if any(poly[field] != mono[field] for field in ("sample_rate", "frames")):
        return frames, "DIFFERENT"
    if poly["audio_format"] == mono["audio_format"] and poly["bits_per_sample"] != mono["bits_per_sample"]:
        return frames, "DIFFERENT"
    poly_sample_width = poly["bytes_per_sample"]
    mono_sample_width = mono["bytes_per_sample"]
    stream_offset = (stream_index - 1) * poly_sample_width
    frames_per_chunk = 8192
    with poly_path.open("rb") as poly_file, mono_path.open("rb") as mono_file:
        poly_file.seek(poly["data_offset"])
        mono_file.seek(mono["data_offset"])
        remaining = frames
        while remaining:
            frame_count = min(frames_per_chunk, remaining)
            poly_data = poly_file.read(frame_count * poly["block_align"])
            mono_data = mono_file.read(frame_count * mono_sample_width)
            if len(poly_data) != frame_count * poly["block_align"] or len(mono_data) != frame_count * mono_sample_width:
                raise RuntimeError("truncated PCM data")
            extracted = bytearray(frame_count * poly_sample_width)
            for byte_index in range(poly_sample_width):
                extracted[byte_index::poly_sample_width] = poly_data[stream_offset + byte_index::poly["block_align"]]
            if poly["audio_format"] == mono["audio_format"] and poly["bits_per_sample"] == mono["bits_per_sample"]:
                if extracted != mono_data:
                    return frames, "DIFFERENT"
            else:
                if poly["audio_format"] == 1:
                    fast_equal = _int_float_chunk_equal(extracted, poly_sample_width, poly["bits_per_sample"], mono_data, mono_sample_width)
                else:
                    fast_equal = _int_float_chunk_equal(mono_data, mono_sample_width, mono["bits_per_sample"], extracted, poly_sample_width)
                if fast_equal is not None:
                    if not fast_equal:
                        return frames, "DIFFERENT"
                    remaining -= frame_count
                    continue
                for frame_index in range(frame_count):
                    poly_bytes = extracted[frame_index * poly_sample_width:(frame_index + 1) * poly_sample_width]
                    mono_bytes = mono_data[frame_index * mono_sample_width:(frame_index + 1) * mono_sample_width]
                    if poly["audio_format"] == 1:
                        integer_value = int.from_bytes(poly_bytes, "little", signed=True)
                        float_value = struct.unpack("<f", mono_bytes)[0]
                        integer_bits = poly["bits_per_sample"]
                    else:
                        integer_value = int.from_bytes(mono_bytes, "little", signed=True)
                        float_value = struct.unpack("<f", poly_bytes)[0]
                        integer_bits = mono["bits_per_sample"]
                    if round(float_value * (1 << (integer_bits - 1))) != integer_value:
                        return frames, "DIFFERENT"
            remaining -= frame_count
    return frames, "PASS"
