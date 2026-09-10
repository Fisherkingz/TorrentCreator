# TorrentCreator v3.4.0

TorrentCreator is a Windows desktop application for creating BitTorrent v1 `.torrent` files and preview media from video files.

Version 3.1 is a major interface and preview-workflow update. Version 3.1.1 improves pre-processing playback and generated-media visibility. It keeps the existing torrent, screenshot/contact-sheet, GIF/WebP, metadata, privacy, queue, and HamsterImg features while adding an embedded media preview workspace.





## What changed in v3.4.0

- The default number of separate screenshots is now **6** instead of 5. Existing settings that still contain the old default value of 5 are migrated to 6 automatically.
- Added an optional **2-column screenshot collage**. With the default six screenshots the collage is a clean **2 × 3** layout.
- The six original single screenshots are still saved exactly as before; the collage is an additional JPEG file named like `Movie_collage.jpg`.
- The collage is built from the finished single screenshots, keeps their timestamps, does not crop the video frames, and uses the same width/margin/JPEG-quality settings as the screenshot workflow.
- The generated-media gallery now shows the collage together with screenshots, contact sheet, GIF and WebP.
- HamsterImg has a separate **Collage** upload checkbox. When selected, its BBCode Full entry is added to the normal BBCode result file.
- WebP now has its own quality profile instead of sharing GIF's 540 px / 8 FPS settings. Default WebP target is **720 px / 12 FPS / quality 90**.
- The 7 MB size limiter remains active. WebP starts at the higher target and only reduces quality, FPS and resolution when necessary to fit the selected size limit.
- GIF keeps its existing default target of **540 px / 8 FPS** for compatibility and speed.

## What changed in v3.3.2

- Added an always-visible **Animated preview output** selector to the General tab.
- Choose **GIF**, **WebP**, or **GIF + WebP** before starting the queue.
- The selected format is used for both **Everything** and **Animated preview only** jobs.
- Preview discovery, output verification, and HamsterImg upload filtering follow the same selected format.
- The existing GIF/WebP selector in Preview → Manual GIF / WebP remains synchronized because both controls use the same setting.


## What changed in v3.3.1

- Fixed the generated-media gallery when video filenames contain literal square brackets, such as `[OF]`, `[1080p]`, `[2160p]`, or source tags like `[Bangbros]`.
- The gallery no longer uses wildcard glob parsing on the video filename. It now compares filenames literally, so all generated screenshots, the contact sheet, GIF and WebP are discovered reliably after processing.
- After a successful job, Preview → All Generated Media refreshes against the completed video and shows the generated thumbnails even after the job is automatically removed from the queue.
- The same bracket-safe matching is used by the **Upload generated media only** HamsterImg fallback, preventing the same filename issue there.


## What changed in v3.3.0

- New rename standard: **`[OF] Name Title Year [Resolution]`**. The year is optional and is only added when present or manually entered.
- The literal prefix is now `[OF]`; the name is no longer embedded inside the brackets.
- Normal spaces are preserved in the Name field.
- `+` characters are converted to spaces before filename parsing.
- A four-digit year (`1900`–`2099`) is detected automatically when present in the source filename.
- Resolution remains fully automatic from the actual video geometry. Existing resolution text in the filename is ignored for the final tag; FFprobe reads the real video stream.
- Example with year: `Lily+Phillips+FT+Gangbang+2026.mp4` → `[OF] Lily Phillips FT Gangbang 2026 [1080p].mp4` (assuming the video is 1080p).
- Example without year: `First+Second+Free+Game.mp4` → `[OF] First Second Free Game [1080p].mp4` (assuming the video is 1080p).
- New **in-place update workflow**. Future update packages can update the existing TorrentCreator folder while preserving cached FFmpeg/VLC runtimes, build tools, user settings, and the previous EXE until the replacement build succeeds.

### In-place updates

For an existing installation, download the smaller **Update Package**, extract it anywhere, and run `UPDATE_TORRENTCREATOR.bat`. Select your existing TorrentCreator project folder when prompted. The updater backs up the current project files, copies the new version into the existing folder, reuses `.build-tools\ffmpeg` and `.build-tools\vlc`, rebuilds the EXE, and restores the previous files automatically if the build fails.

User settings are stored outside the project folder under `%APPDATA%\TorrentCreator`, so they are preserved automatically. HamsterImg credentials stored in Windows Credential Manager are also unaffected.

## What changed in v3.2.1

- Rename resolution is now **fully automatic**. The Resolution field is read-only and is filled from the actual first video stream; no manual resolution choice is required.
- Resolution detection now uses a single minimal FFprobe query for width, height and rotation instead of the full metadata scan, making rename preparation substantially faster.
- A final automatic detection fallback runs immediately before rename if the background check has not finished yet.
- Plus signs in source filenames are treated as encoded spaces. Example: `First+Second+Free+Game` is parsed as performer `First` and title `Second Free Game`, producing `[OF-First] Second Free Game [Resolution]`.
- Plus signs are also normalized in manually edited Performer/Title fields (`First+Second` → `FirstSecond` for Performer, `Free+Game` → `Free Game` for Title).

