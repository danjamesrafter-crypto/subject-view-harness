#!/usr/bin/env python3
"""session-thumbnails: render load-optimised thumbnails for every imaging series in a Flywheel session.

Runs as an analysis gear on a session. For each acquisition it reads the DICOM series (any transfer syntax GDCM
decodes) or NIfTI volume, picks a slice per the thumbnail profile, windows it, and packs every thumbnail into one
JPEG sprite with a JSON index (schema fw.thumbnails/1) that apps read in two requests instead of one download per
series.

Profile (what to render): gear input `profile`, else a project file named thumbnails.json / thumbnails.yaml, else
the built-in default (middle slice of every volume, axial). Rules match on modality / protocol / classification /
label; the first matching rule's views are rendered. Each view lists pickers tried in order; the index records
which one was used and why earlier ones were skipped.

Pickers in 0.1.0: middle, fraction, index, mip, organ_centroid (TotalSegmentator mask on the session).
Declared but not yet implemented (fall through to the next picker): lesion_max_diameter, key_image.

Dependencies: Python stdlib, numpy, SimpleITK (GDCM). Talks to the Flywheel REST API with the gear's api-key input.
Thumbnails are previews for navigation, never measurements.
"""
import fnmatch, hashlib, io, json, math, os, re, shutil, sys, tempfile, time, traceback, urllib.parse, urllib.request, zipfile
from pathlib import Path

import numpy as np
import SimpleITK as sitk

GEAR = "session-thumbnails"
VERSION = "0.2.0"
SCHEMA = "fw.thumbnails/1"
FW = Path(os.environ.get("FW_ROOT", "/flywheel/v0"))
HERE = Path(__file__).resolve().parent

WINDOWS = {  # name: (center, width); None = percentile auto
    "soft_tissue": (40, 400), "lung": (-600, 1500), "bone": (400, 1800), "brain": (40, 80), "liver": (60, 160),
    "mediastinum": (50, 350), "auto": None,
}
DEFAULT_PROFILE = {
    "version": 1,
    "name": "built-in default",
    "defaults": {"views": [{"name": "key", "plane": "axial", "pick": ["middle"]}]},
    "rules": [
        {"match": {"modality": "CT"}, "views": [{"name": "key", "plane": "axial", "pick": ["middle"], "window": "soft_tissue"}]},
        {"match": {"modality": "PT"}, "views": [{"name": "key", "plane": "coronal", "pick": ["mip", "middle"], "window": "auto", "invert": True}]},
    ],
}
SKIP_MOD = {"SR", "KO", "PR", "RTSTRUCT", "RTPLAN", "RTDOSE", "SEG", "REG", "DOC"}  # not images
BURNIN_MOD = {"US", "SC", "XC", "ES"}  # pixel data may carry identifiers -> skipped unless the profile allows it


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- Flywheel REST (stdlib)
class FW_API:
    def __init__(self, key):
        parts = key.split(":")  # site[:port]:secret, as the Flywheel SDK parses it
        if len(parts) < 2:
            raise SystemExit("api-key input is not in site:key form")
        host = ":".join(parts[:-1]).rstrip("/")
        host = re.sub(r"/api$", "", host)
        self.base = host if host.startswith("http") else "https://" + host
        self.hdr = {"Authorization": "scitran-user " + parts[-1]}

    def get(self, path, params=None, raw=False):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=self.hdr), timeout=600) as r:
                    return r if raw else json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 3:
                    raise
            except urllib.error.URLError:
                if attempt == 3:
                    raise
            time.sleep(2 * (attempt + 1))

    def download(self, path, dest):
        url = self.base + path
        with urllib.request.urlopen(urllib.request.Request(url, headers=self.hdr), timeout=1800) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        return dest


def file_path(parent_type, parent_id, name):
    plural = {"analysis": "analyses"}.get(parent_type, parent_type + "s")
    return f"/api/{plural}/{parent_id}/files/{urllib.parse.quote(name, safe='')}"


# ---------------------------------------------------------------- profile
def load_text_profile(text, name):
    t = text.strip()
    if t.startswith("{"):
        return json.loads(t)
    try:
        import yaml  # optional; JSON always works
    except ImportError:
        raise ValueError(f"{name}: YAML profile but PyYAML is not in this image; use thumbnails.json")
    return yaml.safe_load(t)


