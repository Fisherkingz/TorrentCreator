#!/usr/bin/env python3
"""
TorrentCreator v3

Creates BitTorrent v1 .torrent files and video preview media:
- sequential job queue for multiple videos
- 6 separate screenshots by default
- 3 x 9 contact sheet, 1300 px wide by default
- size-limited animated GIF and WebP previews (default max 7 MiB)
- privacy mode, verification, drag & drop, and saved settings

The Windows build can bundle FFmpeg/FFprobe directly into TorrentCreator.exe.
"""
from __future__ import annotations
import base64
import hashlib
import json
import math
import mimetypes
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
import urllib.error
import urllib.request
import uuid
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
    from PIL import Image, ImageDraw, ImageFont, ImageTk
except Exception:
    Image = ImageDraw = ImageFont = ImageTk = None
APP_NAME = 'TorrentCreator'
APP_VERSION = '3.4.0'
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

def wrap_text_to_width(draw, text, font, max_width):
    """Wrap a filename by rendered pixel width while preserving every character.

    Filenames often contain dots/underscores instead of spaces, so ordinary word
    wrapping is not reliable. This routine prefers common filename separators as
    line-break positions and falls back to character-level wrapping when needed.
    """
    text = str(text or '')
    if not text:
        return ['']
    if draw.textlength(text, font=font) <= max_width:
        return [text]

    lines = []
    start = 0
    length = len(text)
    break_chars = set(' ._-[](){}')
    while start < length:
        if draw.textlength(text[start:], font=font) <= max_width:
            lines.append(text[start:])
            break

        lo, hi = start + 1, length
        best = start + 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if draw.textlength(text[start:mid], font=font) <= max_width:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        cut = best
        # Prefer a nearby natural separator without discarding it.
        natural = None
        for pos in range(best - 1, start, -1):
            if text[pos] in break_chars:
                natural = pos + 1
                break
        if natural is not None and natural > start:
            cut = natural

        lines.append(text[start:cut])
        start = cut

    return lines

def fit_contact_sheet_title(draw, title, max_width, preferred_size=18, minimum_size=9, preferred_max_lines=3):
    """Choose a smaller title font when necessary and always keep the full filename visible."""
    for size in range(preferred_size, minimum_size - 1, -1):
        font = load_font(size, bold=True)
        lines = wrap_text_to_width(draw, title, font, max_width)
        if len(lines) <= preferred_max_lines:
            return font, lines, size
    font = load_font(minimum_size, bold=True)
    return font, wrap_text_to_width(draw, title, font, max_width), minimum_size