## What changed in v3.2.0

- New **Rename** tab for optional per-file filename changes.
- Renaming is opt-in for each queued video; files are never renamed unless enabled for that job.
- Target pattern: `[OF-PerformerName] Title [Resolution].ext`.
- Performer is taken from the first filename field and whitespace is removed.
- Title is taken from the second filename field and remains editable.
- Resolution is read from the actual video with FFprobe, not trusted from the filename.
- Horizontal display mapping: **720p, 1080p, 2160p**.
- Vertical display mapping: **1920p or 3840p**. Rotation metadata is considered when available.
- Filename preview updates before any disk change. Performer, title, and resolution can all be corrected manually.
- **VLC-safe rename**: if the source video is loaded in Preview, TorrentCreator stops playback, detaches the media from libVLC, waits for the Windows file handle to release, renames the file, and can reload it without autoplay.
- When `Rename this file before processing` is enabled, rename happens before torrent, screenshots, contact sheet, GIF/WebP, and HamsterImg steps so generated outputs inherit the new base filename.
- Existing-target collision protection: TorrentCreator will not silently overwrite another file during rename.


## What changed in v3.1.1

- **Play videos before processing**: adding a video now selects and loads it into Preview immediately.
- New **Load selected** and **Play selected** buttons in the Preview header.
- Double-clicking a queued video still opens it in Preview.
- Generated media is now shown as an **always-visible thumbnail wall** instead of requiring one file to be selected from a list first.
- All separate screenshots, the contact sheet, GIF and WebP are shown together when they exist.
- Clicking any thumbnail shows a larger preview; double-click opens that file in the operating system.
- After a successful job, TorrentCreator automatically switches to **Preview → All Generated Media** and refreshes the complete result set before the completed queue item is removed.
- The gallery remains available for the last completed video even when the successful job has disappeared from the queue.

## What's new in v3.1

- **Dark / Light / System theme** selector in the main toolbar.
- Theme choice is remembered between sessions.
- New central **Preview** workspace.
- Embedded **VLC/libVLC video player** with picture, audio, seeking, volume, play/pause and stop.
- Frame-step buttons for fine positioning.
- Generated-media browser for separate screenshots, contact sheet, GIF and WebP.
- Click a generated image to preview it inside TorrentCreator.
- Double-click generated media to open it in Windows.
- Replace a selected separate screenshot with the exact frame currently shown in the player.
- **Manual GIF/WebP editor**: choose exact video ranges with Set start / Set end / Add clip.
- Reorder, remove or clear manual clips per queued video.
- Automatic and Manual animated-preview modes can be switched independently of GIF/WebP output format.
- Manual mode preserves the selected time ranges; the size limiter reduces visual quality before timing.
- Cleaner tab structure: **General, Torrent, Screenshots, Preview, Image Hosting, Privacy**.
- Resizable queue/workspace split using a native pane divider.
- Selected-video information and last result summary stay visible above the tabs.
- Collapsible Console panel.
- Keyboard shortcuts: `Ctrl+O` Add files, `Ctrl+Enter` Start queue, `Ctrl+L` Collapse/expand Console, `Space` Play/Pause while the Preview tab is active.

## Existing core features

- Sequential job queue; files are always processed one at a time.
- Drag & drop video files into the queue.
- Successful jobs are removed from the queue automatically.
- Failed/cancelled jobs remain for review or retry.
- Create BitTorrent v1 `.torrent` files.
- Default 6 separate JPEG screenshots.
- Optional 2-column forum collage built from all separate screenshots; with six screenshots this is a 2 × 3 collage.
- Default 3 × 9 contact sheet, 1300 px wide.
- Contact-sheet title always remains visible and wraps using a smaller font when required.
- Contact-sheet header includes **resolution, duration, bitrate and file size**.
- Timestamp on every contact-sheet frame.
- Animated preview as **GIF, WebP, or GIF + WebP**.
- Default maximum preview size: 7 MB per file.
- Automatic preview uses multiple short segments between selected percentages of the video.
- Manual preview uses exact ranges selected in the embedded player.
- GIF defaults to 540 px / 8 FPS; WebP defaults to 720 px / 12 FPS / quality 90.
- Preview size reduction automatically lowers width/FPS/colors/quality as needed to stay under the selected limit.
- HamsterImg upload integration using an API key stored in Windows Credential Manager.
- HamsterImg output is simplified to **BBCode Full**.
- FFprobe analysis for codec, resolution, FPS, bitrate, HDR, audio/subtitle tracks, chapters and metadata tags.
- Privacy mode removes optional metadata generated by TorrentCreator while keeping the filename visible where required.
- Output verification after each job.
- Choose one output folder, optionally with a separate subfolder per video.
- Current-file and entire-queue progress with elapsed time and ETA.

