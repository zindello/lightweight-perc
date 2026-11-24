"""A small PEP 517 build backend, in the repo itself, so installing lightweight-perc fetches
nothing: no setuptools, no flit, nothing from PyPI. It builds a pure-Python wheel of
lightweight_perc/ and an sdist of the repo's own files. Python 3.11 or newer (tomllib)."""

import base64
import hashlib
import io
import os
import tarfile
import tomllib
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "lightweight_perc"
EPOCH = (1980, 1, 1, 0, 0, 0)       # fixed timestamps: the same source builds the same wheel
SDIST_FILES = ["pyproject.toml", "README.md", "_build/backend.py"]


def _project():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)["project"]


def _metadata():
    p = _project()
    lines = ["Metadata-Version: 2.1", f"Name: {p['name']}", f"Version: {p['version']}",
             f"Summary: {p['description']}", f"Requires-Python: {p['requires-python']}"]
    for url_name, url in p.get("urls", {}).items():
        lines.append(f"Project-URL: {url_name}, {url}")
    lines.append("Description-Content-Type: text/markdown")
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        return "\n".join(lines) + "\n\n" + f.read()


def _package_files():
    """The package's modules, and the real cards' recorded answers (for lightweight_perc.testing)."""
    for sub, ext in (("", ".py"), ("/fixtures", ".json")):
        for name in sorted(os.listdir(os.path.join(ROOT, PACKAGE + sub))):
            if name.endswith(ext):
                yield f"{PACKAGE}{sub}/{name}"


def _dist_name():
    p = _project()
    return p["name"].replace("-", "_"), p["version"]


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name, version = _dist_name()
    info = f"{name}-{version}.dist-info"
    files = {path: open(os.path.join(ROOT, path), "rb").read() for path in _package_files()}
    files[f"{info}/METADATA"] = _metadata().encode()
    files[f"{info}/WHEEL"] = (b"Wheel-Version: 1.0\nGenerator: lightweight-perc _build/backend.py\n"
                              b"Root-Is-Purelib: true\nTag: py3-none-any\n")
    record = []
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(data)}")
    record.append(f"{info}/RECORD,,")
    files[f"{info}/RECORD"] = ("\n".join(record) + "\n").encode()
    wheel = f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, wheel), "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in files.items():
            entry = zipfile.ZipInfo(path, EPOCH)
            entry.external_attr = 0o644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(entry, data)
    return wheel


def build_sdist(sdist_directory, config_settings=None):
    name, version = _dist_name()
    base = f"{name}-{version}"
    sdist = f"{base}.tar.gz"
    with tarfile.open(os.path.join(sdist_directory, sdist), "w:gz", format=tarfile.PAX_FORMAT) as t:
        def add(path, data):
            entry = tarfile.TarInfo(f"{base}/{path}")
            entry.size, entry.mode, entry.mtime = len(data), 0o644, 0
            t.addfile(entry, io.BytesIO(data))
        for path in SDIST_FILES + list(_package_files()):
            add(path, open(os.path.join(ROOT, path), "rb").read())
        add("PKG-INFO", _metadata().encode())
    return sdist