def resolve_profile(api, project_id, input_path):
    """-> (profile dict, provenance dict)."""
    if input_path:
        txt = Path(input_path).read_text()
        return load_text_profile(txt, input_path), {"source": "gear input", "file": Path(input_path).name, "sha256": hashlib.sha256(txt.encode()).hexdigest()}
    if api and project_id:
        try:
            proj = api.get(f"/api/projects/{project_id}")
            for f in proj.get("files", []):
                if f.get("name") in ("thumbnails.json", "thumbnails.yaml", "thumbnails.yml"):
                    tmp = Path(tempfile.mkdtemp()) / f["name"]
                    api.download(file_path("project", project_id, f["name"]), tmp)
                    txt = tmp.read_text()
                    return load_text_profile(txt, f["name"]), {"source": "project file", "file": f["name"], "sha256": hashlib.sha256(txt.encode()).hexdigest(), "modified": f.get("modified")}
        except Exception as e:  # a broken profile must not silently fall back: fail loudly
            if isinstance(e, ValueError):
                raise
            log("could not read project files:", e)
    txt = json.dumps(DEFAULT_PROFILE, sort_keys=True)
    return DEFAULT_PROFILE, {"source": "built-in", "file": None, "sha256": hashlib.sha256(txt.encode()).hexdigest()}


def validate_profile(p):
    errs = []
    if not isinstance(p, dict):
        return ["profile is not an object"]
    for i, r in enumerate(p.get("rules", [])):
        if not isinstance(r.get("match"), dict):
            errs.append(f"rules[{i}].match missing")
        for j, v in enumerate(r.get("views", [])):
            errs += [f"rules[{i}].views[{j}]: {e}" for e in validate_view(v)]
    for j, v in enumerate((p.get("defaults") or {}).get("views", [])):
        errs += [f"defaults.views[{j}]: {e}" for e in validate_view(v)]
    return errs


PICKERS = {"middle", "fraction", "index", "mip", "organ_centroid", "lesion_max_diameter", "key_image"}


def validate_view(v):
    e = []
    if v.get("plane", "axial") not in ("axial", "coronal", "sagittal"):
        e.append(f"plane {v.get('plane')!r}")
    w = v.get("window", "auto")
    if not (w in WINDOWS or (isinstance(w, (list, tuple)) and len(w) == 2)):
        e.append(f"window {w!r}")
    for p in v.get("pick", ["middle"]):
        n = p if isinstance(p, str) else (next(iter(p)) if isinstance(p, dict) and len(p) == 1 else None)
        if n not in PICKERS:
            e.append(f"unknown picker {p!r}")
    return e


def _glob(pat, val):
    if pat is None:
        return True
    pats = pat if isinstance(pat, list) else [pat]
    v = str(val or "")
    return any(fnmatch.fnmatchcase(v.lower(), str(x).lower()) for x in pats)


def match_rule(m, s):
    """m: rule.match; s: series facts {modality, protocol, label, classification(list of 'Key:Value')}."""
    if not _glob(m.get("modality"), s["modality"]):
        return False
    if "protocol" in m and not _glob(m["protocol"], s["protocol"]):
        return False
    if "label" in m and not _glob(m["label"], s["label"]):
        return False
    if "classification" in m:
        need = m["classification"] if isinstance(m["classification"], list) else [m["classification"]]
        have = [c.lower() for c in s["classification"]]
        if not all(any(fnmatch.fnmatchcase(h, str(n).lower()) for h in have) for n in need):
            return False
    return True


def views_for(profile, s):
    """Project rules, then the project's defaults, then the built-in rules (unless inherit_builtin is false),
    then the built-in default (middle slice). -> (views, matched rule or label of the layer used)."""
    for r in profile.get("rules", []):
        if match_rule(r["match"], s):
            return r.get("views") or [], r.get("match")
    if (profile.get("defaults") or {}).get("views") is not None and profile is not DEFAULT_PROFILE:
        return profile["defaults"]["views"], "profile defaults"
    if profile.get("inherit_builtin", True) and profile is not DEFAULT_PROFILE:
        for r in DEFAULT_PROFILE["rules"]:
            if match_rule(r["match"], s):
                return r["views"], {"builtin": r["match"]}
    return DEFAULT_PROFILE["defaults"]["views"], "built-in default"