def create_screenshot_collage(screenshot_paths, output_path, width=1300, margin=5, jpeg_quality=80, columns=2, overwrite=False):
    """Combine the already-generated separate screenshots into a forum collage.

    The screenshots themselves remain untouched. Images are placed two per row
    by default without cropping. With the default six screenshots this produces
    an even 2 x 3 collage.
    """
    ensure_pillow()
    output_path = Path(output_path)
    screenshot_paths = [Path(p) for p in screenshot_paths if Path(p).exists()]
    if not screenshot_paths:
        raise RuntimeError('Cannot create collage because no separate screenshots were found.')
    columns = max(1, int(columns))
    width = max(300, int(width))
    margin = max(0, int(margin))
    if output_path.exists() and not overwrite:
        with Image.open(output_path) as im:
            return {'path': output_path, 'size': im.size, 'skipped': True}

    images = []
    try:
        for path in screenshot_paths:
            with Image.open(path) as raw:
                images.append(raw.convert('RGB').copy())
        rows = (len(images) + columns - 1) // columns
        content_width = max(columns, width - margin * (columns + 1))
        cell_width = max(1, content_width // columns)
        # Separate screenshots come from the same video and therefore normally
        # have identical geometry. Use the first frame's aspect ratio and fit
        # every image inside the cell so unusual files are still never cropped.
        first = images[0]
        cell_height = max(1, int(round(cell_width * first.height / max(1, first.width))))
        height = margin * (rows + 1) + rows * cell_height
        collage = Image.new('RGB', (width, height), (20, 20, 20))
        for index, image in enumerate(images):
            row = index // columns
            col = index % columns
            scale = min(cell_width / max(1, image.width), cell_height / max(1, image.height))
            new_w = max(1, int(round(image.width * scale)))
            new_h = max(1, int(round(image.height * scale)))
            resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
            x = margin + col * (cell_width + margin) + (cell_width - new_w) // 2
            y = margin + row * (cell_height + margin) + (cell_height - new_h) // 2
            # If the last row is incomplete, center the row as a group.
            items_in_row = min(columns, len(images) - row * columns)
            if items_in_row < columns:
                row_width = items_in_row * cell_width + max(0, items_in_row - 1) * margin
                row_start = (width - row_width) // 2
                local_col = index - row * columns
                x = row_start + local_col * (cell_width + margin) + (cell_width - new_w) // 2
            collage.paste(resized, (x, y))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        collage.save(output_path, 'JPEG', quality=int(jpeg_quality), optimize=True, exif=b'')
        return {'path': output_path, 'size': collage.size, 'skipped': False}
    finally:
        for image in images:
            try:
                image.close()
            except Exception:
                pass


def generate_screenshots_and_contact_sheet(video_path, output_dir, separate_count=6, layout_cols=3, layout_rows=9, layout_width=1300, margin=5, jpeg_quality=80, start_pct=10, end_pct=90, progress_callback=None, cancel_event=None, overwrite=False, create_collage=True, collage_columns=2):
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
    collage_path = output_dir / f'{video_path.stem}_collage.jpg'
    total_steps = separate_count + layout_count + (1 if create_collage else 0)
    created = []
    skipped = []
    if not overwrite and contact_path.exists() and all((p.exists() for p in separate_paths)) and (not create_collage or collage_path.exists()):
        if progress_callback:
            progress_callback(total_steps, total_steps, 'Screenshots/contact sheet/collage already exist')
        with Image.open(contact_path) as _existing_layout:
            existing_layout_size = _existing_layout.size
        return {'created': [], 'skipped': separate_paths + [contact_path] + ([collage_path] if create_collage else []), 'contact_sheet': contact_path, 'collage': collage_path if create_collage else None, 'separate_count': separate_count, 'layout_count': layout_count, 'layout_size': existing_layout_size}
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
            # Build a compact but information-rich header. The title is allowed to
            # wrap and the font is reduced when needed so the complete filename is
            # always visible, even for long release-style filenames.
            header_pad_x = margin + 8
            header_top = 8
            available_header_width = max(120, layout_width - header_pad_x * 2)
            sizing_canvas = Image.new('RGB', (layout_width, 16), (20, 20, 20))
            sizing_draw = ImageDraw.Draw(sizing_canvas)
            title_font, title_lines, title_font_size = fit_contact_sheet_title(
                sizing_draw, video_path.name, available_header_width, preferred_size=18, minimum_size=9, preferred_max_lines=3
            )
            title_line_bbox = sizing_draw.textbbox((0, 0), 'Ag', font=title_font)
            title_line_height = max(1, title_line_bbox[3] - title_line_bbox[1])
            title_spacing = max(2, title_font_size // 5)
            title_block_height = len(title_lines) * title_line_height + max(0, len(title_lines) - 1) * title_spacing

            info_font = load_font(14, bold=False)
            info_bbox = sizing_draw.textbbox((0, 0), 'Ag', font=info_font)
            info_line_height = max(1, info_bbox[3] - info_bbox[1])
            info_top = header_top + title_block_height + 8
            header_height = info_top + info_line_height + 12

            layout_height = header_height + margin + layout_rows * cell_height + margin * (layout_rows + 1)
            sheet = Image.new('RGB', (layout_width, layout_height), (20, 20, 20))
            draw = ImageDraw.Draw(sheet)
            file_size = video_path.stat().st_size

            y_title = header_top
            for line in title_lines:
                bbox = draw.textbbox((0, 0), line, font=title_font)
                draw.text((header_pad_x, y_title - bbox[1]), line, font=title_font, fill=(245, 245, 245))
                y_title += title_line_height + title_spacing

            info = (
                f"Resolution: {meta['width']}x{meta['height']}  •  "
                f"Duration: {format_duration(duration)}  •  "
                f"Bitrate: {_human_bitrate(meta.get('bitrate'))}  •  "
                f"Size: {format_bytes(file_size)}"
            )
            draw.text((header_pad_x, info_top), info, font=info_font, fill=(205, 205, 205))
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
    if create_collage:
        check_cancel(cancel_event)
        collage_result = create_screenshot_collage(
            separate_paths, collage_path, width=layout_width, margin=margin,
            jpeg_quality=jpeg_quality, columns=collage_columns, overwrite=overwrite,
        )
        if collage_result['skipped']:
            skipped.append(collage_path)
        else:
            created.append(collage_path)
        if progress_callback:
            progress_callback(total_steps, total_steps, 'Creating 2-column screenshot collage')
    return {'created': created, 'skipped': skipped, 'contact_sheet': contact_path, 'collage': collage_path if create_collage else None, 'separate_count': separate_count, 'layout_count': layout_count, 'layout_size': layout_size}

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

def webp_candidate_settings(base_width, base_fps, clip_seconds, base_quality=90):
    """Quality steps for animated WebP, starting above GIF's visual target.

    WebP deliberately starts larger and smoother than GIF so its compression
    advantage is spent on visible quality (720 px / 12 FPS / quality 90 by
    default) rather than only producing a smaller file. The limiter then
    reduces quality, FPS and width only when the selected max-size requires it.
    """
    base_width = max(240, int(base_width) // 2 * 2)
    base_fps = max(4, int(base_fps))
    base_quality = max(1, min(100, int(base_quality)))
    q = lambda delta, floor: max(floor, base_quality - delta)
    raw = [
        (base_width, base_fps, base_quality, clip_seconds),
        (base_width, max(6, base_fps - 1), q(6, 80), clip_seconds),
        (max(240, int(base_width * 0.92) // 2 * 2), max(6, base_fps - 2), q(10, 76), clip_seconds),
        (max(240, int(base_width * 0.84) // 2 * 2), max(6, base_fps - 3), q(16, 68), clip_seconds),
        (max(240, int(base_width * 0.76) // 2 * 2), max(6, base_fps - 4), q(22, 60), clip_seconds),
        (max(240, int(base_width * 0.66) // 2 * 2), max(5, base_fps - 5), q(30, 52), clip_seconds),
        (max(240, int(base_width * 0.56) // 2 * 2), 5, max(45, q(38, 45)), clip_seconds),
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
    quality_factor = max(0.3, quality / 90.0)
    return width * width * fps * clip_seconds * quality_factor

def create_size_limited_webp(video_path, output_path, max_size_mb=7.0, start_pct=20.0, end_pct=90.0, clips=8, clip_seconds=1.0, width=720, fps=12, quality=90, progress_callback=None, cancel_event=None, overwrite=False):
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
    all_candidates = webp_candidate_settings(int(width), int(fps), float(clip_seconds), int(quality))
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


HAMSTER_API_URL = 'https://hamsterimg.net/api/1/upload'
HAMSTER_CREDENTIAL_TARGET = 'TorrentCreator:HamsterImg API Key'


def _credential_struct():
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ('Flags', wintypes.DWORD),
            ('Type', wintypes.DWORD),
            ('TargetName', wintypes.LPWSTR),
            ('Comment', wintypes.LPWSTR),
            ('LastWritten', wintypes.FILETIME),
            ('CredentialBlobSize', wintypes.DWORD),
            ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
            ('Persist', wintypes.DWORD),
            ('AttributeCount', wintypes.DWORD),
            ('Attributes', wintypes.LPVOID),
            ('TargetAlias', wintypes.LPWSTR),
            ('UserName', wintypes.LPWSTR),
        ]
    return CREDENTIALW


def save_windows_credential(secret, target=HAMSTER_CREDENTIAL_TARGET):
    """Store a secret in Windows Credential Manager as a generic credential."""
    if os.name != 'nt':
        raise RuntimeError('Secure credential storage is available in the Windows build.')
    secret = str(secret or '').strip()
    if not secret:
        raise ValueError('The API key is empty.')
    import ctypes
    from ctypes import wintypes
    CREDENTIALW = _credential_struct()
    advapi32 = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    raw = secret.encode('utf-16-le')
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    cred = CREDENTIALW()
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(raw)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = 'TorrentCreator'
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), 'Windows Credential Manager could not save the API key.')
    return True


def read_windows_credential(target=HAMSTER_CREDENTIAL_TARGET):
    """Read a generic credential from Windows Credential Manager."""
    if os.name != 'nt':
        return ''
    import ctypes
    from ctypes import wintypes
    CREDENTIALW = _credential_struct()
    advapi32 = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    pointer = PCREDENTIALW()
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == 1168:  # ERROR_NOT_FOUND
            return ''
        raise OSError(error, 'Windows Credential Manager could not read the API key.')
    try:
        cred = pointer.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode('utf-16-le')
    finally:
        advapi32.CredFree(pointer)


def delete_windows_credential(target=HAMSTER_CREDENTIAL_TARGET):
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    if advapi32.CredDeleteW(target, 1, 0):
        return True
    error = ctypes.get_last_error()
    if error == 1168:
        return False
    raise OSError(error, 'Windows Credential Manager could not remove the API key.')


def build_multipart_form(fields, file_path, file_field='source'):
    """Build a small multipart/form-data body for HamsterImg uploads."""
    file_path = Path(file_path)
    boundary = '----TorrentCreator' + uuid.uuid4().hex
    b = boundary.encode('ascii')
    chunks = []
    for key, value in fields.items():
        if value is None or str(value) == '':
            continue
        chunks.extend([
            b'--' + b + b'\r\n',
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode('utf-8'),
            str(value).encode('utf-8'),
            b'\r\n',
        ])
    mime = mimetypes.guess_type(file_path.name)[0] or 'application/octet-stream'
    safe_name = file_path.name.replace('"', '')
    chunks.extend([
        b'--' + b + b'\r\n',
        f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'.encode('utf-8'),
        f'Content-Type: {mime}\r\n\r\n'.encode('ascii'),
        file_path.read_bytes(),
        b'\r\n',
        b'--' + b + b'--\r\n',
    ])
    return b''.join(chunks), f'multipart/form-data; boundary={boundary}'


def _hamster_error_message(payload, fallback='HamsterImg rejected the upload.'):
    if isinstance(payload, dict):
        for key in ('status_txt', 'message', 'error'):
            value = payload.get(key)
            if isinstance(value, str) and value.strip() and value.strip().upper() != 'OK':
                return value.strip()
            if isinstance(value, dict):
                for nested in ('message', 'error', 'description'):
                    text = value.get(nested)
                    if isinstance(text, str) and text.strip():
                        return text.strip()
        success = payload.get('success')
        if isinstance(success, dict):
            message = success.get('message')
            if isinstance(message, str) and message.strip():
                return message.strip()
    return fallback


def hamster_upload_file(file_path, api_key, title='', tags='', album_id='', category_id='', nsfw=False, expiration='', timeout=120, cancel_event=None, api_url=HAMSTER_API_URL):
    """Upload one generated media file using HamsterImg API v1.1."""
    check_cancel(cancel_event)
    file_path = Path(file_path)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f'Upload file does not exist: {file_path}')
    api_key = str(api_key or '').strip()
    if not api_key:
        raise ValueError('HamsterImg API key is missing.')
    fields = {
        'format': 'json',
        'title': title or file_path.stem,
        'tags': tags,
        'album_id': album_id,
        'category_id': category_id,
        'nsfw': '1' if nsfw else '0',
        'expiration': expiration,
    }
    body, content_type = build_multipart_form(fields, file_path)
    request = urllib.request.Request(
        api_url,
        data=body,
        method='POST',
        headers={
            'X-API-Key': api_key,
            'Content-Type': content_type,
            'Accept': 'application/json',
            'User-Agent': f'{APP_NAME}/{APP_VERSION}',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace') if hasattr(exc, 'read') else ''
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}
        raise RuntimeError(f'HamsterImg upload failed (HTTP {exc.code}): {_hamster_error_message(payload, raw[:300] or exc.reason)}') from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Could not connect to HamsterImg: {getattr(exc, "reason", exc)}') from None
    except TimeoutError:
        raise RuntimeError('HamsterImg upload timed out.') from None
    check_cancel(cancel_event)
    try:
        payload = json.loads(raw)
    except Exception:
        raise RuntimeError('HamsterImg returned an invalid JSON response.') from None
    image = payload.get('image') if isinstance(payload, dict) else None
    status_code = payload.get('status_code') if isinstance(payload, dict) else None
    if not isinstance(image, dict) or (status_code not in (None, 200)):
        raise RuntimeError(_hamster_error_message(payload))
    direct_url = image.get('url') or (image.get('image') or {}).get('url') or image.get('display_url') or ''
    viewer_url = image.get('url_viewer') or image.get('url_short') or direct_url
    thumb_url = (image.get('thumb') or {}).get('url') or image.get('display_url') or direct_url
    if not direct_url:
        raise RuntimeError('HamsterImg accepted the upload but did not return a direct URL.')
    return {
        'file': str(file_path),
        'name': file_path.name,
        'direct_url': direct_url,
        'viewer_url': viewer_url,
        'thumb_url': thumb_url,
        'id': image.get('id_encoded') or '',
        'size': image.get('size') or file_path.stat().st_size,
    }


def hamster_result_formats(result):
    name = result.get('name') or 'image'
    direct = result.get('direct_url') or ''
    viewer = result.get('viewer_url') or direct
    thumb = result.get('thumb_url') or direct
    bbcode_full = f'[url={viewer}][img]{direct}[/img][/url]'
    return {
        'direct': direct,
        'viewer': viewer,
        'bbcode_full': bbcode_full,
        # Backwards-compatible aliases kept internally for older session data.
        'bbcode': bbcode_full,
        'thumbnail_bbcode': f'[url={viewer}][img]{thumb}[/img][/url]',
        'markdown': f'[![{name}]({direct})]({viewer})',
    }


def write_hamster_links_file(job_dir, video, results):
    """Save only HamsterImg BBCode Full, one ready-to-paste line per uploaded file."""
    path = Path(job_dir) / f'{Path(video).stem}_HamsterImg_BBCode_Full.txt'
    lines = [hamster_result_formats(result)['bbcode_full'] for result in results]
    path.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
    return path


def collect_hamster_upload_files(video, job_dir, settings, phases):
    video = Path(video)
    job_dir = Path(job_dir)
    upload_only = settings.get('process_mode') == 'Upload generated media only'
    results = []
    if settings.get('hamster_upload_screens') and ('screens' in phases or upload_only):
        expected = [job_dir / f'{video.stem}_screenshot_{i:02d}.jpg' for i in range(1, int(settings['separate_count']) + 1)]
        existing = [p for p in expected if p.exists()]
        if upload_only and not existing:
            prefix = f'{video.stem}_screenshot_'.casefold()
            existing = sorted(
                p for p in job_dir.iterdir()
                if p.is_file() and p.name.casefold().startswith(prefix) and p.suffix.lower() in ('.jpg', '.jpeg')
            )
        results.extend(existing)
    if settings.get('hamster_upload_collage') and ('screens' in phases or upload_only):
        collage = job_dir / f'{video.stem}_collage.jpg'
        if collage.exists():
            results.append(collage)
    if settings.get('hamster_upload_layout') and ('screens' in phases or upload_only):
        layout = job_dir / f"{video.stem}_layout_{int(settings['layout_cols'])}x{int(settings['layout_rows'])}.jpg"
        if layout.exists():
            results.append(layout)
        elif upload_only:
            prefix = f'{video.stem}_layout_'.casefold()
            alternatives = sorted(
                (p for p in job_dir.iterdir()
                 if p.is_file() and p.name.casefold().startswith(prefix) and p.suffix.lower() in ('.jpg', '.jpeg')),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if alternatives:
                results.append(alternatives[0])
    if settings.get('hamster_upload_gif') and ('gif' in phases or upload_only):
        gif = job_dir / f'{video.stem}_preview.gif'
        if gif.exists() and (upload_only or settings.get('preview_format') in ('GIF', 'GIF + WebP')):
            results.append(gif)
    if settings.get('hamster_upload_webp') and ('gif' in phases or upload_only):
        webp = job_dir / f'{video.stem}_preview.webp'
        if webp.exists() and (upload_only or settings.get('preview_format') in ('WebP', 'GIF + WebP')):
            results.append(webp)
    unique = []
    seen = set()
    for path in results:
        key = str(path.resolve()).casefold() if os.name == 'nt' else str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique

PREVIEW_FORMATS = ('GIF', 'WebP', 'GIF + WebP')
PROCESS_MODES = ('Everything', 'Torrent only', 'Screenshots/contact sheet only', 'Animated preview only', 'Upload generated media only')
PROCESS_MODE_MIGRATION = {
    'Allt': 'Everything',
    'Endast torrent': 'Torrent only',
    'Endast skärmbilder/layout': 'Screenshots/contact sheet only',
    'Endast GIF': 'Animated preview only',
    'GIF only': 'Animated preview only',
    'Upload only': 'Upload generated media only',
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
    if 'hamsterimg' in low and 'timed out' in low:
        return 'The HamsterImg upload timed out. Check your connection and try the upload again.'
    return text[:900]

def phases_for_settings(settings):
    mode = settings['process_mode']
    if mode == 'Upload generated media only':
        return ['upload']
    if mode == 'Torrent only':
        return ['torrent']
    if mode == 'Screenshots/contact sheet only':
        phases = ['screens']
    elif mode == 'Animated preview only':
        phases = ['gif']
    else:
        phases = ['torrent']
        if settings['make_screens']:
            phases.append('screens')
        if settings['make_gif']:
            phases.append('gif')
    if settings.get('hamster_auto_upload') and any(p in phases for p in ('screens', 'gif')):
        phases.append('upload')
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
        if settings.get('make_collage'):
            paths['collage'] = job_dir / f'{video.stem}_collage.jpg'
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
    collage = paths.get('collage')
    if collage is not None:
        if not collage.exists() or collage.stat().st_size == 0:
            issues.append('screenshot collage is missing')
        elif Image is not None:
            try:
                with Image.open(collage) as im:
                    if im.width != int(settings['layout_width']):
                        issues.append(f"collage is {im.width}px wide, expected {int(settings['layout_width'])}px")
            except Exception:
                issues.append('screenshot collage could not be verified')
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
        self.separate_count_var = tk.IntVar(value=6)
        self.make_collage_var = tk.BooleanVar(value=True)
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
        self.webp_width_var = tk.IntVar(value=720)
        self.webp_fps_var = tk.IntVar(value=12)
        self.webp_quality_var = tk.IntVar(value=90)
        self.hamster_api_key_var = tk.StringVar()
        self.hamster_auto_upload_var = tk.BooleanVar(value=False)
        self.hamster_upload_screens_var = tk.BooleanVar(value=True)
        self.hamster_upload_layout_var = tk.BooleanVar(value=True)
        self.hamster_upload_collage_var = tk.BooleanVar(value=True)
        self.hamster_upload_gif_var = tk.BooleanVar(value=True)
        self.hamster_upload_webp_var = tk.BooleanVar(value=True)
        self.hamster_tags_var = tk.StringVar()
        self.hamster_album_id_var = tk.StringVar()
        self.hamster_category_id_var = tk.StringVar()
        self.hamster_nsfw_var = tk.BooleanVar(value=False)
        self.hamster_status_var = tk.StringVar(value='No HamsterImg API key is saved yet.')
        self.upload_history = []
        self.current_progress_var = tk.StringVar(value='0%')
        self.queue_progress_var = tk.StringVar(value='0%')
        self.status_var = tk.StringVar(value='Add video files to the job queue.')
        self.queue_status_var = tk.StringVar(value='Queue: 0 jobs')
        self.queue_overview_var = tk.StringVar(value='Selected files: 0 • Queue empty')
        self.elapsed_var = tk.StringVar(value='Elapsed: 00:00')
        self.eta_var = tk.StringVar(value='Remaining: —')
        self._last_console_text = ''
        self._last_console_key = ''
        self._last_console_time = 0.0
        self._build_ui()
        self._load_settings()
        self._load_hamster_api_key()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.after(80, self._drain_ui_events)

    def _configure_dark_theme(self):
        """Apply a compact dark desktop-tool theme inspired by professional media utilities."""
        self.ui_colors = {
            'bg': '#1b1d20',
            'panel': '#23262a',
            'panel2': '#2a2d32',
            'field': '#17191c',
            'border': '#3a3e44',
            'text': '#e7e9ed',
            'muted': '#a0a6ae',
            'accent': '#3d7eff',
            'accent_hover': '#4c89ff',
            'danger': '#b94c55',
            'warning': '#d2a34d',
            'success': '#63b889',
            'selection': '#315b8a',
        }
        c = self.ui_colors
        try:
            self.configure(background=c['bg'])
        except Exception:
            pass
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('.', font=('Segoe UI', 9), background=c['bg'], foreground=c['text'])
        style.configure('TFrame', background=c['bg'])
        style.configure('Panel.TFrame', background=c['panel'])
        style.configure('Toolbar.TFrame', background=c['panel2'])
        style.configure('TLabel', background=c['bg'], foreground=c['text'])
        style.configure('Panel.TLabel', background=c['panel'], foreground=c['text'])
        style.configure('Muted.TLabel', background=c['bg'], foreground=c['muted'])
        style.configure('PanelMuted.TLabel', background=c['panel'], foreground=c['muted'])
        style.configure('Title.TLabel', background=c['bg'], foreground=c['text'], font=('Segoe UI Semibold', 16))
        style.configure('Status.TLabel', background=c['panel2'], foreground=c['muted'], padding=(8, 4))
        style.configure('TButton', background=c['panel2'], foreground=c['text'], bordercolor=c['border'], lightcolor=c['border'], darkcolor=c['border'], padding=(9, 5), relief='flat')
        style.map('TButton', background=[('active', '#34383e'), ('pressed', '#17191c'), ('disabled', '#25282c')], foreground=[('disabled', '#6d737b')])
        style.configure('Accent.TButton', background=c['accent'], foreground='#ffffff', bordercolor=c['accent'], padding=(12, 6))
        style.map('Accent.TButton', background=[('active', c['accent_hover']), ('pressed', '#2f68d5'), ('disabled', '#42536e')], foreground=[('disabled', '#aeb6c2')])
        style.configure('Danger.TButton', background=c['danger'], foreground='#ffffff', bordercolor=c['danger'], padding=(12, 6))
        style.map('Danger.TButton', background=[('active', '#cc5962'), ('pressed', '#9e3f47'), ('disabled', '#5a4245')])
        style.configure('TEntry', fieldbackground=c['field'], foreground=c['text'], insertcolor=c['text'], bordercolor=c['border'], lightcolor=c['border'], darkcolor=c['border'], padding=5)
        style.map('TEntry', fieldbackground=[('disabled', '#24272b')], foreground=[('disabled', '#777d85')])
        style.configure('TCombobox', fieldbackground=c['field'], background=c['panel2'], foreground=c['text'], arrowcolor=c['text'], bordercolor=c['border'], padding=4)
        style.map('TCombobox', fieldbackground=[('readonly', c['field'])], selectbackground=[('readonly', c['field'])], selectforeground=[('readonly', c['text'])])
        style.configure('TCheckbutton', background=c['bg'], foreground=c['text'], padding=2)
        style.map('TCheckbutton', background=[('active', c['bg'])], foreground=[('disabled', '#737982')])
        style.configure('TLabelframe', background=c['bg'], foreground=c['muted'], bordercolor=c['border'], lightcolor=c['border'], darkcolor=c['border'], relief='solid')
        style.configure('TLabelframe.Label', background=c['bg'], foreground=c['muted'], font=('Segoe UI Semibold', 9))
        style.configure('Treeview', background=c['field'], fieldbackground=c['field'], foreground=c['text'], bordercolor=c['border'], lightcolor=c['border'], darkcolor=c['border'], rowheight=25)
        style.map('Treeview', background=[('selected', c['selection'])], foreground=[('selected', '#ffffff')])
        style.configure('Treeview.Heading', background=c['panel2'], foreground=c['text'], bordercolor=c['border'], relief='flat', padding=(6, 5), font=('Segoe UI Semibold', 9))
        style.map('Treeview.Heading', background=[('active', '#34383e')])
        style.configure('TNotebook', background=c['bg'], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure('TNotebook.Tab', background=c['panel2'], foreground=c['muted'], bordercolor=c['border'], padding=(10, 6), font=('Segoe UI', 9))
        style.map('TNotebook.Tab', background=[('selected', c['field']), ('active', '#34383e')], foreground=[('selected', c['text']), ('active', c['text'])])
        style.configure('Horizontal.TProgressbar', background=c['accent'], troughcolor=c['field'], bordercolor=c['border'], lightcolor=c['accent'], darkcolor=c['accent'])
        style.configure('Vertical.TScrollbar', background=c['panel2'], troughcolor=c['field'], bordercolor=c['border'], arrowcolor=c['text'])
        style.configure('Horizontal.TScrollbar', background=c['panel2'], troughcolor=c['field'], bordercolor=c['border'], arrowcolor=c['text'])
        self.option_add('*TCombobox*Listbox.background', c['field'])
        self.option_add('*TCombobox*Listbox.foreground', c['text'])
        self.option_add('*TCombobox*Listbox.selectBackground', c['selection'])
        self.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')

    def _style_text_widgets(self, parent):
        c = self.ui_colors
        for child in parent.winfo_children():
            if isinstance(child, tk.Text):
                try:
                    child.configure(
                        background=c['field'], foreground=c['text'], insertbackground=c['text'],
                        selectbackground=c['selection'], selectforeground='#ffffff', relief='flat',
                        borderwidth=0, highlightthickness=1, highlightbackground=c['border'],
                        highlightcolor=c['accent'], padx=7, pady=6,
                    )
                except tk.TclError:
                    pass
            self._style_text_widgets(child)

    def _build_ui(self):
        self._configure_dark_theme()
        self.geometry('1420x840')
        self.minsize(1120, 700)
        c = self.ui_colors

        outer = ttk.Frame(self, padding=(8, 7, 8, 7))
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        # Compact application header / toolbar.
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 7))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=APP_NAME, style='Title.TLabel').grid(row=0, column=0, sticky='w')
        dd = 'Drag & drop enabled' if DRAGDROP_AVAILABLE else 'Drag & drop unavailable'
        ttk.Label(header, text=f'v{APP_VERSION}  •  {ffmpeg_bundle_status()}  •  {dd}', style='Muted.TLabel').grid(row=0, column=1, sticky='w', padx=(14, 8))
        self.start_button = ttk.Button(header, text='Start queue', command=self.start_queue, style='Accent.TButton')
        self.start_button.grid(row=0, column=2, sticky='e', padx=(8, 5))
        self.cancel_button = ttk.Button(header, text='Cancel', command=self.cancel_current, state='disabled', style='Danger.TButton')
        self.cancel_button.grid(row=0, column=3, sticky='e')

        # Main two-column workspace: permanent queue on the left, selected-job settings on the right.
        workspace = ttk.Frame(outer)
        workspace.grid(row=1, column=0, sticky='nsew')
        workspace.rowconfigure(0, weight=1)
        workspace.columnconfigure(0, weight=3, minsize=360)
        workspace.columnconfigure(1, weight=7, minsize=690)

        left = ttk.Frame(workspace, style='Panel.TFrame', padding=7)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        ttk.Label(left, text='Job queue', style='Panel.TLabel', font=('Segoe UI Semibold', 10)).grid(row=0, column=0, sticky='w', pady=(0, 5))

        queue_buttons = ttk.Frame(left, style='Panel.TFrame')
        queue_buttons.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        buttons = [
            ('Add files', self.add_files),
            ('Add folder', self.add_folder_job),
            ('Remove', self.remove_selected_jobs),
            ('Move up', lambda: self.move_selected(-1)),
            ('Move down', lambda: self.move_selected(1)),
            ('Quick Check', self.quick_check_selected),
        ]
        self.queue_control_buttons = []
        for idx, (text, command) in enumerate(buttons):
            btn = ttk.Button(queue_buttons, text=text, command=command)
            btn.grid(row=idx // 3, column=idx % 3, sticky='ew', padx=(0 if idx % 3 == 0 else 4, 0), pady=(0 if idx < 3 else 4, 0))
            queue_buttons.columnconfigure(idx % 3, weight=1)
            self.queue_control_buttons.append(btn)

        tree_frame = ttk.Frame(left, style='Panel.TFrame')
        tree_frame.grid(row=2, column=0, sticky='nsew')
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = ('status', 'source')
        self.queue_tree = ttk.Treeview(tree_frame, columns=columns, show='headings', selectmode='extended')
        self.queue_tree.heading('status', text='Status')
        self.queue_tree.heading('source', text='File / folder')
        self.queue_tree.column('status', width=105, minwidth=90, stretch=False)
        self.queue_tree.column('source', width=300, minwidth=180, stretch=True)
        self.queue_tree.grid(row=0, column=0, sticky='nsew')
        qscroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.queue_tree.yview)
        qscroll.grid(row=0, column=1, sticky='ns')
        self.queue_tree.configure(yscrollcommand=qscroll.set)
        self.queue_tree.tag_configure('waiting', foreground=c['muted'])
        self.queue_tree.tag_configure('processing', foreground=c['warning'])
        self.queue_tree.tag_configure('running', foreground=c['warning'])
        self.queue_tree.tag_configure('error', foreground='#ff747d')
        self.queue_tree.tag_configure('failed', foreground='#ff747d')
        self.queue_tree.tag_configure('completed', foreground=c['success'])
        self.queue_tree.bind('<<TreeviewSelect>>', self._queue_selection_changed)
        if DRAGDROP_AVAILABLE:
            try:
                self.queue_tree.drop_target_register(DND_FILES)
                self.queue_tree.dnd_bind('<<Drop>>', self._on_drop)
            except Exception:
                pass

        ttk.Label(left, textvariable=self.queue_overview_var, style='PanelMuted.TLabel', wraplength=340).grid(row=3, column=0, sticky='w', pady=(6, 2))
        ttk.Label(left, textvariable=self.selected_info_var, style='PanelMuted.TLabel', wraplength=340).grid(row=4, column=0, sticky='w', pady=(0, 3))
        drag_text = 'Drop video files here to add them to the queue.' if DRAGDROP_AVAILABLE else 'Use Add files to add video files to the queue.'
        ttk.Label(left, text=drag_text, style='PanelMuted.TLabel').grid(row=5, column=0, sticky='w', pady=(3, 0))

        right = ttk.Frame(workspace)
        right.grid(row=0, column=1, sticky='nsew')
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)

        # Output + progress stay visible regardless of the active settings tab.
        top = ttk.Frame(right, style='Panel.TFrame', padding=7)
        top.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text='Output folder', style='Panel.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Entry(top, textvariable=self.output_dir_var).grid(row=0, column=1, sticky='ew', padx=7)
        ttk.Button(top, text='Browse', command=self.choose_output_dir).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(top, text='Open', command=self.open_output_dir).grid(row=0, column=3)
        ttk.Checkbutton(top, text='Separate subfolder for each video', variable=self.subfolder_per_job_var).grid(row=1, column=1, columnspan=3, sticky='w', pady=(5, 0))

        progress = ttk.Frame(right, style='Panel.TFrame', padding=7)
        progress.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        progress.columnconfigure(1, weight=1)
        ttk.Label(progress, text='Current file', style='Panel.TLabel', width=12).grid(row=0, column=0, sticky='w')
        self.current_progress = ttk.Progressbar(progress, maximum=100)
        self.current_progress.grid(row=0, column=1, sticky='ew', padx=(4, 7))
        ttk.Label(progress, textvariable=self.current_progress_var, style='Panel.TLabel', width=6, anchor='e').grid(row=0, column=2)
        ttk.Label(progress, text='Entire queue', style='Panel.TLabel', width=12).grid(row=1, column=0, sticky='w', pady=(5, 0))
        self.queue_progress = ttk.Progressbar(progress, maximum=100)
        self.queue_progress.grid(row=1, column=1, sticky='ew', padx=(4, 7), pady=(5, 0))
        ttk.Label(progress, textvariable=self.queue_progress_var, style='Panel.TLabel', width=6, anchor='e').grid(row=1, column=2, pady=(5, 0))
        ttk.Label(progress, textvariable=self.status_var, style='PanelMuted.TLabel', anchor='w').grid(row=2, column=0, columnspan=3, sticky='ew', pady=(6, 0))

        notebook = ttk.Notebook(right)
        notebook.grid(row=2, column=0, sticky='nsew')
        media_tab = ttk.Frame(notebook, padding=12)
        run_tab = ttk.Frame(notebook, padding=12)
        torrent_tab = ttk.Frame(notebook, padding=12)
        images_tab = ttk.Frame(notebook, padding=12)
        gif_tab = ttk.Frame(notebook, padding=12)
        hosting_tab = ttk.Frame(notebook, padding=12)
        privacy_tab = ttk.Frame(notebook, padding=12)
        notebook.add(media_tab, text='Media Information')
        notebook.add(run_tab, text='Processing')
        notebook.add(torrent_tab, text='Torrent')
        notebook.add(images_tab, text='Screenshots')
        notebook.add(gif_tab, text='Animated Preview')
        notebook.add(hosting_tab, text='Image Hosting')
        notebook.add(privacy_tab, text='Privacy')
        self._build_media_tab(media_tab)
        self._build_run_tab(run_tab)
        self._build_torrent_tab(torrent_tab)
        self._build_images_tab(images_tab)
        self._build_gif_tab(gif_tab)
        self._build_hosting_tab(hosting_tab)
        self._build_privacy_tab(privacy_tab)

        # Full-width console, similar to a desktop encoding/torrent utility.
        console = ttk.Frame(outer, style='Panel.TFrame', padding=(7, 5))
        console.grid(row=2, column=0, sticky='ew', pady=(6, 0))
        console.columnconfigure(0, weight=1)
        console.rowconfigure(1, weight=1)
        console_head = ttk.Frame(console, style='Panel.TFrame')
        console_head.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 4))
        console_head.columnconfigure(0, weight=1)
        ttk.Label(console_head, text='Console', style='Panel.TLabel', font=('Segoe UI Semibold', 9)).grid(row=0, column=0, sticky='w')
        ttk.Button(console_head, text='Clear', command=self._clear_console).grid(row=0, column=1, padx=(4, 0))
        ttk.Button(console_head, text='Save log', command=self._save_console).grid(row=0, column=2, padx=(4, 0))
        self.console_text = tk.Text(console, height=6, wrap='none', font=('Consolas', 9), state='disabled')
        self.console_text.grid(row=1, column=0, sticky='ew')
        cscroll = ttk.Scrollbar(console, orient='vertical', command=self.console_text.yview)
        cscroll.grid(row=1, column=1, sticky='ns')
        self.console_text.configure(yscrollcommand=cscroll.set)

        statusbar = ttk.Frame(outer, style='Toolbar.TFrame')
        statusbar.grid(row=3, column=0, sticky='ew', pady=(5, 0))
        statusbar.columnconfigure(0, weight=1)
        ttk.Label(statusbar, textvariable=self.queue_status_var, style='Status.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(statusbar, textvariable=self.elapsed_var, style='Status.TLabel').grid(row=0, column=1, sticky='e')
        ttk.Label(statusbar, textvariable=self.eta_var, style='Status.TLabel').grid(row=0, column=2, sticky='e')

        self._style_text_widgets(outer)
        self.status_var.trace_add('write', self._status_changed)
        self._append_console(f'{APP_NAME} v{APP_VERSION} ready. Add one or more videos to the queue.')

    def _build_privacy_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        ttk.Label(tab, text='Privacy controls', font=('Segoe UI Semibold', 11)).grid(row=0, column=0, sticky='w', pady=(0, 10))
        ttk.Checkbutton(tab, text='Privacy mode (recommended)', variable=self.privacy_mode_var).grid(row=1, column=0, sticky='w', pady=4)
        ttk.Label(
            tab,
            text='Removes created-by, creation-date, and comment metadata from the .torrent file and strips metadata from generated JPG, GIF, and WebP files.',
            wraplength=760,
        ).grid(row=2, column=0, sticky='w', pady=(0, 10))
        info = ttk.LabelFrame(tab, text='Information intentionally preserved', padding=10)
        info.grid(row=3, column=0, sticky='ew', pady=(0, 10))
        ttk.Label(info, text='• The source filename remains part of the torrent metadata.').grid(row=0, column=0, sticky='w', pady=2)
        ttk.Label(info, text='• The filename remains clearly visible on the contact sheet.').grid(row=1, column=0, sticky='w', pady=2)
        ttk.Label(info, text='• Resolution, duration, bitrate, and file size may remain visible on the contact sheet.').grid(row=2, column=0, sticky='w', pady=2)
        ttk.Label(tab, text='The original video file is never modified. Embedded metadata already present inside the source video remains in that source file.', wraplength=760).grid(row=4, column=0, sticky='w', pady=(0, 10))
        ttk.Label(tab, text='Quick Check flags common privacy-sensitive metadata such as author/artist fields, comments, creation tools/times, location data, email addresses, and similar values.', wraplength=760).grid(row=5, column=0, sticky='w')

    def _append_console(self, text):
        if not text or not hasattr(self, 'console_text'):
            return
        text = str(text).strip()
        if not text:
            return
        # Collapse percentage-heavy progress chatter while retaining useful stage updates.
        key = re.sub(r'\b\d+(?:\.\d+)?%\b', '%', text)
        key = re.sub(r'\(\d+\s*/\s*\d+\)', '(#/#)', key)
        now = time.monotonic()
        if key == self._last_console_key and now - self._last_console_time < 1.5:
            return
        self._last_console_key = key
        self._last_console_time = now
        self._last_console_text = text
        stamp = time.strftime('%H:%M:%S')
        try:
            self.console_text.configure(state='normal')
            self.console_text.insert('end', f'[{stamp}]  {text}\n')
            # Bound the UI log so long hashing sessions do not grow indefinitely.
            line_count = int(float(self.console_text.index('end-1c').split('.')[0]))
            if line_count > 1200:
                self.console_text.delete('1.0', '201.0')
            self.console_text.see('end')
            self.console_text.configure(state='disabled')
        except tk.TclError:
            pass

    def _status_changed(self, *_args):
        try:
            self.after_idle(lambda: self._append_console(self.status_var.get()))
        except tk.TclError:
            pass

    def _clear_console(self):
        try:
            self.console_text.configure(state='normal')
            self.console_text.delete('1.0', 'end')
            self.console_text.configure(state='disabled')
            self._last_console_key = ''
            self._last_console_text = ''
        except tk.TclError:
            pass

    def _save_console(self):
        if not hasattr(self, 'console_text'):
            return
        path = filedialog.asksaveasfilename(
            title='Save TorrentCreator log',
            defaultextension='.txt',
            filetypes=[('Text file', '*.txt'), ('All files', '*.*')],
            initialfile=f'TorrentCreator-{APP_VERSION}-log.txt',
        )
        if not path:
            return
        try:
            Path(path).write_text(self.console_text.get('1.0', 'end-1c'), encoding='utf-8')
            self._append_console(f'Log saved to {path}')
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def _build_run_tab(self, tab):
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text='What to create').grid(row=0, column=0, sticky='w', pady=5)
        ttk.Combobox(tab, textvariable=self.process_mode_var, values=PROCESS_MODES, state='readonly', width=30).grid(row=0, column=1, sticky='w', padx=8)
        ttk.Checkbutton(tab, text='Overwrite existing files', variable=self.overwrite_var).grid(row=1, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Label(tab, text='When disabled, existing output files are skipped. Enable it when you explicitly want to regenerate media or the torrent.', wraplength=760).grid(row=2, column=0, columnspan=3, sticky='w', pady=(0, 7))
        ttk.Checkbutton(tab, text='Verify output after each job', variable=self.verify_var).grid(row=3, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Checkbutton(tab, text='Open output folder when queue finishes', variable=self.open_folder_var).grid(row=4, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Button(tab, text='Reset default settings', command=self.reset_defaults).grid(row=5, column=0, sticky='w', pady=(14, 0))
        ttk.Label(tab, text='Jobs are always processed sequentially. The next video starts only after the current video is completely finished. Temporary FFmpeg files are cleaned automatically.', wraplength=760).grid(row=6, column=0, columnspan=3, sticky='w', pady=(14, 0))

    def _build_torrent_tab(self, tab):
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)
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
        ttk.Label(tab, text='Privacy-related metadata settings have moved to the Privacy tab.', style='Muted.TLabel').grid(row=3, column=0, columnspan=3, sticky='w', pady=(10, 0))
        tab.rowconfigure(2, weight=1)

    def _build_images_tab(self, tab):
        tab.columnconfigure(1, weight=1)
        ttk.Checkbutton(tab, text='Create separate screenshots and a contact sheet when Everything is selected', variable=self.make_screens_var).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 6))
        ttk.Checkbutton(tab, text='Create 2-column collage from all separate screenshots', variable=self.make_collage_var).grid(row=1, column=0, columnspan=3, sticky='w', pady=(0, 8))
        rows = [('Separate screenshots', self.separate_count_var, 'Default: 6'), ('Contact sheet columns', self.layout_cols_var, 'Default: 3'), ('Contact sheet rows', self.layout_rows_var, 'Default: 9'), ('Contact sheet width / collage width (px)', self.layout_width_var, 'Default: 1300'), ('Margin (px)', self.margin_var, 'Default: 5'), ('JPEG quality', self.jpeg_quality_var, 'Default: 80'), ('From video (%)', self.image_start_pct_var, 'Default: 10'), ('To video (%)', self.image_end_pct_var, 'Default: 90')]
        for r, (label, var, hint) in enumerate(rows, start=2):
            ttk.Label(tab, text=label).grid(row=r, column=0, sticky='w', pady=4)
            ttk.Entry(tab, textvariable=var, width=14).grid(row=r, column=1, sticky='w', padx=8)
            ttk.Label(tab, text=hint).grid(row=r, column=2, sticky='w')
        ttk.Label(tab, text="Six separate screenshots are generated by default. The optional forum collage combines all separate screenshots two per row (2 × 3 with the default six) without cropping and keeps the original single-image files. The contact sheet remains the separate 3 × 9 overview with filename, resolution, duration, bitrate and file size.", wraplength=760).grid(row=10, column=0, columnspan=3, sticky='w', pady=(10, 0))

    def _build_gif_tab(self, tab):
        tab.columnconfigure(1, weight=1)
        ttk.Checkbutton(tab, text='Create animated preview when Everything is selected', variable=self.make_gif_var).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))
        ttk.Label(tab, text='Preview format').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Combobox(tab, textvariable=self.preview_format_var, values=PREVIEW_FORMATS, state='readonly', width=18).grid(row=1, column=1, sticky='w', padx=8)
        ttk.Label(tab, text='GIF, WebP, or both').grid(row=1, column=2, sticky='w')
        common = [('Maximum size per preview (MB)', self.gif_max_mb_var, 'Default: 7'), ('From video (%)', self.gif_start_pct_var, 'Default: 20'), ('To video (%)', self.gif_end_pct_var, 'Default: 90'), ('Number of short clips', self.gif_clips_var, 'Default: 8'), ('Seconds per clip', self.gif_seconds_var, 'Default: 1.0')]
        for r, (label, var, hint) in enumerate(common, start=2):
            ttk.Label(tab, text=label).grid(row=r, column=0, sticky='w', pady=4)
            ttk.Entry(tab, textvariable=var, width=14).grid(row=r, column=1, sticky='w', padx=8)
            ttk.Label(tab, text=hint).grid(row=r, column=2, sticky='w')
        ttk.Label(tab, text='GIF quality target').grid(row=7, column=0, sticky='w', pady=(10, 4))
        ttk.Label(tab, text=f'540 px / 8 FPS default').grid(row=7, column=2, sticky='w', pady=(10, 4))
        ttk.Entry(tab, textvariable=self.gif_width_var, width=7).grid(row=8, column=0, sticky='e', padx=(0, 4))
        ttk.Label(tab, text='px').grid(row=8, column=1, sticky='w')
        ttk.Entry(tab, textvariable=self.gif_fps_var, width=7).grid(row=8, column=2, sticky='w')
        ttk.Label(tab, text='WebP quality target: higher resolution and smoother motion').grid(row=9, column=0, columnspan=3, sticky='w', pady=(10, 4))
        webp = ttk.Frame(tab)
        webp.grid(row=10, column=0, columnspan=3, sticky='w')
        ttk.Label(webp, text='Width').pack(side='left')
        ttk.Entry(webp, textvariable=self.webp_width_var, width=7).pack(side='left', padx=(4, 10))
        ttk.Label(webp, text='FPS').pack(side='left')
        ttk.Entry(webp, textvariable=self.webp_fps_var, width=7).pack(side='left', padx=(4, 10))
        ttk.Label(webp, text='Quality').pack(side='left')
        ttk.Entry(webp, textvariable=self.webp_quality_var, width=7).pack(side='left', padx=(4, 10))
        ttk.Label(webp, text='Defaults: 720 px / 12 FPS / quality 90').pack(side='left')
        ttk.Label(tab, text='GIF and WebP use the same selected video segments and the same max-size limit. WebP now deliberately starts at a higher quality target and only steps down when needed to stay under the selected size.', wraplength=760).grid(row=11, column=0, columnspan=3, sticky='w', pady=(10, 0))

    def _build_media_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text='FFprobe reads technical information, audio/subtitles, HDR, chapters, embedded metadata, and common tags in the filename.', wraplength=500).grid(row=0, column=0, sticky='w')
        self.metadata_analyze_button = ttk.Button(top, text='Analyze selected job', command=self.quick_check_selected)
        self.metadata_analyze_button.grid(row=0, column=1, sticky='e', padx=(10, 0))
        ttk.Label(tab, textvariable=self.metadata_status_var, wraplength=650).grid(row=1, column=0, sticky='w', pady=(0, 8))
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

    def _build_hosting_tab(self, tab):
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(9, weight=1)
        ttk.Label(tab, text='HamsterImg account').grid(row=0, column=0, sticky='w', pady=4)
        key_frame = ttk.Frame(tab)
        key_frame.grid(row=0, column=1, columnspan=3, sticky='ew', padx=(8, 0))
        key_frame.columnconfigure(0, weight=1)
        self.hamster_key_entry = ttk.Entry(key_frame, textvariable=self.hamster_api_key_var, show='•')
        self.hamster_key_entry.grid(row=0, column=0, sticky='ew')
        ttk.Button(key_frame, text='Save key securely', command=self.save_hamster_api_key).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(key_frame, text='Remove saved key', command=self.remove_hamster_api_key).grid(row=0, column=2, padx=(6, 0))
        self.hamster_test_button = ttk.Button(key_frame, text='Test upload', command=self.test_hamster_connection)
        self.hamster_test_button.grid(row=0, column=3, padx=(6, 0))
        ttk.Label(tab, textvariable=self.hamster_status_var, wraplength=650).grid(row=1, column=0, columnspan=4, sticky='w', pady=(2, 10))
        ttk.Checkbutton(tab, text='Upload generated media automatically after each completed media job', variable=self.hamster_auto_upload_var).grid(row=2, column=0, columnspan=4, sticky='w', pady=4)
        choice = ttk.LabelFrame(tab, text='Files to upload', padding=8)
        choice.grid(row=3, column=0, columnspan=4, sticky='ew', pady=(6, 8))
        ttk.Checkbutton(choice, text='Separate screenshots', variable=self.hamster_upload_screens_var).pack(side='left', padx=(0, 12))
        ttk.Checkbutton(choice, text='Collage', variable=self.hamster_upload_collage_var).pack(side='left', padx=(0, 12))
        ttk.Checkbutton(choice, text='Contact sheet', variable=self.hamster_upload_layout_var).pack(side='left', padx=(0, 12))
        ttk.Checkbutton(choice, text='GIF', variable=self.hamster_upload_gif_var).pack(side='left', padx=(0, 12))
        ttk.Checkbutton(choice, text='WebP', variable=self.hamster_upload_webp_var).pack(side='left')
        ttk.Label(tab, text='Tags').grid(row=4, column=0, sticky='w', pady=4)
        ttk.Entry(tab, textvariable=self.hamster_tags_var).grid(row=4, column=1, columnspan=3, sticky='ew', padx=(8, 0))
        ttk.Label(tab, text='Album ID (optional)').grid(row=5, column=0, sticky='w', pady=4)
        ttk.Entry(tab, textvariable=self.hamster_album_id_var, width=18).grid(row=5, column=1, sticky='w', padx=8)
        ttk.Label(tab, text='Category ID (optional)').grid(row=5, column=2, sticky='e', pady=4)
        ttk.Entry(tab, textvariable=self.hamster_category_id_var, width=18).grid(row=5, column=3, sticky='w', padx=(8, 0))
        ttk.Checkbutton(tab, text='Mark uploads as NSFW', variable=self.hamster_nsfw_var).grid(row=6, column=0, columnspan=4, sticky='w', pady=4)
        ttk.Label(tab, text='The API key identifies your HamsterImg account. In the Windows build, Save key securely stores it in Windows Credential Manager — it is never written to settings.json or bundled into the EXE. Test upload creates a tiny image that expires automatically after 5 minutes.', wraplength=650).grid(row=7, column=0, columnspan=4, sticky='w', pady=(6, 10))
        buttons = ttk.Frame(tab)
        buttons.grid(row=8, column=0, columnspan=4, sticky='ew', pady=(0, 6))
        ttk.Button(buttons, text='Copy BBCode Full', command=lambda: self.copy_upload_links('bbcode_full')).pack(side='left')
        ttk.Label(buttons, text='Only BBCode Full is saved after HamsterImg uploads.').pack(side='left', padx=(12, 0))
        results_frame = ttk.LabelFrame(tab, text='BBCode Full – ready to paste', padding=6)
        results_frame.grid(row=9, column=0, columnspan=4, sticky='nsew')
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)
        self.hamster_results_text = tk.Text(results_frame, wrap='word', font=('Consolas', 9), height=10, state='disabled')
        self.hamster_results_text.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(results_frame, orient='vertical', command=self.hamster_results_text.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.hamster_results_text.configure(yscrollcommand=scroll.set)
        self._set_hamster_results_text('No BBCode Full results are available in this session yet.')

    def _set_hamster_results_text(self, text):
        if not hasattr(self, 'hamster_results_text'):
            return
        try:
            self.hamster_results_text.configure(state='normal')
            self.hamster_results_text.delete('1.0', 'end')
            self.hamster_results_text.insert('1.0', text or '')
            self.hamster_results_text.configure(state='disabled')
        except tk.TclError:
            pass

    def _load_hamster_api_key(self):
        if os.name != 'nt':
            self.hamster_status_var.set('API keys can be used for this session. Secure persistence is enabled in the Windows build.')
            return
        try:
            key = read_windows_credential()
        except Exception as exc:
            self.hamster_status_var.set(f'Could not read Windows Credential Manager: {friendly_error(exc)}')
            return
        if key:
            self.hamster_api_key_var.set(key)
            self.hamster_status_var.set('HamsterImg API key loaded securely from Windows Credential Manager.')
        else:
            self.hamster_status_var.set('No HamsterImg API key is saved yet.')

    def save_hamster_api_key(self):
        key = self.hamster_api_key_var.get().strip()
        if not key:
            messagebox.showinfo(APP_NAME, 'Paste your HamsterImg API key first.')
            return
        if os.name != 'nt':
            self.hamster_status_var.set('The API key is available for this session only. Secure persistence is enabled in the Windows build.')
            return
        try:
            save_windows_credential(key)
            self.hamster_status_var.set('API key saved securely in Windows Credential Manager.')
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def remove_hamster_api_key(self):
        try:
            if os.name == 'nt':
                delete_windows_credential()
            self.hamster_api_key_var.set('')
            self.hamster_status_var.set('Saved HamsterImg API key removed.')
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def test_hamster_connection(self):
        key = self.hamster_api_key_var.get().strip()
        if not key:
            messagebox.showinfo(APP_NAME, 'Paste or load your HamsterImg API key first.')
            return
        self.hamster_status_var.set('Running a temporary HamsterImg test upload...')
        self.hamster_test_button.configure(state='disabled')
        threading.Thread(target=self._hamster_test_worker, args=(key,), daemon=True).start()

    def _hamster_test_worker(self, api_key):
        try:
            # 1x1 transparent PNG. Expiration keeps the account clean after a connection test.
            png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
            with tempfile.TemporaryDirectory(prefix='torrentcreator_hamster_test_') as temp:
                test_file = Path(temp) / 'torrentcreator_api_test.png'
                test_file.write_bytes(png)
                result = hamster_upload_file(test_file, api_key, title='TorrentCreator API test', tags='torrentcreator-test', expiration='PT5M', timeout=60)
            self.ui_events.put(('hamster_test', True, f"Connection successful. Test file ID: {result.get('id') or 'OK'}. It is set to expire after 5 minutes."))
        except Exception as exc:
            self.ui_events.put(('hamster_test', False, friendly_error(exc)))

    def copy_upload_links(self, kind):
        if not self.upload_history:
            messagebox.showinfo(APP_NAME, 'No HamsterImg upload results are available yet.')
            return
        lines = []
        for entry in self.upload_history:
            formats = hamster_result_formats(entry)
            value = formats.get(kind, '')
            if value:
                lines.append(value)
        if not lines:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append('\n'.join(lines))
            self.update_idletasks()
            label = 'BBCode Full' if kind == 'bbcode_full' else kind.replace('_', ' ')
            self.hamster_status_var.set(f'Copied {len(lines)} {label} line(s) to the clipboard.')
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def _append_upload_results(self, job_name, results, links_file=''):
        for result in results:
            item = dict(result)
            item['job'] = job_name
            self.upload_history.append(item)
        lines = [hamster_result_formats(item)['bbcode_full'] for item in self.upload_history]
        self._set_hamster_results_text('\n'.join(lines).rstrip())

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

    def _refresh_queue_overview(self):
        total = len(self.jobs)
        if not total:
            self.queue_overview_var.set('Selected files: 0 • Queue empty')
            return
        waiting = sum(1 for job in self.jobs if str(job.get('status', '')).lower() == 'waiting')
        processing = sum(1 for job in self.jobs if str(job.get('status', '')).lower() in ('processing', 'running'))
        errors = sum(1 for job in self.jobs if str(job.get('status', '')).lower() in ('error', 'failed'))
        parts = [f'Selected files: {total}']
        if waiting:
            parts.append(f'Waiting: {waiting}')
        if processing:
            parts.append(f'Processing: {processing}')
        if errors:
            parts.append(f'Errors: {errors}')
        self.queue_overview_var.set(' • '.join(parts))

    def _rebuild_tree(self, select_ids=None):
        current = set(select_ids or self.queue_tree.selection())
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)
        for job in self.jobs:
            iid = str(job['id'])
            status_tag = str(job.get('status', 'waiting')).strip().lower().replace(' ', '_')
            self.queue_tree.insert('', 'end', iid=iid, values=(job['status'], job['source']), tags=(status_tag,))
            if iid in current:
                self.queue_tree.selection_add(iid)
        self._refresh_queue_overview()

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
            raise ValueError('GIF width/FPS is too low.')
        if self.webp_width_var.get() < 120 or self.webp_fps_var.get() < 1:
            raise ValueError('WebP width/FPS is too low.')
        if not 1 <= self.webp_quality_var.get() <= 100:
            raise ValueError('WebP quality must be between 1 and 100.')
        upload_requested = self.hamster_auto_upload_var.get() or self.process_mode_var.get() == 'Upload generated media only'
        if upload_requested and not self.hamster_api_key_var.get().strip():
            raise ValueError('HamsterImg upload is enabled, but no API key is available.')
        if upload_requested and not any((self.hamster_upload_screens_var.get(), self.hamster_upload_collage_var.get(), self.hamster_upload_layout_var.get(), self.hamster_upload_gif_var.get(), self.hamster_upload_webp_var.get())):
            raise ValueError('Select at least one media type to upload to HamsterImg.')

    def _snapshot_settings(self):
        self.validate_settings()
        return {'output_dir': self.output_dir_var.get().strip(), 'subfolder_per_job': bool(self.subfolder_per_job_var.get()), 'process_mode': self.process_mode_var.get(), 'overwrite': bool(self.overwrite_var.get()), 'verify': bool(self.verify_var.get()), 'open_folder': bool(self.open_folder_var.get()), 'trackers': parse_trackers(self.trackers_text.get('1.0', 'end').strip()), 'comment': self.comment_text.get('1.0', 'end').strip(), 'piece_length': self.PIECE_OPTIONS[self.piece_var.get()], 'piece_option': self.piece_var.get(), 'private': bool(self.private_var.get()), 'privacy_mode': bool(self.privacy_mode_var.get()), 'make_screens': bool(self.make_screens_var.get()), 'make_collage': bool(self.make_collage_var.get()), 'separate_count': int(self.separate_count_var.get()), 'layout_cols': int(self.layout_cols_var.get()), 'layout_rows': int(self.layout_rows_var.get()), 'layout_width': int(self.layout_width_var.get()), 'margin': int(self.margin_var.get()), 'jpeg_quality': int(self.jpeg_quality_var.get()), 'image_start_pct': float(self.image_start_pct_var.get()), 'image_end_pct': float(self.image_end_pct_var.get()), 'make_gif': bool(self.make_gif_var.get()), 'preview_format': self.preview_format_var.get(), 'gif_max_mb': float(self.gif_max_mb_var.get()), 'gif_start_pct': float(self.gif_start_pct_var.get()), 'gif_end_pct': float(self.gif_end_pct_var.get()), 'gif_clips': int(self.gif_clips_var.get()), 'gif_seconds': float(self.gif_seconds_var.get()), 'gif_width': int(self.gif_width_var.get()), 'gif_fps': int(self.gif_fps_var.get()), 'webp_width': int(self.webp_width_var.get()), 'webp_fps': int(self.webp_fps_var.get()), 'webp_quality': int(self.webp_quality_var.get()), 'hamster_auto_upload': bool(self.hamster_auto_upload_var.get()), 'hamster_upload_screens': bool(self.hamster_upload_screens_var.get()), 'hamster_upload_collage': bool(self.hamster_upload_collage_var.get()), 'hamster_upload_layout': bool(self.hamster_upload_layout_var.get()), 'hamster_upload_gif': bool(self.hamster_upload_gif_var.get()), 'hamster_upload_webp': bool(self.hamster_upload_webp_var.get()), 'hamster_tags': self.hamster_tags_var.get().strip(), 'hamster_album_id': self.hamster_album_id_var.get().strip(), 'hamster_category_id': self.hamster_category_id_var.get().strip(), 'hamster_nsfw': bool(self.hamster_nsfw_var.get())}

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
        mapping = [(self.output_dir_var, 'output_dir'), (self.subfolder_per_job_var, 'subfolder_per_job'), (self.process_mode_var, 'process_mode'), (self.overwrite_var, 'overwrite'), (self.verify_var, 'verify'), (self.open_folder_var, 'open_folder'), (self.piece_var, 'piece_option'), (self.private_var, 'private'), (self.privacy_mode_var, 'privacy_mode'), (self.make_screens_var, 'make_screens'), (self.make_collage_var, 'make_collage'), (self.separate_count_var, 'separate_count'), (self.layout_cols_var, 'layout_cols'), (self.layout_rows_var, 'layout_rows'), (self.layout_width_var, 'layout_width'), (self.margin_var, 'margin'), (self.jpeg_quality_var, 'jpeg_quality'), (self.image_start_pct_var, 'image_start_pct'), (self.image_end_pct_var, 'image_end_pct'), (self.make_gif_var, 'make_gif'), (self.preview_format_var, 'preview_format'), (self.gif_max_mb_var, 'gif_max_mb'), (self.gif_start_pct_var, 'gif_start_pct'), (self.gif_end_pct_var, 'gif_end_pct'), (self.gif_clips_var, 'gif_clips'), (self.gif_seconds_var, 'gif_seconds'), (self.gif_width_var, 'gif_width'), (self.gif_fps_var, 'gif_fps'), (self.webp_width_var, 'webp_width'), (self.webp_fps_var, 'webp_fps'), (self.webp_quality_var, 'webp_quality'), (self.hamster_auto_upload_var, 'hamster_auto_upload'), (self.hamster_upload_screens_var, 'hamster_upload_screens'), (self.hamster_upload_collage_var, 'hamster_upload_collage'), (self.hamster_upload_layout_var, 'hamster_upload_layout'), (self.hamster_upload_gif_var, 'hamster_upload_gif'), (self.hamster_upload_webp_var, 'hamster_upload_webp'), (self.hamster_tags_var, 'hamster_tags'), (self.hamster_album_id_var, 'hamster_album_id'), (self.hamster_category_id_var, 'hamster_category_id'), (self.hamster_nsfw_var, 'hamster_nsfw')]
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
        # v3.4 migration: the standard screenshot set changed from five to six.
        if self.separate_count_var.get() == 5:
            self.separate_count_var.set(6)
        if 'make_collage' not in data:
            self.make_collage_var.set(True)
        if 'webp_width' not in data:
            self.webp_width_var.set(720)
        if 'webp_fps' not in data:
            self.webp_fps_var.set(12)
        if 'webp_quality' not in data:
            self.webp_quality_var.set(90)
        if 'hamster_upload_collage' not in data:
            self.hamster_upload_collage_var.set(True)
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
        self.separate_count_var.set(6)
        self.make_collage_var.set(True)
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
        self.webp_width_var.set(720)
        self.webp_fps_var.set(12)
        self.webp_quality_var.set(90)
        self.hamster_auto_upload_var.set(False)
        self.hamster_upload_screens_var.set(True)
        self.hamster_upload_collage_var.set(True)
        self.hamster_upload_layout_var.set(True)
        self.hamster_upload_gif_var.set(True)
        self.hamster_upload_webp_var.set(True)
        self.hamster_tags_var.set('')
        self.hamster_album_id_var.set('')
        self.hamster_category_id_var.set('')
        self.hamster_nsfw_var.set(False)
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
            settings['hamster_api_key'] = self.hamster_api_key_var.get().strip()
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
        snapshot = [{'id': j['id'], 'source': j['source'], 'manual_clips': [dict(c) for c in j.get('manual_clips', [])]} for j in self.jobs]
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
        if hasattr(self, 'hamster_test_button'):
            self.hamster_test_button.configure(state='disabled' if busy else 'normal')

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
                job_settings = dict(settings)
                job_settings['manual_clips'] = [dict(c) for c in job.get('manual_clips', [])] if settings.get('preview_mode') == 'Manual' else None
                result = self._process_one_job(job, job_index, total_jobs, job_settings)
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
        if 'screens' in phases or 'gif' in phases or 'upload' in phases:
            video = find_video_source(source)
        weights = {'torrent': 40.0, 'screens': 25.0, 'gif': 20.0, 'upload': 15.0}
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
            image_result = generate_screenshots_and_contact_sheet(video_path=video, output_dir=job_dir, separate_count=settings['separate_count'], layout_cols=settings['layout_cols'], layout_rows=settings['layout_rows'], layout_width=settings['layout_width'], margin=settings['margin'], jpeg_quality=settings['jpeg_quality'], start_pct=settings['image_start_pct'], end_pct=settings['image_end_pct'], progress_callback=image_progress, cancel_event=self.cancel_event, overwrite=settings['overwrite'], create_collage=settings.get('make_collage', True), collage_columns=2)
            w, h = image_result['layout_size']
            if image_result['created']:
                summary.append(f"screenshots/contact sheet {w}×{h}" + (' + 2-column collage' if settings.get('make_collage', True) else ''))
            else:
                summary.append('screenshots/contact sheet: already existed')
            phase_progress('screens', 1.0, f"{source.name}: screenshots/contact sheet{'/collage' if settings.get('make_collage', True) else ''} complete")
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
                common = dict(video_path=video, output_path=preview_path, max_size_mb=settings['gif_max_mb'], start_pct=settings['gif_start_pct'], end_pct=settings['gif_end_pct'], clips=settings['gif_clips'], clip_seconds=settings['gif_seconds'], progress_callback=preview_progress, cancel_event=self.cancel_event, overwrite=settings['overwrite'], manual_clips=settings.get('manual_clips'))
                if preview_kind == 'GIF':
                    preview_result = create_size_limited_gif(width=settings['gif_width'], fps=settings['gif_fps'], **common)
                    if preview_result.get('skipped'):
                        summary.append(f"GIF: already existed ({format_bytes(preview_result['size'])})")
                    else:
                        summary.append(f"GIF {format_bytes(preview_result['size'])}, {preview_result['width']} px, {preview_result['fps']} FPS")
                else:
                    preview_result = create_size_limited_webp(width=settings.get('webp_width', 720), fps=settings.get('webp_fps', 12), quality=settings.get('webp_quality', 90), **common)
                    if preview_result.get('skipped'):
                        summary.append(f"WebP: already existed ({format_bytes(preview_result['size'])})")
                    else:
                        summary.append(f"WebP {format_bytes(preview_result['size'])}, {preview_result['width']} px, {preview_result['fps']} FPS, quality {preview_result['quality']}")
            phase_progress('gif', 1.0, f'{source.name}: animated preview complete')
        if 'upload' in phases:
            check_cancel(self.cancel_event)
            files_to_upload = collect_hamster_upload_files(video, job_dir, settings, phases)
            if not files_to_upload:
                raise RuntimeError('No generated screenshots, collage, contact sheet, GIF, or WebP files were found to upload. Check the output folder and upload selections.')
            upload_results = []
            total_uploads = len(files_to_upload)
            for upload_index, media_file in enumerate(files_to_upload):
                check_cancel(self.cancel_event)
                phase_progress('upload', upload_index / max(1, total_uploads), f'{source.name}: uploading {media_file.name} ({upload_index + 1}/{total_uploads})')
                result = hamster_upload_file(
                    media_file,
                    settings.get('hamster_api_key', ''),
                    title=media_file.stem,
                    tags=settings.get('hamster_tags', ''),
                    album_id=settings.get('hamster_album_id', ''),
                    category_id=settings.get('hamster_category_id', ''),
                    nsfw=settings.get('hamster_nsfw', False),
                    cancel_event=self.cancel_event,
                )
                upload_results.append(result)
                phase_progress('upload', (upload_index + 1) / max(1, total_uploads), f'{source.name}: uploaded {upload_index + 1}/{total_uploads}')
            links_file = write_hamster_links_file(job_dir, video, upload_results)
            summary.append(f'HamsterImg {len(upload_results)} file(s) uploaded')
            self.ui_events.put(('upload_results', source.name, upload_results, str(links_file)))
            phase_progress('upload', 1.0, f'{source.name}: HamsterImg upload complete')
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
                elif kind == 'hamster_test':
                    _, ok, message = event
                    self._append_console(('HamsterImg test: ' if ok else 'HamsterImg test failed: ') + message)
                    self.hamster_test_button.configure(state='disabled' if self.busy else 'normal')
                    if ok:
                        self.hamster_status_var.set(message)
                    else:
                        self.hamster_status_var.set(f'Test upload failed: {message}')
                        messagebox.showerror('HamsterImg', message)
                elif kind == 'upload_results':
                    _, job_name, results, links_file = event
                    self._append_console(f'{job_name}: uploaded {len(results)} file(s) to HamsterImg; BBCode Full saved to {links_file}')
                    self._append_upload_results(job_name, results, links_file)
                    self.hamster_status_var.set(f'Uploaded {len(results)} file(s) to HamsterImg. BBCode Full saved to {links_file}')
                elif kind == 'job_status':
                    _, job_id, status, detail = event
                    job_for_log = self._job_by_id(job_id)
                    if job_for_log:
                        self._append_console(f"{Path(job_for_log['source']).name}: {status}{' — ' + detail if detail else ''}")
                    job = self._job_by_id(job_id)
                    if job:
                        job['status'] = status
                        if detail:
                            job['detail'] = detail
                        self._rebuild_tree(select_ids=self.queue_tree.selection())
                elif kind == 'job_completed':
                    _, job_id, detail = event
                    completed_for_log = self._job_by_id(job_id)
                    if completed_for_log:
                        self._append_console(f"{Path(completed_for_log['source']).name}: completed successfully")
                    removed = next((j for j in self.jobs if j['id'] == int(job_id)), None)
                    if removed is not None:
                        hook = getattr(self, '_job_completed_hook', None)
                        if callable(hook):
                            try:
                                hook(removed, detail)
                            except Exception as hook_exc:
                                self._append_console(f'Preview refresh warning: {friendly_error(hook_exc)}')
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


# -----------------------------------------------------------------------------
# TorrentCreator v3.1 UI / preview layer
# -----------------------------------------------------------------------------

def _normalize_manual_preview_clips(manual_clips, duration):
    """Validate and normalize user-selected preview clips.

    Output entries are dictionaries with start/end/duration in seconds. The
    original video is never modified.
    """
    result = []
    duration = max(0.0, float(duration or 0.0))
    for raw in manual_clips or []:
        try:
            start = float(raw.get('start', 0.0))
            end = float(raw.get('end', 0.0))
        except Exception:
            continue
        start = max(0.0, start)
        if duration > 0:
            start = min(start, duration)
            end = min(end, duration)
        if end <= start:
            continue
        result.append({'start': start, 'end': end, 'duration': end - start})
    result.sort(key=lambda item: item['start'])
    return result


def _manual_candidate_settings_gif(base_width, base_fps, avg_seconds):
    # Manual mode preserves the exact selected time ranges. Compression may
    # reduce width/FPS/colors, but never silently shortens the selected clips.
    candidates = gif_candidate_settings(base_width, base_fps, max(0.2, avg_seconds))
    return [c for c in candidates if abs(c[3] - max(0.2, avg_seconds)) < 1e-6][:6]


def _manual_candidate_settings_webp(base_width, base_fps, avg_seconds, base_quality=90):
    candidates = webp_candidate_settings(base_width, base_fps, max(0.2, avg_seconds), base_quality)
    return [c for c in candidates if abs(c[3] - max(0.2, avg_seconds)) < 1e-6][:7]


def create_size_limited_gif_v31(video_path, output_path, max_size_mb=7.0, start_pct=20.0, end_pct=90.0,
                                clips=8, clip_seconds=1.0, width=540, fps=8, progress_callback=None,
                                cancel_event=None, overwrite=False, manual_clips=None):
    """Create an automatic or manually selected size-limited animated GIF."""
    ffmpeg, _ = require_ffmpeg()
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    check_cancel(cancel_event)
    max_bytes = int(float(max_size_mb) * 1024 * 1024)
    if output_path.exists() and not overwrite:
        size = output_path.stat().st_size
        if size <= max_bytes:
            if progress_callback:
                progress_callback(1.0, 'GIF already exists')
            return {'path': output_path, 'size': size, 'width': 0, 'fps': 0, 'colors': 0,
                    'clip_seconds': clip_seconds, 'clips': clips, 'start_pct': start_pct,
                    'end_pct': end_pct, 'attempts': 0, 'skipped': True,
                    'manual': bool(manual_clips)}
    meta = probe_video(video_path, cancel_event=cancel_event)
    if max_bytes <= 0:
        raise ValueError('The maximum GIF size must be greater than 0 MB.')
    if width < 120 or fps < 1:
        raise ValueError('GIF width/FPS is too low.')

    manual_segments = _normalize_manual_preview_clips(manual_clips, meta['duration'])
    if manual_clips is not None and not manual_segments:
        raise ValueError('Manual preview mode is enabled, but this video has no valid manual clips.')

    if manual_segments:
        avg_seconds = sum(x['duration'] for x in manual_segments) / len(manual_segments)
        all_candidates = _manual_candidate_settings_gif(int(width), int(fps), avg_seconds)
    else:
        if clips < 1 or clip_seconds <= 0:
            raise ValueError('GIF clips and seconds per clip must be greater than 0.')
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
            try_width, try_fps, colors, try_clip_seconds = remaining.pop(0)
            attempt += 1
            if manual_segments:
                starts = [x['start'] for x in manual_segments]
                durations = [x['duration'] for x in manual_segments]
            else:
                starts = percent_positions(meta['duration'], clips, start_pct, end_pct, clip_duration=try_clip_seconds)
                durations = [try_clip_seconds] * len(starts)
            filter_complex = build_fast_gif_filter(len(starts), try_width, try_fps, colors)
            candidate = temp_dir / f'candidate_{attempt:02d}.gif'
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y']
            for start, seg_duration in zip(starts, durations):
                command.extend(['-ss', f'{start:.3f}', '-t', f'{seg_duration:.3f}', '-i', str(video_path)])
            command.extend(['-filter_complex', filter_complex, '-map', '[gif]', '-an', '-map_metadata', '-1', '-loop', '0', str(candidate)])
            expected_duration = sum(durations)
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
            if not candidate.exists():
                continue
            size = candidate.stat().st_size
            last_size = size
            if first_size is None:
                first_size = size
                first_candidate = (try_width, try_fps, colors, try_clip_seconds)
            if size <= max_bytes:
                shutil.copy2(candidate, output_path)
                if progress_callback:
                    progress_callback(1.0, 'GIF complete')
                return {'path': output_path, 'size': size, 'width': try_width, 'fps': try_fps,
                        'colors': colors, 'clip_seconds': try_clip_seconds,
                        'clips': len(starts), 'start_pct': start_pct, 'end_pct': end_pct,
                        'attempts': attempt, 'skipped': False, 'manual': bool(manual_segments)}
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
        extra = ' Shorten the manual clips or reduce the preview width/FPS.' if manual_segments else ' Try fewer clips or a shorter duration per clip.'
        raise RuntimeError(f'Could not reduce the GIF below {max_size_mb:g} MB. The smallest attempt was {format_bytes(last_size)}.{extra}')
    raise RuntimeError('The GIF could not be created.')


def create_size_limited_webp_v31(video_path, output_path, max_size_mb=7.0, start_pct=20.0, end_pct=90.0,
                                 clips=8, clip_seconds=1.0, width=720, fps=12, quality=90, progress_callback=None,
                                 cancel_event=None, overwrite=False, manual_clips=None):
    """Create an automatic or manually selected size-limited animated WebP."""
    ffmpeg, _ = require_ffmpeg()
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    check_cancel(cancel_event)
    max_bytes = int(float(max_size_mb) * 1024 * 1024)
    if output_path.exists() and not overwrite:
        size = output_path.stat().st_size
        if size <= max_bytes:
            if progress_callback:
                progress_callback(1.0, 'WebP already exists')
            return {'path': output_path, 'size': size, 'width': 0, 'fps': 0, 'quality': 0,
                    'clip_seconds': clip_seconds, 'clips': clips, 'start_pct': start_pct,
                    'end_pct': end_pct, 'attempts': 0, 'skipped': True,
                    'manual': bool(manual_clips)}
    meta = probe_video(video_path, cancel_event=cancel_event)
    if max_bytes <= 0:
        raise ValueError('The maximum WebP size must be greater than 0 MB.')
    if width < 120 or fps < 1:
        raise ValueError('WebP width/FPS is too low.')

    manual_segments = _normalize_manual_preview_clips(manual_clips, meta['duration'])
    if manual_clips is not None and not manual_segments:
        raise ValueError('Manual preview mode is enabled, but this video has no valid manual clips.')

    if manual_segments:
        avg_seconds = sum(x['duration'] for x in manual_segments) / len(manual_segments)
        all_candidates = _manual_candidate_settings_webp(int(width), int(fps), avg_seconds, int(quality))
    else:
        if clips < 1 or clip_seconds <= 0:
            raise ValueError('WebP clips and seconds per clip must be greater than 0.')
        all_candidates = webp_candidate_settings(int(width), int(fps), float(clip_seconds), int(quality))

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
            try_width, try_fps, quality, try_clip_seconds = remaining.pop(0)
            attempt += 1
            if manual_segments:
                starts = [x['start'] for x in manual_segments]
                durations = [x['duration'] for x in manual_segments]
            else:
                starts = percent_positions(meta['duration'], clips, start_pct, end_pct, clip_duration=try_clip_seconds)
                durations = [try_clip_seconds] * len(starts)
            filter_complex = build_fast_webp_filter(len(starts), try_width, try_fps)
            candidate = temp_dir / f'candidate_{attempt:02d}.webp'
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y']
            for start, seg_duration in zip(starts, durations):
                command.extend(['-ss', f'{start:.3f}', '-t', f'{seg_duration:.3f}', '-i', str(video_path)])
            command.extend(['-filter_complex', filter_complex, '-map', '[preview]', '-an', '-map_metadata', '-1',
                            '-c:v', 'libwebp_anim', '-lossless', '0', '-quality', str(int(quality)),
                            '-compression_level', '6', '-loop', '0', str(candidate)])
            expected_duration = sum(durations)
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
            if not candidate.exists():
                continue
            size = candidate.stat().st_size
            last_size = size
            if first_size is None:
                first_size = size
                first_candidate = (try_width, try_fps, quality, try_clip_seconds)
            if size <= max_bytes:
                shutil.copy2(candidate, output_path)
                if progress_callback:
                    progress_callback(1.0, 'WebP complete')
                return {'path': output_path, 'size': size, 'width': try_width, 'fps': try_fps,
                        'quality': quality, 'clip_seconds': try_clip_seconds,
                        'clips': len(starts), 'start_pct': start_pct, 'end_pct': end_pct,
                        'attempts': attempt, 'skipped': False, 'manual': bool(manual_segments)}
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
        extra = ' Shorten the manual clips or reduce the preview width/FPS.' if manual_segments else ' Try fewer clips or a shorter duration per clip.'
        raise RuntimeError(f'Could not reduce the WebP below {max_size_mb:g} MB. The smallest attempt was {format_bytes(last_size)}.{extra}')
    raise RuntimeError('The WebP could not be created.')


# Parent processing methods resolve these globals at runtime.
create_size_limited_gif = create_size_limited_gif_v31
create_size_limited_webp = create_size_limited_webp_v31


def _system_prefers_light_theme():
    if os.name == 'nt':
        try:
            import winreg
            key_path = r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
                return bool(int(value))
        except Exception:
            pass
    # A neutral default outside Windows; Windows users normally hit the registry path above.
    return False


def _load_vlc_module():
    """Load python-vlc after preparing paths for a bundled libVLC runtime."""
    runtime = None
    for base in application_resource_dirs():
        candidates = [Path(base) / 'vlc', Path(base)]
        for candidate in candidates:
            if (candidate / 'libvlc.dll').exists() or (candidate / 'libvlc.so').exists() or (candidate / 'libvlc.dylib').exists():
                runtime = candidate
                break
        if runtime:
            break
    if runtime:
        plugins = runtime / 'plugins'
        os.environ['VLC_PLUGIN_PATH'] = str(plugins)
        os.environ['PATH'] = str(runtime) + os.pathsep + os.environ.get('PATH', '')
        if os.name == 'nt' and hasattr(os, 'add_dll_directory'):
            try:
                os.add_dll_directory(str(runtime))
            except Exception:
                pass
    try:
        import vlc  # type: ignore
    except Exception as exc:
        return None, f'python-vlc could not be loaded: {exc}'
    try:
        # Version access forces libVLC to load and catches missing runtime files early.
        version = vlc.libvlc_get_version()
        if isinstance(version, bytes):
            version = version.decode('utf-8', 'replace')
        return vlc, f'VLC {version}'
    except Exception as exc:
        return None, f'libVLC runtime is unavailable: {exc}'


def _parse_clock_text(value):
    text = str(value or '').strip()
    if not text:
        raise ValueError('Enter a time such as 00:12:34.500.')
    parts = text.split(':')
    try:
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
        return float(text)
    except Exception as exc:
        raise ValueError(f'Invalid time: {text}') from exc


def _format_clock_precise(seconds):
    seconds = max(0.0, float(seconds or 0.0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f'{hours:02d}:{minutes:02d}:{secs:06.3f}'


class TorrentCreatorAppV31(TorrentCreatorApp):
    """v3.1 desktop UI: themes, embedded preview, media gallery and manual preview editor."""

    def _ensure_v31_vars(self):
        if hasattr(self, 'theme_var'):
            return
        self.theme_var = tk.StringVar(value='System')
        self.preview_mode_var = tk.StringVar(value='Automatic')
        self.manual_start_var = tk.StringVar(value='00:00:00.000')
        self.manual_end_var = tk.StringVar(value='00:00:01.000')
        self.preview_time_var = tk.StringVar(value='00:00:00 / 00:00:00')
        self.preview_seek_var = tk.DoubleVar(value=0.0)
        self.preview_volume_var = tk.IntVar(value=80)
        self.preview_status_var = tk.StringVar(value='Select a video in the queue to preview it.')
        self.preview_selected_media_var = tk.StringVar(value='No generated media selected.')
        self.results_summary_var = tk.StringVar(value='Results: —')
        self.console_collapsed = False
        self._preview_user_seeking = False
        self._preview_media_path = None
        self._preview_fps = 25.0
        self._preview_duration = 0.0
        self._gallery_files = {}
        self._gallery_photo = None
        self._vlc = None
        self._vlc_instance = None
        self._vlc_player = None
        self._vlc_status = 'Initializing VLC preview...'
        self._last_completed_source = None

    def _theme_palette(self, mode=None):
        requested = mode or self.theme_var.get()
        use_light = requested == 'Light' or (requested == 'System' and _system_prefers_light_theme())
        if use_light:
            return {
                'bg': '#f3f5f7', 'panel': '#ffffff', 'panel2': '#e9edf2', 'field': '#ffffff',
                'border': '#c7cdd5', 'text': '#20252b', 'muted': '#66717c', 'accent': '#2f6feb',
                'accent_hover': '#3b79f0', 'danger': '#cf3f4a', 'warning': '#9a6b16',
                'success': '#20834f', 'selection': '#cfe2ff', 'canvas': '#101216', 'canvas_text': '#e7e9ed'
            }
        return {
            'bg': '#1b1d20', 'panel': '#23262a', 'panel2': '#2a2d32', 'field': '#17191c',
            'border': '#3a3e44', 'text': '#e7e9ed', 'muted': '#a0a6ae', 'accent': '#3d7eff',
            'accent_hover': '#4c89ff', 'danger': '#b94c55', 'warning': '#d2a34d',
            'success': '#63b889', 'selection': '#315b8a', 'canvas': '#0e1012', 'canvas_text': '#e7e9ed'
        }

    def _apply_theme(self, _event=None):
        self._ensure_v31_vars()
        c = self._theme_palette()
        self.ui_colors = c
        try:
            self.configure(background=c['bg'])
        except Exception:
            pass
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('.', font=('Segoe UI', 9), background=c['bg'], foreground=c['text'])
        style.configure('TFrame', background=c['bg'])
        style.configure('Panel.TFrame', background=c['panel'])
        style.configure('Toolbar.TFrame', background=c['panel2'])
        style.configure('TLabel', background=c['bg'], foreground=c['text'])
        style.configure('Panel.TLabel', background=c['panel'], foreground=c['text'])
        style.configure('Muted.TLabel', background=c['bg'], foreground=c['muted'])
        style.configure('PanelMuted.TLabel', background=c['panel'], foreground=c['muted'])
        style.configure('Title.TLabel', background=c['bg'], foreground=c['text'], font=('Segoe UI Semibold', 16))
        style.configure('Status.TLabel', background=c['panel2'], foreground=c['muted'], padding=(8, 4))
        style.configure('TButton', background=c['panel2'], foreground=c['text'], bordercolor=c['border'], lightcolor=c['border'], darkcolor=c['border'], padding=(9, 5), relief='flat')
        style.map('TButton', background=[('active', c['border']), ('pressed', c['field']), ('disabled', c['panel2'])], foreground=[('disabled', c['muted'])])
        style.configure('Accent.TButton', background=c['accent'], foreground='#ffffff', bordercolor=c['accent'], padding=(12, 6))
        style.map('Accent.TButton', background=[('active', c['accent_hover']), ('pressed', c['accent'])])
        style.configure('Danger.TButton', background=c['danger'], foreground='#ffffff', bordercolor=c['danger'], padding=(12, 6))
        style.configure('TEntry', fieldbackground=c['field'], foreground=c['text'], insertcolor=c['text'], bordercolor=c['border'], lightcolor=c['border'], darkcolor=c['border'], padding=5)
        style.configure('TCombobox', fieldbackground=c['field'], background=c['panel2'], foreground=c['text'], arrowcolor=c['text'], bordercolor=c['border'], padding=4)
        style.map('TCombobox', fieldbackground=[('readonly', c['field'])], selectbackground=[('readonly', c['field'])], selectforeground=[('readonly', c['text'])])
        style.configure('TCheckbutton', background=c['bg'], foreground=c['text'], padding=2)
        style.map('TCheckbutton', background=[('active', c['bg'])], foreground=[('disabled', c['muted'])])
        style.configure('TLabelframe', background=c['bg'], foreground=c['muted'], bordercolor=c['border'], relief='solid')
        style.configure('TLabelframe.Label', background=c['bg'], foreground=c['muted'], font=('Segoe UI Semibold', 9))
        style.configure('Treeview', background=c['field'], fieldbackground=c['field'], foreground=c['text'], bordercolor=c['border'], rowheight=25)
        selected_fg = '#ffffff' if self.theme_var.get() != 'Light' else c['text']
        style.map('Treeview', background=[('selected', c['selection'])], foreground=[('selected', selected_fg)])
        style.configure('Treeview.Heading', background=c['panel2'], foreground=c['text'], bordercolor=c['border'], relief='flat', padding=(6, 5), font=('Segoe UI Semibold', 9))
        style.configure('TNotebook', background=c['bg'], borderwidth=0)
        style.configure('TNotebook.Tab', background=c['panel2'], foreground=c['muted'], bordercolor=c['border'], padding=(10, 6))
        style.map('TNotebook.Tab', background=[('selected', c['field']), ('active', c['border'])], foreground=[('selected', c['text']), ('active', c['text'])])
        style.configure('Horizontal.TProgressbar', background=c['accent'], troughcolor=c['field'], bordercolor=c['border'])
        style.configure('Vertical.TScrollbar', background=c['panel2'], troughcolor=c['field'], bordercolor=c['border'], arrowcolor=c['text'])
        style.configure('Horizontal.TScrollbar', background=c['panel2'], troughcolor=c['field'], bordercolor=c['border'], arrowcolor=c['text'])
        self.option_add('*TCombobox*Listbox.background', c['field'])
        self.option_add('*TCombobox*Listbox.foreground', c['text'])
        self.option_add('*TCombobox*Listbox.selectBackground', c['selection'])
        self.option_add('*TCombobox*Listbox.selectForeground', selected_fg)
        try:
            self._style_text_widgets(self)
        except Exception:
            pass
        if hasattr(self, 'preview_canvas'):
            self.preview_canvas.configure(background=c['canvas'], highlightbackground=c['border'])
        if hasattr(self, 'gallery_image_label'):
            self.gallery_image_label.configure(background=c['canvas'], foreground=c['canvas_text'])
        if hasattr(self, 'queue_tree'):
            self.queue_tree.tag_configure('waiting', foreground=c['muted'])
            self.queue_tree.tag_configure('processing', foreground=c['warning'])
            self.queue_tree.tag_configure('running', foreground=c['warning'])
            self.queue_tree.tag_configure('error', foreground=c['danger'])
            self.queue_tree.tag_configure('failed', foreground=c['danger'])
            self.queue_tree.tag_configure('completed', foreground=c['success'])
        if _event is not None:
            self._save_settings()

    def _build_ui(self):
        self._ensure_v31_vars()
        self._apply_theme()
        self.geometry('1500x900')
        self.minsize(1120, 720)
        c = self.ui_colors

        outer = ttk.Frame(self, padding=(8, 7, 8, 7))
        outer.pack(fill='both', expand=True)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 7))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=APP_NAME, style='Title.TLabel').grid(row=0, column=0, sticky='w')
        dd = 'Drag & drop enabled' if DRAGDROP_AVAILABLE else 'Drag & drop unavailable'
        ttk.Label(header, text=f'v{APP_VERSION}  •  {ffmpeg_bundle_status()}  •  {dd}', style='Muted.TLabel').grid(row=0, column=1, sticky='w', padx=(14, 8))
        ttk.Label(header, text='Theme', style='Muted.TLabel').grid(row=0, column=2, padx=(8, 4))
        theme_box = ttk.Combobox(header, textvariable=self.theme_var, values=('System', 'Dark', 'Light'), state='readonly', width=9)
        theme_box.grid(row=0, column=3, padx=(0, 8))
        theme_box.bind('<<ComboboxSelected>>', self._apply_theme)
        self.start_button = ttk.Button(header, text='Start queue', command=self.start_queue, style='Accent.TButton')
        self.start_button.grid(row=0, column=4, padx=(0, 5))
        self.cancel_button = ttk.Button(header, text='Cancel', command=self.cancel_current, state='disabled', style='Danger.TButton')
        self.cancel_button.grid(row=0, column=5)

        self.main_paned = ttk.Panedwindow(outer, orient='horizontal')
        self.main_paned.grid(row=1, column=0, sticky='nsew')

        left = ttk.Frame(self.main_paned, style='Panel.TFrame', padding=7)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        self.main_paned.add(left, weight=3)

        ttk.Label(left, text='Job queue', style='Panel.TLabel', font=('Segoe UI Semibold', 10)).grid(row=0, column=0, sticky='w', pady=(0, 5))
        queue_buttons = ttk.Frame(left, style='Panel.TFrame')
        queue_buttons.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        buttons = [('Add files', self.add_files), ('Add folder', self.add_folder_job), ('Remove', self.remove_selected_jobs),
                   ('Move up', lambda: self.move_selected(-1)), ('Move down', lambda: self.move_selected(1)), ('Quick Check', self.quick_check_selected)]
        self.queue_control_buttons = []
        for idx, (text, command) in enumerate(buttons):
            btn = ttk.Button(queue_buttons, text=text, command=command)
            btn.grid(row=idx // 3, column=idx % 3, sticky='ew', padx=(0 if idx % 3 == 0 else 4, 0), pady=(0 if idx < 3 else 4, 0))
            queue_buttons.columnconfigure(idx % 3, weight=1)
            self.queue_control_buttons.append(btn)

        tree_frame = ttk.Frame(left, style='Panel.TFrame')
        tree_frame.grid(row=2, column=0, sticky='nsew')
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.queue_tree = ttk.Treeview(tree_frame, columns=('status', 'source'), show='headings', selectmode='extended')
        self.queue_tree.heading('status', text='Status')
        self.queue_tree.heading('source', text='File / folder')
        self.queue_tree.column('status', width=100, minwidth=86, stretch=False)
        self.queue_tree.column('source', width=330, minwidth=180, stretch=True)
        self.queue_tree.grid(row=0, column=0, sticky='nsew')
        qscroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.queue_tree.yview)
        qscroll.grid(row=0, column=1, sticky='ns')
        self.queue_tree.configure(yscrollcommand=qscroll.set)
        self.queue_tree.bind('<<TreeviewSelect>>', self._queue_selection_changed)
        self.queue_tree.bind('<Delete>', lambda e: self.remove_selected_jobs())
        self.queue_tree.bind('<Double-1>', lambda e: self._preview_selected_job())
        if DRAGDROP_AVAILABLE:
            try:
                self.queue_tree.drop_target_register(DND_FILES)
                self.queue_tree.dnd_bind('<<Drop>>', self._on_drop)
            except Exception:
                pass
        ttk.Label(left, textvariable=self.queue_overview_var, style='PanelMuted.TLabel', wraplength=390).grid(row=3, column=0, sticky='w', pady=(6, 2))
        ttk.Label(left, text='Jobs run sequentially. Successful jobs are removed automatically.', style='PanelMuted.TLabel', wraplength=390).grid(row=4, column=0, sticky='w', pady=(0, 2))

        right = ttk.Frame(self.main_paned)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)
        self.main_paned.add(right, weight=7)

        top = ttk.Frame(right, style='Panel.TFrame', padding=7)
        top.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text='Output', style='Panel.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Entry(top, textvariable=self.output_dir_var).grid(row=0, column=1, sticky='ew', padx=7)
        ttk.Button(top, text='Browse', command=self.choose_output_dir).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(top, text='Open', command=self.open_output_dir).grid(row=0, column=3)
        ttk.Checkbutton(top, text='Separate subfolder per video', variable=self.subfolder_per_job_var).grid(row=1, column=1, columnspan=3, sticky='w', pady=(4, 0))

        selected = ttk.Frame(right, style='Panel.TFrame', padding=(7, 5))
        selected.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        selected.columnconfigure(0, weight=1)
        ttk.Label(selected, textvariable=self.selected_info_var, style='Panel.TLabel', wraplength=940).grid(row=0, column=0, sticky='w')
        ttk.Label(selected, textvariable=self.results_summary_var, style='PanelMuted.TLabel', wraplength=940).grid(row=1, column=0, sticky='w', pady=(2, 0))

        progress = ttk.Frame(right, style='Panel.TFrame', padding=7)
        progress.grid(row=2, column=0, sticky='ew', pady=(0, 6))
        progress.columnconfigure(1, weight=1)
        ttk.Label(progress, text='Current file', style='Panel.TLabel', width=12).grid(row=0, column=0, sticky='w')
        self.current_progress = ttk.Progressbar(progress, maximum=100)
        self.current_progress.grid(row=0, column=1, sticky='ew', padx=(4, 7))
        ttk.Label(progress, textvariable=self.current_progress_var, style='Panel.TLabel', width=6, anchor='e').grid(row=0, column=2)
        ttk.Label(progress, text='Entire queue', style='Panel.TLabel', width=12).grid(row=1, column=0, sticky='w', pady=(5, 0))
        self.queue_progress = ttk.Progressbar(progress, maximum=100)
        self.queue_progress.grid(row=1, column=1, sticky='ew', padx=(4, 7), pady=(5, 0))
        ttk.Label(progress, textvariable=self.queue_progress_var, style='Panel.TLabel', width=6, anchor='e').grid(row=1, column=2, pady=(5, 0))
        ttk.Label(progress, textvariable=self.elapsed_var, style='PanelMuted.TLabel').grid(row=2, column=1, sticky='w', pady=(4, 0))
        ttk.Label(progress, textvariable=self.eta_var, style='PanelMuted.TLabel').grid(row=2, column=2, sticky='e', pady=(4, 0))

        self.notebook = ttk.Notebook(right)
        self.notebook.grid(row=3, column=0, sticky='nsew')
        general_tab = ttk.Frame(self.notebook, padding=12)
        torrent_tab = ttk.Frame(self.notebook, padding=12)
        images_tab = ttk.Frame(self.notebook, padding=12)
        preview_tab = ttk.Frame(self.notebook, padding=8)
        hosting_tab = ttk.Frame(self.notebook, padding=12)
        privacy_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(general_tab, text='General')
        self.notebook.add(torrent_tab, text='Torrent')
        self.notebook.add(images_tab, text='Screenshots')
        self.notebook.add(preview_tab, text='Preview')
        self.notebook.add(hosting_tab, text='Image Hosting')
        self.notebook.add(privacy_tab, text='Privacy')
        self._build_run_tab(general_tab)
        self._build_torrent_tab(torrent_tab)
        self._build_images_tab(images_tab)
        self._build_preview_tab(preview_tab)
        self._build_hosting_tab(hosting_tab)
        self._build_privacy_tab(privacy_tab)

        self.console_shell = ttk.Frame(outer, style='Panel.TFrame', padding=(7, 5))
        self.console_shell.grid(row=2, column=0, sticky='ew', pady=(6, 0))
        self.console_shell.columnconfigure(0, weight=1)
        console_head = ttk.Frame(self.console_shell, style='Panel.TFrame')
        console_head.grid(row=0, column=0, sticky='ew')
        console_head.columnconfigure(0, weight=1)
        ttk.Label(console_head, text='Console', style='Panel.TLabel', font=('Segoe UI Semibold', 9)).grid(row=0, column=0, sticky='w')
        self.console_toggle_button = ttk.Button(console_head, text='Collapse', command=self._toggle_console)
        self.console_toggle_button.grid(row=0, column=1, padx=(4, 0))
        ttk.Button(console_head, text='Clear', command=self._clear_console).grid(row=0, column=2, padx=(4, 0))
        ttk.Button(console_head, text='Save log', command=self._save_console).grid(row=0, column=3, padx=(4, 0))
        self.console_body = ttk.Frame(self.console_shell, style='Panel.TFrame')
        self.console_body.grid(row=1, column=0, sticky='ew', pady=(4, 0))
        self.console_body.columnconfigure(0, weight=1)
        self.console_text = tk.Text(self.console_body, height=5, wrap='none', font=('Consolas', 9), state='disabled')
        self.console_text.grid(row=0, column=0, sticky='ew')
        cscroll = ttk.Scrollbar(self.console_body, orient='vertical', command=self.console_text.yview)
        cscroll.grid(row=0, column=1, sticky='ns')
        self.console_text.configure(yscrollcommand=cscroll.set)

        statusbar = ttk.Frame(outer, style='Toolbar.TFrame')
        statusbar.grid(row=3, column=0, sticky='ew', pady=(5, 0))
        statusbar.columnconfigure(0, weight=1)
        ttk.Label(statusbar, textvariable=self.queue_status_var, style='Status.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(statusbar, textvariable=self.status_var, style='Status.TLabel').grid(row=0, column=1, sticky='e')

        self.bind('<Control-o>', lambda e: self.add_files())
        self.bind('<Control-Return>', lambda e: self.start_queue())
        self.bind('<Control-l>', lambda e: self._toggle_console())
        self.bind('<space>', self._space_play_pause)
        self._style_text_widgets(outer)
        self._apply_theme()
        self._append_console(f'{APP_NAME} v{APP_VERSION} ready. Preview player and manual GIF/WebP editing are available in the Preview tab.')
        self.after(350, self._init_vlc_player)

    def _build_preview_tab(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=3)
        tab.rowconfigure(1, weight=2)

        video_frame = ttk.Frame(tab, style='Panel.TFrame', padding=6)
        video_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 6))
        video_frame.columnconfigure(0, weight=1)
        video_frame.rowconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(video_frame, background=self.ui_colors['canvas'], highlightthickness=1, highlightbackground=self.ui_colors['border'])
        self.preview_canvas.grid(row=0, column=0, sticky='nsew')
        self.preview_canvas.bind('<Configure>', lambda e: self._attach_vlc_window())
        ttk.Label(video_frame, textvariable=self.preview_status_var, style='PanelMuted.TLabel').grid(row=1, column=0, sticky='w', pady=(4, 0))

        controls = ttk.Frame(video_frame, style='Panel.TFrame')
        controls.grid(row=2, column=0, sticky='ew', pady=(5, 0))
        controls.columnconfigure(5, weight=1)
        ttk.Button(controls, text='Play / Pause', command=self._preview_play_pause).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(controls, text='Stop', command=self._preview_stop).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(controls, text='◀ Frame', command=lambda: self._preview_frame_step(-1)).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(controls, text='Frame ▶', command=lambda: self._preview_frame_step(1)).grid(row=0, column=3, padx=(0, 8))
        ttk.Label(controls, textvariable=self.preview_time_var, style='Panel.TLabel', width=23).grid(row=0, column=4, padx=(0, 8))
        self.preview_seek = ttk.Scale(controls, from_=0, to=1000, variable=self.preview_seek_var, command=self._preview_seek_dragged)
        self.preview_seek.grid(row=0, column=5, sticky='ew', padx=(0, 8))
        self.preview_seek.bind('<ButtonPress-1>', lambda e: setattr(self, '_preview_user_seeking', True))
        self.preview_seek.bind('<ButtonRelease-1>', self._preview_seek_released)
        ttk.Label(controls, text='Vol', style='Panel.TLabel').grid(row=0, column=6)
        vol = ttk.Scale(controls, from_=0, to=100, variable=self.preview_volume_var, command=self._preview_volume_changed)
        vol.grid(row=0, column=7, sticky='ew', padx=(4, 0))

        lower = ttk.Notebook(tab)
        lower.grid(row=1, column=0, sticky='nsew')
        gallery_tab = ttk.Frame(lower, padding=7)
        manual_tab = ttk.Frame(lower, padding=7)
        info_tab = ttk.Frame(lower, padding=7)
        lower.add(gallery_tab, text='Generated Media')
        lower.add(manual_tab, text='Manual GIF / WebP')
        lower.add(info_tab, text='Video Info & Tags')
        self._build_gallery_panel(gallery_tab)
        self._build_manual_preview_panel(manual_tab)
        self._build_media_tab(info_tab)

    def _build_gallery_panel(self, tab):
        tab.columnconfigure(0, weight=2)
        tab.columnconfigure(1, weight=3)
        tab.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 5))
        ttk.Button(toolbar, text='Refresh media', command=self.refresh_preview_gallery).pack(side='left')
        ttk.Button(toolbar, text='Open selected', command=self._open_gallery_selected).pack(side='left', padx=(5, 0))
        ttk.Button(toolbar, text='Open output folder', command=self._open_gallery_folder).pack(side='left', padx=(5, 0))
        ttk.Button(toolbar, text='Replace selected screenshot with current frame', command=self._replace_selected_screenshot).pack(side='left', padx=(12, 0))

        list_frame = ttk.Frame(tab)
        list_frame.grid(row=1, column=0, sticky='nsew', padx=(0, 6))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.gallery_tree = ttk.Treeview(list_frame, columns=('type', 'name', 'size'), show='headings', selectmode='browse')
        self.gallery_tree.heading('type', text='Type')
        self.gallery_tree.heading('name', text='File')
        self.gallery_tree.heading('size', text='Size')
        self.gallery_tree.column('type', width=95, stretch=False)
        self.gallery_tree.column('name', width=330, stretch=True)
        self.gallery_tree.column('size', width=85, stretch=False, anchor='e')
        self.gallery_tree.grid(row=0, column=0, sticky='nsew')
        gscroll = ttk.Scrollbar(list_frame, orient='vertical', command=self.gallery_tree.yview)
        gscroll.grid(row=0, column=1, sticky='ns')
        self.gallery_tree.configure(yscrollcommand=gscroll.set)
        self.gallery_tree.bind('<<TreeviewSelect>>', self._gallery_selected)
        self.gallery_tree.bind('<Double-1>', lambda e: self._open_gallery_selected())

        image_frame = ttk.Frame(tab, style='Panel.TFrame', padding=5)
        image_frame.grid(row=1, column=1, sticky='nsew')
        image_frame.columnconfigure(0, weight=1)
        image_frame.rowconfigure(0, weight=1)
        self.gallery_image_label = tk.Label(image_frame, text='Generated screenshots, contact sheet, GIF and WebP will appear here.',
                                            background=self.ui_colors['canvas'], foreground=self.ui_colors['canvas_text'], compound='top')
        self.gallery_image_label.grid(row=0, column=0, sticky='nsew')
        ttk.Label(image_frame, textvariable=self.preview_selected_media_var, style='PanelMuted.TLabel', wraplength=550).grid(row=1, column=0, sticky='w', pady=(5, 0))

    def _build_manual_preview_panel(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        settings = ttk.Frame(tab)
        settings.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        ttk.Checkbutton(settings, text='Create animated preview when Everything is selected', variable=self.make_gif_var).pack(side='left')
        ttk.Label(settings, text='Mode').pack(side='left', padx=(16, 4))
        mode = ttk.Combobox(settings, textvariable=self.preview_mode_var, values=('Automatic', 'Manual'), state='readonly', width=12)
        mode.pack(side='left')
        mode.bind('<<ComboboxSelected>>', lambda e: self._preview_mode_changed())
        ttk.Label(settings, text='Format').pack(side='left', padx=(16, 4))
        ttk.Combobox(settings, textvariable=self.preview_format_var, values=PREVIEW_FORMATS, state='readonly', width=14).pack(side='left')

        auto = ttk.LabelFrame(tab, text='Encoding settings', padding=7)
        auto.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        common_values = [('Max MB', self.gif_max_mb_var, 8), ('Auto from %', self.gif_start_pct_var, 8), ('Auto to %', self.gif_end_pct_var, 8),
                         ('Auto clips', self.gif_clips_var, 8), ('Sec/clip', self.gif_seconds_var, 8)]
        for i, (label, var, width) in enumerate(common_values):
            ttk.Label(auto, text=label).grid(row=0, column=i*2, sticky='w', padx=(0 if i == 0 else 8, 3))
            ttk.Entry(auto, textvariable=var, width=width).grid(row=0, column=i*2+1, sticky='w')
        ttk.Label(auto, text='GIF').grid(row=1, column=0, sticky='w', pady=(7, 0))
        ttk.Label(auto, text='Width').grid(row=1, column=1, sticky='e', padx=(4, 3), pady=(7, 0))
        ttk.Entry(auto, textvariable=self.gif_width_var, width=8).grid(row=1, column=2, sticky='w', pady=(7, 0))
        ttk.Label(auto, text='FPS').grid(row=1, column=3, sticky='e', padx=(8, 3), pady=(7, 0))
        ttk.Entry(auto, textvariable=self.gif_fps_var, width=8).grid(row=1, column=4, sticky='w', pady=(7, 0))
        ttk.Label(auto, text='WebP').grid(row=2, column=0, sticky='w', pady=(5, 0))
        ttk.Label(auto, text='Width').grid(row=2, column=1, sticky='e', padx=(4, 3), pady=(5, 0))
        ttk.Entry(auto, textvariable=self.webp_width_var, width=8).grid(row=2, column=2, sticky='w', pady=(5, 0))
        ttk.Label(auto, text='FPS').grid(row=2, column=3, sticky='e', padx=(8, 3), pady=(5, 0))
        ttk.Entry(auto, textvariable=self.webp_fps_var, width=8).grid(row=2, column=4, sticky='w', pady=(5, 0))
        ttk.Label(auto, text='Quality').grid(row=2, column=5, sticky='e', padx=(8, 3), pady=(5, 0))
        ttk.Entry(auto, textvariable=self.webp_quality_var, width=8).grid(row=2, column=6, sticky='w', pady=(5, 0))
        ttk.Label(auto, text='WebP defaults to 720 px / 12 FPS / quality 90; GIF remains 540 px / 8 FPS.', style='Muted.TLabel').grid(row=3, column=0, columnspan=10, sticky='w', pady=(6, 0))

        select = ttk.LabelFrame(tab, text='Manual clip selection – use the video player above', padding=7)
        select.grid(row=2, column=0, sticky='ew', pady=(0, 6))
        ttk.Label(select, text='Start').grid(row=0, column=0)
        ttk.Entry(select, textvariable=self.manual_start_var, width=14).grid(row=0, column=1, padx=(4, 4))
        ttk.Button(select, text='Set start', command=self._manual_set_start).grid(row=0, column=2, padx=(0, 10))
        ttk.Label(select, text='End').grid(row=0, column=3)
        ttk.Entry(select, textvariable=self.manual_end_var, width=14).grid(row=0, column=4, padx=(4, 4))
        ttk.Button(select, text='Set end', command=self._manual_set_end).grid(row=0, column=5, padx=(0, 10))
        ttk.Button(select, text='Add clip', command=self._manual_add_clip, style='Accent.TButton').grid(row=0, column=6)
        ttk.Label(select, text='Manual mode preserves the exact selected ranges; compression reduces image quality before changing timing.', style='Muted.TLabel').grid(row=1, column=0, columnspan=7, sticky='w', pady=(5, 0))

        clips_frame = ttk.Frame(tab)
        clips_frame.grid(row=3, column=0, sticky='nsew')
        clips_frame.columnconfigure(0, weight=1)
        clips_frame.rowconfigure(0, weight=1)
        self.manual_clip_tree = ttk.Treeview(clips_frame, columns=('index', 'start', 'end', 'duration'), show='headings', selectmode='browse')
        for col, text, width in [('index', '#', 45), ('start', 'Start', 130), ('end', 'End', 130), ('duration', 'Duration', 100)]:
            self.manual_clip_tree.heading(col, text=text)
            self.manual_clip_tree.column(col, width=width, stretch=(col == 'duration'))
        self.manual_clip_tree.grid(row=0, column=0, sticky='nsew')
        cs = ttk.Scrollbar(clips_frame, orient='vertical', command=self.manual_clip_tree.yview)
        cs.grid(row=0, column=1, sticky='ns')
        self.manual_clip_tree.configure(yscrollcommand=cs.set)
        actions = ttk.Frame(clips_frame)
        actions.grid(row=0, column=2, sticky='ns', padx=(6, 0))
        ttk.Button(actions, text='Remove', command=self._manual_remove_clip).pack(fill='x')
        ttk.Button(actions, text='Move up', command=lambda: self._manual_move_clip(-1)).pack(fill='x', pady=(4, 0))
        ttk.Button(actions, text='Move down', command=lambda: self._manual_move_clip(1)).pack(fill='x', pady=(4, 0))
        ttk.Button(actions, text='Clear', command=self._manual_clear_clips).pack(fill='x', pady=(12, 0))

    def _toggle_console(self):
        self.console_collapsed = not self.console_collapsed
        if self.console_collapsed:
            self.console_body.grid_remove()
            self.console_toggle_button.configure(text='Expand')
        else:
            self.console_body.grid()
            self.console_toggle_button.configure(text='Collapse')

    def _load_settings(self):
        super()._load_settings()
        path = app_settings_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                theme = data.get('theme', 'System')
                mode = data.get('preview_mode', 'Automatic')
                self.theme_var.set(theme if theme in ('System', 'Dark', 'Light') else 'System')
                self.preview_mode_var.set(mode if mode in ('Automatic', 'Manual') else 'Automatic')
            except Exception:
                pass
        self._apply_theme()
        self._preview_mode_changed()

    def _snapshot_settings(self):
        data = super()._snapshot_settings()
        data['theme'] = self.theme_var.get()
        data['preview_mode'] = self.preview_mode_var.get()
        return data

    def validate_settings(self):
        super().validate_settings()
        if self.preview_mode_var.get() not in ('Automatic', 'Manual'):
            raise ValueError('Invalid preview selection mode.')
        makes_preview = self.process_mode_var.get() == 'Animated preview only' or (self.process_mode_var.get() == 'Everything' and self.make_gif_var.get())
        if makes_preview and self.preview_mode_var.get() == 'Manual':
            missing = [Path(j['source']).name for j in self.jobs if not _normalize_manual_preview_clips(j.get('manual_clips'), 10**9)]
            if missing:
                shown = ', '.join(missing[:4]) + ('…' if len(missing) > 4 else '')
                raise ValueError(f'Manual preview mode is selected, but these jobs have no manual clips: {shown}')

    def reset_defaults(self):
        super().reset_defaults()
        self.theme_var.set('System')
        self.preview_mode_var.set('Automatic')
        self._apply_theme()
        self._preview_mode_changed()

    def _add_paths(self, paths):
        before = {j['id'] for j in self.jobs}
        super()._add_paths(paths)
        for job in self.jobs:
            if job['id'] not in before:
                job.setdefault('manual_clips', [])

    def _queue_selection_changed(self, event=None):
        super()._queue_selection_changed(event)
        self._preview_selected_job()

    def _current_selected_job(self):
        if not hasattr(self, 'queue_tree'):
            return None
        selection = self.queue_tree.selection()
        if not selection:
            return None
        return self._job_by_id(selection[0])

    def _preview_selected_job(self):
        job = self._current_selected_job()
        if not job:
            return
        job.setdefault('manual_clips', [])
        try:
            video = find_video_source(Path(job['source']))
            self._set_preview_media(video, autoplay=False)
            self.refresh_preview_gallery(source=Path(job['source']))
        except Exception as exc:
            self.preview_status_var.set(f'Preview unavailable: {friendly_error(exc)}')
        self._refresh_manual_clip_tree()

    def _quick_check_worker(self, job_id, source_text):
        try:
            source = Path(source_text)
            video = find_video_source(source)
            details = probe_video_details(video)
            job = self._job_by_id(job_id)
            if job is not None:
                job['fps_value'] = float(details.get('fps') or 0.0)
                job['duration_seconds'] = float(details.get('duration') or 0.0)
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

    def _init_vlc_player(self):
        vlc_module, status = _load_vlc_module()
        self._vlc_status = status
        if vlc_module is None:
            self.preview_status_var.set(f'Embedded player unavailable. {status}')
            self._append_console(f'Preview player: {status}')
            return
        try:
            self._vlc = vlc_module
            self._vlc_instance = vlc_module.Instance('--no-video-title-show', '--quiet')
            self._vlc_player = self._vlc_instance.media_player_new()
            self._vlc_player.audio_set_volume(int(self.preview_volume_var.get()))
            self._attach_vlc_window()
            self.preview_status_var.set(f'Embedded player ready • {status}')
            self._append_console(f'Preview player ready: {status}')
            if self._preview_media_path:
                self._set_preview_media(self._preview_media_path, autoplay=False)
            self.after(250, self._poll_preview_player)
        except Exception as exc:
            self._vlc_player = None
            self.preview_status_var.set(f'Embedded player could not start: {friendly_error(exc)}')
            self._append_console(f'Preview player failed: {friendly_error(exc)}')

    def _attach_vlc_window(self):
        if not self._vlc_player or not hasattr(self, 'preview_canvas'):
            return
        try:
            self.update_idletasks()
            handle = self.preview_canvas.winfo_id()
            if os.name == 'nt':
                self._vlc_player.set_hwnd(handle)
            elif sys.platform == 'darwin':
                self._vlc_player.set_nsobject(handle)
            else:
                self._vlc_player.set_xwindow(handle)
        except Exception:
            pass

    def _set_preview_media(self, video_path, autoplay=False):
        video_path = Path(video_path)
        self._preview_media_path = video_path
        job = self._current_selected_job()
        if job:
            self._preview_fps = float(job.get('fps_value') or 25.0)
            self._preview_duration = float(job.get('duration_seconds') or 0.0)
        self.preview_status_var.set(f'Preview: {video_path.name}' + (f' • {self._vlc_status}' if self._vlc_status else ''))
        if not self._vlc_player or not self._vlc_instance:
            return
        try:
            media = self._vlc_instance.media_new(str(video_path))
            self._vlc_player.set_media(media)
            self._attach_vlc_window()
            self._vlc_player.audio_set_volume(int(self.preview_volume_var.get()))
            if autoplay:
                self._vlc_player.play()
        except Exception as exc:
            self.preview_status_var.set(f'Could not load video preview: {friendly_error(exc)}')

    def _preview_play_pause(self):
        if not self._vlc_player:
            messagebox.showinfo(APP_NAME, 'The embedded VLC player is not available in this build/session.')
            return
        if self._preview_media_path is None:
            self._preview_selected_job()
        if self._vlc_player.is_playing():
            self._vlc_player.pause()
        else:
            self._attach_vlc_window()
            self._vlc_player.play()

    def _space_play_pause(self, event=None):
        widget = self.focus_get()
        if isinstance(widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox)):
            return None
        try:
            current_tab = self.notebook.tab(self.notebook.select(), 'text')
        except Exception:
            current_tab = ''
        if current_tab == 'Preview':
            self._preview_play_pause()
            return 'break'
        return None

    def _preview_stop(self):
        if self._vlc_player:
            self._vlc_player.stop()
            self.preview_seek_var.set(0)
            self.preview_time_var.set('00:00:00 / 00:00:00')

    def _preview_frame_step(self, direction):
        if not self._vlc_player:
            return
        try:
            self._vlc_player.pause()
            current = max(0, int(self._vlc_player.get_time()))
            fps = max(1.0, float(self._preview_fps or 25.0))
            target = max(0, current + int(direction * 1000.0 / fps))
            self._vlc_player.set_time(target)
        except Exception:
            pass

    def _preview_seek_dragged(self, _value=None):
        # Actual seek is committed on mouse release to avoid fighting VLC's timer.
        pass

    def _preview_seek_released(self, _event=None):
        self._preview_user_seeking = False
        if not self._vlc_player:
            return
        try:
            length = max(0, int(self._vlc_player.get_length()))
            if length > 0:
                self._vlc_player.set_time(int(length * float(self.preview_seek_var.get()) / 1000.0))
        except Exception:
            pass

    def _preview_volume_changed(self, _value=None):
        if self._vlc_player:
            try:
                self._vlc_player.audio_set_volume(int(float(self.preview_volume_var.get())))
            except Exception:
                pass

    def _poll_preview_player(self):
        try:
            if self._vlc_player:
                current = max(0, int(self._vlc_player.get_time()))
                length = max(0, int(self._vlc_player.get_length()))
                if length > 0:
                    self._preview_duration = length / 1000.0
                    if not self._preview_user_seeking:
                        self.preview_seek_var.set(1000.0 * current / length)
                    self.preview_time_var.set(f'{format_duration(current / 1000.0)} / {format_duration(length / 1000.0)}')
        except Exception:
            pass
        try:
            self.after(250, self._poll_preview_player)
        except tk.TclError:
            pass

    def _current_preview_seconds(self):
        if self._vlc_player:
            try:
                return max(0.0, self._vlc_player.get_time() / 1000.0)
            except Exception:
                pass
        return 0.0

    def _manual_set_start(self):
        self.manual_start_var.set(_format_clock_precise(self._current_preview_seconds()))

    def _manual_set_end(self):
        self.manual_end_var.set(_format_clock_precise(self._current_preview_seconds()))

    def _manual_add_clip(self):
        job = self._current_selected_job()
        if not job:
            messagebox.showinfo(APP_NAME, 'Select a queued video first.')
            return
        try:
            start = _parse_clock_text(self.manual_start_var.get())
            end = _parse_clock_text(self.manual_end_var.get())
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))
            return
        if end <= start:
            messagebox.showerror(APP_NAME, 'Clip end must be after clip start.')
            return
        if end - start > 15:
            if not messagebox.askyesno(APP_NAME, 'This manual clip is longer than 15 seconds and may make a 7 MB preview difficult. Add it anyway?'):
                return
        clips = job.setdefault('manual_clips', [])
        if len(clips) >= 16:
            messagebox.showerror(APP_NAME, 'A maximum of 16 manual clips is supported per video.')
            return
        clips.append({'start': float(start), 'end': float(end)})
        self.preview_mode_var.set('Manual')
        self._refresh_manual_clip_tree()
        self.results_summary_var.set(f'Manual preview: {len(clips)} selected clip(s), {sum(c["end"]-c["start"] for c in clips):.1f}s total')

    def _manual_remove_clip(self):
        job = self._current_selected_job()
        sel = self.manual_clip_tree.selection() if hasattr(self, 'manual_clip_tree') else ()
        if not job or not sel:
            return
        try:
            idx = int(sel[0])
            job.setdefault('manual_clips', []).pop(idx)
        except Exception:
            return
        self._refresh_manual_clip_tree()

    def _manual_move_clip(self, direction):
        job = self._current_selected_job()
        sel = self.manual_clip_tree.selection() if hasattr(self, 'manual_clip_tree') else ()
        if not job or not sel:
            return
        clips = job.setdefault('manual_clips', [])
        idx = int(sel[0])
        new_idx = max(0, min(len(clips)-1, idx + direction))
        if new_idx == idx:
            return
        item = clips.pop(idx)
        clips.insert(new_idx, item)
        self._refresh_manual_clip_tree(select_index=new_idx)

    def _manual_clear_clips(self):
        job = self._current_selected_job()
        if job:
            job['manual_clips'] = []
            self._refresh_manual_clip_tree()

    def _refresh_manual_clip_tree(self, select_index=None):
        if not hasattr(self, 'manual_clip_tree'):
            return
        self.manual_clip_tree.delete(*self.manual_clip_tree.get_children())
        job = self._current_selected_job()
        clips = job.setdefault('manual_clips', []) if job else []
        for idx, clip in enumerate(clips):
            start, end = float(clip['start']), float(clip['end'])
            self.manual_clip_tree.insert('', 'end', iid=str(idx), values=(idx + 1, _format_clock_precise(start), _format_clock_precise(end), f'{end-start:.3f}s'))
        if select_index is not None and str(select_index) in self.manual_clip_tree.get_children():
            self.manual_clip_tree.selection_set(str(select_index))

    def _preview_mode_changed(self):
        # The controls remain visible so switching modes never hides existing clip work.
        mode = self.preview_mode_var.get()
        if hasattr(self, 'results_summary_var') and mode == 'Manual':
            job = self._current_selected_job()
            count = len(job.get('manual_clips', [])) if job else 0
            self.results_summary_var.set(f'Manual preview mode • {count} selected clip(s) for the current job')

    def _gallery_output_dir(self, source):
        output = self.output_dir_var.get().strip()
        if not output:
            return None
        settings = {'output_dir': output, 'subfolder_per_job': bool(self.subfolder_per_job_var.get())}
        return job_output_dir(Path(source), settings)

    def refresh_preview_gallery(self, source=None):
        if not hasattr(self, 'gallery_tree'):
            return
        if source is None:
            job = self._current_selected_job()
            if job:
                source = Path(job['source'])
            elif self._last_completed_source:
                source = Path(self._last_completed_source)
        if source is None:
            return
        try:
            video = find_video_source(Path(source))
        except Exception:
            video = Path(source) if Path(source).is_file() else None
        folder = self._gallery_output_dir(source)
        self.gallery_tree.delete(*self.gallery_tree.get_children())
        self._gallery_files = {}
        if folder is None or not folder.exists() or video is None:
            self.preview_selected_media_var.set('No generated media found for this video yet.')
            return
        patterns = [
            ('Screenshot', f'{video.stem}_screenshot_*.jpg'),
            ('Contact sheet', f'{video.stem}_layout_*x*.jpg'),
            ('GIF', f'{video.stem}_preview.gif'),
            ('WebP', f'{video.stem}_preview.webp'),
        ]
        counter = 0
        for media_type, pattern in patterns:
            for path in sorted(folder.glob(pattern)):
                counter += 1
                iid = str(counter)
                self._gallery_files[iid] = path
                self.gallery_tree.insert('', 'end', iid=iid, values=(media_type, path.name, format_bytes(path.stat().st_size)))
        if counter:
            first = self.gallery_tree.get_children()[0]
            self.gallery_tree.selection_set(first)
            self.gallery_tree.focus(first)
            self._gallery_selected()
        else:
            self.preview_selected_media_var.set(f'No generated media found in {folder}')

    def _gallery_selected(self, _event=None):
        sel = self.gallery_tree.selection() if hasattr(self, 'gallery_tree') else ()
        if not sel:
            return
        path = self._gallery_files.get(sel[0])
        if not path:
            return
        self.preview_selected_media_var.set(f'{path.name} • {format_bytes(path.stat().st_size)} • {path}')
        if Image is None or ImageTk is None:
            return
        try:
            with Image.open(path) as im:
                frame = im.convert('RGB')
                max_w = max(320, self.gallery_image_label.winfo_width() - 20)
                max_h = max(220, self.gallery_image_label.winfo_height() - 35)
                frame.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(frame.copy())
            self._gallery_photo = photo
            self.gallery_image_label.configure(image=photo, text='')
        except Exception as exc:
            self._gallery_photo = None
            self.gallery_image_label.configure(image='', text=f'Preview unavailable\n{friendly_error(exc)}')

    def _open_gallery_selected(self):
        sel = self.gallery_tree.selection() if hasattr(self, 'gallery_tree') else ()
        if not sel:
            return
        path = self._gallery_files.get(sel[0])
        if not path:
            return
        try:
            if os.name == 'nt':
                os.startfile(str(path))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def _open_gallery_folder(self):
        job = self._current_selected_job()
        source = Path(job['source']) if job else (Path(self._last_completed_source) if self._last_completed_source else None)
        if not source:
            return
        folder = self._gallery_output_dir(source)
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
            open_folder(folder)

    def _replace_selected_screenshot(self):
        sel = self.gallery_tree.selection() if hasattr(self, 'gallery_tree') else ()
        if not sel:
            messagebox.showinfo(APP_NAME, 'Select one of the separate screenshots first.')
            return
        path = self._gallery_files.get(sel[0])
        if not path or '_screenshot_' not in path.name.lower() or path.suffix.lower() not in ('.jpg', '.jpeg'):
            messagebox.showinfo(APP_NAME, 'Select a separate screenshot, not the contact sheet/GIF/WebP.')
            return
        if not self._preview_media_path:
            messagebox.showinfo(APP_NAME, 'Load a video in the Preview player first.')
            return
        timestamp = self._current_preview_seconds()
        try:
            with tempfile.TemporaryDirectory(prefix='torrentcreator_frame_') as temp:
                png = Path(temp) / 'frame.png'
                extract_frame(self._preview_media_path, timestamp, png)
                if Image is None:
                    raise RuntimeError('Pillow is required to replace JPEG screenshots.')
                with Image.open(png) as im:
                    rgb = im.convert('RGB')
                    rgb.save(path, 'JPEG', quality=int(self.jpeg_quality_var.get()), optimize=True, exif=b'')
            self._append_console(f'Replaced {path.name} with frame at {_format_clock_precise(timestamp)}')
            self.refresh_preview_gallery()
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def _job_completed_hook(self, job, detail):
        source = Path(job['source'])
        self._last_completed_source = str(source)
        marks = []
        lower = detail.lower()
        if 'torrent' in lower: marks.append('Torrent ✓')
        if 'screenshot' in lower or 'contact sheet' in lower: marks.append('Screenshots ✓')
        if 'gif' in lower: marks.append('GIF ✓')
        if 'webp' in lower: marks.append('WebP ✓')
        if 'hamsterimg' in lower: marks.append('HamsterImg ✓')
        if 'verified' in lower: marks.append('Verified ✓')
        self.results_summary_var.set('Results: ' + ('   '.join(marks) if marks else 'Completed ✓'))
        try:
            video = find_video_source(source)
            self._set_preview_media(video, autoplay=False)
        except Exception:
            pass
        self.refresh_preview_gallery(source=source)

    def _on_close(self):
        try:
            if self._vlc_player:
                self._vlc_player.stop()
                self._vlc_player.release()
            if self._vlc_instance:
                self._vlc_instance.release()
        except Exception:
            pass
        super()._on_close()


