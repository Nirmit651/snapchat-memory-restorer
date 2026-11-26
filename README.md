# Snapchat Memory Restore

A local web app for restoring media exported with Snapchat's **My Data** tool. Drop one or more export ZIP files into the page, review the result, then download one archive for your phone.

> **Hosted demo (may be offline):** [Open the Vercel demo](https://snap-memory-verceldehd9x.vercel.app). It may no longer be available later and may require Vercel access. This is a size-limited demo; ZIPs are sent to Vercel for processing. Do not use it for private or large exports. Run the app locally by following the instructions below to keep files on your computer.

## What it restores

Snapchat exports can separate a memory's original media (`*-main.jpg` or `*-main.mp4`) from its transparent caption and drawing layer (`*-overlay.png`). They can also include a `Saved Media` JSON index with date and location fields. This app pairs matching main and overlay names, composites overlays into the media, preserves embedded capture timestamps, and uses index timestamps and GPS only when one memory can be matched unambiguously by date and media type. The archives inspected for this project contained the media directly; their download URL fields were empty.

## Features

- Process multiple Snapchat ZIP archives together, without extracting them by hand.
- Merge transparent overlays into matching photos and videos.
- Skip duplicate entries only when the export memory ID and media content match. Flag exact-content matches with different IDs, but keep them so distinct memories are not lost.
- Preserve existing photo EXIF and video metadata. Add an unambiguous export-index timestamp or GPS coordinates when available.
- Copy video audio streams. Videos with overlays are encoded as H.264 for broad Android compatibility.
- Show upload and per-memory processing progress, elapsed time estimates, and a restoration summary.
- Include a text processing report in the output archive for files that failed or lacked exact capture times; missing times are reported separately from processing failures.
- Process media on the computer running the app. The server listens on `127.0.0.1` and does not send memories to a remote service.

## Technology

- Python and FastAPI for the local server and background processing
- Pillow and piexif for photo overlays and EXIF metadata
- FFmpeg and FFprobe for video composition, audio preservation, and metadata
- Vanilla HTML, CSS, and JavaScript for the browser interface

## Requirements

- macOS, Linux, or Windows with Python 3.10 or newer
- FFmpeg and FFprobe available on `PATH`
- Internet access the first time you install Python packages
- Enough free disk space for the uploaded ZIP copies and restored media. As a rule of thumb, allow around 2.5 times the uncompressed media size in addition to the original archives.

Install FFmpeg on macOS with Homebrew:

```sh
brew install ffmpeg
```

On other systems, install FFmpeg and make sure both `ffmpeg` and `ffprobe` commands work in a terminal.

## Install and run

From the project directory, run:

```sh
chmod +x run.sh
./run.sh
```

The first launch creates a local `.venv` and installs the required Python packages. Keep the terminal open while the app is running. Open <http://127.0.0.1:8765> in a browser. Stop the server with `Ctrl+C`.

To install packages separately instead, use:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765
```

## Restore an export

1. In Snapchat, open **Profile → Settings → My Data** (or sign in to the Accounts Portal), choose **Export your Memories**, select a date range, and submit the request. When Snapchat marks the export ready, open **See exports** and download the ZIP archive or archives. Snapchat's [official My Data instructions](https://help.snapchat.com/hc/en-us/articles/7012305371156-How-do-I-download-my-data-from-Snapchat) describe the current steps and note that Memories can also be downloaded from the export's `index.html` page.
2. Open the local page at <http://127.0.0.1:8765>.
3. Drop the ZIP files into the upload area, or choose them with **Browse files**. Remove any archive you did not mean to include.
4. Select **Start restoration**. The page shows upload progress and then the actual number of media entries processed.
5. Select **Download restored ZIP** when processing finishes. Review `RESTORATION-REPORT.txt` inside the ZIP for failures or missing exact capture times.

The app processes only media it finds inside the provided ZIPs. It does not follow or download Snapchat URLs in the export index. If an export contains links instead of media, this behavior is reported as a limitation rather than silently fetching private content.

## On Android

Transfer `restored-memories.zip` to your phone, extract it with the Android Files app, and move the restored `.jpg` and `.mp4` files into `Pictures` or `DCIM`. Google Photos and other gallery apps can scan those folders. The processing report is a text file and can be left out of the media folder if you prefer.

## Privacy

When run locally, restoration runs on the computer where this app is started. The app binds to loopback (`127.0.0.1`), and media is not uploaded to Snapchat, Google, or another server. Uploaded working copies and temporary files are stored under the ignored `data/` folder and removed after processing; the resulting ZIP remains there until you start another restoration or restart the server. The hosted Vercel preview is different: files selected there are uploaded to Vercel for processing. Use the local setup above for private exports. Original export archives are never modified by the app. Do not add personal exports, restored media, logs, or metadata files to Git.

## Troubleshooting

- **The page does not open:** confirm the startup terminal still shows the server running at port `8765`; then try <http://127.0.0.1:8765>.
- **FFmpeg is missing:** install FFmpeg and FFprobe, then restart `./run.sh`.
- **A ZIP cannot be read:** download that export again and check that it opens in your archive utility. Other valid ZIPs can be processed separately.
- **Not enough disk space:** free space on the disk containing the project and run the restoration again. The app checks for available space before processing media.
- **A time or location is absent:** the export may not contain it, or the index may have multiple memories on the same date. The app does not guess a match; see the report.
- **A video overlay fails:** the report identifies the file. FFmpeg must include H.264 encoding support (`libx264`) to create Android-compatible composited video.

## Known limitations

- Snapchat filenames provide a date but not necessarily an exact time. The app keeps that date in the output filename and ZIP timestamp and does not invent an EXIF capture time when no reliable time exists.
- The JSON index has no media identifier in the inspected export structure. Index date/location data is therefore applied only when the date and media type match exactly one index row and one selected media file. Index rows without matching embedded media are counted in the report. If identical media has different export IDs, the app keeps both entries and marks them as possible duplicates.
- Video overlays require video re-encoding, so video quality can change slightly. Audio streams are copied without re-encoding. Videos without overlays are copied or remuxed without video re-encoding.
- Fragmented video parts are not joined in this version; the inspected exports used standalone main video files. Unrecognized media files are listed in the report.
- The app handles media embedded in the archives. It does not retrieve expired or external download links.
- Media files that are corrupt or unsupported are listed in `RESTORATION-REPORT.txt`; they are not silently described as restored.

## Development checks

Run the safe synthetic checks with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

These checks generate temporary media at runtime and do not include personal export files.
