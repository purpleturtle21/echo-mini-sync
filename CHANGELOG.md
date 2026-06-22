# Changelog

## v0.2.0

### Thread Safety & Performance
- **Queue-based callback system**: All background-to-UI communication now goes through a thread-safe Queue polled at 60fps. Fixes `RuntimeError: main thread is not in main loop` crashes on Windows when switching playlists or running audits.
- **Async track loading with generation counter**: Tag reading and USB I/O happen in a background thread. A generation counter prevents stale results from overwriting fresh data if the user switches playlists quickly.
- **Tag & audit caching**: First visit to a playlist reads tags from disk (shows "Loading..." indicator); subsequent visits are instant from cache. Cache is invalidated on sync, metadata fix, offload/onload.
- **Smart sync/async path**: If all track tags are already cached, the refresh runs synchronously (no flicker, no thread overhead).

### Track Ordering Fix (Echo Mini)
- **Tracknumber metadata**: All formats (FLAC, MP3, M4A) now get `tracknumber = str(index)` written (plain "1" to "45"). Previously M4A was skipped. Filenames remain zero-padded (`01 - Song.flac`) via `track_filename()`.
- **`_normalize_tracknumber`**: Strips `N/M` and `N\M` total-tracks suffixes on read. No padding applied.
- **Audit and fix**: `audit_playlist_metadata` and `fix_playlist_metadata` now correctly detect and fix tracknumber mismatches on M4A files.

### Source Search
- Inline search/filter bar in the SOURCE panel. Case-insensitive substring match on filenames. Results shown as flat list with relative paths. Escape clears and restores the normal tree. Debounced at 150ms.

### Playlist Offload / Onload
- **Offload**: Backs up metadata, deletes files from device, marks playlist as offloaded (grey, `○` icon). Saves device space without losing playlist structure.
- **Onload**: Restores from backup, stages all tracks as pending adds. User reviews and syncs to copy files back.
- Right-click context menu on playlists: Offload, Onload, Rename, Delete, Restore points.

### Track Context Menu
- Right-click a track: **Show in source** expands the source tree to the track's original file. **Show in playlists** highlights which other playlists contain the same source file.

### External Playlist Adoption
- `adopt_playlist(folder_name)` — takes an existing folder on the device and registers it as an EchoList-managed playlist. Reads existing files, infers track order from filenames, writes proper metadata (albumartist, album, tracknumber, echolist tags for FLAC).
- Adopted playlists shown with amber color in the UI and participate fully in sync/audit/fix flows.

### .m3u Import
- Import `.m3u`/`.m3u8` files via File menu or drag-and-drop. Resolves paths relative to the m3u location, then falls back to `source_root`. Reports missing tracks. Extracts playlist name from `#PLAYLIST:` directive or filename.

### Metadata Backup & Restore
- Automatic backups every N syncs (configurable). Manual "Create restore point" via right-click menu.
- Restore from any backup: re-applies saved tags to tracks on device. Handles moved/renamed source files with multi-strategy resolution.
- Full playlist snapshot on first sync — recoverable even if `.echolist/` is deleted from the device.

### Tag Sanitization (FLAC)
- Strips 30+ metadata fields that confuse the Echo Mini's album/artist browser: `ALBUMARTISTSORT`, `ARTISTSORT`, MusicBrainz IDs, `TOTALTRACKS`, `COMPILATION`, etc.
- Normalizes `DATE` to 4-digit year.
- Strips equivalent fields from MP3/M4A via EasyID3/EasyMP4.

### Unified Settings UI
- Single settings screen for both initial setup and in-app configuration. Shows source, destination, playlist folder name, backup interval.
- Pre-populates from existing config when destination already has a workspace.
- File dialogs now show descriptive titles ("Select source music library", "Select destination device or folder").

### Bug Fixes
- `_renumber_tracks` no longer updates `copy_name` in the store when the disk rename fails (prevents store/disk desync).
- Removed orphaned error-handling code left over from `root.after()` replacement.
- Fixed 43 pytest warnings from audit threads calling `root.after()` after test teardown.
- Journal-based crash recovery for interrupted syncs.
- Lock file prevents concurrent instances from corrupting the workspace.

### Software Hardening
- Path traversal prevention in backup operations (validates resolved paths stay inside `BACKUPS_ROOT`).
- `SafeWriter` validates all write operations stay within workspace root.
- Atomic file writes via temp-file + rename pattern.
- Source files are never modified — only copies on the device are tagged.

### Tests
- 203 tests passing (9.6s).
- New tests: MP3/M4A tracknumber handling, `_normalize_tracknumber`, M4A audit/fix, adopt with tracknumbers.
- GUI tests use `_flush_tracks()` helper to wait for async track loading.
