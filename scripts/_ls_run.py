import json, sys
sys.path.insert(0, r'd:\VideoStudio')
import httpx
from pathlib import Path
import comfy
pid = "f0c9d345-dcc7-4607-b407-dbfe520e394c"
ok, history = comfy.wait_done(pid, timeout=5400)
log = open(r'd:\VideoStudio\output\_ls_run.log', 'a', encoding='utf-8')
if ok:
    v = comfy.find_video(history)
    if v:
        comfy.download_video(v, Path(r"d:\VideoStudio\output\_kangbo3_segs\latentsync_probe.mp4"))
        print("saved latentsync_probe.mp4", file=log, flush=True)
    else:
        print("no video in history", file=log, flush=True)
else:
    for m in history.get("status", {}).get("messages", []):
        if m[0] == "execution_error":
            print(json.dumps(m[1], indent=1)[:600], file=log, flush=True)
