from __future__ import annotations

import re


SPEED_FIELD_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(DF|ND)", re.IGNORECASE)


def parse_speed_field(text: str) -> tuple[float, bool] | None:
    match = SPEED_FIELD_PATTERN.search(text)
    if not match:
        return None
    return float(match.group(1)), match.group(2).upper() == "DF"


def format_smpte(hours: int, minutes: int, seconds: int, frames: int, drop_frame: bool) -> str:
    frame_separator = ";" if drop_frame else ":"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{frame_separator}{frames:02d}"


def samples_to_smpte(samples: int, sample_rate: float, fps: float, drop_frame: bool) -> str:
    if samples < 0 or sample_rate <= 0 or fps <= 0:
        raise ValueError("samples, sample_rate, and fps must all be positive.")
    nominal_fps = round(fps)
    total_frames = round(samples / sample_rate * fps)
    use_drop_frame = drop_frame and nominal_fps in (30, 60)
    if use_drop_frame:
        drop = 2 if nominal_fps == 30 else 4
        frames_per_minute = nominal_fps * 60 - drop
        frames_per_ten_minutes = nominal_fps * 600 - drop * 9
        ten_minute_blocks, remainder = divmod(total_frames, frames_per_ten_minutes)
        if remainder > drop:
            adjustment = drop * 9 * ten_minute_blocks + drop * ((remainder - drop) // frames_per_minute)
        else:
            adjustment = drop * 9 * ten_minute_blocks
        total_frames += adjustment
    frames = total_frames % nominal_fps
    seconds = (total_frames // nominal_fps) % 60
    minutes = (total_frames // (nominal_fps * 60)) % 60
    hours = total_frames // (nominal_fps * 3600)
    return format_smpte(hours, minutes, seconds, frames, use_drop_frame)
