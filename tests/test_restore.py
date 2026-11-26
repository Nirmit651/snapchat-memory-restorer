import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import piexif
from PIL import Image

import app


class RestoreTests(unittest.TestCase):
    def test_date_gps_and_member_safety(self):
        self.assertEqual(app.parse_date("2022-06-18 13:24:55 UTC").isoformat(), "2022-06-18T13:24:55+00:00")
        self.assertEqual(app.parse_gps("12.3456, 45.6789"), (12.3456, 45.6789))
        self.assertIsNone(app.parse_gps("New York"))
        self.assertFalse(app.safe_member(zipfile.ZipInfo("../../outside.jpg")))
        self.assertTrue(app.safe_member(zipfile.ZipInfo("memories/photo-main.jpg")))

    def test_photo_overlay_and_exif_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source=root/"source.jpg"; overlay=root/"overlay.png"; output=root/"restored.jpg"
            Image.new("RGB", (120, 80), (20, 40, 210)).save(source)
            top=Image.new("RGBA", (120, 80), (0, 0, 0, 0))
            for x in range(60):
                for y in range(80): top.putpixel((x,y),(240,20,20,255))
            top.save(overlay)
            stamp=datetime(2021,4,5,6,7,8,tzinfo=timezone.utc)
            stamped=app.restore_photo(source,overlay,output,stamp,(12.3456,45.6789))
            self.assertTrue(stamped)
            with Image.open(output) as image:
                self.assertEqual(image.size,(120,80))
                self.assertGreater(image.getpixel((20,20))[0],image.getpixel((20,20))[2])
                self.assertGreater(image.getpixel((100,20))[2],image.getpixel((100,20))[0])
                exif=piexif.load(image.info["exif"])
                self.assertEqual(exif["Exif"][piexif.ExifIFD.DateTimeOriginal],b"2021:04:05 06:07:08")
                self.assertIn(piexif.GPSIFD.GPSLatitude,exif["GPS"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_video_overlay_keeps_audio_and_creation_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/"source.mp4"; overlay=root/"overlay.png"; output=root/"restored.mp4"
            subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i","testsrc2=size=320x240:rate=12",
                            "-f","lavfi","-i","sine=frequency=440:sample_rate=44100","-t","1","-c:v","libx264",
                            "-pix_fmt","yuv420p","-c:a","aac",str(source)],check=True)
            layer=Image.new("RGBA",(320,240),(0,0,0,0))
            for x in range(100):
                for y in range(240): layer.putpixel((x,y),(20,240,40,255))
            layer.save(overlay)
            stamp=datetime(2020,1,2,3,4,5,tzinfo=timezone.utc)
            stamped,audio=app.restore_video(source,overlay,output,stamp,(12.3456,45.6789))
            self.assertTrue(stamped); self.assertTrue(audio)
            result=subprocess.run(["ffprobe","-v","error","-show_entries","format_tags=creation_time,location:stream=codec_type",
                                   "-of","json",str(output)],capture_output=True,text=True,check=True)
            info=json.loads(result.stdout)
            self.assertIn("audio",[stream["codec_type"] for stream in info["streams"]])
            self.assertIn("creation_time",info.get("format",{}).get("tags",{}))
            self.assertIn("location",info.get("format",{}).get("tags",{}))

    def test_two_archive_restore_merges_overlay_and_deduplicates(self):
        with tempfile.TemporaryDirectory(dir=app.DATA) as folder:
            root=Path(folder); archives=[]
            for number in (1,2):
                archive=root/f"part-{number}.zip"; archives.append(archive)
                with zipfile.ZipFile(archive,"w") as z:
                    if number==1:
                        z.writestr("memories/index.json",json.dumps({"Saved Media":[{"Date":"2021-04-05 06:07:08 UTC","Media Type":"Image","Location":"12.3456, 45.6789"}]}))
                    image=root/"source.jpg"
                    if not image.exists(): Image.new("RGB",(80,60),(20,40,210)).save(image)
                    overlay=root/"overlay.png"
                    if not overlay.exists():
                        layer=Image.new("RGBA",(80,60),(0,0,0,0)); layer.paste((240,20,20,255),(0,0,30,60)); layer.save(overlay)
                    z.write(image,"memories/2021-04-05_synthetic-main.jpg")
                    z.write(overlay,"memories/2021-04-05_synthetic-overlay.png")
            job_id=f"test-{root.name}"; (app.DATA/job_id).mkdir(parents=True); app.JOBS[job_id]={"status":"processing"}
            app.run_restore(job_id,archives)
            result=app.JOBS[job_id]
            self.assertEqual(result["status"],"complete",result)
            self.assertEqual(result["counts"]["restored"],1)
            self.assertEqual(result["counts"]["duplicates"],1)
            self.assertEqual(result["counts"]["overlays"],1)
            with zipfile.ZipFile(app.DATA/job_id/"restored-memories.zip") as z:
                names=z.namelist()
                self.assertEqual(len([name for name in names if name.endswith(".jpg")]),1)
                self.assertIn("RESTORATION-REPORT.txt",names)
            shutil.rmtree(app.DATA/job_id,ignore_errors=True); app.JOBS.pop(job_id,None)

    def test_same_pixels_with_different_memory_ids_are_kept(self):
        with tempfile.TemporaryDirectory(dir=app.DATA) as folder:
            root=Path(folder); image=root/"same.jpg"; Image.new("RGB",(40,30),(22,44,66)).save(image)
            archives=[]
            for index,ident in enumerate(("memory-one","memory-two"),1):
                archive=root/f"export-{index}.zip"; archives.append(archive)
                with zipfile.ZipFile(archive,"w") as z: z.write(image,f"memories/2021-04-05_{ident}-main.jpg")
            job_id=f"test-{root.name}"; (app.DATA/job_id).mkdir(parents=True); app.JOBS[job_id]={"status":"processing"}
            app.run_restore(job_id,archives); result=app.JOBS[job_id]
            self.assertEqual(result["status"],"complete",result)
            self.assertEqual(result["counts"]["restored"],2)
            self.assertEqual(result["counts"]["possible_duplicates"],1)
            self.assertEqual(result["counts"].get("duplicates",0),0)
            shutil.rmtree(app.DATA/job_id,ignore_errors=True); app.JOBS.pop(job_id,None)

    def test_unmatched_export_index_entries_are_reported(self):
        with tempfile.TemporaryDirectory(dir=app.DATA) as folder:
            root=Path(folder); archive=root/"export.zip"; image=root/"image.jpg"
            Image.new("RGB",(40,30),(22,44,66)).save(image)
            rows=[{"Date":"2021-04-05 06:07:08 UTC","Media Type":"Image","Location":"12.3456, 45.6789"},
                  {"Date":"2021-04-05 08:09:10 UTC","Media Type":"Image","Location":"30.1234, -70.5678"}]
            with zipfile.ZipFile(archive,"w") as z:
                z.write(image,"memories/2021-04-05_single-main.jpg")
                z.writestr("memories/index.json",json.dumps({"Saved Media":rows}))
            job_id=f"test-{root.name}"; (app.DATA/job_id).mkdir(parents=True); app.JOBS[job_id]={"status":"processing"}
            app.run_restore(job_id,[archive]); result=app.JOBS[job_id]
            self.assertEqual(result["status"],"complete",result)
            self.assertEqual(result["counts"]["missing_media_index_entries"],1)
            with zipfile.ZipFile(app.DATA/job_id/"restored-memories.zip") as z:
                report=z.read("RESTORATION-REPORT.txt").decode()
                self.assertIn("Index entries without matching included media: 1",report)
            shutil.rmtree(app.DATA/job_id,ignore_errors=True); app.JOBS.pop(job_id,None)


if __name__=="__main__": unittest.main()
