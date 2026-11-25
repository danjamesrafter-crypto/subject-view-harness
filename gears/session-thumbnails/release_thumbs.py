#!/usr/bin/env python3
"""Release the session-thumbnails gear on a Flywheel site without Docker.

Image = the Exchange pyradiomics image (Python 3.13 + numpy + SimpleITK/GDCM) + one layer with
/flywheel/v0/{run_thumbs.py, ts_total_classmap.json, manifest.json}. Reuses the registry/release code in
build_push.py (same folder).

  python3 release_thumbs.py --check     # fetch base layers, build the layer, smoke-test with the image's own Python
  python3 release_thumbs.py --release   # same, then asks for a personal API key (hidden) and installs the gear

The key is read with getpass, checked against /api/users/self, never printed or written to disk.
"""
import argparse, getpass, gzip, hashlib, io, json, os, subprocess, sys, tarfile, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_push as bp  # noqa: E402

GEAR_DIR = os.path.join(HERE, "..", "gear")
FILES = (("run_thumbs.py", 0o755), ("ts_total_classmap.json", 0o644), ("manifest.json", 0o644))


def build_layer(man, mt, cfg_bytes, gm):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as t:
        for d in ("flywheel", "flywheel/v0"):
            ti = tarfile.TarInfo(d); ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, 0; t.addfile(ti)
        for name, mode in FILES:
            data = open(os.path.join(GEAR_DIR, name), "rb").read()
            ti = tarfile.TarInfo(f"flywheel/v0/{name}"); ti.size, ti.mode, ti.mtime = len(data), mode, 0
            t.addfile(ti, io.BytesIO(data))
    tar = raw.getvalue()
    diff_id = "sha256:" + hashlib.sha256(tar).hexdigest()
    gz = gzip.compress(tar, mtime=0)
    digest = "sha256:" + hashlib.sha256(gz).hexdigest()
    os.makedirs(bp.CACHE, exist_ok=True)
    lpath = os.path.join(bp.CACHE, digest.replace(":", "_")); open(lpath, "wb").write(gz)
    cfg = json.loads(cfg_bytes)
    cfg["rootfs"]["diff_ids"].append(diff_id)
    cfg.setdefault("history", []).append({"created": "1970-01-01T00:00:00Z", "created_by": f"{gm['name']}: COPY {' '.join(n for n, _ in FILES)} /flywheel/v0/"})
    labels = cfg.setdefault("config", {}).setdefault("Labels", {}) or {}
    labels.update({"io.flywheel.base-image": f"flywheel-pyradiomics:{bp.SRC_TAG}", "io.flywheel.gear": f"{gm['name']} {gm['version']}"})
    cfg["config"]["Labels"] = labels
    cfg["config"]["Cmd"] = None
    new_cfg = json.dumps(cfg, separators=(",", ":")).encode()
    m = json.loads(man)
    m["config"] = dict(m["config"], digest="sha256:" + hashlib.sha256(new_cfg).hexdigest(), size=len(new_cfg))
    m["layers"].append({"mediaType": bp.LAYER_MT[mt], "size": len(gz), "digest": digest})
    out = json.dumps(m, indent=3).encode()
    cdig = m["config"]["digest"]
    open(os.path.join(bp.CACHE, cdig.replace(":", "_")), "wb").write(new_cfg)
    return out, cdig, lpath, digest


def unpack_rootfs(layer_paths, R):
    shutil.rmtree(R, ignore_errors=True); os.makedirs(R)
    for p in layer_paths:
        with tarfile.open(p, "r:*") as t:
            for ti in t:
                base = os.path.basename(ti.name)
                if base.startswith(".wh."):
                    tgt = os.path.join(R, os.path.dirname(ti.name), base[4:])
                    if os.path.lexists(tgt):
                        shutil.rmtree(tgt, ignore_errors=True) if os.path.isdir(tgt) and not os.path.islink(tgt) else os.remove(tgt)
                    continue
                if ti.isdev():
                    continue
                try:
                    t.extract(ti, R, filter="tar")
                except Exception:
                    pass