# ---------------------------------------------------------------- reading volumes
def read_dicom_zip(path, work):
    d = Path(work) / "dcm"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    with zipfile.ZipFile(path) as z:
        for i, m in enumerate(z.infolist()):
            if m.is_dir() or m.file_size == 0:
                continue
            base = os.path.basename(m.filename)
            if base.lower() in ("dicomdir",) or base.lower().endswith((".txt", ".json", ".xml")):
                continue
            with z.open(m) as src, open(d / f"{i:06d}_{re.sub(r'[^A-Za-z0-9._-]', '_', base)}", "wb") as dst:
                shutil.copyfileobj(src, dst)
    return read_dicom_dir(d)


def read_dicom_dir(d):
    r = sitk.ImageSeriesReader()
    ids = r.GetGDCMSeriesIDs(str(d))
    if not ids:
        raise ValueError("no DICOM series found")
    best = max(ids, key=lambda i: len(r.GetGDCMSeriesFileNames(str(d), i)))
    files = r.GetGDCMSeriesFileNames(str(d), best)
    first = sitk.ImageFileReader(); first.SetFileName(files[0]); first.LoadPrivateTagsOff(); first.ReadImageInformation()
    tags = {k: first.GetMetaData(k).strip() for k in first.GetMetaDataKeys()}
    if len(files) == 1:
        img = sitk.ReadImage(files[0])
    else:
        r.SetFileNames(files)
        img = r.Execute()
    return img, tags, len(files)


def read_volume(path, kind, work):
    if kind == "dicom-zip":
        return read_dicom_zip(path, work)
    if kind == "dicom":
        d = Path(work) / "one"; d.mkdir(parents=True, exist_ok=True); shutil.copy(path, d / "a.dcm")
        return read_dicom_dir(d)
    img = sitk.ReadImage(str(path))
    return img, {}, 1


def to_display_array(img):
    """Reorient to LPS and return (array[z,y,x] float32, spacing (sx,sy,sz)). Multi-component -> first component;
    4D -> first volume."""
    if img.GetDimension() == 4:
        size = list(img.GetSize()); size[3] = 0
        img = sitk.Extract(img, size, [0, 0, 0, 0])
    if img.GetNumberOfComponentsPerPixel() > 1:
        if img.GetNumberOfComponentsPerPixel() in (3, 4) and img.GetDimension() == 2:
            return np.asarray(sitk.GetArrayFromImage(img))[None, ..., :3], (img.GetSpacing() + (1.0,)), True
        img = sitk.VectorIndexSelectionCast(img, 0)
    if img.GetDimension() == 2:
        img = sitk.JoinSeries(img)
    try:
        img = sitk.DICOMOrient(img, "LPS")
    except Exception:
        pass
    return sitk.GetArrayFromImage(img).astype(np.float32), img.GetSpacing(), False


# ---------------------------------------------------------------- pickers
class PickUnavailable(Exception):
    pass


AXIS = {"axial": 0, "coronal": 1, "sagittal": 2}


def pick_slice(arr, plane, picker, ctx):
    """-> (2D array, slice index or None, label). arr is [z,y,x] in LPS."""
    name, opt = (picker, {}) if isinstance(picker, str) else next(iter(picker.items()))
    opt = opt or {}
    ax = AXIS[plane]
    n = arr.shape[ax]
    if name == "middle":
        k = n // 2
    elif name == "fraction":
        f = float(opt.get("at", opt.get("value", 0.5)) if isinstance(opt, dict) else opt)
        k = min(n - 1, max(0, int(round(f * (n - 1)))))
    elif name == "index":
        k = int(opt.get("at", 0) if isinstance(opt, dict) else opt)
        if not 0 <= k < n:
            raise PickUnavailable(f"index {k} outside 0..{n - 1}")
    elif name == "mip":
        if n < 2:
            raise PickUnavailable("single slice: no projection")
        return arr.max(axis=ax), None, "mip"
    elif name == "organ_centroid":
        organ = opt.get("organ") if isinstance(opt, dict) else opt
        c = ctx.get("organ_centroid")
        if c is None:
            raise PickUnavailable("no TotalSegmentator mask on this session")
        idx = c(organ)  # -> (x, y, z) index in this volume's LPS array or None
        if idx is None:
            raise PickUnavailable(f"{organ} not in the mask or outside this series")
        k = {0: idx[2], 1: idx[1], 2: idx[0]}[ax]  # idx is (x, y, z); array axes are (z, y, x)
        if not 0 <= k < n:
            raise PickUnavailable(f"{organ} centroid outside this series")
        name = f"organ_centroid:{organ}"
    elif name in ("lesion_max_diameter", "key_image"):
        raise PickUnavailable(f"{name} is not implemented in {VERSION}")
    else:
        raise PickUnavailable(f"unknown picker {name}")
    sl = np.take(arr, k, axis=ax)
    return sl, int(k), name


