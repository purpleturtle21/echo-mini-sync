# EchoList

> Playlist simulation for the SnowSky Echo Mini.

The Echo Mini does not offer the playlist experience many users expect because the SoC is incompatible with `.m3u` files. EchoList provides a simple way to build playlists, organize tracks, and syn[...]

With EchoList, songs can belong to multiple playlists while keeping your albums and library structure clean.

## Features

* Create, rename, and delete playlists
* Add and remove tracks
* Drag-and-drop track ordering
* Undo support
* Batch synchronization

## Advantages

### Multiple Playlist Membership

A song can belong to multiple playlists at the same time.

Unlike the Genre workaround, you are not limited to a single playlist per track.

### Clean Library Integration

Playlists appear inside the normal Music Library.

```text
Artists
├── AC/DC
├── Scorpions
├── Daft Punk
│
└── Playlists
    ├── Driving
    ├── Party
    └── Gym
```
```text
Albums
├── Back In Black
├── Love at First Sting
├── Discovery
│
├── Driving
├── Party
└── Gym
```

### Albums Remain Untouched

You can keep a complete album while also placing selected songs into playlists.

Browsing by album or artist behaves normally and is not polluted by playlist entries.

## Drawbacks

### Additional Storage Usage

EchoList creates a physical copy of each song that is added to a playlist.

Storage usage increases depending on the number of playlist tracks.

### Shuffle-All Bias

Tracks that appear in multiple playlists exist multiple times on the device.

As a result, Shuffle All will play those songs more frequently.

### Playlist-Only Tracks Are Not Artist Tracks

Tracks that exist only as playlist entries will not behave like normal artist-library tracks when browsing by artist.

## Safety Notice

**EchoList is almost completely a vibe-coded app**. It includes multiple safeguards to reduce the risk of accidental data loss, but nothing is fail-proof.

However, you should always keep backups of:

* Your music library
* Existing playlists
* Your SD card contents

## How It Works

1. Select your music library.
2. Create one or more playlists.
3. Add tracks and arrange them as desired.
4. Press **Sync**.

EchoList handles the rest.

## Screenshots

![App main view](screenshots/1.png)

![App with tracks](screenshots/2.png)

![Device connected](screenshots/3.jpeg)

![Device synced](screenshots/4.jpeg)

## Contributing

Bug reports, suggestions, and pull requests are welcome. Go wild.

If you test EchoList on devices other than the Echo Mini, feel free to share your results.

## Download

Pre-built binaries for Windows, Linux, and macOS are available on the [Releases](https://github.com/purpleturtle21/echo-mini-sync/releases) page.

**macOS (untested):** Download `echolist-macos-arm64` (Apple Silicon). macOS has not been tested — it may work but is not guaranteed. Apple will block the app because it is not signed. To open [...]
```bash
xattr -cr echolist-macos-arm64
chmod +x echolist-macos-arm64
./echolist-macos-arm64
```

**Windows:** SmartScreen may show "Windows protected your PC." Click **More info** → **Run anyway**.

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0). See the LICENSE file for details.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
