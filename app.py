from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import piexif
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

ROOT = Path(__file__).parent.resolve()
DATA = ROOT / "data" / "runs"
STATIC = ROOT / "static"
DATA.mkdir(parents=True, exist_ok=True)
for stale_run in DATA.iterdir():
    if stale_run.is_dir(): shutil.rmtree(stale_run, ignore_errors=True)
app = FastAPI(title="Snapchat Memory Restore")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
JOBS: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()
MAIN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_([\w-]+)-main\.(jpg|jpeg|mp4)$", re.I)
OVERLAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_[\w-]+)-overlay\.png$", re.I)


def update(job_id: str, **values: Any) -> None:
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)


def parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str): return None
    value = value.strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        try: return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError: return None


def parse_gps(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, str): return None
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", value)
    if len(nums) != 2: return None
    lat, lon = map(float, nums)
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def safe_member(info: zipfile.ZipInfo) -> bool:
    path = PurePosixPath(info.filename)
    kind = (info.external_attr >> 16) & 0o170000
    return not path.is_absolute() and ".." not in path.parts and kind != 0o120000


def copy_hash(src: Any, target: Path) -> str:
    digest = hashlib.sha256()
    with target.open("wb") as dest:
        while chunk := src.read(1024 * 1024):
            dest.write(chunk); digest.update(chunk)
    return digest.hexdigest()


def probe_video(path: Path) -> tuple[datetime | None, bool]:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
        "format_tags=creation_time:stream=codec_type:stream_tags=creation_time", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=90)
    if p.returncode: return None, False
    data = json.loads(p.stdout or "{}")
    raw = data.get("format", {}).get("tags", {}).get("creation_time")
    if not raw:
        raw = next((s.get("tags", {}).get("creation_time") for s in data.get("streams", [])
                    if s.get("tags", {}).get("creation_time")), None)
    audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return parse_date(raw), audio