def orient_plane(sl, plane, spacing):
    """Display convention: axial as seen from the feet (patient right on screen left, anterior up); coronal and
    sagittal with superior up. Returns (2D, row_spacing, col_spacing)."""
    sx, sy, sz = spacing
    if plane == "axial":
        return sl, sy, sx
    if plane == "coronal":  # rows z (inferior->superior in LPS) -> flip so superior is up; cols x (R->L)
        return sl[::-1, :], sz, sx
    return sl[::-1, :], sz, sy  # sagittal: rows z flipped, cols y (anterior -> posterior)


def window(sl, w, modality, invert=False):
    a = sl.astype(np.float32)
    spec = WINDOWS.get(w, None) if isinstance(w, str) else tuple(w)
    if spec is None or (modality not in ("CT",) and isinstance(w, str) and w != "auto" and w in WINDOWS):
        fin = a[np.isfinite(a)]
        if fin.size == 0:
            raise PickUnavailable("empty slice")
        smp = fin if fin.size < 200000 else fin[:: max(1, fin.size // 200000)]
        lo, hi = np.percentile(smp, [1, 99.5])
        if hi <= lo:
            hi = lo + 1
        used = ("auto", float((lo + hi) / 2), float(hi - lo))
    else:
        c, wd = spec
        lo, hi = c - wd / 2, c + wd / 2
        used = (w if isinstance(w, str) else "custom", float(c), float(wd))
    y = np.clip((a - lo) / (hi - lo), 0, 1)
    if invert:
        y = 1 - y
    return (y * 255 + 0.5).astype(np.uint8), used


def resize_to_box(img, row_sp, col_sp, box):
    """Resample a 2D uint8 (or HxWx3) image to physical aspect and fit inside box x box (bilinear via SimpleITK)."""
    h, w = img.shape[:2]
    ph, pw = h * row_sp, w * col_sp
    s = box / max(ph, pw)
    nh, nw = max(1, int(round(ph * s))), max(1, int(round(pw * s)))
    def rs(ch):
        im = sitk.GetImageFromArray(ch.astype(np.float32))
        im.SetSpacing((col_sp, row_sp))
        out = sitk.Resample(im, [nw, nh], sitk.Transform(), sitk.sitkLinear, (0.0, 0.0), (pw / nw, ph / nh), (1, 0, 0, 1), 0.0)
        return np.clip(sitk.GetArrayFromImage(out), 0, 255).astype(np.uint8)
    if img.ndim == 3:
        return np.stack([rs(img[..., c]) for c in range(3)], -1)
    return rs(img)


# ---------------------------------------------------------------- organ centroids from a TotalSegmentator mask
TS_NAMES = None


def ts_names():
    global TS_NAMES
    if TS_NAMES is None:
        p = HERE / "ts_total_classmap.json"
        TS_NAMES = {v: int(k) for k, v in json.loads(p.read_text()).items()} if p.exists() else {}
    return TS_NAMES


def make_centroid_fn(mask_path, img):
    """Returns organ -> (x,y,z) index into img's LPS-oriented array, computed in physical space so a mask stored
    on a flipped or resampled grid still lands on the right slice."""
    m = sitk.ReadImage(str(mask_path))
    arr = sitk.GetArrayViewFromImage(m)
    lps = sitk.DICOMOrient(img, "LPS") if img.GetDimension() == 3 else img
    cache = {}

    def fn(organ):
        lab = ts_names().get(organ) if not str(organ).isdigit() else int(organ)
        if lab is None:
            return None
        if lab not in cache:
            zz, yy, xx = np.nonzero(arr == lab)
            if zz.size == 0:
                cache[lab] = None
            else:
                p = m.TransformContinuousIndexToPhysicalPoint((float(xx.mean()), float(yy.mean()), float(zz.mean())))
                try:
                    i = lps.TransformPhysicalPointToIndex(p)
                except Exception:
                    i = None
                cache[lab] = i
        return cache[lab]
    return fn


# ---------------------------------------------------------------- session
def series_facts(acq, f):
    info = (f.get("info") or {})
    h = (info.get("header") or {}).get("dicom") or info
    cls = []
    for k, v in (f.get("classification") or {}).items():
        for x in (v if isinstance(v, list) else [v]):
            cls.append(f"{k}:{x}")
    return {
        "modality": (f.get("modality") or h.get("Modality") or "").upper() or ("MR" if "mr" in (f.get("type") or "") else ""),
        "protocol": h.get("SeriesDescription") or h.get("ProtocolName") or acq.get("label") or "",
        "label": acq.get("label") or "",
        "classification": cls,
        "image_type": h.get("ImageType") or [],
        "burned_in": str(h.get("BurnedInAnnotation", "")).upper() == "YES",
    }


def source_file(acq):
    if acq.get("_only"):  # triggered on one file (gear rule): render that file only
        f = next((x for x in acq.get("files", []) if x["name"] == acq["_only"]), None)
        if not f:
            return None, None
        nm = f["name"].lower()
        if f.get("type") == "dicom" or nm.endswith(".dcm") or nm.endswith(".dicom.zip"):
            return f, "dicom-zip" if nm.endswith(".zip") or f.get("zip_member_count") else "dicom"
        if f.get("type") == "nifti" or nm.endswith((".nii", ".nii.gz")):
            return f, "nifti"
        return None, None
    for f in acq.get("files", []):
        if f.get("type") == "dicom":
            return f, "dicom-zip" if f["name"].lower().endswith(".zip") or f.get("zip_member_count") else "dicom"
    nii = [f for f in acq.get("files", []) if f["name"].lower().endswith((".nii", ".nii.gz")) or f.get("type") == "nifti"]
    for f in sorted(nii, key=lambda f: (f.get("origin") or {}).get("type") == "job"):  # uploaded before gear-made
            return f, "nifti"
    return None, None


def fingerprint(f, view, prof_sha):
    basis = json.dumps({"file": f.get("file_id") or f.get("_id"), "hash": f.get("hash"), "modified": f.get("modified"), "size": f.get("size"), "view": view, "profile": prof_sha, "gear": VERSION}, sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()[:20]


def find_ts_mask(api, session_id, acq_ids):
    """Newest TotalSegmentator combined mask on the session or its acquisitions -> (container type, id, file)."""
    cands = []
    for ctype, cid in [("session", session_id)] + [("acquisition", a) for a in acq_ids]:
        try:
            for an in api.get(f"/api/{ctype}s/{cid}/analyses") or []:
                for f in an.get("files", []) or []:
                    if f.get("name") == "high_res_total_combined_seg_output.nii.gz":
                        cands.append((an.get("created", ""), an["_id"], f))
        except Exception:
            pass
    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0]


def compose_sprite(cells, box, cols):
    n = len(cells)
    rows = max(1, math.ceil(n / cols))
    sheet = np.zeros((rows * box, cols * box, 3), np.uint8)
    rects = []
    for i, im in enumerate(cells):
        r, c = divmod(i, cols)
        h, w = im.shape[:2]
        y0, x0 = r * box + (box - h) // 2, c * box + (box - w) // 2
        sheet[y0:y0 + h, x0:x0 + w] = im if im.ndim == 3 else im[..., None]
        rects.append({"cell": i, "x": c * box, "y": r * box, "w": box, "h": box, "content": [x0, y0, w, h]})
    return sheet, rects


def write_jpeg(arr, path, quality=85):
    img = sitk.GetImageFromArray(arr, isVector=True)
    w = sitk.ImageFileWriter(); w.SetFileName(str(path)); w.SetUseCompression(True); w.SetCompressionLevel(quality); w.Execute(img)


def run(cfg, outdir, workdir, api=None, session=None, acqs=None, input_profile=None):
    c = cfg.get("config", {})
    box = int(c.get("size", 256))
    cols = int(c.get("sprite_columns", 8))
    max_mb = float(c.get("max_file_mb", 2048))
    allow_burnin = bool(c.get("allow_burned_in_modalities", False))
    t0 = time.time()
    project_id = (session or {}).get("parents", {}).get("project") or (session or {}).get("project")
    profile, prov = resolve_profile(api, project_id, input_profile)
    errs = validate_profile(profile)
    if errs:
        raise SystemExit("thumbnail profile is invalid:\n  " + "\n  ".join(errs))
    prov["name"] = profile.get("name")
    log(f"profile: {prov['source']} {prov.get('file') or ''} sha256 {prov['sha256'][:12]}")

    ts = None
    if api and session and any("organ_centroid" in json.dumps(v) for r in profile.get("rules", []) for v in r.get("views", [])) | ("organ_centroid" in json.dumps(profile.get("defaults", {}))):
        ts = find_ts_mask(api, session["_id"], [a["_id"] for a in acqs])
        log("TotalSegmentator mask:", "found" if ts else "none")
    ts_local = None

    items, skipped, cells = [], [], []
    for acq in acqs:
        f, kind = source_file(acq)
        if not f:
            skipped.append({"acquisition_id": acq["_id"], "label": acq.get("label"), "reason": "no DICOM or NIfTI file"}); continue
        s = series_facts(acq, f)
        if s["modality"] in SKIP_MOD:
            skipped.append({"acquisition_id": acq["_id"], "label": acq.get("label"), "file": f["name"], "reason": f"{s['modality']} is not an image"}); continue
        if not allow_burnin and (s["modality"] in BURNIN_MOD or s["burned_in"]):
            skipped.append({"acquisition_id": acq["_id"], "label": acq.get("label"), "file": f["name"], "reason": "may contain burned-in identifiers"}); continue
        if (f.get("size") or 0) > max_mb * 1e6:
            skipped.append({"acquisition_id": acq["_id"], "label": acq.get("label"), "file": f["name"], "reason": f"larger than {max_mb:.0f} MB"}); continue
        views, matched = views_for(profile, s)
        if not views:
            skipped.append({"acquisition_id": acq["_id"], "label": acq.get("label"), "file": f["name"], "reason": "profile renders no views for this series"}); continue
        local = Path(workdir) / "src" / re.sub(r"[^A-Za-z0-9._-]", "_", f["name"])
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            if acq.get("_local"):
                local = Path(acq["_local"])
            else:
                api.download(file_path("acquisition", acq["_id"], f["name"]), local)
            img, tags, nfiles = read_volume(local, kind, workdir)
            if tags:
                it = tags.get("0008|0008", "").upper()
                if not allow_burnin and ("LABEL" in it or "OVERVIEW" in it or tags.get("0028|0301", "").upper() == "YES"):
                    raise PickUnavailable("label/overview image or burned-in annotation")
                s["modality"] = s["modality"] or tags.get("0008|0060", "").upper()
            arr, spacing, rgb = to_display_array(img)
        except Exception as e:
            skipped.append({"acquisition_id": acq["_id"], "label": acq.get("label"), "file": f["name"], "reason": f"could not read: {str(e)[:160]}"})
            log("skip", acq.get("label"), e); continue
        finally:
            if not acq.get("_local") and local.exists():
                local.unlink()
        ctx = {}
        if ts:
            try:
                if ts_local is None:
                    ts_local = Path(workdir) / "ts_mask.nii.gz"
                    api.download(file_path("analysis", ts[1], ts[2]["name"]), ts_local)
                ctx["organ_centroid"] = make_centroid_fn(ts_local, img)
            except Exception as e:
                log("mask unusable:", e)
        for v in views:
            plane = v.get("plane", "axial")
            if rgb:
                plane = "axial"
            tried, out = [], None
            for p in v.get("pick", ["middle"]):
                try:
                    sl, k, used = (arr[0], 0, "rgb") if rgb else pick_slice(arr, plane, p, ctx)
                    out = (sl, k, used); break
                except PickUnavailable as e:
                    tried.append({"picker": p if isinstance(p, str) else next(iter(p)), "reason": str(e)})
            if out is None:
                try:
                    sl, k, used = pick_slice(arr, plane, "middle", ctx); out = (sl, k, used)
                    tried.append({"picker": "middle", "reason": "fallback"})
                except Exception as e:
                    skipped.append({"acquisition_id": acq["_id"], "label": acq.get("label"), "view": v.get("name"), "reason": str(e)}); continue
            sl, k, used = out
            row_sp, col_sp = 1.0, 1.0
            if not rgb:
                sl, row_sp, col_sp = orient_plane(sl, plane, spacing)
                img8, win = window(sl, v.get("window", "soft_tissue" if s["modality"] == "CT" else "auto"), s["modality"], bool(v.get("invert")))
            else:
                img8, win = sl.astype(np.uint8), ("rgb", None, None)
            cell = resize_to_box(img8, row_sp, col_sp, int(v.get("size", box)) if int(v.get("size", box)) <= box else box)
            cells.append(cell)
            n_along = arr.shape[AXIS[plane]] if not rgb else 1
            items.append({
                "acquisition_id": acq["_id"], "acquisition_label": acq.get("label"),
                "file_id": f.get("file_id") or f.get("_id"), "file_name": f["name"], "file_modified": f.get("modified"), "file_hash": f.get("hash"),
                "modality": s["modality"], "series_description": s["protocol"], "instances": nfiles,
                "view": v.get("name", "key"), "plane": plane, "picker": used, "pickers_skipped": tried,
                "slice": k, "slices": int(n_along), "window": {"name": win[0], "center": win[1], "width": win[2]}, "inverted": bool(v.get("invert")),
                "rule": matched, "fingerprint": fingerprint(f, v, prov["sha256"]),
                "pixel_spacing_mm": [round(float(row_sp), 4), round(float(col_sp), 4)],
            })
            log(f"  {acq.get('label')}: {v.get('name', 'key')} {plane} {used} slice {k}/{n_along}")
    outdir = Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    sprite = None
    if cells:
        sheet, rects = compose_sprite(cells, box, cols)
        write_jpeg(sheet, outdir / "thumbs.jpg", int(cfg.get("config", {}).get("jpeg_quality", 85)))
        for it, r in zip(items, rects):
            it["sprite"] = r
        sprite = {"file": "thumbs.jpg", "format": "jpeg", "cell": box, "columns": cols, "width": int(sheet.shape[1]), "height": int(sheet.shape[0]), "bytes": (outdir / "thumbs.jpg").stat().st_size}
    index = {
        "schema": SCHEMA,
        "gear": {"name": GEAR, "version": VERSION, "url": "https://github.com/danjamesrafter-crypto/subject-view-harness/tree/main/gears/session-thumbnails"},
        "session_id": (session or {}).get("_id"), "session_label": (session or {}).get("label"),
        "scope": "acquisition" if any(a.get("_only") for a in (acqs or [])) else "session",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "runtime_s": round(time.time() - t0, 1),
        "profile": prov, "sprite": sprite, "items": items, "skipped": skipped,
        "note": "Previews for navigation only, not measurements. Slices are not registered across sessions.",
    }
    (outdir / "thumbs.json").write_text(json.dumps(index, indent=1))
    log(f"done: {len(items)} thumbnails, {len(skipped)} skipped, sprite {sprite['bytes'] if sprite else 0} bytes, {index['runtime_s']} s")
    return index


def main():
    cfg = json.loads((FW / "config.json").read_text())
    key = ((cfg.get("inputs") or {}).get("api-key") or {}).get("key")
    if not key:
        raise SystemExit("api-key input missing (manifest must declare it)")
    api = FW_API(key)
    dest = cfg.get("destination") or {}
    sid = None
    if dest.get("type") == "analysis":
        an = api.get(f"/api/analyses/{dest['id']}")
        par = an.get("parent") or {}
        sid = par.get("id") if par.get("type") == "session" else (an.get("parents") or {}).get("session")
    elif dest.get("type") == "session":
        sid = dest["id"]
    if not sid:
        raise SystemExit("run this gear as an analysis on a session, or from a gear rule on an acquisition file")
    session = api.get(f"/api/sessions/{sid}")
    ser = (cfg.get("inputs") or {}).get("series") or {}
    if ser:
        # Gear-rule mode: one series. The analysis sits on that acquisition; render only the triggering file,
        # from the copy the engine already downloaded.
        hier = ser.get("hierarchy") or {}
        aid = hier.get("id") if hier.get("type") == "acquisition" else None
        if not aid:
            raise SystemExit("series input must be a file on an acquisition")
        acq = api.get(f"/api/acquisitions/{aid}")
        acq["_only"] = (ser.get("object") or {}).get("name") or ser.get("location", {}).get("name")
        acq["_local"] = ser.get("location", {}).get("path")
        acqs = [acq]
        log(f"series mode: {acq.get('label')} / {acq['_only']}")
    else:
        acqs = api.get(f"/api/sessions/{sid}/acquisitions") or []
    prof_in = ((cfg.get("inputs") or {}).get("profile") or {}).get("location", {}).get("path")
    work = tempfile.mkdtemp(prefix="thumbs_")
    try:
        run(cfg, FW / "output", work, api, session, acqs, prof_in)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
