TorrentCreator v3.4.0 - In-place update

To update an existing TorrentCreator Build Kit folder:

1. Close TorrentCreator completely.
2. Double-click UPDATE_TORRENTCREATOR.bat in this NEW version folder.
3. Select your EXISTING TorrentCreator project folder.
4. The updater keeps .build-tools\ffmpeg, .build-tools\vlc, the existing venv, user settings and credentials.
5. The old dist-windows\single build is held aside until the new EXE has built successfully.
6. If the build fails, the updater restores the previous project files and previous EXE automatically.

v3.4.0 adds six default separate screenshots, a 2-column screenshot collage for forum uploads, and a higher-quality WebP profile (720 px / 12 FPS / quality 90 by default).

The finished EXE remains in:
dist-windows\single\TorrentCreator.exe