def photo_exif(path: Path) -> tuple[bytes, datetime | None]:
    raw = b""
    try:
        with Image.open(path) as image: raw = image.info.get("exif", b"")
        exif = piexif.load(raw) if raw else {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        stamp = exif["Exif"].get(piexif.ExifIFD.DateTimeOriginal) or exif["Exif"].get(piexif.ExifIFD.DateTimeDigitized)
        try: dt = datetime.strptime(stamp.decode("ascii").strip("\0"), "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc) if stamp else None
        except (UnicodeDecodeError, ValueError): dt = None
        return raw, dt
    except Exception: return raw, None


def gps_ifd(lat: float, lon: float) -> dict[int, Any]:
    def dms(n: float) -> tuple[tuple[int, int], ...]:
        n = abs(n); d = int(n); m = int((n-d)*60); s = round((n-d-m/60)*3600*10000)
        return ((d, 1), (m, 1), (s, 10000))
    return {piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S", piexif.GPSIFD.GPSLatitude: dms(lat),
            piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W", piexif.GPSIFD.GPSLongitude: dms(lon)}


def restore_photo(src: Path, overlay: Path | None, dest: Path,
                  timestamp: datetime | None, gps: tuple[float, float] | None) -> bool:
    raw, old_stamp = photo_exif(src)
    changed = False
    try:
        exif = piexif.load(raw) if raw else {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        if timestamp and not old_stamp:
            val = timestamp.astimezone(timezone.utc).strftime("%Y:%m:%d %H:%M:%S").encode()
            exif["0th"][piexif.ImageIFD.DateTime] = val
            exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = val
            exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = val
            exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = b"+00:00"
            exif["Exif"][piexif.ExifIFD.OffsetTimeDigitized] = b"+00:00"
            changed = True
        if gps and not exif.get("GPS", {}).get(piexif.GPSIFD.GPSLatitude):
            exif.setdefault("GPS", {}).update(gps_ifd(*gps)); changed = True
        exif_bytes = piexif.dump(exif)
    except Exception: exif_bytes = raw
    if overlay is None and not changed:
        shutil.copyfile(src, dest); return bool(old_stamp)
    with Image.open(src) as im:
        orientation = im.getexif().get(274, 1)
        base = ImageOps.exif_transpose(im).convert("RGBA")
    if overlay:
        with Image.open(overlay) as im: top = im.convert("RGBA")
        if orientation == 3: top = top.rotate(180, expand=True)
        elif orientation == 6: top = top.rotate(270, expand=True)
        elif orientation == 8: top = top.rotate(90, expand=True)
        if top.size != base.size:
            scale = min(base.width/top.width, base.height/top.height)
            top = top.resize((round(top.width*scale), round(top.height*scale)), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", base.size)
            canvas.alpha_composite(top, ((base.width-top.width)//2, (base.height-top.height)//2)); top = canvas
        base = Image.alpha_composite(base, top)
    try:
        exif["0th"][piexif.ImageIFD.Orientation] = 1
        exif_bytes = piexif.dump(exif)
    except Exception: pass
    base.convert("RGB").save(dest, "JPEG", quality=96, subsampling=0, optimize=True, exif=exif_bytes)
    return bool(old_stamp or timestamp)


def save_photo_preview(src: Path, overlay: Path | None, restored: Path, target: Path) -> None:
    """Write small, metadata-free visual steps for the local progress wall."""
    target.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as image:
        orientation = image.getexif().get(274, 1)
        base = ImageOps.exif_transpose(image).convert("RGBA")
    layer = Image.new("RGBA", base.size)
    if overlay:
        with Image.open(overlay) as image:
            layer = image.convert("RGBA")
        if orientation == 3: layer = layer.rotate(180, expand=True)
        elif orientation == 6: layer = layer.rotate(270, expand=True)
        elif orientation == 8: layer = layer.rotate(90, expand=True)
        if layer.size != base.size:
            scale = min(base.width/layer.width, base.height/layer.height)
            layer = layer.resize((round(layer.width*scale), round(layer.height*scale)), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", base.size)
            canvas.alpha_composite(layer, ((base.width-layer.width)//2, (base.height-layer.height)//2))
            layer = canvas
    size = (480, 480)
    base.thumbnail(size, Image.Resampling.LANCZOS)
    layer.thumbnail(size, Image.Resampling.LANCZOS)
    with Image.open(restored) as image:
        after = ImageOps.exif_transpose(image).convert("RGB")
        after.thumbnail(size, Image.Resampling.LANCZOS)
    base.convert("RGB").save(target / "before.jpg", "JPEG", quality=78, optimize=True)
    layer.save(target / "overlay.png", "PNG", optimize=True)
    after.save(target / "after.jpg", "JPEG", quality=78, optimize=True)


def restore_video(src: Path, overlay: Path | None, dest: Path,
                  timestamp: datetime | None, gps: tuple[float, float] | None) -> tuple[bool, bool]:
    old_stamp, audio = probe_video(src)
    if overlay is None and (old_stamp is not None or timestamp is None) and gps is None:
        shutil.copyfile(src, dest); return bool(old_stamp), audio
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-noautorotate", "-i", str(src)]
    if overlay:
        cmd += ["-i", str(overlay), "-filter_complex",
                "[1:v][0:v]scale2ref=w='min(iw,main_w)':h='min(ih,main_h)'[ol][base];[base][ol]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2:format=auto[v]"]
    cmd += ["-map", "[v]" if overlay else "0:v:0", "-map", "0:a?", "-map_metadata", "0"]
    if timestamp:
        cmd += ["-metadata", f"creation_time={timestamp.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"]
    if gps: cmd += ["-metadata", f"location={gps[0]:+.6f}{gps[1]:+.6f}/"]
    cmd += (["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p"] if overlay else ["-c", "copy"])
    cmd += ["-c:a", "copy", "-movflags", "+faststart", str(dest)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if p.returncode: raise RuntimeError((p.stderr or "FFmpeg processing failed")[-400:])
    return bool(old_stamp or timestamp), audio


def run_restore(job_id: str, archives: list[Path]) -> None:
    run_dir = DATA / job_id; output_path = run_dir / "restored-memories.zip"
    failures: list[str] = []; incomplete: list[str] = []; counts = Counter(); done = 0; opened = []; media = []; rows = []
    started = time.monotonic()
    try:
        update(job_id, stage="Inspecting archives", percent=0)
        for path in archives:
            try: z = zipfile.ZipFile(path)
            except (zipfile.BadZipFile, OSError):
                failures.append("An archive could not be opened; its memories were not processed.")
                counts["failed"] += 1; continue
            all_infos = z.infolist()
            unsafe = [i for i in all_infos if not i.is_dir() and not safe_member(i) and MAIN_RE.match(PurePosixPath(i.filename).name)]
            if unsafe:
                counts["failed"] += len(unsafe)
                failures.append(f"Rejected {len(unsafe)} memory file path(s) that were unsafe.")
            infos = [i for i in all_infos if not i.is_dir() and safe_member(i)]; opened.append((path,z,infos))
            for info in infos:
                base = PurePosixPath(info.filename).name
                m = MAIN_RE.match(base)
                if m: media.append((path,z,info,*m.groups()))
                elif PurePosixPath(base).suffix.lower() in (".jpg", ".jpeg", ".mp4", ".mov", ".heic", ".webp"):
                    failures.append("An unsupported media file was found and was not changed.")
                    counts["failed"] += 1
                elif base.lower().endswith(".json") and info.file_size <= 30_000_000:
                    try: rows.extend(json.loads(z.read(info)).get("Saved Media", []))
                    except Exception: failures.append(f"Could not read a metadata index in {path.name}.")
        total = len(media) + counts["failed"]; done = counts["failed"]; indexes = defaultdict(list)
        required = sum(info.file_size for _, _, info, _, _, _ in media) * 2.2 + 512 * 1024 * 1024
        if shutil.disk_usage(run_dir).free < required:
            raise RuntimeError("Not enough free disk space for the restored media. Free disk space and try again.")
        for row in rows:
            dt = parse_date(row.get("Date")); kind = str(row.get("Media Type", "")).lower()
            if dt and kind in ("image", "video"): indexes[(dt.date().isoformat(), kind)].append(row)
        media_counts = Counter((day, "image" if ext.lower() in ("jpg", "jpeg") else "video") for _,_,_,day,_,ext in media)
        missing_index_media = Counter()
        for (day, kind), records in indexes.items():
            missing_index_media[kind] += max(0, len(records) - media_counts[(day, kind)])
        overlays = {id(z): {m.group(1): i for i in infos if (m := OVERLAY_RE.match(PurePosixPath(i.filename).name))}
                    for _,z,infos in opened}
        update(job_id, stage="Restoring memories", total=total, percent=0)
        seen = set(); seen_content = set(); used_names = set(); preview_ids: list[tuple[str, bool]] = []
        with zipfile.ZipFile(output_path, "w", allowZip64=True, compression=zipfile.ZIP_STORED) as out:
            for archive,z,info,day,ident,ext in media:
                type_name = "image" if ext.lower() in ("jpg", "jpeg") else "video"
                key = (day, type_name); candidates = indexes.get(key, [])
                row = candidates[0] if len(candidates) == 1 and media_counts[key] == 1 else None
                mapped = parse_date(row.get("Date")) if row else None; gps = parse_gps(row.get("Location")) if row else None
                stem = re.sub(r"-main\.(jpg|jpeg|mp4)$", "", PurePosixPath(info.filename).name, flags=re.I)
                ov_info = overlays[id(z)].get(stem); work = run_dir / "work"; work.mkdir(exist_ok=True)
                src = work / f"{uuid.uuid4().hex}.{ext.lower()}"; dest = work / f"{uuid.uuid4().hex}.{ext.lower()}"
                ov = work / f"{uuid.uuid4().hex}.png" if ov_info else None
                try:
                    with z.open(info) as member: digest = copy_hash(member, src)
                    if ov_info and ov:
                        with z.open(ov_info) as member: ov_digest = copy_hash(member, ov)
                    else: ov_digest = ""
                    signature = (day, ident, digest, ov_digest)
                    if signature in seen: counts["duplicates"] += 1
                    else:
                        content_signature = (day, type_name, digest, ov_digest)
                        if content_signature in seen_content: counts["possible_duplicates"] += 1
                        seen_content.add(content_signature)
                        if type_name == "image":
                            source_time = photo_exif(src)[1]; restored_time = source_time or mapped
                            stamped = restore_photo(src, ov, dest, mapped, gps); counts["photos"] += 1
                        else:
                            source_time, _ = probe_video(src); restored_time = source_time or mapped
                            stamped, audio = restore_video(src, ov, dest, restored_time, gps)
                            counts["videos"] += 1
                            if audio: counts["audio_preserved"] += 1
                        if ov: counts["overlays"] += 1
                        if stamped: counts["timestamps"] += 1
                        if gps: counts["gps"] += 1
                        if not restored_time:
                            incomplete.append(f"{PurePosixPath(info.filename).name}: no exact capture time was present; the archive date is retained.")
                        seen.add(signature)
                        suffix = ".jpg" if type_name == "image" else ".mp4"
                        output_name = f"{day}_{ident}_restored{suffix}"
                        if output_name in used_names: output_name = f"{day}_{ident}_{len(used_names)+1}_restored{suffix}"
                        used_names.add(output_name)
                        stamp = restored_time or parse_date(day)
                        if stamp:
                            local = stamp.astimezone().timestamp(); os.utime(dest, (local, local))
                        out.write(dest, output_name); counts["restored"] += 1
                        if type_name == "image":
                            preview_id = uuid.uuid4().hex
                            try:
                                save_photo_preview(src, ov, dest, run_dir / "previews" / preview_id)
                                preview_ids.append((preview_id, bool(ov)))
                                if len(preview_ids) > 12:
                                    expired, _ = preview_ids.pop(0)
                                    shutil.rmtree(run_dir / "previews" / expired, ignore_errors=True)
                                update(job_id, photo_previews=[{"id": item, "overlay": has_overlay} for item, has_overlay in preview_ids])
                            except Exception:
                                shutil.rmtree(run_dir / "previews" / preview_id, ignore_errors=True)
                except Exception as exc:
                    counts["failed"] += 1
                    failures.append(f"{PurePosixPath(info.filename).name}: {str(exc)[:200] or type(exc).__name__}")
                finally:
                    src.unlink(missing_ok=True); dest.unlink(missing_ok=True)
                    if ov: ov.unlink(missing_ok=True)
                done += 1; elapsed = time.monotonic()-started; rate = done/elapsed if elapsed else 0
                update(job_id, processed=done, percent=int(done/max(total,1)*100),
                       remaining_seconds=int((total-done)/rate) if rate else None,
                       counts=dict(counts), stage=f"Restoring {type_name}s")
            counts["incomplete_timestamps"] = len(incomplete)
            counts["missing_media_index_entries"] = sum(missing_index_media.values())
            report = ["Snapchat Memory Restore — processing report", "", f"Archives: {len(archives)}",
                f"Media entries inspected: {total}", f"Restored: {counts['restored']}", f"Photos: {counts['photos']}",
                f"Videos: {counts['videos']}", f"Overlays merged: {counts['overlays']}",
                f"Timestamps embedded or preserved: {counts['timestamps']}", f"GPS coordinates restored: {counts['gps']}",
                      f"Confirmed duplicates skipped: {counts['duplicates']}",
                      f"Exact-content matches kept because export IDs differ: {counts['possible_duplicates']}",
                      f"Index entries without matching included media: {counts['missing_media_index_entries']}",
                      f"Failures: {counts['failed']}", "",
                "Dates and exact times are taken only from source metadata or a one-to-one export-index match.",
                      "Snapchat download URLs are not contacted. Index entries without a matching main file are listed by count because the index has no stable media ID.",
                "Overlay videos are encoded as H.264; original audio streams are copied when present."]
            if failures or incomplete: report += ["", "Files needing attention:", *failures, *incomplete]
            out.writestr("RESTORATION-REPORT.txt", "\n".join(report)+"\n")
        for _,z,_ in opened: z.close()
        shutil.rmtree(run_dir / "work", ignore_errors=True)
        for path in archives: path.unlink(missing_ok=True)
        update(job_id, status="complete", stage="Restoration complete", percent=100, processed=done, total=total,
               remaining_seconds=0, counts=dict(counts), failures=failures, incomplete_count=len(incomplete),
               download_url=f"/api/jobs/{job_id}/download",
               elapsed_seconds=round(time.monotonic()-started, 1))
    except Exception as exc:
        for _,z,_ in opened:
            try: z.close()
            except Exception: pass
        for path in archives: path.unlink(missing_ok=True)
        shutil.rmtree(run_dir / "work", ignore_errors=True)
        (run_dir / "restored-memories.zip").unlink(missing_ok=True)
        update(job_id, status="error", stage="Could not complete restoration", error=str(exc)[:500], failures=failures)


@app.get("/", response_class=HTMLResponse)
def home() -> str: return (STATIC / "index.html").read_text()


@app.post("/api/jobs")
def create_job() -> dict[str,str]:
    with LOCK:
        stale = [key for key, value in JOBS.items() if value.get("status") in ("complete", "error")]
        for key in stale: JOBS.pop(key, None)
    for key in stale: shutil.rmtree(DATA/key, ignore_errors=True)
    job_id = uuid.uuid4().hex; (DATA/job_id/"uploads").mkdir(parents=True)
    with LOCK: JOBS[job_id] = {"status":"created","stage":"Waiting for ZIP files","percent":0,"processed":0,"total":0,"counts":{},"photo_previews":[]}
    return {"id":job_id}


@app.post("/api/jobs/{job_id}/upload/{index}")
async def upload(job_id: str, index: int, request: Request, background: BackgroundTasks,
                 name: str, count: int) -> dict[str,str]:
    with LOCK: job=JOBS.get(job_id)
    if not job or job.get("status") not in ("created", "uploading"): raise HTTPException(404,"Restoration session not found")
    if not 1 <= count <= 1000 or index < 0 or index >= count: raise HTTPException(400,"Invalid archive sequence")
    if Path(name).name != name or not name.lower().endswith(".zip"): raise HTTPException(400,"Choose a ZIP archive")
    if job.get("expected_count",count) != count or job.get("next_upload",0) != index:
        raise HTTPException(409,"Archive upload is out of order")
    try: length=int(request.headers.get("content-length", "-1"))
    except ValueError: length=-1
    if length < 0: raise HTTPException(411,"Content-Length is required for large archive uploads")
    if shutil.disk_usage(DATA).free < length + 512*1024*1024:
        raise HTTPException(507,"Not enough free disk space to receive this ZIP file")
    run=DATA/job_id; dest=run/"uploads"/f"{index+1:03d}-{uuid.uuid4().hex}.zip"; received=0
    try:
        with dest.open("wb") as out:
            async for chunk in request.stream():
                out.write(chunk); received += len(chunk)
        if received != length: raise HTTPException(400,"The archive upload ended before all bytes arrived")
    except Exception:
        dest.unlink(missing_ok=True); raise
    with LOCK:
        job=JOBS[job_id]
        job.setdefault("archives", []).append(dest)
        job["expected_count"]=count; job["next_upload"]=index+1; job["status"]="uploading"
    if index+1 < count:
        update(job_id,stage=f"Received archive {index+1} of {count}")
        return {"status":"uploading"}
    saved=job["archives"]
    if len(saved) != count: raise HTTPException(409,"One or more archives were not received")
    update(job_id,status="processing",stage="Inspecting archives",percent=0)
    background.add_task(run_restore,job_id,saved)
    return {"status":"processing"}


@app.get("/api/jobs/{job_id}")
def status(job_id: str) -> dict[str,Any]:
    with LOCK: result=JOBS.get(job_id)
    if not result: raise HTTPException(404,"Restoration session not found")
    return result


@app.get("/api/jobs/{job_id}/previews/{preview_id}/{kind}")
def photo_preview(job_id: str, preview_id: str, kind: str) -> FileResponse:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not re.fullmatch(r"[a-f0-9]{32}", preview_id):
        raise HTTPException(404, "Preview not found")
    if kind not in ("before.jpg", "overlay.png", "after.jpg"):
        raise HTTPException(404, "Preview not found")
    path = DATA / job_id / "previews" / preview_id / kind
    if job_id not in JOBS or not path.is_file():
        raise HTTPException(404, "Preview not found")
    media_type = "image/png" if kind.endswith(".png") else "image/jpeg"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    path=DATA/job_id/"restored-memories.zip"
    if job_id not in JOBS or not path.exists(): raise HTTPException(404,"Restored ZIP is not ready")
    return FileResponse(path,media_type="application/zip",filename="restored-memories.zip")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str,bool]:
    shutil.rmtree(DATA/job_id,ignore_errors=True)
    with LOCK: JOBS.pop(job_id,None)
    return {"deleted":True}
