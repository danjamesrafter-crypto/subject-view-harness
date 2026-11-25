"""End-to-end main() against a fake Flywheel REST API on localhost. Run after t_gear.py (reuses its fixtures).
Usage: thv/bin/python t_main.py FIXDIR(th1/fx) OUT"""
import json, os, sys, threading, http.server, urllib.parse, shutil
from pathlib import Path
FX, OUT = Path(sys.argv[1]), Path(sys.argv[2])
SERIES = os.environ.get("SERIES") == "1"
shutil.rmtree(OUT, ignore_errors=True); (OUT / "output").mkdir(parents=True)
seen = []
PROFILE = json.dumps({"version": 1, "name": "project test", "rules": [{"match": {"modality": "CT"}, "views": [{"name": "key", "plane": "coronal", "pick": [{"organ_centroid": {"organ": "liver"}}, "middle"]}]}]})
ACQ = [
    {"_id": "a1", "label": "CT_Soft", "files": [{"name": "CT_Soft.dicom.zip", "type": "dicom", "modality": "CT", "file_id": "f1", "size": 1, "modified": "x", "classification": {}},
                                               {"name": "CT_Soft.nii.gz", "type": "nifti", "origin": {"type": "job"}, "file_id": "f1n", "size": 1}]},
    {"_id": "a2", "label": "PET", "files": [{"name": "PET.dicom.zip", "type": "dicom", "modality": "PT", "file_id": "f2", "size": 1}]},
    {"_id": "a3", "label": "notes", "files": [{"name": "notes.pdf", "type": "pdf"}]},
]
FILES = {"/api/acquisitions/a1/files/CT_Soft.dicom.zip": FX / "CT_Soft.dicom.zip", "/api/acquisitions/a2/files/PET.dicom.zip": FX / "PET.dicom.zip",
         "/api/analyses/ts1/files/high_res_total_combined_seg_output.nii.gz": FX / "ts_mask.nii.gz"}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path); seen.append(p)
        if self.headers.get("Authorization") != "scitran-user SECRET":
            self.send_response(401); self.end_headers(); return
        body = None
        if p == "/api/analyses/an1": body = {"_id": "an1", "parent": {"type": "acquisition", "id": "a1"} if SERIES else {"type": "session", "id": "s1"}, "parents": {"session": "s1", "acquisition": "a1" if SERIES else None}}
        elif p == "/api/acquisitions/a1": body = ACQ[0]
        elif p == "/api/sessions/s1": body = {"_id": "s1", "label": "Baseline", "parents": {"project": "p1"}}
        elif p == "/api/sessions/s1/acquisitions": body = ACQ
        elif p == "/api/projects/p1": body = {"_id": "p1", "files": [{"name": "thumbnails.json", "modified": "2026-11-25"}]}
        elif p == "/api/projects/p1/files/thumbnails.json":
            self.send_response(200); self.end_headers(); self.wfile.write(PROFILE.encode()); return
        elif p == "/api/sessions/s1/analyses": body = [{"_id": "ts1", "created": "2026-11-24", "files": [{"name": "high_res_total_combined_seg_output.nii.gz"}]}]
        elif p.startswith("/api/acquisitions/") and p.endswith("/analyses"): body = []
        elif p in FILES:
            self.send_response(200); self.end_headers(); self.wfile.write(FILES[p].read_bytes()); return
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(body).encode())


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
(OUT / "config.json").write_text(json.dumps({"config": {"size": 128}, "destination": {"type": "analysis", "id": "an1"},
                                             "inputs": {"api-key": {"base": "api-key", "key": f"http://127.0.0.1:{port}:SECRET"},
                                                        **({"series": {"base": "file", "hierarchy": {"type": "acquisition", "id": "a1"}, "object": {"name": "CT_Soft.dicom.zip"},
                                                                       "location": {"name": "CT_Soft.dicom.zip", "path": str(FX / "CT_Soft.dicom.zip")}}} if SERIES else {})}}))
os.environ["FW_ROOT"] = str(OUT)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gear"))
import run_thumbs as rt
rt.FW = OUT
rt.main()
idx = json.loads((OUT / "output" / "thumbs.json").read_text())
print(json.dumps({"items": [(i["acquisition_label"], i["file_name"], i["plane"], i["picker"], i["slice"], i["sprite"]["cell"]) for i in idx["items"]],
                  "skipped": [(s["label"], s["reason"]) for s in idx["skipped"]], "profile": idx["profile"]["source"] + " " + str(idx["profile"]["file"]),
                  "sprite": idx["sprite"], "scope": idx.get("scope"), "downloads": [p for p in seen if "/files/" in p]}, indent=1))
