"""Local test of run_thumbs.run() with synthetic series (no Flywheel). Usage: thv/bin/python t_gear.py OUTDIR"""
import json, sys, zipfile, shutil, time
from pathlib import Path
import numpy as np, SimpleITK as sitk
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gear"))
import run_thumbs as rt

OUT = Path(sys.argv[1]); shutil.rmtree(OUT, ignore_errors=True); OUT.mkdir(parents=True)
W = OUT / "fx"; W.mkdir()
rng = np.random.default_rng(1)


def body(Z=60, Y=128, X=128):
    zz, yy, xx = np.mgrid[:Z, :Y, :X]
    ct = np.full((Z, Y, X), -1000, np.int16)
    trunk = ((yy - 64) / 50) ** 2 + ((xx - 64) / 58) ** 2 < 1
    ct[trunk] = 40
    lungL = (((yy - 60) / 30) ** 2 + ((xx - 88) / 18) ** 2 < 1) & (zz > 30)
    lungR = (((yy - 60) / 30) ** 2 + ((xx - 40) / 18) ** 2 < 1) & (zz > 30)
    ct[lungL | lungR] = -850
    liver = (((yy - 64) / 28) ** 2 + ((xx - 40) / 22) ** 2 + ((zz - 18) / 12) ** 2) < 1  # patient right = low x in LPS
    spleen = (((yy - 70) / 14) ** 2 + ((xx - 92) / 10) ** 2 + ((zz - 22) / 8) ** 2) < 1
    ct[liver] = 60; ct[spleen] = 45
    lab = np.zeros(ct.shape, np.uint8); lab[spleen] = 1; lab[liver] = 5
    lab[(lungR) & (zz < 45)] = 14  # lung_lower_lobe_right
    return (ct + rng.normal(0, 8, ct.shape)).astype(np.int16), lab


def write_series(arr, spacing, origin, d, modality, desc, compress=None, extra=()):
    img = sitk.GetImageFromArray(arr); img.SetSpacing(spacing); img.SetOrigin(origin)
    d.mkdir(parents=True, exist_ok=True)
    w = sitk.ImageFileWriter(); w.KeepOriginalImageUIDOn()
    if compress:
        w.SetUseCompression(True); w.SetCompressor(compress)
    uid = "1.2.826.0.1.3680043.2.1125.%d" % rng.integers(1e9)
    for k in range(arr.shape[0]):
        s = img[:, :, k]
        for tag, v in [("0008|0060", modality), ("0020|000e", uid + ".2"), ("0020|000d", uid + ".1"),
                       ("0020|0032", "\\".join(map(str, img.TransformIndexToPhysicalPoint((0, 0, k))))), ("0020|0037", "1\\0\\0\\0\\1\\0"),
                       ("0020|0013", str(k + 1)), ("0008|103e", desc)] + list(extra):
            s.SetMetaData(tag, v)
        w.SetFileName(str(d / f"IM{k:04d}.dcm")); w.Execute(s)
    return img


def zipdir(d, z):
    with zipfile.ZipFile(z, "w") as zf:
        for i, f in enumerate(sorted(d.iterdir(), reverse=True)):  # reversed names: reader must sort by position
            zf.write(f, f"x/{f.name}")
    return z


ct, lab = body()
img = write_series(ct, (0.8, 0.8, 2.5), (-51.2, -51.2, -75), W / "ct", "CT", "CT CHEST ABD W CONTRAST")
ctz = zipdir(W / "ct", W / "CT_Soft.dicom.zip")
try:
    write_series(ct, (0.8, 0.8, 2.5), (-51.2, -51.2, -75), W / "ctj", "CT", "CT J2K", compress="JPEG2000")
    j2k = zipdir(W / "ctj", W / "CT_J2K.dicom.zip")
except Exception as e:
    print("JPEG2000 writer unavailable:", e); j2k = None
