#!/usr/bin/env python3
"""
TorrentCreator v2

Creates BitTorrent v1 .torrent files and video preview media:
- sequential job queue for multiple videos
- 5 separate screenshots by default
- 3 x 9 contact sheet, 1300 px wide by default
- size-limited animated GIF and WebP previews (default max 7 MiB)
- privacy mode, verification, drag & drop, and saved settings

The Windows build can bundle FFmpeg/FFprobe directly into TorrentCreator.exe.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    BaseTk = TkinterDnD.Tk
    DRAGDROP_AVAILABLE = True
except Exception:
    DND_FILES = None
    TkinterDnD = None
    BaseTk = tk.Tk
    DRAGDROP_AVAILABLE = False
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None
APP_NAME = 'TorrentCreator'
APP_VERSION = '2.3.0'
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v', '.webm', '.mpg', '.mpeg', '.ts', '.mts', '.m2ts'}

class JobCancelled(Exception):
    """Internal signal used to cancel an active job without displaying an error."""

def check_cancel(cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled('The job was cancelled.')

def bencode(value):
    """Minimal bencode enligt BitTorrent-specifikationen."""
    if isinstance(value, bytes):
        return str(len(value)).encode('ascii') + b':' + value
    if isinstance(value, str):
        return bencode(value.encode('utf-8'))
    if isinstance(value, int):
        return b'i' + str(value).encode('ascii') + b'e'
    if isinstance(value, list):
        return b'l' + b''.join((bencode(item) for item in value)) + b'e'
    if isinstance(value, dict):
        sortable = []
        for key, item in value.items():
            key_bytes = key if isinstance(key, bytes) else str(key).encode('utf-8')
            sortable.append((key_bytes, item))
        encoded_items = []
        for key_bytes, item in sorted(sortable, key=lambda pair: pair[0]):
            encoded_items.append(bencode(key_bytes))
            encoded_items.append(bencode(item))
        return b'd' + b''.join(encoded_items) + b'e'
    raise TypeError(f'Cannot bencode type: {type(value)!r}')

def auto_piece_length(total_size):
    mib = 1024 * 1024
    gib = 1024 * mib
    if total_size <= 256 * mib:
        return 256 * 1024
    if total_size <= 1 * gib:
        return 512 * 1024
    if total_size <= 4 * gib:
        return 1 * mib
    if total_size <= 16 * gib:
        return 2 * mib
    return 4 * mib

def folder_files(root, excluded=None):
    root = Path(root)
    excluded = {Path(p).resolve() for p in excluded or []}
    files = []
    for p in root.rglob('*'):
        if not p.is_file():
            continue
        try:
            resolved = p.resolve()
        except Exception:
            resolved = p
        if resolved in excluded:
            continue
        files.append(p)
    files.sort(key=lambda p: p.relative_to(root).as_posix().casefold())
    return files

def hash_pieces(paths, piece_length, progress_callback=None, cancel_event=None):
    pieces = []
    buffer = bytearray()
    total = sum((p.stat().st_size for p in paths))
    processed = 0
    for path in paths:
        check_cancel(cancel_event)
        with path.open('rb') as f:
            while True:
                check_cancel(cancel_event)
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                processed += len(chunk)
                buffer.extend(chunk)
                while len(buffer) >= piece_length:
                    piece = bytes(buffer[:piece_length])
                    del buffer[:piece_length]
                    pieces.append(hashlib.sha1(piece).digest())
                if progress_callback:
                    progress_callback(processed, total)
    if buffer:
        pieces.append(hashlib.sha1(bytes(buffer)).digest())
    if progress_callback:
        progress_callback(total, total)
    return b''.join(pieces)

def parse_trackers(text):
    return [line.strip() for line in text.splitlines() if line.strip()]

def privacy_name_warnings(source, max_items=5):
    """A conservative check for file/folder names that may contain contact information.

    Personal names cannot be identified reliably, so the application only flags
    clear patterns such as email addresses, phone numbers, and personal-data labels.
    """
    source = Path(source)
    names = [source.name]
    if source.is_dir():
        try:
            names.extend((p.name for p in source.rglob('*') if p.is_file()))
        except Exception:
            pass
    email_re = re.compile('[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}', re.I)
    phone_re = re.compile('(?<!\\d)(?:\\+?\\d[\\s().-]*){8,}(?!\\d)')
    label_re = re.compile('(?:personnummer|ssn|telefon|phone|e[-_ ]?mail)', re.I)
    found = []
    for name in names:
        reasons = []
        if email_re.search(name):
            reasons.append('email address')
        if phone_re.search(name):
            reasons.append('phone number/long digit sequence')
        if label_re.search(name):
            reasons.append('personal-data label')
        if reasons:
            found.append((name, ', '.join(reasons)))
            if len(found) >= max_items:
                break
    return found

def create_torrent(source, output, trackers, piece_length, comment='', private=False, privacy_mode=False, progress_callback=None, cancel_event=None):
    source = Path(source)
    output = Path(output)
    if not source.exists():
        raise FileNotFoundError('The source does not exist.')
    excluded = [output]
    if source.is_file():
        paths = [source]
        total_size = source.stat().st_size
        info = {'name': source.name, 'length': total_size}
    else:
        paths = folder_files(source, excluded=excluded)
        if not paths:
            raise ValueError('The folder contains no files.')
        total_size = sum((p.stat().st_size for p in paths))
        info_files = []
        for p in paths:
            info_files.append({'length': p.stat().st_size, 'path': list(p.relative_to(source).parts)})
        info = {'name': source.name, 'files': info_files}
    if piece_length == 0:
        piece_length = auto_piece_length(total_size)
    info['piece length'] = piece_length
    info['pieces'] = hash_pieces(paths, piece_length, progress_callback, cancel_event=cancel_event)
    if private:
        info['private'] = 1
    torrent = {'info': info}
    if not privacy_mode:
        torrent['creation date'] = int(time.time())
        torrent['created by'] = APP_NAME
    if trackers:
        torrent['announce'] = trackers[0]
        if len(trackers) > 1:
            torrent['announce-list'] = [[tracker] for tracker in trackers]
    if comment.strip() and (not privacy_mode):
        torrent['comment'] = comment.strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(bencode(torrent))
    return (total_size, piece_length, len(info['pieces']) // 20)

def subprocess_startupinfo():
    if os.name != 'nt':
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return info

def run_command(command, capture_output=True, cancel_event=None):
    check_cancel(cancel_event)
    if cancel_event is None:
        return subprocess.run(command, check=True, stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL, stderr=subprocess.PIPE, startupinfo=subprocess_startupinfo(), text=True, encoding='utf-8', errors='replace')
    kwargs = {'stdout': subprocess.PIPE if capture_output else subprocess.DEVNULL, 'stderr': subprocess.PIPE, 'startupinfo': subprocess_startupinfo(), 'text': True, 'encoding': 'utf-8', 'errors': 'replace'}
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    process = subprocess.Popen(command, **kwargs)
    while True:
        if cancel_event.is_set():
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            raise JobCancelled('The job was cancelled.')
        try:
            stdout, stderr = process.communicate(timeout=0.12)
            break
        except subprocess.TimeoutExpired:
            continue
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command, output=stdout if capture_output else '', stderr=stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout if capture_output else '', stderr)

def application_resource_dirs():
    """Locations where bundled resources may exist in source, PyInstaller onedir, and onefile builds."""
    dirs = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        dirs.append(Path(meipass))
    if getattr(sys, 'frozen', False):
        dirs.append(Path(sys.executable).resolve().parent)
    try:
        dirs.append(Path(__file__).resolve().parent)
    except Exception:
        pass
    dirs.append(Path.cwd())
    expanded = []
    for base in dirs:
        expanded.extend([base, base / 'ffmpeg', base / 'bin', base / 'tools' / 'ffmpeg'])
    unique = []
    seen = set()
    for item in expanded:
        key = str(item).casefold() if os.name == 'nt' else str(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique

def find_executable(name):
    """Looks for bundled FFmpeg first, then a system installation."""
    filename = f'{name}.exe' if os.name == 'nt' else name
    for base in application_resource_dirs():
        candidate = base / filename
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    if os.name == 'nt':
        common = [Path('C:\\ffmpeg\\bin') / filename, Path('C:\\Program Files\\ffmpeg\\bin') / filename]
        for candidate in common:
            if candidate.is_file():
                return str(candidate)
    return None

def ffmpeg_bundle_status():
    ffmpeg = find_executable('ffmpeg')
    ffprobe = find_executable('ffprobe')
    if not ffmpeg or not ffprobe:
        return 'FFmpeg missing'
    app_dirs = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        app_dirs.append(Path(meipass))
    if getattr(sys, 'frozen', False):
        app_dirs.append(Path(sys.executable).resolve().parent)
    try:
        app_dirs.append(Path(__file__).resolve().parent)
    except Exception:
        pass
    try:
        ffmpeg_path = Path(ffmpeg).resolve()
        for base in app_dirs:
            try:
                base = base.resolve()
            except Exception:
                pass
            if base == ffmpeg_path.parent or base in ffmpeg_path.parents:
                return 'FFmpeg bundled'
    except Exception:
        pass
    return 'FFmpeg found on system'

def require_ffmpeg():
    ffmpeg = find_executable('ffmpeg')
    ffprobe = find_executable('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('FFmpeg/ffprobe were not found. The Windows EXE build includes them automatically. If you run the Python source: install FFmpeg or place ffmpeg/ffprobe next to the application.')
    return (ffmpeg, ffprobe)

def find_video_source(source):
    source = Path(source)
    if source.is_file():
        if source.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError('The selected file does not appear to be a video file.')
        return source
    candidates = [p for p in source.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
    if not candidates:
        raise ValueError('No video file was found in the selected folder.')
    return max(candidates, key=lambda p: p.stat().st_size)

def _parse_fps(value):
    if not value or value in ('0/0', 'N/A'):
        return 0.0
    try:
        if '/' in str(value):
            a, b = str(value).split('/', 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(value)
    except Exception:
        return 0.0

def probe_video(video_path, cancel_event=None):
    _, ffprobe = require_ffmpeg()
    command = [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,codec_name,avg_frame_rate,r_frame_rate:format=duration,size,bit_rate,format_name', '-of', 'json', str(video_path)]
    result = run_command(command, cancel_event=cancel_event)
    data = json.loads(result.stdout)
    streams = data.get('streams') or []
    if not streams:
        raise RuntimeError('The video contains no readable video stream.')
    stream = streams[0]
    fmt = data.get('format') or {}
    duration = float(fmt.get('duration') or 0)
    width = int(stream.get('width') or 0)
    height = int(stream.get('height') or 0)
    fps = _parse_fps(stream.get('avg_frame_rate')) or _parse_fps(stream.get('r_frame_rate'))
    codec = str(stream.get('codec_name') or '?')
    bitrate = int(float(fmt.get('bit_rate') or 0)) if fmt.get('bit_rate') not in (None, 'N/A') else 0
    format_name = str(fmt.get('format_name') or '')
    if duration <= 0 or width <= 0 or height <= 0:
        raise RuntimeError("Could not read the video's duration or resolution.")
    return {'duration': duration, 'width': width, 'height': height, 'fps': fps, 'codec': codec, 'bitrate': bitrate, 'format_name': format_name}

def _clean_tag_dict(tags):
    """Normalizes FFprobe tags into a stable str->str dictionary and removes empty values."""
    result = {}
    for key, value in (tags or {}).items():
        key = str(key).strip()
        if not key or value is None:
            continue
        if isinstance(value, (dict, list)):
            try:
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except Exception:
                value = str(value)
        value = str(value).strip()
        if value:
            result[key] = value
    return result

def _safe_int(value, default=0):
    try:
        if value in (None, '', 'N/A'):
            return default
        return int(float(value))
    except Exception:
        return default

def _safe_float(value, default=0.0):
    try:
        if value in (None, '', 'N/A'):
            return default
        return float(value)
    except Exception:
        return default

def detect_filename_tags(filename):
    """Detects common release/video tags in the filename without changing the name."""
    name = Path(filename).name
    normalized = re.sub('[._\\-]+', ' ', name, flags=re.UNICODE)
    rules = [('\\b(?:2160p|4k|uhd)\\b', '2160p / 4K'), ('\\b1080p\\b', '1080p'), ('\\b720p\\b', '720p'), ('\\b(?:web[ ._-]?dl|webdl)\\b', 'WEB-DL'), ('\\bweb[ ._-]?rip\\b', 'WEBRip'), ('\\b(?:blu[ ._-]?ray|bluray|bdrip|brrip)\\b', 'BluRay'), ('\\bremux\\b', 'REMUX'), ('\\bhdtv\\b', 'HDTV'), ('\\bdvd[ ._-]?rip\\b', 'DVDRip'), ('\\b(?:x265|h[ ._-]?265|hevc)\\b', 'HEVC / H.265'), ('\\b(?:x264|h[ ._-]?264|avc)\\b', 'AVC / H.264'), ('\\bav1\\b', 'AV1'), ('\\bvp9\\b', 'VP9'), ('\\b(?:dolby[ ._-]?vision|dovi|dv)\\b', 'Dolby Vision'), ('\\bhdr10\\+\\b|\\bhdr10plus\\b', 'HDR10+'), ('\\bhdr10\\b', 'HDR10'), ('\\bhdr\\b', 'HDR'), ('\\b10[ ._-]?bit\\b', '10-bit'), ('\\b(?:truehd|true[ ._-]?hd)\\b', 'TrueHD'), ('\\batmos\\b', 'Dolby Atmos'), ('\\bdts[ ._-]?x\\b', 'DTS:X'), ('\\bdts[ ._-]?hd(?:[ ._-]?ma)?\\b', 'DTS-HD MA'), ('\\bdts\\b', 'DTS'), ('\\b(?:eac3|e[ ._-]?ac[ ._-]?3|ddp(?:\\s*\\d(?:\\s*\\d)?)?|dd\\+)\\b', 'Dolby Digital Plus / E-AC-3'), ('\\b(?:ac3|ac[ ._-]?3|dd5[ ._-]?1|dolby[ ._-]?digital)\\b', 'Dolby Digital / AC-3'), ('\\baac\\b', 'AAC'), ('\\bflac\\b', 'FLAC'), ('\\b(?:repack|rerip)\\b', 'REPACK'), ('\\bproper\\b', 'PROPER'), ('\\bmulti\\b', 'MULTi')]
    detected = []
    for pattern, label in rules:
        if re.search(pattern, normalized, re.I) and label not in detected:
            detected.append(label)
    return detected

def _stream_hdr_labels(stream):
    labels = []
    transfer = str(stream.get('color_transfer') or '').lower()
    primaries = str(stream.get('color_primaries') or '').lower()
    side_data = stream.get('side_data_list') or []
    side_names = ' '.join((str(x.get('side_data_type') or '') for x in side_data)).lower()
    if 'dovi' in side_names or 'dolby vision' in side_names:
        labels.append('Dolby Vision')
    if 'hdr dynamic metadata smpte2094-40' in side_names or 'smpte2094-40' in side_names:
        labels.append('HDR10+')
    if transfer == 'smpte2084':
        labels.append('HDR10 / PQ')
    elif transfer == 'arib-std-b67':
        labels.append('HLG')
    elif primaries == 'bt2020':
        labels.append('BT.2020')
    return list(dict.fromkeys(labels))

def _metadata_privacy_flags(format_tags, stream_tag_groups):
    """Flags metadata that may reveal the creation environment, dates, contact information, or location."""
    sensitive_fragments = ('artist', 'author', 'album_artist', 'encoded_by', 'encoder', 'comment', 'description', 'creation_time', 'date', 'copyright', 'location', 'gps', 'latitude', 'longitude', 'make', 'model', 'software', 'owner', 'publisher', 'email', 'phone', 'telephone')
    email_re = re.compile('[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}', re.I)
    phone_re = re.compile('(?<!\\d)(?:\\+?\\d[\\s().-]*){8,}(?!\\d)')
    flags = []

    def scan(source, tags):
        for key, value in tags.items():
            normalized_key = re.sub('[^a-z0-9]+', '_', key.lower()).strip('_')
            reasons = []
            if any((fragment in normalized_key for fragment in sensitive_fragments)):
                reasons.append('privacy-sensitive metadata field')
            if email_re.search(value):
                reasons.append('email address')
            time_like_key = any((x in normalized_key for x in ('duration', 'creation_time', 'timestamp', 'date')))
            if not time_like_key and phone_re.search(value):
                reasons.append('phone number/long digit sequence')
            if reasons:
                flags.append({'source': source, 'key': key, 'value': value, 'reason': ', '.join(dict.fromkeys(reasons))})
    scan('Container', format_tags)
    for group in stream_tag_groups:
        scan(group['source'], group['tags'])
    return flags

def probe_video_details(video_path, cancel_event=None):
    """Reads technical video/audio information, embedded tags, chapters, and filename tags through FFprobe."""
    _, ffprobe = require_ffmpeg()
    command = [ffprobe, '-v', 'error', '-show_format', '-show_streams', '-show_chapters', '-of', 'json', str(video_path)]
    result = run_command(command, cancel_event=cancel_event)
    data = json.loads(result.stdout or '{}')
    streams = data.get('streams') or []
    fmt = data.get('format') or {}
    video_streams = []
    audio_streams = []
    subtitle_streams = []
    stream_tag_groups = []
    for stream in streams:
        stype = str(stream.get('codec_type') or 'unknown')
        index = _safe_int(stream.get('index'), 0)
        tags = _clean_tag_dict(stream.get('tags'))
        if tags:
            stream_tag_groups.append({'source': f'Stream #{index} ({stype})', 'tags': tags})
        common = {'index': index, 'codec': str(stream.get('codec_name') or '?'), 'codec_long': str(stream.get('codec_long_name') or ''), 'profile': str(stream.get('profile') or ''), 'bitrate': _safe_int(stream.get('bit_rate')), 'tags': tags}
        if stype == 'video':
            video_streams.append({**common, 'width': _safe_int(stream.get('width')), 'height': _safe_int(stream.get('height')), 'fps': _parse_fps(stream.get('avg_frame_rate')) or _parse_fps(stream.get('r_frame_rate')), 'pix_fmt': str(stream.get('pix_fmt') or ''), 'bits_per_raw_sample': _safe_int(stream.get('bits_per_raw_sample')), 'color_space': str(stream.get('color_space') or ''), 'color_transfer': str(stream.get('color_transfer') or ''), 'color_primaries': str(stream.get('color_primaries') or ''), 'hdr': _stream_hdr_labels(stream)})
        elif stype == 'audio':
            audio_streams.append({**common, 'channels': _safe_int(stream.get('channels')), 'channel_layout': str(stream.get('channel_layout') or ''), 'sample_rate': _safe_int(stream.get('sample_rate')), 'language': tags.get('language', tags.get('LANGUAGE', '')), 'title': tags.get('title', tags.get('TITLE', ''))})
        elif stype == 'subtitle':
            subtitle_streams.append({**common, 'language': tags.get('language', tags.get('LANGUAGE', '')), 'title': tags.get('title', tags.get('TITLE', ''))})
    if not video_streams:
        raise RuntimeError('The video contains no readable video stream.')
    primary = video_streams[0]
    duration = _safe_float(fmt.get('duration'))
    if duration <= 0:
        duration = max((_safe_float(s.get('duration')) for s in streams), default=0.0)
    file_size = _safe_int(fmt.get('size')) or Path(video_path).stat().st_size
    overall_bitrate = _safe_int(fmt.get('bit_rate'))
    format_tags = _clean_tag_dict(fmt.get('tags'))
    chapters = []
    for chapter in data.get('chapters') or []:
        tags = _clean_tag_dict(chapter.get('tags'))
        chapters.append({'id': chapter.get('id'), 'start': _safe_float(chapter.get('start_time')), 'end': _safe_float(chapter.get('end_time')), 'title': tags.get('title', tags.get('TITLE', '')), 'tags': tags})
    filename_tags = detect_filename_tags(Path(video_path).name)
    privacy_flags = _metadata_privacy_flags(format_tags, stream_tag_groups)
    hdr = []
    for stream in video_streams:
        hdr.extend(stream.get('hdr') or [])
    hdr = list(dict.fromkeys(hdr))
    if duration <= 0 or primary['width'] <= 0 or primary['height'] <= 0:
        raise RuntimeError("Could not read the video's duration or resolution.")
    return {'path': str(video_path), 'filename': Path(video_path).name, 'format_name': str(fmt.get('format_name') or ''), 'format_long_name': str(fmt.get('format_long_name') or ''), 'duration': duration, 'size': file_size, 'bitrate': overall_bitrate, 'width': primary['width'], 'height': primary['height'], 'fps': primary['fps'], 'codec': primary['codec'], 'profile': primary['profile'], 'pix_fmt': primary['pix_fmt'], 'hdr': hdr, 'video_streams': video_streams, 'audio_streams': audio_streams, 'subtitle_streams': subtitle_streams, 'chapters': chapters, 'format_tags': format_tags, 'stream_tag_groups': stream_tag_groups, 'filename_tags': filename_tags, 'privacy_flags': privacy_flags}

def _human_bitrate(value):
    value = _safe_int(value)
    if value <= 0:
        return '?'
    if value >= 1000000:
        return f'{value / 1000000:.1f} Mbit/s'
    return f'{value / 1000:.0f} kbit/s'

def format_video_details(details):
    """Human-readable report for the Video Info tab."""
    lines = []
    lines.append('FILE')
    lines.append(f"Name: {details['filename']}")
    lines.append(f"Container: {details.get('format_long_name') or details.get('format_name') or '?'}")
    lines.append(f"Size: {format_bytes(details['size'])}")
    lines.append(f"Duration: {format_duration(details['duration'])}")
    lines.append(f"Total bitrate: {_human_bitrate(details.get('bitrate'))}")
    filename_tags = details.get('filename_tags') or []
    lines.append('')
    lines.append('FILENAME TAGS')
    lines.append(', '.join(filename_tags) if filename_tags else 'No common release tags detected.')
    lines.append('')
    lines.append('VIDEO')
    for stream in details.get('video_streams') or []:
        bits = []
        bits.append(f"Stream #{stream['index']}: {stream['codec'].upper()}")
        if stream.get('profile'):
            bits.append(stream['profile'])
        bits.append(f"{stream['width']}×{stream['height']}")
        if stream.get('fps'):
            bits.append(f"{stream['fps']:.3f} FPS")
        if stream.get('pix_fmt'):
            bits.append(stream['pix_fmt'])
        if stream.get('bitrate'):
            bits.append(_human_bitrate(stream['bitrate']))
        if stream.get('hdr'):
            bits.append(' / '.join(stream['hdr']))
        lines.append(' • '.join(bits))
    lines.append('')
    lines.append(f"AUDIO ({len(details.get('audio_streams') or [])} tracks)")
    if details.get('audio_streams'):
        for stream in details['audio_streams']:
            label = f"Stream #{stream['index']}: {stream['codec'].upper()}"
            extras = []
            if stream.get('profile'):
                extras.append(stream['profile'])
            if stream.get('channels'):
                ch = stream.get('channel_layout') or f"{stream['channels']} channels"
                extras.append(ch)
            if stream.get('sample_rate'):
                extras.append(f"{stream['sample_rate'] / 1000:g} kHz")
            if stream.get('bitrate'):
                extras.append(_human_bitrate(stream['bitrate']))
            if stream.get('language'):
                extras.append(f"language {stream['language']}")
            if stream.get('title'):
                extras.append(f"title: {stream['title']}")
            lines.append(label + (' • ' + ' • '.join(extras) if extras else ''))
    else:
        lines.append('No audio tracks detected.')
    lines.append('')
    lines.append(f"SUBTITLES ({len(details.get('subtitle_streams') or [])} tracks)")
    if details.get('subtitle_streams'):
        for stream in details['subtitle_streams']:
            extras = []
            if stream.get('language'):
                extras.append(f"language {stream['language']}")
            if stream.get('title'):
                extras.append(f"title: {stream['title']}")
            lines.append(f"Stream #{stream['index']}: {stream['codec'].upper()}" + (' • ' + ' • '.join(extras) if extras else ''))
    else:
        lines.append('No subtitle tracks detected.')
    lines.append('')
    lines.append(f"CHAPTERS ({len(details.get('chapters') or [])})")
    if details.get('chapters'):
        preview = details['chapters'][:12]
        for i, chapter in enumerate(preview, start=1):
            title = chapter.get('title') or f'Chapter {i}'
            lines.append(f"{i}. {format_timestamp(chapter['start'])} – {title}")
        if len(details['chapters']) > len(preview):
            lines.append(f"… och {len(details['chapters']) - len(preview)} more")
    else:
        lines.append('No chapters detected.')
    lines.append('')
    lines.append('EMBEDDED METADATA TAGS')
    if details.get('format_tags'):
        lines.append('Container:')
        for key, value in sorted(details['format_tags'].items(), key=lambda x: x[0].casefold()):
            lines.append(f'  {key}: {value}')
    else:
        lines.append('Container: no tags')
    for group in details.get('stream_tag_groups') or []:
        if group['tags']:
            lines.append(f"{group['source']}:")
            for key, value in sorted(group['tags'].items(), key=lambda x: x[0].casefold()):
                lines.append(f'  {key}: {value}')
    lines.append('')
    lines.append('PRIVACY CHECK')
    flags = details.get('privacy_flags') or []
    if flags:
        lines.append('NOTE: The following metadata may reveal the creation environment, date, location, or contact information. Privacy mode does not modify the original video.')
        for flag in flags[:20]:
            value = flag['value']
            if len(value) > 160:
                value = value[:157] + '…'
            lines.append(f"⚠ {flag['source']} / {flag['key']}: {value} ({flag['reason']})")
        if len(flags) > 20:
            lines.append(f'… och {len(flags) - 20} additional flagged fields')
    else:
        lines.append('No clearly privacy-sensitive metadata fields were detected.')
    return '\n'.join(lines)

def percent_positions(duration, count, start_pct, end_pct, clip_duration=0.0):
    if count < 1:
        return []
    if start_pct < 0 or end_pct > 100 or start_pct >= end_pct:
        raise ValueError('The percentage range must be between 0 and 100, with start < end.')
    if count == 1:
        percentages = [(start_pct + end_pct) / 2]
    else:
        step = (end_pct - start_pct) / (count - 1)
        percentages = [start_pct + i * step for i in range(count)]
    max_start = max(0.0, duration - max(0.0, clip_duration) - 0.05)
    return [min(max_start, max(0.0, duration * p / 100.0)) for p in percentages]

def extract_frame(video_path, timestamp, output_png, cancel_event=None):
    ffmpeg, _ = require_ffmpeg()
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-ss', f'{timestamp:.3f}', '-i', str(video_path), '-frames:v', '1', '-an', str(output_png)]
    try:
        run_command(command, cancel_event=cancel_event)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or '').strip()
        raise RuntimeError(f'Could not create screenshot. {detail}') from exc

def load_font(size, bold=False):
    if ImageFont is None:
        return None
    candidates = []
    if os.name == 'nt':
        win_fonts = Path(os.environ.get('WINDIR', 'C:\\Windows')) / 'Fonts'
        candidates.extend([win_fonts / ('arialbd.ttf' if bold else 'arial.ttf'), win_fonts / ('segoeuib.ttf' if bold else 'segoeui.ttf')])
    else:
        candidates.extend([Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'), Path('/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf')])
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except Exception:
                pass
    return ImageFont.load_default()

def format_timestamp(seconds):
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'

def format_duration(seconds):
    return format_timestamp(seconds)

def format_bytes(value):
    value = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024

def add_timestamp_badge(image, timestamp_text, font_size=None):
    if ImageDraw is None:
        return image
    draw = ImageDraw.Draw(image, 'RGBA')
    font_size = font_size or max(14, int(image.width * 0.032))
    font = load_font(font_size, bold=True)
    bbox = draw.textbbox((0, 0), timestamp_text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad_x = max(6, font_size // 3)
    pad_y = max(4, font_size // 4)
    x = image.width - tw - pad_x * 2 - max(8, image.width // 50)
    y = image.height - th - pad_y * 2 - max(8, image.height // 50)
    draw.rounded_rectangle((x, y, x + tw + pad_x * 2, y + th + pad_y * 2), radius=max(4, font_size // 4), fill=(0, 0, 0, 165))
    draw.text((x + pad_x, y + pad_y - bbox[1]), timestamp_text, font=font, fill=(255, 255, 255, 255))
    return image

def ensure_pillow():
    if Image is None:
        raise RuntimeError('Pillow is missing. Install it with: pip install pillow\nTorrent and GIF features can be used without Pillow, but screenshots/contact sheets cannot.')

def generate_screenshots_and_contact_sheet(video_path, output_dir, separate_count=5, layout_cols=3, layout_rows=9, layout_width=1300, margin=5, jpeg_quality=80, start_pct=10, end_pct=90, progress_callback=None, cancel_event=None, overwrite=False):
    ensure_pillow()
    require_ffmpeg()
    check_cancel(cancel_event)
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    meta = probe_video(video_path, cancel_event=cancel_event)
    duration = meta['duration']
    separate_positions = percent_positions(duration, separate_count, start_pct, end_pct)
    layout_count = layout_cols * layout_rows
    layout_positions = percent_positions(duration, layout_count, start_pct, end_pct)
    separate_paths = [output_dir / f'{video_path.stem}_screenshot_{index:02d}.jpg' for index in range(1, separate_count + 1)]
    contact_path = output_dir / f'{video_path.stem}_layout_{layout_cols}x{layout_rows}.jpg'
    total_steps = separate_count + layout_count
    created = []
    skipped = []
    if not overwrite and contact_path.exists() and all((p.exists() for p in separate_paths)):
        if progress_callback:
            progress_callback(total_steps, total_steps, 'Screenshots/contact sheet already exist')
        return {'created': [], 'skipped': separate_paths + [contact_path], 'contact_sheet': contact_path, 'separate_count': separate_count, 'layout_count': layout_count, 'layout_size': Image.open(contact_path).size if Image is not None else (layout_width, 0)}
    with tempfile.TemporaryDirectory(prefix='torrent_frames_') as temp_name:
        temp_dir = Path(temp_name)
        for index, timestamp in enumerate(separate_positions, start=1):
            check_cancel(cancel_event)
            out = separate_paths[index - 1]
            if out.exists() and (not overwrite):
                skipped.append(out)
            else:
                temp_png = temp_dir / f'separate_{index:02d}.png'
                extract_frame(video_path, timestamp, temp_png, cancel_event=cancel_event)
                with Image.open(temp_png) as raw:
                    frame = raw.convert('RGB')
                    add_timestamp_badge(frame, format_timestamp(timestamp))
                    frame.save(out, 'JPEG', quality=jpeg_quality, optimize=True, exif=b'')
                    created.append(out)
            if progress_callback:
                progress_callback(index, total_steps, 'Creating separate screenshots')
        if contact_path.exists() and (not overwrite):
            skipped.append(contact_path)
            if progress_callback:
                progress_callback(total_steps, total_steps, 'Contact sheet already exists')
            layout_size = Image.open(contact_path).size
        else:
            frames = []
            for idx, timestamp in enumerate(layout_positions, start=1):
                check_cancel(cancel_event)
                temp_png = temp_dir / f'layout_{idx:02d}.png'
                extract_frame(video_path, timestamp, temp_png, cancel_event=cancel_event)
                with Image.open(temp_png) as raw:
                    frame = raw.convert('RGB').copy()
                frames.append((frame, timestamp))
                if progress_callback:
                    progress_callback(separate_count + idx, total_steps, 'Creating contact sheet frames')
            content_width = layout_width - margin * (layout_cols + 1)
            cell_width = max(1, content_width // layout_cols)
            aspect = meta['height'] / meta['width']
            cell_height = max(1, int(round(cell_width * aspect)))
            title_font = load_font(26, bold=True)
            info_font = load_font(18, bold=False)
            header_height = 86
            layout_height = header_height + margin + layout_rows * cell_height + margin * (layout_rows + 1)
            sheet = Image.new('RGB', (layout_width, layout_height), (20, 20, 20))
            draw = ImageDraw.Draw(sheet)
            file_size = video_path.stat().st_size
            draw.text((margin + 8, 10), video_path.name, font=title_font, fill=(245, 245, 245))
            info = f"{meta['width']}x{meta['height']}  •  {format_duration(duration)}  •  {format_bytes(file_size)}"
            draw.text((margin + 8, 48), info, font=info_font, fill=(205, 205, 205))
            for idx, (frame, timestamp) in enumerate(frames):
                check_cancel(cancel_event)
                row = idx // layout_cols
                col = idx % layout_cols
                resized = frame.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                add_timestamp_badge(resized, format_timestamp(timestamp), font_size=max(15, cell_width // 24))
                x = margin + col * (cell_width + margin)
                y = header_height + margin * 2 + row * (cell_height + margin)
                sheet.paste(resized, (x, y))
            sheet.save(contact_path, 'JPEG', quality=jpeg_quality, optimize=True, exif=b'')
            created.append(contact_path)
            layout_size = (layout_width, layout_height)
    return {'created': created, 'skipped': skipped, 'contact_sheet': contact_path, 'separate_count': separate_count, 'layout_count': layout_count, 'layout_size': layout_size}

def build_fast_gif_filter(input_count, width, fps, colors):
    """Builds the filter for clips that have already been fast-seeked.

    Each clip is provided as a separate FFmpeg input using -ss/-t so FFmpeg
    does not need to decode the file from the beginning up to the 90% point.
    """
    filters = []
    labels = []
    for i in range(input_count):
        label = f'v{i}'
        filters.append(f'[{i}:v]setpts=PTS-STARTPTS,scale={int(width)}:-2:flags=bilinear,fps={int(fps)}[{label}]')
        labels.append(f'[{label}]')
    filters.append(''.join(labels) + f'concat=n={input_count}:v=1:a=0,split[s0][s1]')
    filters.append(f'[s0]palettegen=max_colors={int(colors)}:stats_mode=diff[p]')
    filters.append('[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle[gif]')
    return ';'.join(filters)

def gif_candidate_settings(base_width, base_fps, clip_seconds):
    """Quality steps from normal preview quality to aggressive compression."""
    base_width = max(240, int(base_width) // 2 * 2)
    base_fps = max(4, int(base_fps))
    raw = [(base_width, base_fps, 192, clip_seconds), (max(240, int(base_width * 0.9) // 2 * 2), max(6, base_fps - 1), 160, clip_seconds), (max(240, int(base_width * 0.8) // 2 * 2), max(6, base_fps - 2), 128, clip_seconds), (max(240, int(base_width * 0.7) // 2 * 2), max(5, base_fps - 3), 96, clip_seconds), (max(240, int(base_width * 0.6) // 2 * 2), 5, 80, clip_seconds), (240, 5, 64, clip_seconds), (240, 5, 64, max(0.2, clip_seconds * 0.8)), (240, 4, 64, max(0.2, clip_seconds * 0.65)), (240, 4, 48, max(0.2, clip_seconds * 0.5))]
    result = []
    for item in raw:
        if item not in result:
            result.append(item)
    return result

def _gif_complexity_score(candidate):
    width, fps, colors, clip_seconds = candidate
    color_factor = max(0.35, (colors / 192.0) ** 0.35)
    return width * width * fps * clip_seconds * color_factor

def _parse_ffmpeg_time(value):
    try:
        hours, minutes, seconds = value.strip().split(':')
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except Exception:
        return None

def run_ffmpeg_with_progress(command, expected_duration, progress_callback=None, cancel_event=None):
    """Runs FFmpeg and reports real progress from -progress pipe:1."""
    check_cancel(cancel_event)
    cmd = list(command)
    cmd[-1:-1] = ['-progress', 'pipe:1', '-nostats', '-nostdin']
    kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'text': True, 'encoding': 'utf-8', 'errors': 'replace', 'bufsize': 1}
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    process = subprocess.Popen(cmd, **kwargs)
    last_fraction = -1.0
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
                raise JobCancelled('The job was cancelled.')
            line = raw_line.strip()
            if line.startswith('out_time='):
                current = _parse_ffmpeg_time(line.split('=', 1)[1])
                if current is not None and expected_duration > 0:
                    fraction = max(0.0, min(1.0, current / expected_duration))
                    if fraction - last_fraction >= 0.01 or fraction >= 1.0:
                        last_fraction = fraction
                        if progress_callback:
                            progress_callback(fraction)
    finally:
        if cancel_event is not None and cancel_event.is_set() and (process.poll() is None):
            try:
                process.terminate()
            except Exception:
                pass
    stderr = process.stderr.read() if process.stderr else ''
    return_code = process.wait()
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled('The job was cancelled.')
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, cmd, output='', stderr=stderr)
    if progress_callback:
        progress_callback(1.0)

def create_size_limited_gif(video_path, output_path, max_size_mb=7.0, start_pct=20.0, end_pct=90.0, clips=8, clip_seconds=1.0, width=540, fps=8, progress_callback=None, cancel_event=None, overwrite=False):
    """Creates a fast size-limited GIF."""
    ffmpeg, _ = require_ffmpeg()
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    check_cancel(cancel_event)
    max_bytes = int(float(max_size_mb) * 1024 * 1024)
    if output_path.exists() and (not overwrite):
        size = output_path.stat().st_size
        if size <= max_bytes:
            if progress_callback:
                progress_callback(1.0, 'GIF already exists')
            return {'path': output_path, 'size': size, 'width': 0, 'fps': 0, 'colors': 0, 'clip_seconds': clip_seconds, 'clips': clips, 'start_pct': start_pct, 'end_pct': end_pct, 'attempts': 0, 'skipped': True}
    meta = probe_video(video_path, cancel_event=cancel_event)
    if max_bytes <= 0:
        raise ValueError('The maximum GIF size must be greater than 0 MB.')
    if clips < 1:
        raise ValueError('The number of GIF clips must be at least 1.')
    if clip_seconds <= 0:
        raise ValueError('Seconds per GIF clip must be greater than 0.')
    if width < 120:
        raise ValueError('GIF width must be at least 120 px.')
    if fps < 1:
        raise ValueError('GIF FPS must be at least 1.')
    all_candidates = gif_candidate_settings(int(width), int(fps), float(clip_seconds))
    remaining = list(all_candidates)
    attempt = 0
    last_size = None
    last_error = None
    first_size = None
    first_candidate = None
    with tempfile.TemporaryDirectory(prefix='torrent_gif_') as temp_name:
        temp_dir = Path(temp_name)
        while remaining:
            check_cancel(cancel_event)
            candidate_settings = remaining.pop(0)
            try_width, try_fps, colors, try_clip_seconds = candidate_settings
            attempt += 1
            starts = percent_positions(meta['duration'], clips, start_pct, end_pct, clip_duration=try_clip_seconds)
            filter_complex = build_fast_gif_filter(len(starts), try_width, try_fps, colors)
            candidate = temp_dir / f'candidate_{attempt:02d}.gif'
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y']
            for start in starts:
                command.extend(['-ss', f'{start:.3f}', '-t', f'{try_clip_seconds:.3f}', '-i', str(video_path)])
            command.extend(['-filter_complex', filter_complex, '-map', '[gif]', '-an', '-map_metadata', '-1', '-loop', '0', str(candidate)])
            expected_duration = len(starts) * try_clip_seconds
            if progress_callback:
                progress_callback((attempt - 1) / max(1, len(all_candidates)), f'GIF attempt {attempt}')
            try:

                def internal_progress(fraction):
                    if progress_callback:
                        base = (attempt - 1) / max(1, len(all_candidates))
                        span = 1.0 / max(1, len(all_candidates))
                        progress_callback(min(0.98, base + fraction * span), f'GIF attempt {attempt}')
                run_ffmpeg_with_progress(command, expected_duration, internal_progress, cancel_event=cancel_event)
            except JobCancelled:
                raise
            except subprocess.CalledProcessError as exc:
                last_error = (exc.stderr or '').strip()
                continue
            check_cancel(cancel_event)
            if not candidate.exists():
                continue
            size = candidate.stat().st_size
            last_size = size
            if first_size is None:
                first_size = size
                first_candidate = candidate_settings
            if size <= max_bytes:
                shutil.copy2(candidate, output_path)
                if progress_callback:
                    progress_callback(1.0, 'GIF complete')
                return {'path': output_path, 'size': size, 'width': try_width, 'fps': try_fps, 'colors': colors, 'clip_seconds': try_clip_seconds, 'clips': clips, 'start_pct': start_pct, 'end_pct': end_pct, 'attempts': attempt, 'skipped': False}
            if attempt == 1 and first_size and first_candidate and remaining:
                ratio = max_bytes / first_size
                desired_score = _gif_complexity_score(first_candidate) * ratio * 0.85
                eligible = [c for c in remaining if _gif_complexity_score(c) <= desired_score]
                if eligible:
                    best = max(eligible, key=_gif_complexity_score)
                    remaining.remove(best)
                    remaining.insert(0, best)
    if last_error and last_size is None:
        raise RuntimeError(f'FFmpeg could not create the GIF. {last_error}')
    if last_size is not None:
        raise RuntimeError(f'Could not reduce the GIF below {max_size_mb:g} MB using reasonable quality settings. The smallest attempt was {format_bytes(last_size)}. Try fewer clips or a shorter duration per clip.')
    raise RuntimeError('The GIF could not be created.')

def build_fast_webp_filter(input_count, width, fps):
    """Builds a filter graph for an animated WebP from fast-seeked clips."""
    filters = []
    labels = []
    for i in range(input_count):
        label = f'v{i}'
        filters.append(f'[{i}:v]setpts=PTS-STARTPTS,scale={int(width)}:-2:flags=bilinear,fps={int(fps)}[{label}]')
        labels.append(f'[{label}]')
    filters.append(''.join(labels) + f'concat=n={input_count}:v=1:a=0[preview]')
    return ';'.join(filters)

def webp_candidate_settings(base_width, base_fps, clip_seconds):
    """Quality steps for animated WebP, from high quality to aggressive compression."""
    base_width = max(240, int(base_width) // 2 * 2)
    base_fps = max(4, int(base_fps))
    raw = [
        (base_width, base_fps, 82, clip_seconds),
        (max(240, int(base_width * 0.95) // 2 * 2), base_fps, 74, clip_seconds),
        (max(240, int(base_width * 0.88) // 2 * 2), max(6, base_fps - 1), 68, clip_seconds),
        (max(240, int(base_width * 0.80) // 2 * 2), max(6, base_fps - 2), 60, clip_seconds),
        (max(240, int(base_width * 0.72) // 2 * 2), max(5, base_fps - 3), 52, clip_seconds),
        (max(240, int(base_width * 0.62) // 2 * 2), 5, 45, clip_seconds),
        (240, 5, 40, clip_seconds),
        (240, 5, 36, max(0.2, clip_seconds * 0.8)),
        (240, 4, 32, max(0.2, clip_seconds * 0.65)),
        (240, 4, 28, max(0.2, clip_seconds * 0.5)),
    ]
    result = []
    for item in raw:
        if item not in result:
            result.append(item)
    return result

def _webp_complexity_score(candidate):
    width, fps, quality, clip_seconds = candidate
    quality_factor = max(0.3, quality / 82.0)
    return width * width * fps * clip_seconds * quality_factor

def create_size_limited_webp(video_path, output_path, max_size_mb=7.0, start_pct=20.0, end_pct=90.0, clips=8, clip_seconds=1.0, width=540, fps=8, progress_callback=None, cancel_event=None, overwrite=False):
    """Creates a size-limited animated WebP using FFmpeg/libwebp."""
    ffmpeg, _ = require_ffmpeg()
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    check_cancel(cancel_event)
    max_bytes = int(float(max_size_mb) * 1024 * 1024)
    if output_path.exists() and (not overwrite):
        size = output_path.stat().st_size
        if size <= max_bytes:
            if progress_callback:
                progress_callback(1.0, 'WebP already exists')
            return {'path': output_path, 'size': size, 'width': 0, 'fps': 0, 'quality': 0, 'clip_seconds': clip_seconds, 'clips': clips, 'start_pct': start_pct, 'end_pct': end_pct, 'attempts': 0, 'skipped': True}
    meta = probe_video(video_path, cancel_event=cancel_event)
    if max_bytes <= 0:
        raise ValueError('The maximum WebP size must be greater than 0 MB.')
    if clips < 1:
        raise ValueError('The number of WebP clips must be at least 1.')
    if clip_seconds <= 0:
        raise ValueError('Seconds per WebP clip must be greater than 0.')
    if width < 120:
        raise ValueError('WebP width must be at least 120 px.')
    if fps < 1:
        raise ValueError('WebP FPS must be at least 1.')
    all_candidates = webp_candidate_settings(int(width), int(fps), float(clip_seconds))
    remaining = list(all_candidates)
    attempt = 0
    last_size = None
    last_error = None
    first_size = None
    first_candidate = None
    with tempfile.TemporaryDirectory(prefix='torrent_webp_') as temp_name:
        temp_dir = Path(temp_name)
        while remaining:
            check_cancel(cancel_event)
            candidate_settings = remaining.pop(0)
            try_width, try_fps, quality, try_clip_seconds = candidate_settings
            attempt += 1
            starts = percent_positions(meta['duration'], clips, start_pct, end_pct, clip_duration=try_clip_seconds)
            filter_complex = build_fast_webp_filter(len(starts), try_width, try_fps)
            candidate = temp_dir / f'candidate_{attempt:02d}.webp'
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y']
            for start in starts:
                command.extend(['-ss', f'{start:.3f}', '-t', f'{try_clip_seconds:.3f}', '-i', str(video_path)])
            command.extend([
                '-filter_complex', filter_complex,
                '-map', '[preview]', '-an', '-map_metadata', '-1',
                '-c:v', 'libwebp_anim', '-lossless', '0',
                '-quality', str(int(quality)), '-compression_level', '6',
                '-loop', '0', str(candidate)
            ])
            expected_duration = len(starts) * try_clip_seconds
            if progress_callback:
                progress_callback((attempt - 1) / max(1, len(all_candidates)), f'WebP attempt {attempt}')
            try:
                def internal_progress(fraction):
                    if progress_callback:
                        base = (attempt - 1) / max(1, len(all_candidates))
                        span = 1.0 / max(1, len(all_candidates))
                        progress_callback(min(0.98, base + fraction * span), f'WebP attempt {attempt}')
                run_ffmpeg_with_progress(command, expected_duration, internal_progress, cancel_event=cancel_event)
            except JobCancelled:
                raise
            except subprocess.CalledProcessError as exc:
                last_error = (exc.stderr or '').strip()
                continue
            check_cancel(cancel_event)
            if not candidate.exists():
                continue
            size = candidate.stat().st_size
            last_size = size
            if first_size is None:
                first_size = size
                first_candidate = candidate_settings
            if size <= max_bytes:
                shutil.copy2(candidate, output_path)
                if progress_callback:
                    progress_callback(1.0, 'WebP complete')
                return {'path': output_path, 'size': size, 'width': try_width, 'fps': try_fps, 'quality': quality, 'clip_seconds': try_clip_seconds, 'clips': clips, 'start_pct': start_pct, 'end_pct': end_pct, 'attempts': attempt, 'skipped': False}
            if attempt == 1 and first_size and first_candidate and remaining:
                ratio = max_bytes / first_size
                desired_score = _webp_complexity_score(first_candidate) * ratio * 0.85
                eligible = [c for c in remaining if _webp_complexity_score(c) <= desired_score]
                if eligible:
                    best = max(eligible, key=_webp_complexity_score)
                    remaining.remove(best)
                    remaining.insert(0, best)
    if last_error and last_size is None:
        raise RuntimeError(f'FFmpeg could not create the WebP. {last_error}')
    if last_size is not None:
        raise RuntimeError(f'Could not reduce the WebP below {max_size_mb:g} MB using reasonable quality settings. The smallest attempt was {format_bytes(last_size)}. Try fewer clips or a shorter duration per clip.')
    raise RuntimeError('The WebP could not be created.')

PREVIEW_FORMATS = ('GIF', 'WebP', 'GIF + WebP')
PROCESS_MODES = ('Everything', 'Torrent only', 'Screenshots/contact sheet only', 'Animated preview only')
PROCESS_MODE_MIGRATION = {
    'Allt': 'Everything',
    'Endast torrent': 'Torrent only',
    'Endast skärmbilder/layout': 'Screenshots/contact sheet only',
    'Endast GIF': 'Animated preview only',
    'GIF only': 'Animated preview only',
}


def app_settings_path():
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA') or Path.home())
        return base / 'TorrentCreator' / 'settings.json'
    return Path.home() / '.torrent_creator' / 'settings.json'

def open_folder(path):
    path = str(Path(path))
    try:
        if os.name == 'nt':
            os.startfile(path)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception as exc:
        raise RuntimeError(f'Could not open folder: {exc}') from exc

def friendly_error(exc):
    text = str(exc).strip() or exc.__class__.__name__
    low = text.lower()
    if 'permission denied' in low or 'access is denied' in low:
        return 'Access denied. Make sure the files are not locked and that you have permission to write to the output folder.'
    if 'no space left' in low or 'disk full' in low:
        return 'There is not enough free disk space.'
    if 'invalid data found' in low:
        return 'FFmpeg could not read the video file correctly.'
    if 'ffmpeg/ffprobe were not found' in low:
        return 'FFmpeg is missing. Build the Windows version with BUILD_WINDOWS_EXE.bat to bundle FFmpeg.'
    return text[:900]

def phases_for_settings(settings):
    mode = settings['process_mode']
    if mode == 'Torrent only':
        return ['torrent']
    if mode == 'Screenshots/contact sheet only':
        return ['screens']
    if mode == 'Animated preview only':
        return ['gif']
    phases = ['torrent']
    if settings['make_screens']:
        phases.append('screens')
    if settings['make_gif']:
        phases.append('gif')
    return phases

def job_output_dir(source, settings):
    base = Path(settings['output_dir'])
    if settings['subfolder_per_job']:
        label = source.stem if source.is_file() else source.name
        return base / label
    return base

def expected_paths(source, video, job_dir, settings, phases):
    paths = {}
    label = source.stem if source.is_file() else source.name
    if 'torrent' in phases:
        paths['torrent'] = job_dir / f'{label}.torrent'
    if video is not None and 'screens' in phases:
        paths['screens'] = [job_dir / f'{video.stem}_screenshot_{i:02d}.jpg' for i in range(1, int(settings['separate_count']) + 1)]
        paths['layout'] = job_dir / f"{video.stem}_layout_{int(settings['layout_cols'])}x{int(settings['layout_rows'])}.jpg"
    if video is not None and 'gif' in phases:
        preview_format = settings.get('preview_format', 'GIF')
        if preview_format in ('GIF', 'GIF + WebP'):
            paths['gif'] = job_dir / f'{video.stem}_preview.gif'
        if preview_format in ('WebP', 'GIF + WebP'):
            paths['webp'] = job_dir / f'{video.stem}_preview.webp'
    return paths

def verify_job_outputs(source, video, job_dir, settings, phases):
    """Checks that the expected output files were created and are usable."""
    issues = []
    paths = expected_paths(source, video, job_dir, settings, phases)
    torrent = paths.get('torrent')
    if torrent is not None and (not torrent.exists() or torrent.stat().st_size < 32):
        issues.append('torrent file is missing or empty')
    for image_path in paths.get('screens', []):
        if not image_path.exists() or image_path.stat().st_size == 0:
            issues.append(f'screenshot is missing: {image_path.name}')
    layout = paths.get('layout')
    if layout is not None:
        if not layout.exists() or layout.stat().st_size == 0:
            issues.append('contact sheet is missing')
        elif Image is not None:
            try:
                with Image.open(layout) as im:
                    if im.width != int(settings['layout_width']):
                        issues.append(f"contact sheet is {im.width}px wide, expected {int(settings['layout_width'])}px")
            except Exception:
                issues.append('contact sheet could not be verified')
    gif = paths.get('gif')
    if gif is not None:
        if not gif.exists() or gif.stat().st_size == 0:
            issues.append('GIF file is missing')
        else:
            max_bytes = int(float(settings['gif_max_mb']) * 1024 * 1024)
            if gif.stat().st_size > max_bytes:
                issues.append(f"GIF file is {format_bytes(gif.stat().st_size)}, above the limit {settings['gif_max_mb']:g} MB")
    webp = paths.get('webp')
    if webp is not None:
        if not webp.exists() or webp.stat().st_size == 0:
            issues.append('WebP file is missing')
        else:
            max_bytes = int(float(settings['gif_max_mb']) * 1024 * 1024)
            if webp.stat().st_size > max_bytes:
                issues.append(f"WebP file is {format_bytes(webp.stat().st_size)}, above the limit {settings['gif_max_mb']:g} MB")
    return issues

class TorrentCreatorApp(BaseTk):
    PIECE_OPTIONS = {'Auto': 0, '256 KiB': 256 * 1024, '512 KiB': 512 * 1024, '1 MiB': 1024 * 1024, '2 MiB': 2 * 1024 * 1024, '4 MiB': 4 * 1024 * 1024}

    def __init__(self):
        super().__init__()
        self.title(f'{APP_NAME} {APP_VERSION}')
        self.geometry('980x850')
        self.minsize(860, 720)
        self.jobs = []
        self.next_job_id = 1
        self.ui_events = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread = None
        self.queue_started_at = None
        self.busy = False
        self.output_dir_var = tk.StringVar()
        self.subfolder_per_job_var = tk.BooleanVar(value=True)
        self.process_mode_var = tk.StringVar(value='Everything')
        self.overwrite_var = tk.BooleanVar(value=False)
        self.verify_var = tk.BooleanVar(value=True)
        self.open_folder_var = tk.BooleanVar(value=False)
        self.selected_info_var = tk.StringVar(value='Select a job and click Quick Check for technical information.')
        self.metadata_status_var = tk.StringVar(value='Select a job and run Quick Check to read video information and tags.')
        self.piece_var = tk.StringVar(value='Auto')
        self.private_var = tk.BooleanVar(value=False)
        self.privacy_mode_var = tk.BooleanVar(value=True)
        self.make_screens_var = tk.BooleanVar(value=True)
        self.separate_count_var = tk.IntVar(value=5)
        self.layout_cols_var = tk.IntVar(value=3)
        self.layout_rows_var = tk.IntVar(value=9)
        self.layout_width_var = tk.IntVar(value=1300)
        self.margin_var = tk.IntVar(value=5)
        self.jpeg_quality_var = tk.IntVar(value=80)
        self.image_start_pct_var = tk.DoubleVar(value=10)
        self.image_end_pct_var = tk.DoubleVar(value=90)
        self.make_gif_var = tk.BooleanVar(value=True)
        self.preview_format_var = tk.StringVar(value='GIF')
        self.gif_max_mb_var = tk.DoubleVar(value=7.0)
        self.gif_start_pct_var = tk.DoubleVar(value=20.0)
        self.gif_end_pct_var = tk.DoubleVar(value=90.0)
        self.gif_clips_var = tk.IntVar(value=8)
        self.gif_seconds_var = tk.DoubleVar(value=1.0)
        self.gif_width_var = tk.IntVar(value=540)
        self.gif_fps_var = tk.IntVar(value=8)
        self.current_progress_var = tk.StringVar(value='0%')
        self.queue_progress_var = tk.StringVar(value='0%')
        self.status_var = tk.StringVar(value='Add video files to the job queue.')
        self.queue_status_var = tk.StringVar(value='Queue: 0 jobs')
        self.elapsed_var = tk.StringVar(value='Elapsed: 00:00')
        self.eta_var = tk.StringVar(value='Remaining: —')
        self._build_ui()
        self._load_settings()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.after(80, self._drain_ui_events)

    def _build_ui(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        outer.rowconfigure(3, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, font=('Segoe UI', 18, 'bold')).grid(row=0, column=0, sticky='w')
        dd = 'Drag & drop enabled' if DRAGDROP_AVAILABLE else 'Drag & drop unavailable in Python mode'
        ttk.Label(header, text=f'v{APP_VERSION}  •  {ffmpeg_bundle_status()}  •  {dd}').grid(row=0, column=1, sticky='e')
        output_box = ttk.LabelFrame(outer, text='Output', padding=10)
        output_box.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        output_box.columnconfigure(1, weight=1)
        ttk.Label(output_box, text='Output folder').grid(row=0, column=0, sticky='w')
        ttk.Entry(output_box, textvariable=self.output_dir_var).grid(row=0, column=1, sticky='ew', padx=8)
        ttk.Button(output_box, text='Choose folder', command=self.choose_output_dir).grid(row=0, column=2, sticky='ew', padx=(0, 5))
        ttk.Button(output_box, text='Open', command=self.open_output_dir).grid(row=0, column=3, sticky='ew')
        ttk.Checkbutton(output_box, text='Separate subfolder for each video', variable=self.subfolder_per_job_var).grid(row=1, column=1, columnspan=3, sticky='w', padx=8, pady=(6, 0))
        queue_box = ttk.LabelFrame(outer, text='Job queue – files are always processed one at a time', padding=10)
        queue_box.grid(row=2, column=0, sticky='nsew', pady=(0, 10))
        queue_box.columnconfigure(0, weight=1)
        queue_box.rowconfigure(1, weight=1)
        queue_buttons = ttk.Frame(queue_box)
        queue_buttons.grid(row=0, column=0, sticky='ew', pady=(0, 7))
        buttons = [('Add files', self.add_files), ('Add folder job', self.add_folder_job), ('Remove', self.remove_selected_jobs), ('Move up', lambda: self.move_selected(-1)), ('Move down', lambda: self.move_selected(1)), ('Quick Check', self.quick_check_selected)]
        self.queue_control_buttons = []
        for text, command in buttons:
            btn = ttk.Button(queue_buttons, text=text, command=command)
            btn.pack(side='left', padx=(0, 5))
            self.queue_control_buttons.append(btn)
        columns = ('status', 'source', 'info')
        self.queue_tree = ttk.Treeview(queue_box, columns=columns, show='headings', height=7, selectmode='extended')
        self.queue_tree.heading('status', text='Status')
        self.queue_tree.heading('source', text='File / folder')
        self.queue_tree.heading('info', text='Quick Check')
        self.queue_tree.column('status', width=115, minwidth=95, stretch=False)
        self.queue_tree.column('source', width=470, minwidth=250, stretch=True)
        self.queue_tree.column('info', width=285, minwidth=180, stretch=True)
        self.queue_tree.grid(row=1, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(queue_box, orient='vertical', command=self.queue_tree.yview)
        scrollbar.grid(row=1, column=1, sticky='ns')
        self.queue_tree.configure(yscrollcommand=scrollbar.set)
        self.queue_tree.bind('<<TreeviewSelect>>', self._queue_selection_changed)
        if DRAGDROP_AVAILABLE:
            try:
                self.queue_tree.drop_target_register(DND_FILES)
                self.queue_tree.dnd_bind('<<Drop>>', self._on_drop)
            except Exception:
                pass
        drag_text = 'You can also drag video files directly onto the list.' if DRAGDROP_AVAILABLE else 'In the Windows build, you can drag video files directly onto the list.'
        ttk.Label(queue_box, text=drag_text).grid(row=2, column=0, sticky='w', pady=(6, 0))
        ttk.Label(queue_box, textvariable=self.selected_info_var, wraplength=840).grid(row=3, column=0, sticky='w', pady=(4, 0))
        notebook = ttk.Notebook(outer)
        notebook.grid(row=3, column=0, sticky='nsew')
        run_tab = ttk.Frame(notebook, padding=14)
        torrent_tab = ttk.Frame(notebook, padding=14)
        images_tab = ttk.Frame(notebook, padding=14)
        gif_tab = ttk.Frame(notebook, padding=14)
        media_tab = ttk.Frame(notebook, padding=14)
        notebook.add(run_tab, text='Processing')
        notebook.add(torrent_tab, text='Torrent')
        notebook.add(images_tab, text='Screenshots')
        notebook.add(gif_tab, text='Animated Preview')
        notebook.add(media_tab, text='Video-info & tags')
        self._build_run_tab(run_tab)
        self._build_torrent_tab(torrent_tab)
        self._build_images_tab(images_tab)
        self._build_gif_tab(gif_tab)
        self._build_media_tab(media_tab)
        progress_box = ttk.LabelFrame(outer, text='Progress', padding=10)
        progress_box.grid(row=4, column=0, sticky='ew', pady=(10, 0))
        progress_box.columnconfigure(1, weight=1)
        ttk.Label(progress_box, text='Current job').grid(row=0, column=0, sticky='w')
        self.current_progress = ttk.Progressbar(progress_box, maximum=100)
        self.current_progress.grid(row=0, column=1, sticky='ew', padx=8)
        ttk.Label(progress_box, textvariable=self.current_progress_var, width=6, anchor='e').grid(row=0, column=2)
        ttk.Label(progress_box, text='Entire queue').grid(row=1, column=0, sticky='w', pady=(7, 0))
        self.queue_progress = ttk.Progressbar(progress_box, maximum=100)
        self.queue_progress.grid(row=1, column=1, sticky='ew', padx=8, pady=(7, 0))
        ttk.Label(progress_box, textvariable=self.queue_progress_var, width=6, anchor='e').grid(row=1, column=2, pady=(7, 0))
        status_line = ttk.Frame(progress_box)
        status_line.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(8, 0))
        status_line.columnconfigure(1, weight=1)
        ttk.Label(status_line, textvariable=self.queue_status_var).grid(row=0, column=0, sticky='w')
        ttk.Label(status_line, textvariable=self.status_var).grid(row=0, column=1, sticky='w', padx=14)
        ttk.Label(status_line, textvariable=self.elapsed_var).grid(row=0, column=2, sticky='e', padx=(10, 0))
        ttk.Label(status_line, textvariable=self.eta_var).grid(row=0, column=3, sticky='e', padx=(10, 0))
        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, sticky='ew', pady=(10, 0))
        actions.columnconfigure(0, weight=1)
        self.start_button = ttk.Button(actions, text='Start queue', command=self.start_queue)
        self.start_button.grid(row=0, column=0, sticky='ew', padx=(0, 6), ipady=6)
        self.cancel_button = ttk.Button(actions, text='Cancel', command=self.cancel_current, state='disabled')
        self.cancel_button.grid(row=0, column=1, sticky='ew', ipady=6)

    def _build_run_tab(self, tab):
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text='What to create').grid(row=0, column=0, sticky='w', pady=5)
        ttk.Combobox(tab, textvariable=self.process_mode_var, values=PROCESS_MODES, state='readonly', width=28).grid(row=0, column=1, sticky='w', padx=8)
        ttk.Checkbutton(tab, text='Overwrite existing files', variable=self.overwrite_var).grid(row=1, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Label(tab, text='Off = existing output files are skipped. Turn this on when you want to regenerate only the animated preview, contact sheet, or torrent.', wraplength=690).grid(row=2, column=0, columnspan=3, sticky='w', pady=(0, 7))
        ttk.Checkbutton(tab, text='Verify output after each job', variable=self.verify_var).grid(row=3, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Checkbutton(tab, text='Open output folder when queue finishes', variable=self.open_folder_var).grid(row=4, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Checkbutton(tab, text='Separate subfolder for each video', variable=self.subfolder_per_job_var).grid(row=5, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Button(tab, text='Reset default settings', command=self.reset_defaults).grid(row=6, column=0, sticky='w', pady=(14, 0))
        ttk.Label(tab, text='The job queue is sequential: the next video starts only after the previous one is completely finished. Temporary FFmpeg files are cleaned up automatically after each step.', wraplength=690).grid(row=7, column=0, columnspan=3, sticky='w', pady=(14, 0))

    def _build_torrent_tab(self, tab):
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text='Tracker(s)').grid(row=0, column=0, sticky='nw', pady=5)
        tracker_frame = ttk.Frame(tab)
        tracker_frame.grid(row=0, column=1, columnspan=2, sticky='nsew', padx=(8, 0))
        tracker_frame.columnconfigure(0, weight=1)
        tracker_frame.rowconfigure(0, weight=1)
        self.trackers_text = tk.Text(tracker_frame, height=6, wrap='none', font=('Consolas', 10))
        self.trackers_text.grid(row=0, column=0, sticky='nsew')
        tracker_scroll = ttk.Scrollbar(tracker_frame, orient='vertical', command=self.trackers_text.yview)
        tracker_scroll.grid(row=0, column=1, sticky='ns')
        self.trackers_text.configure(yscrollcommand=tracker_scroll.set)
        ttk.Label(tab, text='Piece size').grid(row=1, column=0, sticky='w', pady=5)
        ttk.Combobox(tab, textvariable=self.piece_var, values=list(self.PIECE_OPTIONS.keys()), state='readonly', width=18).grid(row=1, column=1, sticky='w', padx=8)
        ttk.Checkbutton(tab, text='Private torrent (private flag)', variable=self.private_var).grid(row=1, column=2, sticky='w')
        ttk.Label(tab, text='Comment').grid(row=2, column=0, sticky='nw', pady=5)
        self.comment_text = tk.Text(tab, height=4, wrap='word')
        self.comment_text.grid(row=2, column=1, columnspan=2, sticky='nsew', padx=(8, 0))
        ttk.Checkbutton(tab, text='Privacy mode (recommended)', variable=self.privacy_mode_var).grid(row=3, column=0, columnspan=3, sticky='w', pady=(10, 2))
        ttk.Label(tab, text='Removes created-by, creation-date, and comment metadata from the torrent file and strips metadata from generated JPG/GIF/WebP files. The filename is always preserved and still shown on the contact sheet.', wraplength=690).grid(row=4, column=0, columnspan=3, sticky='w')
        tab.rowconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

    def _build_images_tab(self, tab):
        tab.columnconfigure(1, weight=1)
        ttk.Checkbutton(tab, text='Create separate screenshots and a contact sheet when Everything is selected', variable=self.make_screens_var).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))
        rows = [('Separate screenshots', self.separate_count_var, 'Default: 5'), ('Contact sheet columns', self.layout_cols_var, 'Default: 3'), ('Contact sheet rows', self.layout_rows_var, 'Default: 9'), ('Contact sheet width (px)', self.layout_width_var, 'Default: 1300'), ('Margin (px)', self.margin_var, 'Default: 5'), ('JPEG quality', self.jpeg_quality_var, 'Default: 80'), ('From video (%)', self.image_start_pct_var, 'Default: 10'), ('To video (%)', self.image_end_pct_var, 'Default: 90')]
        for r, (label, var, hint) in enumerate(rows, start=1):
            ttk.Label(tab, text=label).grid(row=r, column=0, sticky='w', pady=4)
            ttk.Entry(tab, textvariable=var, width=14).grid(row=r, column=1, sticky='w', padx=8)
            ttk.Label(tab, text=hint).grid(row=r, column=2, sticky='w')
        ttk.Label(tab, text="The filename is shown clearly at the top of the contact sheet. Each frame has a timestamp. The contact sheet height is calculated automatically from the video's aspect ratio.", wraplength=690).grid(row=10, column=0, columnspan=3, sticky='w', pady=(10, 0))

    def _build_gif_tab(self, tab):
        tab.columnconfigure(1, weight=1)
        ttk.Checkbutton(tab, text='Create animated preview when Everything is selected', variable=self.make_gif_var).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))
        ttk.Label(tab, text='Preview format').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Combobox(tab, textvariable=self.preview_format_var, values=PREVIEW_FORMATS, state='readonly', width=18).grid(row=1, column=1, sticky='w', padx=8)
        ttk.Label(tab, text='GIF, WebP, or both').grid(row=1, column=2, sticky='w')
        rows = [('Maximum size per preview (MB)', self.gif_max_mb_var, 'Default: 7'), ('From video (%)', self.gif_start_pct_var, 'Default: 20'), ('To video (%)', self.gif_end_pct_var, 'Default: 90'), ('Number of short clips', self.gif_clips_var, 'Default: 8'), ('Seconds per clip', self.gif_seconds_var, 'Default: 1.0'), ('Preview width (px)', self.gif_width_var, 'Default: 540'), ('Frame rate (FPS)', self.gif_fps_var, 'Default: 8')]
        for r, (label, var, hint) in enumerate(rows, start=2):
            ttk.Label(tab, text=label).grid(row=r, column=0, sticky='w', pady=4)
            ttk.Entry(tab, textvariable=var, width=14).grid(row=r, column=1, sticky='w', padx=8)
            ttk.Label(tab, text=hint).grid(row=r, column=2, sticky='w')
        ttk.Label(tab, text='GIF and animated WebP use the same selected video segments. Each generated preview is automatically compressed to stay under the selected maximum size. WebP usually provides better quality at a smaller file size.', wraplength=690).grid(row=10, column=0, columnspan=3, sticky='w', pady=(10, 0))

    def _build_media_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text='FFprobe reads technical information, audio/subtitles, HDR, chapters, embedded metadata, and common tags in the filename.', wraplength=700).grid(row=0, column=0, sticky='w')
        self.metadata_analyze_button = ttk.Button(top, text='Analyze selected job', command=self.quick_check_selected)
        self.metadata_analyze_button.grid(row=0, column=1, sticky='e', padx=(10, 0))
        ttk.Label(tab, textvariable=self.metadata_status_var, wraplength=820).grid(row=1, column=0, sticky='w', pady=(0, 8))
        frame = ttk.Frame(tab)
        frame.grid(row=2, column=0, sticky='nsew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.metadata_text = tk.Text(frame, wrap='word', font=('Consolas', 10), height=15, state='disabled')
        self.metadata_text.grid(row=0, column=0, sticky='nsew')
        yscroll = ttk.Scrollbar(frame, orient='vertical', command=self.metadata_text.yview)
        yscroll.grid(row=0, column=1, sticky='ns')
        self.metadata_text.configure(yscrollcommand=yscroll.set)
        self._set_metadata_text("No video has been analyzed yet.\n\nSelect a job in the queue and click 'Quick Check' or 'Analyze selected job'.")

    def _set_metadata_text(self, text):
        if not hasattr(self, 'metadata_text'):
            return
        try:
            self.metadata_text.configure(state='normal')
            self.metadata_text.delete('1.0', 'end')
            self.metadata_text.insert('1.0', text or '')
            self.metadata_text.configure(state='disabled')
        except tk.TclError:
            pass

    def add_files(self):
        paths = filedialog.askopenfilenames(title='Add video files', filetypes=[('Video files', '*.mp4 *.mkv *.avi *.mov *.wmv *.m4v *.webm *.mpg *.mpeg *.ts *.mts *.m2ts'), ('All files', '*.*')])
        self._add_paths(paths)

    def add_folder_job(self):
        path = filedialog.askdirectory(title='Add folder as a job')
        if path:
            self._add_paths([path])

    def _add_paths(self, paths):
        existing = {str(Path(j['source']).resolve()).casefold() for j in self.jobs}
        added = 0
        for raw in paths:
            p = Path(str(raw).strip())
            if not p.exists():
                continue
            try:
                key = str(p.resolve()).casefold()
            except Exception:
                key = str(p).casefold()
            if key in existing:
                continue
            job = {'id': self.next_job_id, 'source': str(p), 'status': 'Waiting', 'info': 'Not analyzed', 'detail': '', 'metadata_text': 'Not analyzed yet.', 'privacy_flags': 0}
            self.next_job_id += 1
            self.jobs.append(job)
            existing.add(key)
            added += 1
        if added:
            self._rebuild_tree()
            self.queue_status_var.set(f'Queue: {len(self.jobs)} jobs')
            self.status_var.set(f'{added} job(s) added. The queue runs one at a time.')

    def _on_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self._add_paths(paths)
        return 'break'

    def _rebuild_tree(self, select_ids=None):
        current = set(select_ids or self.queue_tree.selection())
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)
        for job in self.jobs:
            iid = str(job['id'])
            self.queue_tree.insert('', 'end', iid=iid, values=(job['status'], job['source'], job['info']))
            if iid in current:
                self.queue_tree.selection_add(iid)

    def _job_by_id(self, job_id):
        for job in self.jobs:
            if job['id'] == int(job_id):
                return job
        return None

    def remove_selected_jobs(self):
        if self.busy:
            return
        selected = {int(i) for i in self.queue_tree.selection()}
        if not selected:
            return
        self.jobs = [j for j in self.jobs if j['id'] not in selected]
        self._rebuild_tree()
        self.queue_status_var.set(f'Queue: {len(self.jobs)} jobs')
        self.selected_info_var.set('Select a job and click Quick Check for technical information.')
        self.metadata_status_var.set('Select a job and run Quick Check to read video information and tags.')
        self._set_metadata_text('No video selected.')

    def move_selected(self, direction):
        if self.busy:
            return
        selection = self.queue_tree.selection()
        if len(selection) != 1:
            return
        job_id = int(selection[0])
        index = next((i for i, j in enumerate(self.jobs) if j['id'] == job_id), None)
        if index is None:
            return
        new_index = max(0, min(len(self.jobs) - 1, index + direction))
        if new_index == index:
            return
        job = self.jobs.pop(index)
        self.jobs.insert(new_index, job)
        self._rebuild_tree(select_ids=[str(job_id)])
        self.queue_tree.see(str(job_id))

    def _queue_selection_changed(self, _event=None):
        selection = self.queue_tree.selection()
        if not selection:
            return
        job = self._job_by_id(selection[0])
        if job:
            detail = job.get('detail') or job.get('info') or 'Not analyzed'
            self.selected_info_var.set(detail)
            self._set_metadata_text(job.get('metadata_text') or 'Not analyzed yet.')
            flags = int(job.get('privacy_flags') or 0)
            if flags:
                self.metadata_status_var.set(f'Privacy check: {flags} metadata field(s) were flagged. See the report below.')
            elif job.get('metadata_text') and job.get('metadata_text') != 'Not analyzed yet.':
                self.metadata_status_var.set('Metadata analyzed. No clearly privacy-sensitive fields were flagged.')
            else:
                self.metadata_status_var.set('Select a job and run Quick Check to read video information and tags.')

    def quick_check_selected(self):
        selection = self.queue_tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, 'Select a job in the list first.')
            return
        job = self._job_by_id(selection[0])
        if not job:
            return
        job['info'] = 'Analyzing tags...'
        self.metadata_status_var.set(f"Analyzing {Path(job['source']).name}...")
        self._set_metadata_text('Reading video information, streams, chapters, and metadata with FFprobe...')
        self._rebuild_tree(select_ids=selection)
        threading.Thread(target=self._quick_check_worker, args=(job['id'], job['source']), daemon=True).start()

    def _quick_check_worker(self, job_id, source_text):
        try:
            source = Path(source_text)
            video = find_video_source(source)
            details = probe_video_details(video)
            fps_text = f"{details['fps']:.3f} fps" if details['fps'] else '? fps'
            extras = []
            if details.get('hdr'):
                extras.append('/'.join(details['hdr']))
            extras.append(f"{len(details.get('audio_streams') or [])} audio")
            extras.append(f"{len(details.get('subtitle_streams') or [])} subtitles")
            if details.get('privacy_flags'):
                extras.append(f"⚠ {len(details['privacy_flags'])} metadata")
            info = f"{details['width']}×{details['height']} • {details['codec'].upper()} • {fps_text}" + (' • ' + ' • '.join(extras) if extras else '')
            detected = details.get('filename_tags') or []
            tag_text = f" • Tags: {', '.join(detected)}" if detected else ''
            detail = f"{video.name} — {format_duration(details['duration'])} • {format_bytes(details['size'])} • {details['codec'].upper()} • {fps_text}{tag_text}"
            report = format_video_details(details)
            self.ui_events.put(('analysis', job_id, info, detail, report, len(details.get('privacy_flags') or [])))
        except Exception as exc:
            error = friendly_error(exc)
            self.ui_events.put(('analysis', job_id, 'Check failed', error, f'Analysis failed.\n\n{error}', 0))

    def choose_output_dir(self):
        current = self.output_dir_var.get().strip()
        initial = current if current and Path(current).exists() else None
        path = filedialog.askdirectory(title='Choose output folder', initialdir=initial)
        if path:
            self.output_dir_var.set(path)

    def open_output_dir(self):
        path = self.output_dir_var.get().strip()
        if not path:
            messagebox.showinfo(APP_NAME, 'Choose an output folder first.')
            return
        Path(path).mkdir(parents=True, exist_ok=True)
        try:
            open_folder(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def validate_settings(self):
        if self.process_mode_var.get() not in PROCESS_MODES:
            raise ValueError('Invalid processing mode.')
        if self.separate_count_var.get() < 1:
            raise ValueError('The number of separate screenshots must be at least 1.')
        if self.layout_cols_var.get() < 1 or self.layout_rows_var.get() < 1:
            raise ValueError('Contact sheet rows and columns must be at least 1.')
        if self.layout_width_var.get() < 300:
            raise ValueError('Contact sheet width must be at least 300 px.')
        if self.margin_var.get() < 0:
            raise ValueError('Margin cannot be negative.')
        if not 20 <= self.jpeg_quality_var.get() <= 100:
            raise ValueError('JPEG quality must be between 20 and 100.')
        if not 0 <= self.image_start_pct_var.get() < self.image_end_pct_var.get() <= 100:
            raise ValueError('The screenshot percentage range is invalid.')
        if self.preview_format_var.get() not in PREVIEW_FORMATS:
            raise ValueError('Invalid animated preview format.')
        if self.gif_max_mb_var.get() <= 0:
            raise ValueError('Animated preview maximum size must be greater than 0 MB.')
        if not 0 <= self.gif_start_pct_var.get() < self.gif_end_pct_var.get() <= 100:
            raise ValueError('The animated preview percentage range is invalid.')
        if self.gif_clips_var.get() < 1 or self.gif_seconds_var.get() <= 0:
            raise ValueError('Animated preview clips and seconds per clip must be greater than 0.')
        if self.gif_width_var.get() < 120 or self.gif_fps_var.get() < 1:
            raise ValueError('Animated preview width/FPS is too low.')

    def _snapshot_settings(self):
        self.validate_settings()
        return {'output_dir': self.output_dir_var.get().strip(), 'subfolder_per_job': bool(self.subfolder_per_job_var.get()), 'process_mode': self.process_mode_var.get(), 'overwrite': bool(self.overwrite_var.get()), 'verify': bool(self.verify_var.get()), 'open_folder': bool(self.open_folder_var.get()), 'trackers': parse_trackers(self.trackers_text.get('1.0', 'end').strip()), 'comment': self.comment_text.get('1.0', 'end').strip(), 'piece_length': self.PIECE_OPTIONS[self.piece_var.get()], 'piece_option': self.piece_var.get(), 'private': bool(self.private_var.get()), 'privacy_mode': bool(self.privacy_mode_var.get()), 'make_screens': bool(self.make_screens_var.get()), 'separate_count': int(self.separate_count_var.get()), 'layout_cols': int(self.layout_cols_var.get()), 'layout_rows': int(self.layout_rows_var.get()), 'layout_width': int(self.layout_width_var.get()), 'margin': int(self.margin_var.get()), 'jpeg_quality': int(self.jpeg_quality_var.get()), 'image_start_pct': float(self.image_start_pct_var.get()), 'image_end_pct': float(self.image_end_pct_var.get()), 'make_gif': bool(self.make_gif_var.get()), 'preview_format': self.preview_format_var.get(), 'gif_max_mb': float(self.gif_max_mb_var.get()), 'gif_start_pct': float(self.gif_start_pct_var.get()), 'gif_end_pct': float(self.gif_end_pct_var.get()), 'gif_clips': int(self.gif_clips_var.get()), 'gif_seconds': float(self.gif_seconds_var.get()), 'gif_width': int(self.gif_width_var.get()), 'gif_fps': int(self.gif_fps_var.get())}

    def _save_settings(self):
        try:
            data = self._snapshot_settings()
        except Exception:
            return
        data['trackers_text'] = self.trackers_text.get('1.0', 'end').strip()
        data['comment_text'] = self.comment_text.get('1.0', 'end').strip()
        path = app_settings_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _load_settings(self):
        path = app_settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return
        mapping = [(self.output_dir_var, 'output_dir'), (self.subfolder_per_job_var, 'subfolder_per_job'), (self.process_mode_var, 'process_mode'), (self.overwrite_var, 'overwrite'), (self.verify_var, 'verify'), (self.open_folder_var, 'open_folder'), (self.piece_var, 'piece_option'), (self.private_var, 'private'), (self.privacy_mode_var, 'privacy_mode'), (self.make_screens_var, 'make_screens'), (self.separate_count_var, 'separate_count'), (self.layout_cols_var, 'layout_cols'), (self.layout_rows_var, 'layout_rows'), (self.layout_width_var, 'layout_width'), (self.margin_var, 'margin'), (self.jpeg_quality_var, 'jpeg_quality'), (self.image_start_pct_var, 'image_start_pct'), (self.image_end_pct_var, 'image_end_pct'), (self.make_gif_var, 'make_gif'), (self.preview_format_var, 'preview_format'), (self.gif_max_mb_var, 'gif_max_mb'), (self.gif_start_pct_var, 'gif_start_pct'), (self.gif_end_pct_var, 'gif_end_pct'), (self.gif_clips_var, 'gif_clips'), (self.gif_seconds_var, 'gif_seconds'), (self.gif_width_var, 'gif_width'), (self.gif_fps_var, 'gif_fps')]
        for var, key in mapping:
            if key in data:
                try:
                    var.set(data[key])
                except Exception:
                    pass
        saved_mode = self.process_mode_var.get()
        if saved_mode in PROCESS_MODE_MIGRATION:
            self.process_mode_var.set(PROCESS_MODE_MIGRATION[saved_mode])
        if self.process_mode_var.get() not in PROCESS_MODES:
            self.process_mode_var.set('Everything')
        if self.preview_format_var.get() not in PREVIEW_FORMATS:
            self.preview_format_var.set('GIF')
        self.trackers_text.delete('1.0', 'end')
        self.trackers_text.insert('1.0', data.get('trackers_text', ''))
        self.comment_text.delete('1.0', 'end')
        self.comment_text.insert('1.0', data.get('comment_text', ''))

    def reset_defaults(self):
        self.subfolder_per_job_var.set(True)
        self.process_mode_var.set('Everything')
        self.overwrite_var.set(False)
        self.verify_var.set(True)
        self.open_folder_var.set(False)
        self.piece_var.set('Auto')
        self.private_var.set(False)
        self.privacy_mode_var.set(True)
        self.make_screens_var.set(True)
        self.separate_count_var.set(5)
        self.layout_cols_var.set(3)
        self.layout_rows_var.set(9)
        self.layout_width_var.set(1300)
        self.margin_var.set(5)
        self.jpeg_quality_var.set(80)
        self.image_start_pct_var.set(10)
        self.image_end_pct_var.set(90)
        self.make_gif_var.set(True)
        self.preview_format_var.set('GIF')
        self.gif_max_mb_var.set(7.0)
        self.gif_start_pct_var.set(20)
        self.gif_end_pct_var.set(90)
        self.gif_clips_var.set(8)
        self.gif_seconds_var.set(1.0)
        self.gif_width_var.set(540)
        self.gif_fps_var.set(8)
        self.trackers_text.delete('1.0', 'end')
        self.comment_text.delete('1.0', 'end')
        self.status_var.set('Default settings restored.')

    def start_queue(self):
        if self.busy:
            return
        if not self.jobs:
            messagebox.showinfo(APP_NAME, 'Add at least one job first.')
            return
        if not self.output_dir_var.get().strip():
            messagebox.showinfo(APP_NAME, 'Choose where the output should be saved under Output folder first.')
            return
        try:
            settings = self._snapshot_settings()
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))
            return
        output_dir = Path(settings['output_dir'])
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))
            return
        for job in self.jobs:
            source = Path(job['source'])
            if source.is_dir():
                try:
                    out_resolved = output_dir.resolve()
                    src_resolved = source.resolve()
                    if out_resolved == src_resolved or src_resolved in out_resolved.parents:
                        messagebox.showerror(APP_NAME, f"For folder job '{source.name}' the Output folder must be outside the source folder.")
                        return
                except Exception:
                    pass
        if settings['privacy_mode']:
            flagged_all = []
            for job in self.jobs:
                for name, reason in privacy_name_warnings(job['source'], max_items=3):
                    flagged_all.append((name, reason))
                    if len(flagged_all) >= 8:
                        break
                if len(flagged_all) >= 8:
                    break
            if flagged_all:
                details = '\n'.join((f'• {name} ({reason})' for name, reason in flagged_all))
                if not messagebox.askyesno('Privacy warning', 'Some names may contain personal information:\n\n' + details + '\n\nFilenames must be preserved in the torrent and are shown on the contact sheet. Continue?'):
                    return
        self._save_settings()
        self.cancel_event.clear()
        self.queue_started_at = time.monotonic()
        self.current_progress['value'] = 0
        self.queue_progress['value'] = 0
        self.current_progress_var.set('0%')
        self.queue_progress_var.set('0%')
        self.elapsed_var.set('Elapsed: 00:00')
        self.eta_var.set('Remaining: —')
        for job in self.jobs:
            job['status'] = 'Waiting'
        self._rebuild_tree()
        self._set_busy(True)
        self.status_var.set('Starting job queue...')
        snapshot = [{'id': j['id'], 'source': j['source']} for j in self.jobs]
        self.worker_thread = threading.Thread(target=self._queue_worker, args=(snapshot, settings), daemon=True)
        self.worker_thread.start()

    def _set_busy(self, busy):
        self.busy = busy
        self.start_button.configure(state='disabled' if busy else 'normal')
        self.cancel_button.configure(state='normal' if busy else 'disabled')
        for btn in self.queue_control_buttons:
            btn.configure(state='disabled' if busy else 'normal')
        if hasattr(self, 'metadata_analyze_button'):
            self.metadata_analyze_button.configure(state='disabled' if busy else 'normal')

    def cancel_current(self):
        if not self.busy:
            return
        self.cancel_event.set()
        self.status_var.set('Cancelling current job...')

    def _queue_worker(self, jobs, settings):
        errors = []
        total_jobs = len(jobs)
        for job_index, job in enumerate(jobs):
            if self.cancel_event.is_set():
                self.ui_events.put(('queue_cancelled',))
                return
            self.ui_events.put(('job_status', job['id'], 'Running', ''))
            try:
                result = self._process_one_job(job, job_index, total_jobs, settings)
                self.ui_events.put(('job_completed', job['id'], result['detail']))
            except JobCancelled:
                self.ui_events.put(('job_status', job['id'], 'Cancelled', 'The job was cancelled by the user.'))
                self.ui_events.put(('queue_cancelled',))
                return
            except Exception as exc:
                message = friendly_error(exc)
                errors.append((Path(job['source']).name, message))
                self.ui_events.put(('job_status', job['id'], 'Error', message))
                self.ui_events.put(('progress', job_index, total_jobs, 1.0, f"Error in {Path(job['source']).name} – continuing"))
        self.ui_events.put(('queue_done', errors, settings['output_dir'], settings['open_folder'], total_jobs))

    def _process_one_job(self, job, job_index, total_jobs, settings):
        source = Path(job['source'])
        if not source.exists():
            raise FileNotFoundError(f'The source no longer exists: {source}')
        phases = phases_for_settings(settings)
        if not phases:
            raise ValueError('No processing step is enabled.')
        job_dir = job_output_dir(source, settings)
        job_dir.mkdir(parents=True, exist_ok=True)
        video = None
        if 'screens' in phases or 'gif' in phases:
            video = find_video_source(source)
        weights = {'torrent': 45.0, 'screens': 30.0, 'gif': 25.0}
        active_weight = sum((weights[p] for p in phases))
        ranges = {}
        cursor = 0.0
        for idx, phase in enumerate(phases):
            start = cursor
            end = 1.0 if idx == len(phases) - 1 else cursor + weights[phase] / active_weight
            ranges[phase] = (start, end)
            cursor = end

        def phase_progress(phase, fraction, text):
            start, end = ranges[phase]
            fraction = max(0.0, min(1.0, float(fraction)))
            job_fraction = start + (end - start) * fraction
            self.ui_events.put(('progress', job_index, total_jobs, job_fraction, text))
        summary = []
        label = source.stem if source.is_file() else source.name
        torrent_path = job_dir / f'{label}.torrent'
        if 'torrent' in phases:
            check_cancel(self.cancel_event)
            if torrent_path.exists() and (not settings['overwrite']):
                summary.append('torrent: already existed')
                phase_progress('torrent', 1.0, f'{source.name}: torrent already exists – skipping')
            else:

                def torrent_progress(processed, total):
                    fraction = 0.0 if total <= 0 else processed / total
                    phase_progress('torrent', fraction, f'{source.name}: hashing {fraction * 100:.0f}%')
                total_size, actual_piece, piece_count = create_torrent(source=source, output=torrent_path, trackers=settings['trackers'], piece_length=settings['piece_length'], comment=settings['comment'], private=settings['private'], privacy_mode=settings['privacy_mode'], progress_callback=torrent_progress, cancel_event=self.cancel_event)
                summary.append(f'torrent {format_bytes(total_size)}, bit {format_bytes(actual_piece)}, {piece_count} hash pieces')
                phase_progress('torrent', 1.0, f'{source.name}: torrent complete')
        if 'screens' in phases:
            check_cancel(self.cancel_event)

            def image_progress(done, total, text):
                phase_progress('screens', done / max(1, total), f'{source.name}: {text} {done}/{total}')
            image_result = generate_screenshots_and_contact_sheet(video_path=video, output_dir=job_dir, separate_count=settings['separate_count'], layout_cols=settings['layout_cols'], layout_rows=settings['layout_rows'], layout_width=settings['layout_width'], margin=settings['margin'], jpeg_quality=settings['jpeg_quality'], start_pct=settings['image_start_pct'], end_pct=settings['image_end_pct'], progress_callback=image_progress, cancel_event=self.cancel_event, overwrite=settings['overwrite'])
            w, h = image_result['layout_size']
            if image_result['created']:
                summary.append(f'screenshots/contact sheet {w}×{h}')
            else:
                summary.append('screenshots/contact sheet: already existed')
            phase_progress('screens', 1.0, f'{source.name}: screenshots/contact sheet complete')
        if 'gif' in phases:
            check_cancel(self.cancel_event)
            preview_format = settings.get('preview_format', 'GIF')
            preview_jobs = []
            if preview_format in ('GIF', 'GIF + WebP'):
                preview_jobs.append(('GIF', job_dir / f'{video.stem}_preview.gif'))
            if preview_format in ('WebP', 'GIF + WebP'):
                preview_jobs.append(('WebP', job_dir / f'{video.stem}_preview.webp'))
            if not preview_jobs:
                raise ValueError('No animated preview format is selected.')
            for preview_index, (preview_kind, preview_path) in enumerate(preview_jobs):
                preview_last = {'value': 0.0}
                def preview_progress(fraction, text, _index=preview_index, _kind=preview_kind):
                    fraction = max(preview_last['value'], float(fraction))
                    preview_last['value'] = fraction
                    combined = (_index + fraction) / len(preview_jobs)
                    phase_progress('gif', combined, f'{source.name}: {text} {fraction * 100:.0f}%')
                common = dict(video_path=video, output_path=preview_path, max_size_mb=settings['gif_max_mb'], start_pct=settings['gif_start_pct'], end_pct=settings['gif_end_pct'], clips=settings['gif_clips'], clip_seconds=settings['gif_seconds'], width=settings['gif_width'], fps=settings['gif_fps'], progress_callback=preview_progress, cancel_event=self.cancel_event, overwrite=settings['overwrite'])
                if preview_kind == 'GIF':
                    preview_result = create_size_limited_gif(**common)
                    if preview_result.get('skipped'):
                        summary.append(f"GIF: already existed ({format_bytes(preview_result['size'])})")
                    else:
                        summary.append(f"GIF {format_bytes(preview_result['size'])}, {preview_result['width']} px, {preview_result['fps']} FPS")
                else:
                    preview_result = create_size_limited_webp(**common)
                    if preview_result.get('skipped'):
                        summary.append(f"WebP: already existed ({format_bytes(preview_result['size'])})")
                    else:
                        summary.append(f"WebP {format_bytes(preview_result['size'])}, {preview_result['width']} px, {preview_result['fps']} FPS, quality {preview_result['quality']}")
            phase_progress('gif', 1.0, f'{source.name}: animated preview complete')
        check_cancel(self.cancel_event)
        if settings['verify']:
            issues = verify_job_outputs(source, video, job_dir, settings, phases)
            if issues:
                raise RuntimeError('Verification found problems: ' + '; '.join(issues))
            summary.append('verified')
        self.ui_events.put(('progress', job_index, total_jobs, 1.0, f'{source.name}: complete'))
        return {'job_dir': str(job_dir), 'detail': ' • '.join(summary) + f' • {job_dir}'}

    def _drain_ui_events(self):
        try:
            while True:
                event = self.ui_events.get_nowait()
                kind = event[0]
                if kind == 'analysis':
                    _, job_id, info, detail, report, privacy_flags = event
                    job = self._job_by_id(job_id)
                    if job:
                        job['info'] = info
                        job['detail'] = detail
                        job['metadata_text'] = report
                        job['privacy_flags'] = int(privacy_flags or 0)
                        self._rebuild_tree(select_ids=self.queue_tree.selection())
                        if str(job_id) in self.queue_tree.selection():
                            self.selected_info_var.set(detail)
                            self._set_metadata_text(report)
                            if privacy_flags:
                                self.metadata_status_var.set(f'Privacy check: {privacy_flags} metadata field(s) were flagged. The original video is not modified.')
                            else:
                                self.metadata_status_var.set('Analysis complete. No clearly privacy-sensitive metadata fields were flagged.')
                elif kind == 'job_status':
                    _, job_id, status, detail = event
                    job = self._job_by_id(job_id)
                    if job:
                        job['status'] = status
                        if detail:
                            job['detail'] = detail
                        self._rebuild_tree(select_ids=self.queue_tree.selection())
                elif kind == 'job_completed':
                    _, job_id, detail = event
                    removed = next((j for j in self.jobs if j['id'] == int(job_id)), None)
                    if removed is not None:
                        self.jobs = [j for j in self.jobs if j['id'] != int(job_id)]
                        self._rebuild_tree()
                        self.selected_info_var.set(f"Completed: {Path(removed['source']).name}. The job was removed from the queue automatically.")
                        self.queue_status_var.set(f'Queue: {len(self.jobs)} remaining')
                elif kind == 'progress':
                    _, job_index, total_jobs, job_fraction, text = event
                    self._apply_progress(job_index, total_jobs, job_fraction, text)
                elif kind == 'queue_cancelled':
                    self._set_busy(False)
                    self.status_var.set('The job queue was cancelled.')
                    self.eta_var.set('Remaining: —')
                    self.queue_status_var.set('Queue: cancelled')
                elif kind == 'queue_done':
                    _, errors, out_dir, should_open, total_jobs = event
                    self.current_progress['value'] = 100
                    self.queue_progress['value'] = 100
                    self.current_progress_var.set('100%')
                    self.queue_progress_var.set('100%')
                    self._set_busy(False)
                    self.eta_var.set('Remaining: 00:00')
                    if errors:
                        self.queue_status_var.set(f'Queue: finished • {len(errors)} errors remaining')
                        self.status_var.set(f'Queue finished with {len(errors)} errors. Successful jobs were removed from the queue.')
                        details = '\n\n'.join((f'{name}: {msg}' for name, msg in errors[:5]))
                        messagebox.showwarning(APP_NAME, f'The queue is finished. {total_jobs - len(errors)} jobs succeeded and were removed from the queue. {len(errors)} jobs failed and remain in the queue.\n\n{details}')
                    else:
                        self.queue_status_var.set('Queue: finished • 0 remaining')
                        self.status_var.set('The entire queue is finished and is now empty.')
                        messagebox.showinfo(APP_NAME, f'The entire queue is finished. {total_jobs} jobs were processed and removed from the queue.\n\nOutput:\n{out_dir}')
                    if should_open:
                        try:
                            open_folder(out_dir)
                        except Exception:
                            pass
        except queue.Empty:
            pass
        finally:
            try:
                self.after(80, self._drain_ui_events)
            except tk.TclError:
                pass

    def _apply_progress(self, job_index, total_jobs, job_fraction, text):
        job_fraction = max(0.0, min(1.0, float(job_fraction)))
        current_pct = job_fraction * 100
        queue_fraction = (job_index + job_fraction) / max(1, total_jobs)
        queue_pct = queue_fraction * 100
        self.current_progress['value'] = current_pct
        self.queue_progress['value'] = queue_pct
        self.current_progress_var.set(f'{current_pct:.0f}%')
        self.queue_progress_var.set(f'{queue_pct:.0f}%')
        self.status_var.set(text)
        self.queue_status_var.set(f'Queue: {job_index + 1}/{total_jobs}')
        if self.queue_started_at is not None:
            elapsed = max(0, time.monotonic() - self.queue_started_at)
            elapsed_int = int(elapsed)
            self.elapsed_var.set(f'Elapsed: {elapsed_int // 60:02d}:{elapsed_int % 60:02d}')
            if queue_fraction >= 0.03 and elapsed >= 2:
                remaining = max(0, int(elapsed * (1.0 - queue_fraction) / queue_fraction))
                self.eta_var.set(f'Remaining: approx. {remaining // 60:02d}:{remaining % 60:02d}')
            else:
                self.eta_var.set('Remaining: calculating...')

    def _on_close(self):
        self._save_settings()
        if self.busy:
            if not messagebox.askyesno(APP_NAME, 'A job is running. Cancel it and close the application?'):
                return
            self.cancel_event.set()
        self.destroy()
if __name__ == '__main__':
    TorrentCreatorApp().mainloop()
