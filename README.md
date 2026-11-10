# JP Music Tool

A small Windows desktop app that makes Japanese music libraries readable. It renames files, folders and tags from Japanese to **English (romaji)** and embeds cover art.

## The problem

Japanese music usually arrives with file names, folder names and tags written in kanji and kana, e.g. `01 夜に駆ける.flac`. That causes a few problems:

- **Hard to read.** If you can't read Japanese, you can't tell which track is which, or search and sort your library.
- **Breaks on some devices.** Some car stereos, older players and file systems show garbled text (`□□□`) or refuse to handle the names at all.
- **Slow to fix by hand.** You'd have to look up the reading and meaning of every title, then type both in for each track.
- **Missing artwork.** Many releases ship a `cover.jpg` next to the audio files but never embed it, so phones and players show a blank cover.

JP Music Tool does all of this for a whole folder in a few clicks:

```
01 夜に駆ける.flac   →   01 Racing into the Night (Yoru ni Kakeru).flac
```

## Features

- **Rename files.** Changes file names to `English translation (Romaji).ext`. Keeps track-number prefixes (`01 `, `1-01 - `, …), and renames matching `.lrc` lyric files along with the audio.
- **Fix tags and folders.** Converts Title, Album, Artist and Album-artist tags and folder names. Choose `English (Romaji)`, `Romaji only` or `English only` for each.
- **Embed cover art.** Finds `cover` / `folder` / `front` / `album` images (or one you pick) and embeds them in FLAC, MP3, M4A, OGG/Opus, WAV and AIFF files.
- **Preview first.** Every change is listed before anything is written, and you can edit any proposed name by hand.
- **Undo.** Each batch is logged to `_rename_log.json` in the music folder, so you can reverse the last batch.
- Optional subfolder (recursive) processing, and light/dark theme.

## Usage

### Option 1: Portable .exe (no Python needed)
Download **`JP Music Tool.exe`** from the [Releases](../../releases) page and run it.

### Option 2: Run from source
Requires Python 3.10+.

```bash
pip install -r requirements.txt
python jp_music_tool.py
```

On Windows you can also double-click `Run JP Music Tool.bat`.

### Steps
1. Choose your music folder.
2. Click **Generate preview** and check the proposed names (double-click a row to edit it).
3. Click **Apply**. If you don't like the result, use **Undo**.

## Notes

- **Translation** uses Google Translate's free web endpoint, so it needs an internet connection. If translation fails, the tool falls back to romaji only.
- **Romanization** is done offline with [cutlet](https://github.com/polm/cutlet) (MeCab/UniDic) and [pykakasi](https://github.com/miurahr/pykakasi).
- **Machine translation of song titles isn't perfect.** Check the preview, especially for wordplay or stylised titles.

## Dependencies

[mutagen](https://github.com/quodlibet/mutagen), pykakasi, cutlet, unidic-lite, deep-translator, Pillow and sv-ttk.

## License

MIT. See [LICENSE](LICENSE).
