# session-thumbnails (Flywheel gear, prototype)

Renders one load-optimised thumbnail per imaging series in a session and packs them into `thumbs.jpg` (sprite) +
`thumbs.json` (index, schema `fw.thumbnails/1`) on a session analysis, so apps fetch two files instead of one
download per series. Previews for navigation only, never measurements.

- **Default:** middle slice of every volume (axial), true slice order from DICOM positions; CT soft-tissue window;
  PET coronal MIP, inverted. Any transfer syntax GDCM decodes (incl. JPEG 2000), NIfTI.
- **Profile:** gear input `profile`, else project file `thumbnails.json`, layered over the built-in rules
  (`"inherit_builtin": false` to opt out). Rules match `modality`, `protocol` (SeriesDescription), `label`,
  `classification` ("Key:Value", globs). Each view: `plane` (axial/coronal/sagittal), `pick` (ordered fallback
  list), `window` (soft_tissue, lung, bone, brain, liver, mediastinum, auto, or [center, width]), `invert`.
- **Pickers (0.1.0):** `middle`, `fraction` {at}, `index` {at}, `mip`, `organ_centroid` {organ} (TotalSegmentator
  `total` mask on the session; physical-space mapping, so flipped/resampled masks land correctly).
  Declared, not yet implemented (fall through): `lesion_max_diameter`, `key_image`.
- **Two ways to run (0.2.0):**
  - *Session:* run as an analysis on a session (by hand or `batch_thumbs.py`): one sprite for every series.
  - *Gear rule:* a project rule on DICOM files that sets the `series` input. The analysis sits on that acquisition
    and covers only the triggering file (already downloaded by the engine, so no second download). The gear needs a
    role in the rule (read-only is enough; outputs are written by the engine). Apps should read both: newest
    session-level analysis plus newest per acquisition, newest wins per acquisition (Subject View 0.10.1 does this).
  - Note: a rule can't fire on TotalSegmentator output (it lands in an analysis), so `organ_centroid` profiles
    fall back to their next picker if the rule runs before TotalSegmentator. Re-run in session mode afterwards.
- **Index:** per thumbnail the picker used and why earlier pickers were skipped, slice/slices, window, sprite rect,
  and a fingerprint of (file, view, profile sha256, gear version) so apps can tell when a thumbnail is stale.
- **Safety:** skips US/SC/XC/ES, BurnedInAnnotation=YES and LABEL/OVERVIEW images unless
  `allow_burned_in_modalities` is on. Records the profile's sha256 in every index.

Files: `run_thumbs.py`, `manifest.json`, `ts_total_classmap.json` (gear); `thumbnails.example.json`;
`release_thumbs.py` + `build_push.py` (Docker-free release onto the Exchange pyradiomics base image);
`batch_thumbs.py` (queue on a project); `t_gear.py` (synthetic test); `t_main.py` (end-to-end against a fake API,
`SERIES=1` for gear-rule mode).
