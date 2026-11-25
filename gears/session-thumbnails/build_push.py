#!/usr/bin/env python3
"""Registry + Flywheel gear-release helpers (from the pyradiomics-multilabel release script). Standard library only."""
import argparse, base64, getpass, gzip, hashlib, io, json, os, re, sys, tarfile, time
import urllib.error, urllib.parse, urllib.request

SCHEME = "https"  # tests override
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "blobs")
SRC_TAG = "2.2.2_3.1.0"  # Exchange release = GitLab tag 2.2.2_3.1.0 at commit 5473b1c3
SRC_COMMIT = "5473b1c3"
SOURCES = [  # tried in order; first one that serves the tag wins
    ("us-docker.pkg.dev", "flywheel-exchange/gear-exchange/flywheel-pyradiomics"),  # found 11-24
    ("registry-1.docker.io", "flywheel/pyradiomics"),
]
ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])
LAYER_MT = {"application/vnd.docker.distribution.manifest.v2+json": "application/vnd.docker.image.rootfs.diff.tar.gzip",
            "application/vnd.oci.image.manifest.v1+json": "application/vnd.oci.image.layer.v1.tar+gzip"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http(method, url, headers=None, data=None, ok=(200, 201, 202, 204, 307, 302, 301)):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        r = OPENER.open(req, timeout=600)
        return r.status, dict(r.headers), r
    except urllib.error.HTTPError as e:
        if e.code in ok:
            return e.code, dict(e.headers), e
        return e.code, dict(e.headers), e


class Registry:
    def __init__(self, host, repo, basic=None, scope="pull"):
        self.base, self.repo, self.basic, self.scope, self.token = f"{SCHEME}://{host}/v2", repo, basic, scope, None

    def _auth(self, www):
        m = dict(re.findall(r'(\w+)="([^"]*)"', www or ""))
        if not www or www.lower().startswith("basic"):
            self.token = ("Basic", self.basic)
            return
        q = {"service": m.get("service", ""), "scope": f"repository:{self.repo}:{self.scope}"}
        h = {"Authorization": "Basic " + self.basic} if self.basic else {}
        st, _, r = http("GET", m["realm"] + "?" + urllib.parse.urlencode(q), h)
        if st != 200:
            raise SystemExit(f"Registry token request failed ({st}) at {m['realm']}")
        j = json.loads(r.read())
        self.token = ("Bearer", j.get("token") or j.get("access_token"))

    def call(self, method, path, headers=None, data=None, absolute=False):
        url = path if absolute else f"{self.base}/{self.repo}/{path}"
        for attempt in (0, 1):
            h = dict(headers or {})
            if self.token and self.token[1]:
                h["Authorization"] = f"{self.token[0]} {self.token[1]}"
            if hasattr(data, "seek"):
                data.seek(0)
            st, hd, r = http(method, url, h, data)
            if st == 401 and attempt == 0:
                self._auth(hd.get("Www-Authenticate") or hd.get("WWW-Authenticate"))
                continue
            return st, hd, r

    def manifest(self, ref):
        st, hd, r = self.call("GET", f"manifests/{ref}", {"Accept": ACCEPT})
        if st != 200:
            return None, None
        body = r.read()
        return body, hd.get("Content-Type") or json.loads(body).get("mediaType")

    def tags(self):
        st, _, r = self.call("GET", "tags/list")
        return json.loads(r.read()).get("tags", []) if st == 200 else []

    def fetch_blob(self, digest, size=None):
        os.makedirs(CACHE, exist_ok=True)
        path = os.path.join(CACHE, digest.replace(":", "_"))
        if os.path.exists(path) and sha_file(path) == digest:
            return path
        st, hd, r = self.call("GET", f"blobs/{digest}")
        hops = 0
        while st in (301, 302, 303, 307, 308) and hops < 5:  # CDN: follow, without our auth header when leaving the host
            url = urllib.parse.urljoin(f"{self.base}/{self.repo}/blobs/{digest}", hd["Location"])
            same = urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(self.base).netloc
            st, hd, r = self.call("GET", url, absolute=True) if same else http("GET", url)
            hops += 1
        if st != 200:
            raise SystemExit(f"Blob {digest[:19]} download failed ({st})")
        h, n = hashlib.sha256(), 0
        with open(path + ".part", "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b); h.update(b); n += len(b)
        if "sha256:" + h.hexdigest() != digest:
            raise SystemExit(f"Blob {digest[:19]} checksum mismatch")
        os.replace(path + ".part", path)
        print(f"  downloaded {digest[7:19]} {n / 1e6:.1f} MB")
        return path

    def has_blob(self, digest):
        return self.call("HEAD", f"blobs/{digest}")[0] == 200

    def push_blob(self, digest, path):
        if self.has_blob(digest):
            print(f"  exists     {digest[7:19]}")
            return
        st, hd, _ = self.call("POST", "blobs/uploads/", {"Content-Length": "0"}, b"")
        if st not in (202, 201):
            raise SystemExit(f"Upload start failed ({st}) for {digest[:19]}: is there an open ticket?")
        loc = hd["Location"]
        if loc.startswith("/"):
            loc = self.base.rsplit("/v2", 1)[0] + loc
        loc += ("&" if "?" in loc else "?") + "digest=" + urllib.parse.quote(digest)
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            st, _, r = self.call("PUT", loc, {"Content-Type": "application/octet-stream", "Content-Length": str(size)}, f, absolute=True)
        if st not in (201, 204):
            raise SystemExit(f"Blob upload failed ({st}) for {digest[:19]}: {r.read()[:300]!r}")
        print(f"  pushed     {digest[7:19]} {size / 1e6:.1f} MB")

    def push_manifest(self, ref, body, mt):
        st, hd, r = self.call("PUT", f"manifests/{ref}", {"Content-Type": mt}, body)
        if st not in (201, 202):
            raise SystemExit(f"Manifest push failed ({st}): {r.read()[:300]!r}")
        return hd.get("Docker-Content-Digest") or "sha256:" + hashlib.sha256(body).hexdigest()


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def find_source():
    for host, repo in SOURCES:
        reg = Registry(host, repo)
        try:
            body, mt = reg.manifest(SRC_TAG)
        except Exception as e:  # network refusals, DNS
            print(f"  {host}/{repo}: {e}")
            continue
        if body is None:
            tags = reg.tags()
            print(f"  {host}/{repo}: tag {SRC_TAG} not found" + (f"; tags: {', '.join(tags[-8:])}" if tags else ""))
            continue
        j = json.loads(body)
        if "manifests" in j:  # index: pick linux/amd64
            sel = next(m for m in j["manifests"] if m.get("platform", {}).get("architecture") == "amd64" and m.get("platform", {}).get("os") == "linux")
            body, mt = reg.manifest(sel["digest"])
        print(f"  source {host}/{repo}:{SRC_TAG} ({mt.split('.')[-1]})")
        return reg, body, mt
    raise SystemExit("Exchange image not found in any known registry. Ask for the image location (Exchange rootfs-url) and add it to SOURCES.")


def inspect_layers(paths):
    """Return (gear manifest from /flywheel/v0/manifest.json, set of interpreter paths) from the top-most layers."""
    found, pys = None, set()
    for path in reversed(paths):
        with tarfile.open(path, "r:*") as t:
            for ti in t:
                n = ti.name.lstrip("./")
                if n in ("venv/bin/python", "venv/bin/python3"):
                    pys.add("/" + n)
                if found is None and n == "flywheel/v0/manifest.json" and ti.isfile():
                    found = json.load(t.extractfile(ti))
    return found, pys


def fw_api(site, key, method, path, body=None):
    req = urllib.request.Request(f"{SCHEME}://{site}/api{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"scitran-user {key}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"message": e.read()[:400].decode(errors="replace")}


def release(site, key, user, gear_manifest, img_manifest, mt, blobs):
    name, version = gear_manifest["name"], gear_manifest["version"]
    cfg_digest = json.loads(img_manifest)["config"]["digest"]
    doc = {"category": gear_manifest.get("custom", {}).get("gear-builder", {}).get("category", "analysis"), "gear": gear_manifest,
           "exchange": {"rootfs-url": f"docker://{site}/{name}:{version}", "rootfs-hash": cfg_digest}}
    st, j = fw_api(site, key, "POST", "/gears/prepare-add", doc)
    if st != 200 or "ticket" not in j:
        raise SystemExit(f"prepare-add for {name} {version} failed ({st}): {j.get('message')}")
    ticket = j["ticket"]
    print(f"[{name} {version}] ticket open; pushing {len(blobs)} blobs")
    cleaned = ":".join([key.split(":")[0], key.split(":")[-1]])
    reg = Registry(site, name, base64.b64encode(f"{user}:{cleaned}".encode()).decode(), "pull,push")
    for digest, path in blobs:
        reg.push_blob(digest, path)
    pushed = reg.push_manifest(version, img_manifest, mt)
    want = "sha256:" + hashlib.sha256(img_manifest).hexdigest()
    if pushed != want:
        print(f"  note: registry digest {pushed[:19]} differs from local {want[:19]}; using the registry's")
    st, j = fw_api(site, key, "POST", "/gears/save", {"ticket": ticket, "repo": name, "pointer": pushed})
    if st == 409:
        print(f"[{name} {version}] already installed (409)")
    elif st != 200:
        raise SystemExit(f"save for {name} failed ({st}): {j.get('message')}")
    else:
        print(f"[{name} {version}] installed, gear id {j.get('_id') or j.get('id')}, image {pushed}")
