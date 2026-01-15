#!/usr/bin/env python3
import os
import zipfile
import shutil
import subprocess
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
import uuid

MAGIC_READ_BYTES = 65536
MIN_SIZE_BYTES_DEFAULT = 10 * 1024
DEFAULT_WORKERS = os.cpu_count() or 2
DEFAULT_TARGET_FORMAT = "mp3"
DEFAULT_BITRATE = "192k"
DEFAULT_SAMPLE_RATE = None

def parse_prefix(line, fmt):
    '''
    Parses the prefix from a line with the specified format.
    
    Returns:
        str: The parsed prefix.
    '''
    try:
        t = datetime.strptime(line, fmt)
    except ValueError as v:
        if len(v.args) > 0 and v.args[0].startswith('unconverted data remains: '):
            line = line[:-(len(v.args[0]) - 26)]
            t = datetime.strptime(line, fmt)
        else:
            raise
    return t.strftime('%m-%d_%H-%M')

def safe_filename(prefix, unique_id, ext):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{unique_id}{ext}"

def detect_extension_by_magic(path):
    try:
        with open(path, "rb") as f:
            data = f.read(MAGIC_READ_BYTES)
    except Exception:
        return None
    if len(data) >= 12 and data[4:8] == b"ftyp":
        up = data.upper()
        if b"M4A" in up or b"M4A " in up:
            return ".m4a"
        return ".mp4"
    if data.startswith(b"RIFF") and b"WAVE" in data[8:12]:
        return ".wav"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"caff"):
        return ".caf"
    if data.startswith(b"ID3"):
        return ".mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return ".mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xF6) == 0xF0:
        return ".aac"
    if data.startswith(b'\xff\xd8'):
        return ".jpg"
    return None

def ffprobe_has_audio(path):
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return bool(proc.stdout.strip())

def convert_with_ffmpeg(src, dst, target_format, bitrate="192k", sample_rate=None):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src), "-vn"]
    if sample_rate:
        cmd += ["-ar", str(sample_rate)]
    if target_format == "mp3":
        cmd += ["-ac", "2", "-b:a", bitrate, "-f", "mp3", str(dst)]
    elif target_format == "wav":
        cmd += ["-ac", "2", "-c:a", "pcm_s16le", "-f", "wav", str(dst)]
    else:
        raise ValueError("Unsupported format: " + str(target_format))
    res = subprocess.run(cmd)
    return res.returncode == 0

def _process_single_file(args):
    (src, relpath, output_dir, target_format, bitrate, sample_rate, min_size, ffmpeg_ok, ffprobe_ok) = args
    res = {"src": src, "relpath": relpath, "status": None, "out": None, "err": None}
    try:
        size = os.path.getsize(src)
        if size < min_size:
            try:
                os.remove(src)
                res["status"] = "deleted_small"
            except Exception as e:
                res["status"] = "delete_failed"
                res["err"] = str(e)
            return res
        guessed_ext = detect_extension_by_magic(src)
        is_audio = False
        if guessed_ext and guessed_ext.lower() in (".m4a", ".mp3", ".wav", ".aac", ".ogg", ".caf"):
            is_audio = True
        if guessed_ext and guessed_ext.lower() == ".mp4":
            if ffprobe_ok:
                is_audio = ffprobe_has_audio(src)
            else:
                is_audio = not ffmpeg_ok or False
        if guessed_ext is None and ffprobe_ok:
            is_audio = ffprobe_has_audio(src)
        if is_audio:
            unique_id = uuid.uuid4().hex[:8]
            if ffmpeg_ok and target_format in ("mp3", "wav"):
                out_ext = ".mp3" if target_format == "mp3" else ".wav"
                out_name = safe_filename("Audio", unique_id, out_ext)
                out_path = os.path.join(output_dir, out_name)
                ok = convert_with_ffmpeg(src, out_path, target_format, bitrate=bitrate, sample_rate=sample_rate)
                if ok:
                    try:
                        os.remove(src)
                    except Exception:
                        pass
                    res["status"] = "converted"
                    res["out"] = out_path
                else:
                    fallback_ext = guessed_ext if guessed_ext else ".mp4"
                    fallback_name = safe_filename("Attachment", unique_id, fallback_ext)
                    fallback_path = os.path.join(output_dir, fallback_name)
                    try:
                        shutil.move(src, fallback_path)
                        res["status"] = "kept_original"
                        res["out"] = fallback_path
                    except Exception as e:
                        res["status"] = "keep_failed"
                        res["err"] = str(e)
            else:
                fallback_ext = guessed_ext if guessed_ext else ".mp4"
                fallback_name = safe_filename("Attachment", unique_id, fallback_ext)
                fallback_path = os.path.join(output_dir, fallback_name)
                try:
                    shutil.move(src, fallback_path)
                    res["status"] = "kept_original_no_ffmpeg"
                    res["out"] = fallback_path
                except Exception as e:
                    res["status"] = "keep_failed"
                    res["err"] = str(e)
        else:
            try:
                os.remove(src)
                res["status"] = "deleted_non_audio"
            except Exception as e:
                res["status"] = "delete_failed"
                res["err"] = str(e)
    except Exception as e:
        res["status"] = "error"
        res["err"] = str(e)
    return res