pet = np.zeros_like(ct, dtype=np.int16); pet[lab == 5] = 300; pet[(np.indices(ct.shape)[0] == 40) & (np.indices(ct.shape)[1] == 60)] = 3000
pet = (pet + rng.normal(100, 20, pet.shape)).clip(0).astype(np.int16)
write_series(pet, (0.8, 0.8, 2.5), (-51.2, -51.2, -75), W / "pt", "PT", "PET WB AC")
ptz = zipdir(W / "pt", W / "PET.dicom.zip")
write_series(ct[:3], (0.8, 0.8, 2.5), (0, 0, 0), W / "us", "US", "US ABD")
usz = zipdir(W / "us", W / "US.dicom.zip")
mr = sitk.GetImageFromArray((ct.astype(np.float32) + 1000) / 4); mr.SetSpacing((1, 1, 1)); sitk.WriteImage(mr, str(W / "T1w.nii.gz"))
# TotalSegmentator-like mask stored x-flipped (as seen on 00030): same physical content, different grid
m = sitk.GetImageFromArray(lab[:, :, ::-1].copy()); m.SetSpacing((0.8, 0.8, 2.5))
m.SetDirection((-1, 0, 0, 0, 1, 0, 0, 0, 1)); m.SetOrigin((-51.2 + 0.8 * 127, -51.2, -75))
sitk.WriteImage(m, str(W / "ts_mask.nii.gz"))


def acq(i, label, path, ftype, modality, cls=None):
    return {"_id": f"a{i}", "label": label, "_local": str(path), "files": [{"name": Path(path).name, "type": ftype, "modality": modality, "file_id": f"f{i}", "modified": "2026-11-25T00:00:00Z", "size": Path(path).stat().st_size, "classification": cls or {}}]}


acqs = [acq(1, "CT_Soft", ctz, "dicom", "CT"), acq(2, "PET", ptz, "dicom", "PT"), acq(3, "US", usz, "dicom", "US"), acq(4, "T1w_MPR", W / "T1w.nii.gz", "nifti", "MR", {"Intent": ["Structural"], "Measurement": ["T1"]})]
if j2k:
    acqs.append(acq(5, "CT_J2K", j2k, "dicom", "CT"))

# 1) built-in default
t = time.time(); idx = rt.run({"config": {}}, OUT / "default", OUT / "w1", None, {"_id": "s1", "label": "Baseline"}, acqs)
res = {"default": [(i["acquisition_label"], i["plane"], i["picker"], i["slice"], i["slices"], i["window"]["name"]) for i in idx["items"]], "skipped": [(s["label"], s["reason"]) for s in idx["skipped"]], "sprite": idx["sprite"], "s": round(time.time() - t, 2)}

# 2) project profile with organ_centroid (mask via ctx) and a fallback chain
prof = json.loads((Path(__file__).resolve().parents[1] / "thumbnails.example.json").read_text())
(OUT / "thumbnails.json").write_text(json.dumps(prof))
orig = rt.find_ts_mask
class FakeAPI:  # only the mask lookup + download are exercised
    def get(self, *a, **k): return {}
    def download(self, path, dest): shutil.copy(W / "ts_mask.nii.gz", dest); return dest
rt.find_ts_mask = lambda api, sid, ids: ("2026", "an1", {"name": "high_res_total_combined_seg_output.nii.gz"})
idx2 = rt.run({"config": {}}, OUT / "profile", OUT / "w2", FakeAPI(), {"_id": "s1", "label": "Baseline"}, acqs, str(OUT / "thumbnails.json"))
res["profile"] = [(i["acquisition_label"], i["view"], i["plane"], i["picker"], i["slice"], [p["picker"] + ": " + p["reason"] for p in i["pickers_skipped"]]) for i in idx2["items"]]
res["profile_prov"] = idx2["profile"]

# organ check: lung_lower_lobe_right centroid z should be ~ (31..44 mean) in the CT's LPS array
zz = np.nonzero(lab == 14)[0].mean(); res["expected_lung_z"] = round(float(zz), 1)

# 3) invalid profile is refused
bad = {"version": 1, "rules": [{"match": {"modality": "CT"}, "views": [{"plane": "oblique", "pick": ["biggest"]}]}]}
(OUT / "bad.json").write_text(json.dumps(bad))
try:
    rt.run({"config": {}}, OUT / "bad", OUT / "w3", None, {"_id": "s1"}, acqs, str(OUT / "bad.json")); res["bad"] = "ACCEPTED (wrong)"
except SystemExit as e:
    res["bad"] = str(e).replace("\n", " | ")
print(json.dumps(res, indent=1, default=str))