## Preview player

The Windows build bundles a VLC 3.0.23/libVLC runtime and the `python-vlc` bindings. End users do not need to install VLC separately.

Select a queued video and open the **Preview** tab, or use **Play selected**. Videos can be played before any processing starts. The player supports:

- Play / Pause
- Stop
- Seek bar
- Volume
- Frame backward / frame forward
- Current time / total duration

The Preview tab also contains:

### Generated Media

Shows **all** generated screenshots, the contact sheet, GIF and WebP together in a scrollable thumbnail wall. Click a thumbnail for a larger in-app preview, or double-click it to open the file. Use **Refresh media** if files were created outside the current session.

The button **Replace selected screenshot with current frame** lets you overwrite one of the separate screenshot JPEGs with the exact frame currently selected in the video player. The original video is never changed.

### Manual GIF / WebP

Set **Mode** to `Manual`, then:

1. Seek to the desired start frame.
2. Click **Set start**.
3. Seek to the desired end frame.
4. Click **Set end**.
5. Click **Add clip**.
6. Repeat for additional segments.
7. Reorder clips if needed.
8. Run the queue normally.

Manual clips are stored per queued video. A video in Manual mode must have at least one valid clip before preview generation starts.

The same manual clip selection can create GIF, WebP, or both. The existing maximum file-size limit still applies.

### Video Info & Tags

The Preview tab contains the existing FFprobe report. Quick Check can display:

- container format
- resolution
- duration
- total bitrate
- video codec/profile
- frame rate
- pixel format
- HDR information
- audio streams, codecs, languages and titles
- subtitle streams, languages and titles
- chapters
- embedded metadata tags
- common release tags detected in the filename

## Screenshots / contact sheet

Default settings:

- Separate screenshots: 6
- Optional collage: 2 screenshots per row (2 × 3 with the default six)
- Contact sheet: 3 columns × 9 rows
- Width: 1300 px
- Margin: 5 px
- JPEG quality: 80
- Screenshot range: 10% to 90%

The complete filename remains visible in the header. Long names wrap automatically and the font becomes smaller rather than truncating the title.

The information row includes:

- Resolution
- Duration
- Bitrate
- File size

## Imagehost

TorrentCreator can upload selected generated media to Imagehost after a job completes.

- API key is entered inside TorrentCreator.
- **Save key securely** stores it in Windows Credential Manager.
- The API key is not written to `settings.json`, generated files or the EXE.
- Optional tags, Album ID, Category ID and NSFW marking are supported.
- Upload files one at a time.
- Output is **BBCode Full only**.
- A file named like `MyVideo_Imagehost_BBCode_Full.txt` is written after a successful upload.

## Privacy mode

Privacy mode is enabled by default. It removes optional metadata generated by TorrentCreator, including torrent `created by`, optional creation date/comment, EXIF metadata in newly generated JPEGs, and copied metadata in generated GIF/WebP files.

The original video is never modified. Metadata already embedded in the source video remains in that original file.

The filename is intentionally preserved in the torrent and contact sheet.

## Build the Windows EXE

1. Extract the Build Kit.
2. If you already have `.build-tools\ffmpeg` from an older version, copy only that FFmpeg folder into the new `.build-tools` folder to avoid downloading FFmpeg again.
3. Do **not** copy an old `.build-tools\venv`; Python virtual environments contain absolute paths.
4. Double-click `BUILD_WINDOWS_EXE.bat`.
5. On the first v3.1 build, the builder also downloads the official VLC 3.0.23 64-bit ZIP runtime for the embedded player.
6. The final EXE is created at:

```text
dist-windows\single\TorrentCreator.exe
```

The build script bundles:

- FFmpeg
- FFprobe
- VLC/libVLC runtime and plugins
- Python runtime
- Pillow
- tkinterdnd2
- python-vlc

The first v3.1 build is therefore larger and may take longer than previous releases. The FFmpeg and VLC downloads are cached under `.build-tools` for future builds.

## Portable build

Run `BUILD_PORTABLE.bat`. The portable output is written under:

```text
dist-windows\portable\
```

## Requirements when running from source

Install:

```text
pip install -r requirements.txt
```

FFmpeg/FFprobe must be available. For the embedded player, VLC/libVLC must also be installed or otherwise available to `python-vlc`.

## Third-party software

See `THIRD_PARTY_NOTICES.txt` for FFmpeg, VLC/libVLC, python-vlc, Pillow, tkinterdnd2 and PyInstaller information.

## License

TorrentCreator source code in this Build Kit is provided under the included `LICENSE`. Bundled/downloaded third-party components remain subject to their own licenses.