def extract_voice_files(goodnotes_file, output_dir):
    '''
    Extracts audio files from a GoodNotes file and renames them.
    
    Returns:
        output_dir (str): The directory to save the extracted audio files.
    '''
    os.makedirs(output_dir, exist_ok=True)
    temp_dir = os.path.join(output_dir, "temp")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    ffprobe_ok = shutil.which("ffprobe") is not None
    target_format = DEFAULT_TARGET_FORMAT if ffmpeg_ok else "keep"
    min_size_bytes = MIN_SIZE_BYTES_DEFAULT
    bitrate = DEFAULT_BITRATE
    sample_rate = DEFAULT_SAMPLE_RATE
    workers = DEFAULT_WORKERS
    try:
        with zipfile.ZipFile(goodnotes_file, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        attachments_dir = None
        for root, dirs, files in os.walk(temp_dir):
            for d in dirs:
                if d.lower() == "attachments":
                    attachments_dir = os.path.join(root, d)
                    break
            if attachments_dir:
                break
        if not attachments_dir:
            print(f"No attachments found in {goodnotes_file}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return output_dir
        tasks = []
        for root, _, files in os.walk(attachments_dir):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, temp_dir)
                tasks.append((full, rel, output_dir, target_format, bitrate, sample_rate, min_size_bytes, ffmpeg_ok, ffprobe_ok))
        if not tasks:
            print(f"No files in attachments for {goodnotes_file}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return output_dir
        converted = kept = deleted = errors = 0
        workers = max(1, int(workers))
        with ProcessPoolExecutor(max_workers=workers) as exe:
            futures = {exe.submit(_process_single_file, t): t[1] for t in tasks}
            for fut in as_completed(futures):
                relpath = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    print(f"ERROR processing {relpath}: {e}")
                    errors += 1
                    continue
                status = r.get("status")
                if status == "converted":
                    converted += 1
                    print(f"Converted: {relpath} -> {os.path.basename(r.get('out',''))}")
                elif status in ("kept_original", "kept_original_no_ffmpeg"):
                    kept += 1
                    print(f"Kept (no conversion): {relpath} -> {os.path.basename(r.get('out',''))}")
                elif status in ("deleted_non_audio", "deleted_small"):
                    deleted += 1
                else:
                    errors += 1
                    print(f"{status} for {relpath}. Err: {r.get('err')}")
        print(f"Total for {goodnotes_file}: converted={converted}, kept={kept}, deleted={deleted}, errors={errors}")
    except zipfile.BadZipFile:
        print(f"Error: Not a valid GoodNotes file: {goodnotes_file}")
    except Exception as e:
        print(f"An error occurred while processing {goodnotes_file}: {e}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return output_dir

if __name__ == "__main__":
    files_in_dir = os.listdir()
    for file in files_in_dir:
        if file.endswith('.goodnotes') and os.path.isfile(file):
            output_dir = os.path.splitext(file)[0] + "_Extracted_Audio_Files"
            extract_voice_files(file, output_dir)
