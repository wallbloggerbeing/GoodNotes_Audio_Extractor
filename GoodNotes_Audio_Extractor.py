#!/usr/bin/env python3
import os,zipfile,shutil,subprocess,uuid
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor,as_completed

MAGIC=65536;MIN_SIZE=10*1024;WORKERS=os.cpu_count() or 2;DFMT="mp3";DBIT="192k"

def parse_prefix(line,fmt):
    try: t=datetime.strptime(line,fmt)
    except ValueError as v:
        if v.args and v.args[0].startswith('unconverted data remains: '):
            line=line[:-(len(v.args[0])-26)]; t=datetime.strptime(line,fmt)
        else: raise
    return t.strftime('%m-%d_%H-%M')

def safe_name(p,uid,ext): return f"{p}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uid}{ext}"

def magic_ext(p):
    try:
        with open(p,'rb') as f: d=f.read(MAGIC)
    except: return None
    if len(d)>=12 and d[4:8]==b'ftyp': u=d.upper(); return '.m4a' if b'M4A' in u else '.mp4'
    if d.startswith(b'RIFF') and b'WAVE' in d[8:12]: return '.wav'
    if d.startswith(b'OggS'): return '.ogg'
    if d.startswith(b'caff'): return '.caf'
    if d.startswith(b'ID3'): return '.mp3'
    if len(d)>=2 and d[0]==0xFF and (d[1]&0xE0)==0xE0: return '.mp3'
    if len(d)>=2 and d[0]==0xFF and (d[1]&0xF6)==0xF0: return '.aac'
    if d.startswith(b'\xff\xd8') or d.startswith(b'\xff\xd9') or d.startswith(b'\xff\xe0'):
        return '.jpg'
    return None

def ffprobe_has_audio(p):
    try:
        r=subprocess.run(["ffprobe","-v","error","-select_streams","a","-show_entries","stream=index","-of","csv=p=0",p],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        return bool(r.stdout.strip())
    except FileNotFoundError:
        return False

def convert_ffmpeg(src,dst,fmt,bitrate=DBIT,sr=None):
    cmd=["ffmpeg","-y","-hide_banner","-loglevel","error","-nostdin","-i",src,"-vn"]
    if sr: cmd += ["-ar",str(sr)]
    if fmt=="mp3": cmd += ["-ac","2","-b:a",bitrate,"-f","mp3",dst]
    elif fmt=="wav": cmd += ["-ac","2","-c:a","pcm_s16le","-f","wav",dst]
    else: raise ValueError(fmt)
    return subprocess.run(cmd).returncode==0

def _proc(args):
    src,rel,outd,fmt,bitrate,sr,minsz,ffmpeg_ok,ffprobe_ok = args
    r={"src":src,"rel":rel,"status":None,"out":None,"err":None}
    try:
        if os.path.getsize(src) < minsz:
            os.remove(src); r['status']='deleted_small'; return r
        g=magic_ext(src); is_audio=False
        if g and g.lower() in ('.m4a','.mp3','.wav','.aac','.ogg','.caf'): is_audio=True
        if g and g.lower()=='.mp4': is_audio = ffprobe_has_audio(src) if ffprobe_ok else (not ffmpeg_ok or False)
        if g is None and ffprobe_ok: is_audio = ffprobe_has_audio(src)
        if not is_audio:
            os.remove(src); r['status']='deleted_non_audio'; return r
        uid=uuid.uuid4().hex[:8]
        if ffmpeg_ok and fmt in ('mp3','wav'):
            out_ext = '.mp3' if fmt=='mp3' else '.wav'
            out = os.path.join(outd, safe_name('Audio',uid,out_ext))
            if convert_ffmpeg(src,out,fmt,bitrate,sr):
                try: os.remove(src)
                except: pass
                r['status']='converted'; r['out']=out
            else:
                fe = g or '.mp4'; fn = os.path.join(outd, safe_name('Attachment',uid,fe)); shutil.move(src,fn); r['status']='kept_original'; r['out']=fn
        else:
            fe = g or '.mp4'; fn = os.path.join(outd, safe_name('Attachment',uid,fe)); shutil.move(src,fn); r['status']='kept_original_no_ffmpeg'; r['out']=fn
    except Exception as e:
        r['status']='error'; r['err']=str(e)
    return r

def extract_voice_files(gf,outd,fmt=DFMT,minsz=MIN_SIZE,bitrate=DBIT,sr=None,workers=WORKERS):
    os.makedirs(outd,exist_ok=True); td=os.path.join(outd,'temp'); shutil.rmtree(td,ignore_errors=True)
    ffmpeg_ok=shutil.which('ffmpeg') is not None; ffprobe_ok=shutil.which('ffprobe') is not None
    try:
        with zipfile.ZipFile(gf) as z: z.extractall(td)
        ads=None
        for r,dirs,files in os.walk(td):
            for d in dirs:
                if d.lower()=='attachments': ads=os.path.join(r,d); break
            if ads: break
        if not ads: print(f"No attachments found in {gf}"); return outd
        tasks=[]
        for r,_,files in os.walk(ads):
            for f in files: tasks.append((os.path.join(r,f), os.path.relpath(os.path.join(r,f),td), outd, fmt, bitrate, sr, minsz, ffmpeg_ok, ffprobe_ok))
        if not tasks: print(f"No files in attachments for {gf}"); return outd
        c=k=d=e=0
        with ProcessPoolExecutor(max_workers=max(1,int(workers))) as ex:
            futures={ex.submit(_proc,t):t[1] for t in tasks}
            for fut in as_completed(futures):
                rel=futures[fut]
                try: res=fut.result()
                except Exception as exv: print('ERROR',rel,exv); e+=1; continue
                s=res.get('status')
                if s=='converted': c+=1; print(f"Converted: {rel} -> {os.path.basename(res.get('out',''))}")
                elif s in ('kept_original','kept_original_no_ffmpeg'): k+=1; print(f"Kept: {rel} -> {os.path.basename(res.get('out',''))}")
                elif s in ('deleted_non_audio','deleted_small'): d+=1
                else: e+=1; print(s,rel,res.get('err'))
        print(f"Total for {gf}: converted={c}, kept={k}, deleted={d}, errors={e}")
    finally:
        shutil.rmtree(td,ignore_errors=True)
    return outd

if __name__=='__main__':
    for f in [x for x in os.listdir() if x.endswith('.goodnotes') and os.path.isfile(x)]:
        extract_voice_files(f, os.path.splitext(f)[0] + '_Extracted_Audio_Files')