def resolve_in(R, path, depth=0):
    p = os.path.join(R, path.lstrip("/"))
    if os.path.islink(p) and depth < 10:
        tgt = os.readlink(p)
        tgt = tgt if tgt.startswith("/") else os.path.normpath(os.path.join(os.path.dirname(path), tgt))
        return resolve_in(R, tgt, depth + 1)
    return path


def smoke(layer_paths):
    """Run the local test (synthetic CT/J2K/PET/MR/US + flipped mask) with the image's own Python."""
    R = os.path.join(HERE, "rootfs")
    print("Unpacking image rootfs for the smoke test…")
    unpack_rootfs(layer_paths, R)
    py = resolve_in(R, "/venv/bin/python")
    prefix = os.path.dirname(os.path.dirname(py))
    env = dict(os.environ, PYTHONHOME=os.path.join(R, prefix.lstrip("/")), PYTHONPATH=os.path.join(R, "venv/lib/python3.13/site-packages"),
               LD_LIBRARY_PATH=":".join(os.path.join(R, d) for d in ("usr/local/lib", "usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu", prefix.lstrip("/") + "/lib")))
    exe = os.path.join(R, py.lstrip("/"))
    out = os.path.join(HERE, "smoke_out")
    t = os.path.join(HERE, "..", "test", "t_gear.py")
    r = subprocess.run([exe, "-c", "import sys, numpy, SimpleITK as s; print(sys.version.split()[0], numpy.__version__, s.Version_VersionString())"], env=env, capture_output=True, text=True)
    print("  image python:", (r.stdout or r.stderr).strip()[:300])
    r = subprocess.run([exe, t, out], env=env, capture_output=True, text=True)
    tail = r.stdout[r.stdout.find("{"):] if "{" in r.stdout else r.stdout[-1500:]
    ok = r.returncode == 0 and "organ_centroid:lung_lower_lobe_right" in r.stdout and "1.2.840" not in r.stderr
    print("  smoke:", "PASS" if ok else "FAIL")
    if not ok:
        print(r.stdout[-3000:], r.stderr[-3000:])
        raise SystemExit("smoke test failed; nothing pushed")
    shutil.rmtree(R, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--no-smoke", action="store_true")
    ap.add_argument("--site", default="r6nvg8.prod.ten.flywheel.io")
    a = ap.parse_args()
    gm = json.load(open(os.path.join(GEAR_DIR, "manifest.json")))
    print(f"Gear {gm['name']} {gm['version']}")
    print("Finding the base image:")
    src, man, mt = bp.find_source()
    m = json.loads(man)
    cfg_path = src.fetch_blob(m["config"]["digest"])
    cfg_bytes = open(cfg_path, "rb").read()
    base = [(l["digest"], src.fetch_blob(l["digest"], l["size"])) for l in m["layers"]]
    ex_man, pys = bp.inspect_layers([p for _, p in base])
    if "/venv/bin/python" not in pys:
        raise SystemExit("/venv/bin/python not in the base image")
    gm["environment"] = ex_man.get("environment", gm.get("environment"))
    json.dump(gm, open(os.path.join(GEAR_DIR, "manifest.json"), "w"), indent=2)
    img_man, cdig, lpath, ldig = build_layer(man, mt, cfg_bytes, gm)
    print(f"  layer {ldig[7:19]} ({os.path.getsize(lpath)} B), config {cdig[7:19]}")
    if not a.no_smoke:
        smoke([p for _, p in base] + [lpath])
    if not a.release:
        print("Check only: nothing pushed. Re-run with --release.")
        return
    key = getpass.getpass("Paste a personal Flywheel API key for %s (input hidden): " % a.site).strip()
    st, me = bp.fw_api(a.site, key, "GET", "/users/self")
    if st != 200:
        raise SystemExit(f"Key rejected by {a.site} ({st})")
    print(f"Key OK for {me.get('email')}")
    bp.release(a.site, key, me["email"], gm, img_man, mt, base + [(ldig, lpath), (cdig, os.path.join(bp.CACHE, cdig.replace(":", "_")))])
    del key


if __name__ == "__main__":
    main()