class TorrentCreatorAppV311(TorrentCreatorAppV31):
    """v3.1.1: always-visible generated media gallery and pre-processing playback."""

    def _ensure_v31_vars(self):
        super()._ensure_v31_vars()
        if not hasattr(self, '_gallery_thumb_photos'):
            self._gallery_thumb_photos = []
            self._gallery_thumb_labels = []
            self._gallery_selected_path = None
            self._gallery_source = None
            self._pending_preview_autoplay = False

    def _apply_theme(self, _event=None):
        super()._apply_theme(_event)
        c = getattr(self, 'ui_colors', self._theme_palette())
        for widget in getattr(self, '_gallery_thumb_labels', []):
            try:
                widget.configure(background=c['canvas'], foreground=c['canvas_text'])
            except Exception:
                pass
        if hasattr(self, 'gallery_canvas'):
            try:
                self.gallery_canvas.configure(background=c['panel'], highlightbackground=c['border'])
            except Exception:
                pass

    def _build_preview_tab(self, tab):
        self.preview_tab = tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=3)
        tab.rowconfigure(1, weight=2)

        video_frame = ttk.Frame(tab, style='Panel.TFrame', padding=6)
        video_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 6))
        video_frame.columnconfigure(0, weight=1)
        video_frame.rowconfigure(1, weight=1)

        preview_head = ttk.Frame(video_frame, style='Panel.TFrame')
        preview_head.grid(row=0, column=0, sticky='ew', pady=(0, 5))
        preview_head.columnconfigure(1, weight=1)
        ttk.Label(preview_head, text='Video preview', style='Panel.TLabel', font=('Segoe UI Semibold', 10)).grid(row=0, column=0, sticky='w')
        ttk.Label(preview_head, text='You can play queued videos before starting any processing.', style='PanelMuted.TLabel').grid(row=0, column=1, sticky='w', padx=(10, 8))
        ttk.Button(preview_head, text='Load selected', command=self._preview_selected_job).grid(row=0, column=2, padx=(0, 5))
        ttk.Button(preview_head, text='Play selected', command=self._play_selected_before_processing, style='Accent.TButton').grid(row=0, column=3)

        self.preview_canvas = tk.Canvas(video_frame, background=self.ui_colors['canvas'], highlightthickness=1, highlightbackground=self.ui_colors['border'])
        self.preview_canvas.grid(row=1, column=0, sticky='nsew')
        self.preview_canvas.bind('<Configure>', lambda e: self._attach_vlc_window())
        ttk.Label(video_frame, textvariable=self.preview_status_var, style='PanelMuted.TLabel').grid(row=2, column=0, sticky='w', pady=(4, 0))

        controls = ttk.Frame(video_frame, style='Panel.TFrame')
        controls.grid(row=3, column=0, sticky='ew', pady=(5, 0))
        controls.columnconfigure(5, weight=1)
        ttk.Button(controls, text='Play / Pause', command=self._preview_play_pause).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(controls, text='Stop', command=self._preview_stop).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(controls, text='◀ Frame', command=lambda: self._preview_frame_step(-1)).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(controls, text='Frame ▶', command=lambda: self._preview_frame_step(1)).grid(row=0, column=3, padx=(0, 8))
        ttk.Label(controls, textvariable=self.preview_time_var, style='Panel.TLabel', width=23).grid(row=0, column=4, padx=(0, 8))
        self.preview_seek = ttk.Scale(controls, from_=0, to=1000, variable=self.preview_seek_var, command=self._preview_seek_dragged)
        self.preview_seek.grid(row=0, column=5, sticky='ew', padx=(0, 8))
        self.preview_seek.bind('<ButtonPress-1>', lambda e: setattr(self, '_preview_user_seeking', True))
        self.preview_seek.bind('<ButtonRelease-1>', self._preview_seek_released)
        ttk.Label(controls, text='Vol', style='Panel.TLabel').grid(row=0, column=6)
        vol = ttk.Scale(controls, from_=0, to=100, variable=self.preview_volume_var, command=self._preview_volume_changed)
        vol.grid(row=0, column=7, sticky='ew', padx=(4, 0))

        lower = ttk.Notebook(tab)
        lower.grid(row=1, column=0, sticky='nsew')
        self.preview_lower_notebook = lower
        gallery_tab = ttk.Frame(lower, padding=7)
        manual_tab = ttk.Frame(lower, padding=7)
        info_tab = ttk.Frame(lower, padding=7)
        lower.add(gallery_tab, text='All Generated Media')
        lower.add(manual_tab, text='Manual GIF / WebP')
        lower.add(info_tab, text='Video Info & Tags')
        self.gallery_tab = gallery_tab
        self._build_gallery_panel(gallery_tab)
        self._build_manual_preview_panel(manual_tab)
        self._build_media_tab(info_tab)

    def _build_gallery_panel(self, tab):
        tab.columnconfigure(0, weight=3)
        tab.columnconfigure(1, weight=2)
        tab.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 5))
        self.gallery_count_var = tk.StringVar(value='Generated media: 0 files')
        ttk.Label(toolbar, textvariable=self.gallery_count_var, style='Muted.TLabel').pack(side='left')
        ttk.Button(toolbar, text='Refresh media', command=self.refresh_preview_gallery).pack(side='left', padx=(12, 0))
        ttk.Button(toolbar, text='Open selected', command=self._open_gallery_selected).pack(side='left', padx=(5, 0))
        ttk.Button(toolbar, text='Open output folder', command=self._open_gallery_folder).pack(side='left', padx=(5, 0))
        ttk.Button(toolbar, text='Replace selected screenshot with current frame', command=self._replace_selected_screenshot).pack(side='left', padx=(12, 0))

        # Scrollable thumbnail wall: every generated image is visible at once.
        wall = ttk.Frame(tab, style='Panel.TFrame', padding=5)
        wall.grid(row=1, column=0, sticky='nsew', padx=(0, 6))
        wall.columnconfigure(0, weight=1)
        wall.rowconfigure(0, weight=1)
        self.gallery_canvas = tk.Canvas(wall, background=self.ui_colors['panel'], highlightthickness=1, highlightbackground=self.ui_colors['border'])
        self.gallery_canvas.grid(row=0, column=0, sticky='nsew')
        gscroll = ttk.Scrollbar(wall, orient='vertical', command=self.gallery_canvas.yview)
        gscroll.grid(row=0, column=1, sticky='ns')
        self.gallery_canvas.configure(yscrollcommand=gscroll.set)
        self.gallery_inner = ttk.Frame(self.gallery_canvas, style='Panel.TFrame')
        self._gallery_window_id = self.gallery_canvas.create_window((0, 0), window=self.gallery_inner, anchor='nw')
        self.gallery_inner.bind('<Configure>', lambda e: self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox('all')))
        self.gallery_canvas.bind('<Configure>', self._gallery_canvas_resized)
        self.gallery_canvas.bind('<MouseWheel>', self._gallery_mousewheel)

        viewer = ttk.Frame(tab, style='Panel.TFrame', padding=5)
        viewer.grid(row=1, column=1, sticky='nsew')
        viewer.columnconfigure(0, weight=1)
        viewer.rowconfigure(1, weight=1)
        ttk.Label(viewer, text='Selected media', style='Panel.TLabel', font=('Segoe UI Semibold', 9)).grid(row=0, column=0, sticky='w', pady=(0, 4))
        self.gallery_image_label = tk.Label(viewer, text='All screenshots, collage, contact sheet and generated preview files will appear in the thumbnail wall after processing.',
                                            background=self.ui_colors['canvas'], foreground=self.ui_colors['canvas_text'], compound='top')
        self.gallery_image_label.grid(row=1, column=0, sticky='nsew')
        ttk.Label(viewer, textvariable=self.preview_selected_media_var, style='PanelMuted.TLabel', wraplength=500).grid(row=2, column=0, sticky='w', pady=(5, 0))

    def _gallery_canvas_resized(self, event):
        try:
            self.gallery_canvas.itemconfigure(self._gallery_window_id, width=max(100, event.width - 2))
        except Exception:
            pass

    def _gallery_mousewheel(self, event):
        try:
            self.gallery_canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        except Exception:
            pass
        return 'break'

    def _add_paths(self, paths):
        before = {j['id'] for j in self.jobs}
        super()._add_paths(paths)
        new_jobs = [j for j in self.jobs if j['id'] not in before]
        if new_jobs and hasattr(self, 'queue_tree'):
            iid = str(new_jobs[0]['id'])
            try:
                self.queue_tree.selection_set(iid)
                self.queue_tree.focus(iid)
                self.queue_tree.see(iid)
            except Exception:
                pass
            # Load immediately so playback is available before Start queue is pressed.
            self.after(80, self._preview_selected_job)

    def _play_selected_before_processing(self):
        job = self._current_selected_job()
        if not job:
            messagebox.showinfo(APP_NAME, 'Select a queued video first. No processing is required to preview it.')
            return
        try:
            video = find_video_source(Path(job['source']))
            if hasattr(self, 'notebook') and hasattr(self, 'preview_tab'):
                self.notebook.select(self.preview_tab)
            self._set_preview_media(video, autoplay=bool(self._vlc_player))
            if self._vlc_player:
                self.after(150, lambda: self._vlc_player.play())
            else:
                self._pending_preview_autoplay = True
                self.preview_status_var.set(f'Preview ready: {video.name} • waiting for VLC player initialization…')
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def _init_vlc_player(self):
        super()._init_vlc_player()
        if getattr(self, '_pending_preview_autoplay', False) and self._vlc_player and self._preview_media_path:
            self._pending_preview_autoplay = False
            self.after(150, lambda: self._vlc_player.play())

    def _collect_gallery_files(self, video, folder):
        """Collect generated media using literal filename matching.

        Path.glob() treats square brackets in names such as ``[OF]`` or
        ``[1080p]`` as wildcard character classes. TorrentCreator filenames
        intentionally contain those brackets, so gallery discovery must never
        feed the video stem into a glob pattern.
        """
        files = []
        folder = Path(folder) if folder else None
        video = Path(video)
        if not folder or not folder.exists():
            return files

        try:
            entries = [p for p in folder.iterdir() if p.is_file()]
        except OSError:
            return files

        stem = video.stem
        stem_cf = stem.casefold()
        allowed = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}

        def add_matching(media_type, predicate):
            for path in sorted(entries, key=lambda p: p.name.casefold()):
                if path.suffix.lower() not in allowed:
                    continue
                if predicate(path.name.casefold(), path.suffix.lower()):
                    files.append((media_type, path))

        screenshot_prefix = f'{stem_cf}_screenshot_'
        layout_prefix = f'{stem_cf}_layout_'
        add_matching('Screenshot', lambda name, suffix: name.startswith(screenshot_prefix) and suffix in ('.jpg', '.jpeg', '.png'))
        exact_collage = f'{stem_cf}_collage.jpg'
        add_matching('Collage', lambda name, suffix: name == exact_collage)
        add_matching('Contact sheet', lambda name, suffix: name.startswith(layout_prefix) and suffix in ('.jpg', '.jpeg', '.png'))

        exact_gif = f'{stem_cf}_preview.gif'
        exact_webp = f'{stem_cf}_preview.webp'
        add_matching('GIF', lambda name, suffix: name == exact_gif)
        add_matching('WebP', lambda name, suffix: name == exact_webp)

        # De-duplicate defensively while preserving display order.
        unique = []
        seen = set()
        for media_type, path in files:
            try:
                key = str(path.resolve()).casefold() if os.name == 'nt' else str(path.resolve())
            except Exception:
                key = str(path).casefold() if os.name == 'nt' else str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append((media_type, path))
        return unique

    def _make_gallery_thumb(self, path, max_size=(220, 130)):
        if Image is None or ImageTk is None:
            return None
        with Image.open(path) as im:
            try:
                im.seek(0)
            except Exception:
                pass
            frame = im.convert('RGB')
            frame.thumbnail(max_size, Image.Resampling.LANCZOS)
            canvas = Image.new('RGB', max_size, (18, 20, 23))
            x = (max_size[0] - frame.width) // 2
            y = (max_size[1] - frame.height) // 2
            canvas.paste(frame, (x, y))
            return ImageTk.PhotoImage(canvas)

    def refresh_preview_gallery(self, source=None):
        if not hasattr(self, 'gallery_inner'):
            return
        if source is None:
            job = self._current_selected_job()
            if job:
                source = Path(job['source'])
            elif self._last_completed_source:
                source = Path(self._last_completed_source)
        if source is None:
            return
        source = Path(source)
        self._gallery_source = source
        try:
            video = find_video_source(source)
        except Exception:
            video = source if source.is_file() else None
        folder = self._gallery_output_dir(source)

        for child in self.gallery_inner.winfo_children():
            child.destroy()
        self._gallery_files = {}
        self._gallery_thumb_photos = []
        self._gallery_thumb_labels = []
        self._gallery_selected_path = None
        self._gallery_photo = None

        if folder is None or video is None:
            self.gallery_count_var.set('Generated media: 0 files')
            self.preview_selected_media_var.set('No generated media found for this video yet.')
            self.gallery_image_label.configure(image='', text='Generated media will appear here after processing.')
            return

        media_files = self._collect_gallery_files(video, folder)
        self.gallery_count_var.set(f'Generated media: {len(media_files)} file(s) • {folder}')
        if not media_files:
            ttk.Label(self.gallery_inner, text='No generated media found yet. You can still play the selected video above before processing.', style='PanelMuted.TLabel').grid(row=0, column=0, sticky='w', padx=8, pady=8)
            self.preview_selected_media_var.set(f'No generated media found in {folder}')
            self.gallery_image_label.configure(image='', text='No images yet. Run Screenshots or Everything, then this panel refreshes automatically.')
            return

        # Three-column thumbnail wall. All outputs are visible without selecting rows first.
        columns = 3
        for idx, (media_type, path) in enumerate(media_files):
            key = str(idx)
            self._gallery_files[key] = path
            card = ttk.Frame(self.gallery_inner, style='Panel.TFrame', padding=5)
            card.grid(row=idx // columns, column=idx % columns, sticky='nsew', padx=3, pady=3)
            self.gallery_inner.columnconfigure(idx % columns, weight=1, uniform='gallerycol')
            try:
                photo = self._make_gallery_thumb(path)
            except Exception:
                photo = None
            if photo is not None:
                self._gallery_thumb_photos.append(photo)
                thumb = tk.Label(card, image=photo, background=self.ui_colors['canvas'], cursor='hand2')
            else:
                thumb = tk.Label(card, text='Preview unavailable', width=28, height=7,
                                 background=self.ui_colors['canvas'], foreground=self.ui_colors['canvas_text'], cursor='hand2')
            thumb.pack(fill='both', expand=True)
            self._gallery_thumb_labels.append(thumb)
            caption = ttk.Label(card, text=f'{media_type}\n{path.name}\n{format_bytes(path.stat().st_size)}', style='PanelMuted.TLabel', justify='center', anchor='center', wraplength=215)
            caption.pack(fill='x', pady=(4, 0))
            for widget in (card, thumb, caption):
                widget.bind('<Button-1>', lambda e, p=path: self._select_gallery_path(p))
                widget.bind('<Double-1>', lambda e, p=path: self._open_gallery_path(p))

        first_path = media_files[0][1]
        self._select_gallery_path(first_path)
        try:
            self.gallery_canvas.yview_moveto(0)
        except Exception:
            pass

    def _select_gallery_path(self, path):
        path = Path(path)
        self._gallery_selected_path = path
        self.preview_selected_media_var.set(f'{path.name} • {format_bytes(path.stat().st_size)} • {path}')
        if Image is None or ImageTk is None:
            self.gallery_image_label.configure(image='', text=path.name)
            return
        try:
            with Image.open(path) as im:
                try:
                    im.seek(0)
                except Exception:
                    pass
                frame = im.convert('RGB')
                max_w = max(360, self.gallery_image_label.winfo_width() - 20)
                max_h = max(260, self.gallery_image_label.winfo_height() - 30)
                frame.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(frame.copy())
            self._gallery_photo = photo
            self.gallery_image_label.configure(image=photo, text='')
        except Exception as exc:
            self._gallery_photo = None
            self.gallery_image_label.configure(image='', text=f'Preview unavailable\n{friendly_error(exc)}')

    def _open_gallery_path(self, path):
        path = Path(path)
        try:
            if os.name == 'nt':
                os.startfile(str(path))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def _open_gallery_selected(self):
        if self._gallery_selected_path:
            self._open_gallery_path(self._gallery_selected_path)

    def _replace_selected_screenshot(self):
        path = Path(self._gallery_selected_path) if self._gallery_selected_path else None
        if not path:
            messagebox.showinfo(APP_NAME, 'Select one of the separate screenshots first.')
            return
        if '_screenshot_' not in path.name.lower() or path.suffix.lower() not in ('.jpg', '.jpeg'):
            messagebox.showinfo(APP_NAME, 'Select a separate screenshot, not the contact sheet/GIF/WebP.')
            return
        if not self._preview_media_path:
            messagebox.showinfo(APP_NAME, 'Load a video in the Preview player first.')
            return
        timestamp = self._current_preview_seconds()
        try:
            with tempfile.TemporaryDirectory(prefix='torrentcreator_frame_') as temp:
                png = Path(temp) / 'frame.png'
                extract_frame(self._preview_media_path, timestamp, png)
                if Image is None:
                    raise RuntimeError('Pillow is required to replace JPEG screenshots.')
                with Image.open(png) as im:
                    rgb = im.convert('RGB')
                    rgb.save(path, 'JPEG', quality=int(self.jpeg_quality_var.get()), optimize=True, exif=b'')
            self._append_console(f'Replaced {path.name} with frame at {_format_clock_precise(timestamp)}')
            self.refresh_preview_gallery(source=self._gallery_source)
            self._select_gallery_path(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def _job_completed_hook(self, job, detail):
        super()._job_completed_hook(job, detail)
        # Keep all freshly-created media visible after the successful job is removed from the queue.
        source = Path(job['source'])
        self.refresh_preview_gallery(source=source)
        if hasattr(self, 'notebook') and hasattr(self, 'preview_tab'):
            try:
                self.notebook.select(self.preview_tab)
                if hasattr(self, 'preview_lower_notebook') and hasattr(self, 'gallery_tab'):
                    self.preview_lower_notebook.select(self.gallery_tab)
            except Exception:
                pass



# -----------------------------------------------------------------------------
# TorrentCreator v3.3 - optional filename renaming + update workflow
# -----------------------------------------------------------------------------

_RENAME_RESOLUTIONS = ('720p', '1080p', '2160p', '1920p', '3840p')
_WINDOWS_INVALID_NAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
_YEAR_RE = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')


def _clean_name_for_rename(value):
    """Clean the Name field while preserving normal spaces."""
    value = str(value or '').strip()
    value = re.sub(r'^\[?OF(?:-[^\]]+)?\]?\s*', '', value, flags=re.I)
    value = re.sub(r'\++', ' ', value)
    value = re.sub(r'[._]+', ' ', value)
    value = _WINDOWS_INVALID_NAME_CHARS_RE.sub('', value)
    value = re.sub(r'\s+', ' ', value).strip(' .-')
    return value


def _clean_performer_name(value):
    """Backward-compatible alias; v3.3 treats this field as Name and preserves spaces."""
    return _clean_name_for_rename(value)


def _clean_title_for_rename(value):
    value = str(value or '').strip()
    value = re.sub(r'\++', ' ', value)
    value = re.sub(r'[._]+', ' ', value)
    value = _WINDOWS_INVALID_NAME_CHARS_RE.sub('', value)
    value = re.sub(r'\s+', ' ', value).strip(' .-')
    return value


def _clean_year_for_rename(value):
    value = str(value or '').strip()
    if not value:
        return ''
    match = _YEAR_RE.search(value)
    if not match:
        raise ValueError('Year must be a four-digit year between 1900 and 2099, or left blank.')
    return match.group(1)


def _extract_year(value):
    """Extract a year when present and remove it from the remaining text."""
    text = str(value or '')
    matches = list(_YEAR_RE.finditer(text))
    if not matches:
        return text, ''
    # Filename years normally appear toward the end. Use the last one.
    match = matches[-1]
    year = match.group(1)
    text = (text[:match.start()] + ' ' + text[match.end():]).strip()
    text = re.sub(r'\s+', ' ', text).strip(' .-_+-')
    return text, year


def _parse_rename_parts(path):
    """Best-effort parser for Name + Title + optional Year + automatic Resolution."""
    stem = Path(path).stem.strip()
    stem = re.sub(r'\++', ' ', stem)

    # New v3.3 target format. The Name/Title boundary cannot always be recovered
    # perfectly from a space-only final filename, so use the same conservative
    # name heuristic as ordinary filenames.
    new_existing = re.match(r'^\[OF\]\s*(.*?)\s*\[(720p|1080p|2160p|1920p|3840p)\]$', stem, re.I)
    existing_res = ''
    if new_existing:
        stem = new_existing.group(1).strip()
        existing_res = new_existing.group(2).lower()

    # Migration support for v3.2 names: [OF-Name] Title [1080p]
    old_existing = re.match(r'^\[OF-([^\]]+)\]\s*(.*?)\s*\[(720p|1080p|2160p|1920p|3840p)\]$', stem, re.I)
    if old_existing:
        remaining, year = _extract_year(old_existing.group(2))
        return _clean_name_for_rename(old_existing.group(1)), _clean_title_for_rename(remaining), year, ''

    # Remove a trailing/standalone resolution token if the source filename already has one.
    if not existing_res:
        res_match = re.search(r'(?:\[|\b)(720p|1080p|2160p|1920p|3840p)(?:\]|\b)', stem, re.I)
        if res_match:
            existing_res = res_match.group(1).lower()
            stem = (stem[:res_match.start()] + ' ' + stem[res_match.end():]).strip()

    stem, year = _extract_year(stem)

    # Prefer visible separators between Name and Title.
    parts = None
    for pattern in (r'\s+-\s+', r'\s+–\s+', r'\s+—\s+', r'\s+\|\s+', r'__+'):
        candidate = [x.strip() for x in re.split(pattern, stem) if x.strip()]
        if len(candidate) >= 2:
            parts = candidate
            break

    if parts is not None:
        name = parts[0]
        title = ' '.join(parts[1:])
    else:
        clean = _clean_title_for_rename(stem)
        words = clean.split()
        if len(words) >= 4:
            # Common downloaded-video form after '+' normalization:
            # First Second Free Game -> Name "First Second", Title "Free Game".
            name = ' '.join(words[:2])
            title = ' '.join(words[2:])
        elif len(words) == 3:
            name = ' '.join(words[:2])
            title = words[2]
        elif len(words) == 2:
            name, title = words
        elif words:
            name, title = words[0], ''
        else:
            name, title = '', ''

    return _clean_name_for_rename(name), _clean_title_for_rename(title), year, ''


def _fast_probe_display_geometry(video_path):
    """Read only first-video-stream geometry/rotation in one fast FFprobe call."""
    _, ffprobe = require_ffmpeg()
    cmd = [
        ffprobe, '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height:stream_tags=rotate:stream_side_data=rotation',
        '-of', 'json', str(video_path)
    ]
    result = run_command(cmd)
    data = json.loads(result.stdout or '{}')
    streams = data.get('streams') or []
    if not streams:
        raise ValueError('Could not find a readable video stream.')
    stream = streams[0]
    width = _safe_int(stream.get('width'))
    height = _safe_int(stream.get('height'))
    if width <= 0 or height <= 0:
        raise ValueError('Could not detect the video resolution.')

    rotation = 0
    tags = stream.get('tags') or {}
    raw = tags.get('rotate') or tags.get('ROTATE')
    if raw not in (None, ''):
        try:
            rotation = int(round(float(raw))) % 360
        except Exception:
            rotation = 0
    if not rotation:
        for side in stream.get('side_data_list') or []:
            raw = side.get('rotation')
            if raw not in (None, ''):
                try:
                    rotation = int(round(float(raw))) % 360
                    break
                except Exception:
                    pass

    display_width, display_height = width, height
    if rotation in (90, 270):
        display_width, display_height = height, width
    return display_width, display_height, rotation


def detect_rename_resolution(video_path):
    """Automatically choose the requested filename resolution from actual display geometry."""
    width, height, rotation = _fast_probe_display_geometry(video_path)
    vertical = height > width
    if vertical:
        long_edge = height
        resolution = '1920p' if abs(long_edge - 1920) <= abs(long_edge - 3840) else '3840p'
    else:
        short_edge = height
        standards = (720, 1080, 2160)
        nearest = min(standards, key=lambda value: abs(short_edge - value))
        resolution = f'{nearest}p'
    return resolution, width, height, rotation


def build_renamed_filename(source_path, name, title, year, resolution):
    source_path = Path(source_path)
    name = _clean_name_for_rename(name)
    title = _clean_title_for_rename(title)
    year = _clean_year_for_rename(year)
    resolution = str(resolution or '').strip().lower()
    if resolution and not resolution.endswith('p'):
        resolution += 'p'
    if not name:
        raise ValueError('Name is required for renaming.')
    if not title:
        raise ValueError('Title is required for renaming.')
    if resolution not in {x.lower() for x in _RENAME_RESOLUTIONS}:
        raise ValueError('Resolution must be detected as 720p, 1080p, 2160p, 1920p, or 3840p.')
    year_part = f' {year}' if year else ''
    filename = f'[OF] {name} {title}{year_part} [{resolution}]{source_path.suffix}'
    if len(filename) > 240:
        raise ValueError('The resulting filename is too long for reliable Windows use. Shorten the Name or Title.')
    return filename


class TorrentCreatorAppV32(TorrentCreatorAppV311):
    """v3.3: optional per-file renaming with VLC-safe file-handle release and updater support."""

    def _ensure_v31_vars(self):
        super()._ensure_v31_vars()
        if hasattr(self, 'rename_enabled_var'):
            return
        self.rename_enabled_var = tk.BooleanVar(value=False)
        self.rename_performer_var = tk.StringVar(value='')
        self.rename_title_var = tk.StringVar(value='')
        self.rename_year_var = tk.StringVar(value='')
        self.rename_resolution_var = tk.StringVar(value='')
        self.rename_original_var = tk.StringVar(value='No file selected.')
        self.rename_preview_var = tk.StringVar(value='New filename: —')
        self.rename_detected_var = tk.StringVar(value='Select a video to prepare a rename.')
        self._rename_loading_fields = False
        self._rename_trace_ids = []
        self._rename_detection_pending = set()

    def _build_ui(self):
        super()._build_ui()
        rename_tab = ttk.Frame(self.notebook, padding=12)
        self.rename_tab = rename_tab
        # Put Rename immediately before Preview.
        try:
            self.notebook.insert(3, rename_tab, text='Rename')
        except Exception:
            self.notebook.add(rename_tab, text='Rename')
        self._build_rename_tab(rename_tab)
        for var in (self.rename_enabled_var, self.rename_performer_var, self.rename_title_var, self.rename_year_var, self.rename_resolution_var):
            try:
                self._rename_trace_ids.append(var.trace_add('write', self._rename_vars_changed))
            except Exception:
                pass
        self._sync_rename_fields_from_selected_job()

    def _build_run_tab(self, tab):
        """General processing settings with an always-visible animated preview format selector."""
        tab.columnconfigure(1, weight=1)
        ttk.Label(tab, text='What to create').grid(row=0, column=0, sticky='w', pady=5)
        ttk.Combobox(tab, textvariable=self.process_mode_var, values=PROCESS_MODES, state='readonly', width=30).grid(row=0, column=1, sticky='w', padx=8)

        preview_box = ttk.LabelFrame(tab, text='Animated preview output', padding=10)
        preview_box.grid(row=1, column=0, columnspan=3, sticky='ew', pady=(8, 10))
        preview_box.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            preview_box,
            text='Create animated preview when Everything is selected',
            variable=self.make_gif_var,
        ).grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 8))
        ttk.Label(preview_box, text='Format').grid(row=1, column=0, sticky='w', padx=(0, 10))
        for col, value in enumerate(PREVIEW_FORMATS, start=1):
            ttk.Radiobutton(
                preview_box,
                text=value,
                value=value,
                variable=self.preview_format_var,
            ).grid(row=1, column=col, sticky='w', padx=(0, 18))
        ttk.Label(
            preview_box,
            text='Choose GIF, animated WebP, or create both. The same choice is used by Everything and Animated preview only jobs, the Preview gallery, verification, and HamsterImg upload filtering.',
            style='Muted.TLabel',
            wraplength=760,
        ).grid(row=2, column=0, columnspan=4, sticky='w', pady=(7, 0))

        ttk.Checkbutton(tab, text='Overwrite existing files', variable=self.overwrite_var).grid(row=2, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Label(tab, text='When disabled, existing output files are skipped. Enable it when you explicitly want to regenerate media or the torrent.', wraplength=760).grid(row=3, column=0, columnspan=3, sticky='w', pady=(0, 7))
        ttk.Checkbutton(tab, text='Verify output after each job', variable=self.verify_var).grid(row=4, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Checkbutton(tab, text='Open output folder when queue finishes', variable=self.open_folder_var).grid(row=5, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Button(tab, text='Reset default settings', command=self.reset_defaults).grid(row=6, column=0, sticky='w', pady=(14, 0))
        ttk.Label(tab, text='Jobs are always processed sequentially. The next video starts only after the current video is completely finished. Temporary FFmpeg files are cleaned automatically.', wraplength=760).grid(row=7, column=0, columnspan=3, sticky='w', pady=(14, 0))

    def _build_rename_tab(self, tab):
        tab.columnconfigure(0, weight=1)

        intro = ttk.Frame(tab, style='Panel.TFrame', padding=10)
        intro.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        intro.columnconfigure(0, weight=1)
        ttk.Label(intro, text='Optional filename rename', style='Panel.TLabel', font=('Segoe UI Semibold', 11)).grid(row=0, column=0, sticky='w')
        ttk.Label(
            intro,
            text='Renaming is optional per file. Standard: [OF] Name Title Year-if-present [Resolution]. TorrentCreator fully unloads the file from VLC before Windows renames it.',
            style='PanelMuted.TLabel', wraplength=900
        ).grid(row=1, column=0, sticky='w', pady=(4, 0))

        current = ttk.LabelFrame(tab, text='Selected file', padding=10)
        current.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        current.columnconfigure(1, weight=1)
        ttk.Checkbutton(current, text='Rename this file before processing', variable=self.rename_enabled_var).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))
        ttk.Label(current, text='Original filename').grid(row=1, column=0, sticky='nw', pady=4)
        ttk.Label(current, textvariable=self.rename_original_var, style='Muted.TLabel', wraplength=800).grid(row=1, column=1, columnspan=2, sticky='w', padx=(8, 0), pady=4)

        ttk.Label(current, text='Name').grid(row=2, column=0, sticky='w', pady=4)
        ttk.Entry(current, textvariable=self.rename_performer_var).grid(row=2, column=1, sticky='ew', padx=(8, 8), pady=4)
        ttk.Label(current, text='Name field; plus signs become spaces.', style='Muted.TLabel').grid(row=2, column=2, sticky='w', pady=4)

        ttk.Label(current, text='Title').grid(row=3, column=0, sticky='w', pady=4)
        ttk.Entry(current, textvariable=self.rename_title_var).grid(row=3, column=1, sticky='ew', padx=(8, 8), pady=4)
        ttk.Label(current, text='Title field; plus signs become spaces.', style='Muted.TLabel').grid(row=3, column=2, sticky='w', pady=4)

        ttk.Label(current, text='Year').grid(row=4, column=0, sticky='w', pady=4)
        ttk.Entry(current, textvariable=self.rename_year_var, width=14).grid(row=4, column=1, sticky='w', padx=(8, 8), pady=4)
        ttk.Label(current, text='Optional • detected automatically when a four-digit year is present in the filename.', style='Muted.TLabel').grid(row=4, column=2, sticky='w', pady=4)

        ttk.Label(current, text='Resolution').grid(row=5, column=0, sticky='w', pady=4)
        ttk.Entry(current, textvariable=self.rename_resolution_var, width=14, state='readonly').grid(row=5, column=1, sticky='w', padx=(8, 8), pady=4)
        ttk.Label(current, text='Detected automatically • Horizontal: 720p / 1080p / 2160p • Vertical: 1920p / 3840p', style='Muted.TLabel').grid(row=5, column=2, sticky='w', pady=4)

        preview = ttk.LabelFrame(tab, text='Filename preview', padding=10)
        preview.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        preview.columnconfigure(0, weight=1)
        ttk.Label(preview, textvariable=self.rename_preview_var, style='Panel.TLabel', wraplength=920).grid(row=0, column=0, sticky='w')
        ttk.Label(preview, textvariable=self.rename_detected_var, style='PanelMuted.TLabel', wraplength=920).grid(row=1, column=0, sticky='w', pady=(5, 0))

        actions = ttk.Frame(tab)
        actions.grid(row=3, column=0, sticky='w')
        self.rename_detect_button = ttk.Button(actions, text='Refresh automatic detection', command=lambda: self._detect_rename_selected(force=True))
        self.rename_detect_button.pack(side='left')
        self.rename_now_button = ttk.Button(actions, text='Rename selected now', command=self._rename_selected_now)
        self.rename_now_button.pack(side='left', padx=(6, 0))
        ttk.Button(actions, text='Reload current name', command=self._reset_rename_selected).pack(side='left', padx=(6, 0))

    def _add_paths(self, paths):
        before = {j['id'] for j in self.jobs}
        super()._add_paths(paths)
        for job in self.jobs:
            if job['id'] in before:
                continue
            source = Path(job['source'])
            performer, title, year, existing_res = _parse_rename_parts(source)
            job['rename_enabled'] = False
            job['rename_performer'] = performer
            job['rename_title'] = title
            job['rename_year'] = year
            job['rename_resolution'] = existing_res
        self._sync_rename_fields_from_selected_job()
        self.after(10, self._ensure_selected_rename_resolution)

    def _queue_selection_changed(self, event=None):
        super()._queue_selection_changed(event)
        self._sync_rename_fields_from_selected_job()

    def remove_selected_jobs(self):
        super().remove_selected_jobs()
        self._sync_rename_fields_from_selected_job()

    def _rename_vars_changed(self, *_args):
        if self._rename_loading_fields:
            return
        job = self._current_selected_job()
        if job:
            job['rename_enabled'] = bool(self.rename_enabled_var.get())
            job['rename_performer'] = self.rename_performer_var.get()
            job['rename_title'] = self.rename_title_var.get()
            job['rename_year'] = self.rename_year_var.get()
            job['rename_resolution'] = self.rename_resolution_var.get()
            if job['rename_enabled'] and not job.get('rename_resolution'):
                self.after(10, self._ensure_selected_rename_resolution)
        self._update_rename_preview()

    def _sync_rename_fields_from_selected_job(self):
        if not hasattr(self, 'rename_enabled_var'):
            return
        job = self._current_selected_job()
        self._rename_loading_fields = True
        try:
            if not job:
                self.rename_enabled_var.set(False)
                self.rename_performer_var.set('')
                self.rename_title_var.set('')
                self.rename_year_var.set('')
                self.rename_resolution_var.set('')
                self.rename_original_var.set('No file selected.')
                self.rename_detected_var.set('Select a queued video to configure optional renaming.')
            else:
                source = Path(job['source'])
                if source.is_file():
                    performer, title, year, existing_res = _parse_rename_parts(source)
                    job.setdefault('rename_enabled', False)
                    job.setdefault('rename_performer', performer)
                    job.setdefault('rename_title', title)
                    job.setdefault('rename_year', year)
                    job.setdefault('rename_resolution', existing_res)
                    self.rename_enabled_var.set(bool(job.get('rename_enabled')))
                    self.rename_performer_var.set(job.get('rename_performer') or '')
                    self.rename_title_var.set(job.get('rename_title') or '')
                    self.rename_year_var.set(job.get('rename_year') or '')
                    self.rename_resolution_var.set(job.get('rename_resolution') or '')
                    self.rename_original_var.set(source.name)
                    self.rename_detected_var.set('Resolution has not been analyzed yet.' if not job.get('rename_resolution') else f"Resolution: {job.get('rename_resolution')}")
                else:
                    self.rename_enabled_var.set(False)
                    self.rename_performer_var.set('')
                    self.rename_title_var.set('')
                    self.rename_year_var.set('')
                    self.rename_resolution_var.set('')
                    self.rename_original_var.set(source.name)
                    self.rename_detected_var.set('Folder jobs are not renamed. Add the video file itself to use this feature.')
        finally:
            self._rename_loading_fields = False
        self._update_rename_preview()
        self.after(10, self._ensure_selected_rename_resolution)

    def _update_rename_preview(self):
        job = self._current_selected_job()
        if not job:
            self.rename_preview_var.set('New filename: —')
            return
        source = Path(job['source'])
        if not source.is_file():
            self.rename_preview_var.set('New filename: folder jobs are not renamed')
            return
        try:
            filename = build_renamed_filename(source, self.rename_performer_var.get(), self.rename_title_var.get(), self.rename_year_var.get(), self.rename_resolution_var.get())
            self.rename_preview_var.set(f'New filename: {filename}')
        except Exception as exc:
            self.rename_preview_var.set(f'New filename: incomplete — {friendly_error(exc)}')

    def _reset_rename_selected(self):
        job = self._current_selected_job()
        if not job:
            return
        source = Path(job['source'])
        performer, title, year, existing_res = _parse_rename_parts(source)
        job['rename_performer'] = performer
        job['rename_title'] = title
        job['rename_year'] = year
        job['rename_resolution'] = existing_res
        self._sync_rename_fields_from_selected_job()

    def _ensure_selected_rename_resolution(self):
        job = self._current_selected_job()
        if not job:
            return
        source = Path(job.get('source', ''))
        if not source.is_file() or job.get('rename_resolution'):
            return
        self._detect_rename_selected(force=False)

    def _detect_rename_selected(self, force=False):
        job = self._current_selected_job()
        if not job:
            if force:
                messagebox.showinfo(APP_NAME, 'Select a queued video first.')
            return
        source = Path(job['source'])
        if not source.is_file():
            if force:
                messagebox.showinfo(APP_NAME, 'Rename is available for individual video files, not folder jobs.')
            return
        if not force and job.get('rename_resolution'):
            return
        job_id = str(job['id'])
        if job_id in self._rename_detection_pending:
            return

        performer, title, year, _ = _parse_rename_parts(source)
        if not job.get('rename_performer'):
            job['rename_performer'] = performer
        if not job.get('rename_title'):
            job['rename_title'] = title
        if not job.get('rename_year') and year:
            job['rename_year'] = year

        self._rename_detection_pending.add(job_id)
        try:
            self.rename_detect_button.configure(state='disabled')
        except Exception:
            pass
        self.rename_detected_var.set('Detecting resolution automatically…')
        threading.Thread(target=self._detect_rename_worker, args=(job['id'], str(source)), daemon=True).start()

    def _detect_rename_worker(self, job_id, source_text):
        try:
            resolution, width, height, rotation = detect_rename_resolution(Path(source_text))
            self.after(0, lambda: self._apply_rename_detection(job_id, resolution, width, height, rotation, None))
        except Exception as exc:
            message = friendly_error(exc)
            self.after(0, lambda: self._apply_rename_detection(job_id, '', 0, 0, 0, message))

    def _apply_rename_detection(self, job_id, resolution, width, height, rotation, error):
        self._rename_detection_pending.discard(str(job_id))
        try:
            self.rename_detect_button.configure(state='normal' if not self.busy else 'disabled')
        except Exception:
            pass
        job = self._job_by_id(job_id)
        if not job:
            return
        if error:
            if self._current_selected_job() is job:
                self.rename_detected_var.set(f'Resolution detection failed: {error}')
            return
        job['rename_resolution'] = resolution
        job['rename_dimensions'] = f'{width}x{height}'
        job['rename_rotation'] = rotation
        if self._current_selected_job() is job:
            self._rename_loading_fields = True
            try:
                self.rename_resolution_var.set(resolution)
            finally:
                self._rename_loading_fields = False
            orientation = 'Vertical' if height > width else 'Horizontal'
            rotation_text = f' • rotation metadata {rotation}°' if rotation else ''
            self.rename_detected_var.set(f'Detected: {width}×{height} display • {orientation} • filename tag {resolution}{rotation_text}')
            self._update_rename_preview()

    def _release_vlc_file_for_rename(self, source):
        """Fully detach the current media from VLC so Windows can rename the file."""
        source = Path(source)
        loaded = Path(self._preview_media_path) if self._preview_media_path else None
        if loaded is None:
            return False
        try:
            same = loaded.resolve() == source.resolve()
        except Exception:
            same = str(loaded).casefold() == str(source).casefold()
        if not same:
            return False
        try:
            if self._vlc_player:
                self._vlc_player.stop()
                try:
                    self._vlc_player.set_media(None)
                except Exception:
                    pass
        finally:
            self._preview_media_path = None
            self.preview_seek_var.set(0)
            self.preview_time_var.set('00:00:00 / 00:00:00')
            self.preview_status_var.set('VLC preview unloaded for safe Windows filename rename.')
            self.update_idletasks()
            # libVLC may release the final Windows file handle asynchronously.
            time.sleep(0.18)
        return True

    def _preflight_rename_job(self, job):
        source = Path(job['source'])
        if not bool(job.get('rename_enabled')):
            return None
        if not source.is_file():
            raise ValueError(f"Rename is enabled for '{source.name}', but rename only supports individual video files.")
        # Resolution is never a required manual choice. If background detection has not
        # completed yet, do one minimal FFprobe query here before validating the rename.
        if not job.get('rename_resolution'):
            resolution, width, height, rotation = detect_rename_resolution(source)
            job['rename_resolution'] = resolution
            job['rename_dimensions'] = f'{width}x{height}'
            job['rename_rotation'] = rotation
        filename = build_renamed_filename(
            source,
            job.get('rename_performer', ''),
            job.get('rename_title', ''),
            job.get('rename_year', ''),
            job.get('rename_resolution', ''),
        )
        target = source.with_name(filename)
        try:
            same = target.resolve() == source.resolve()
        except Exception:
            same = str(target).casefold() == str(source).casefold()
        if not same and target.exists():
            raise FileExistsError(f"Cannot rename '{source.name}' because '{target.name}' already exists in the same folder.")
        return target

    def _perform_rename_job(self, job, reload_preview=True):
        target = self._preflight_rename_job(job)
        if target is None:
            return Path(job['source'])
        source = Path(job['source'])
        try:
            same = target.resolve() == source.resolve()
        except Exception:
            same = str(target).casefold() == str(source).casefold()
        if same:
            return source
        was_loaded = self._release_vlc_file_for_rename(source)
        old_name = source.name
        source.rename(target)
        job['source'] = str(target)
        job['rename_enabled'] = False
        job['rename_performer'] = _clean_performer_name(job.get('rename_performer', ''))
        job['rename_title'] = _clean_title_for_rename(job.get('rename_title', ''))
        job['rename_year'] = _clean_year_for_rename(job.get('rename_year', ''))
        job['rename_resolution'] = str(job.get('rename_resolution', '')).lower()
        self._append_console(f'Renamed: {old_name} → {target.name}')
        if was_loaded and reload_preview:
            try:
                self._set_preview_media(target, autoplay=False)
            except Exception:
                pass
        return target

    def _rename_selected_now(self):
        if self.busy:
            return
        job = self._current_selected_job()
        if not job:
            messagebox.showinfo(APP_NAME, 'Select a queued video first.')
            return
        self._rename_vars_changed()
        if not bool(job.get('rename_enabled')):
            if not messagebox.askyesno(APP_NAME, 'Rename this selected file now using the filename preview?'):
                return
            job['rename_enabled'] = True
        try:
            target = self._preflight_rename_job(job)
            if target is None:
                return
            source = Path(job['source'])
            if not messagebox.askyesno('Confirm rename', f'Rename this file?\n\n{source.name}\n\n→\n\n{target.name}\n\nVLC will be unloaded first so Windows can release the file.'):
                return
            self._perform_rename_job(job, reload_preview=True)
            self._rebuild_tree(select_ids=[str(job['id'])])
            self._sync_rename_fields_from_selected_job()
            self.status_var.set(f'Renamed to {Path(job["source"]).name}')
        except Exception as exc:
            messagebox.showerror(APP_NAME, friendly_error(exc))

    def start_queue(self):
        if self.busy:
            return
        # Store the currently edited fields before preflight.
        self._rename_vars_changed()
        enabled_jobs = [job for job in self.jobs if bool(job.get('rename_enabled'))]
        if enabled_jobs:
            # Validate the complete rename plan before changing any filename.
            try:
                plans = [(job, self._preflight_rename_job(job)) for job in enabled_jobs]
            except Exception as exc:
                messagebox.showerror('Rename validation', friendly_error(exc))
                if hasattr(self, 'notebook') and hasattr(self, 'rename_tab'):
                    self.notebook.select(self.rename_tab)
                return
            summary = '\n'.join(f"• {Path(job['source']).name}  →  {target.name}" for job, target in plans if target is not None)
            if summary and not messagebox.askyesno(
                'Rename before processing',
                'The following optional filename changes will be made before processing. VLC will be unloaded from each affected file first.\n\n' + summary + '\n\nContinue?'
            ):
                return
            try:
                selected_id = self.queue_tree.selection()[0] if self.queue_tree.selection() else None
                for job, target in plans:
                    if target is not None:
                        self._perform_rename_job(job, reload_preview=False)
                self._rebuild_tree(select_ids=[selected_id] if selected_id and self._job_by_id(selected_id) else None)
                self._sync_rename_fields_from_selected_job()
            except Exception as exc:
                messagebox.showerror('Rename failed', friendly_error(exc))
                self._rebuild_tree()
                self._sync_rename_fields_from_selected_job()
                return
        super().start_queue()

    def _set_busy(self, busy):
        super()._set_busy(busy)
        for name in ('rename_detect_button', 'rename_now_button'):
            widget = getattr(self, name, None)
            if widget is not None:
                try:
                    widget.configure(state='disabled' if busy else 'normal')
                except Exception:
                    pass


if __name__ == '__main__':
    TorrentCreatorAppV32().mainloop()
