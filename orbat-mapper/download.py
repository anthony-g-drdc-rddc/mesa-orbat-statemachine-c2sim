"""Install the pinned, unchanged upstream distribution after SHA-256 verification."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent


def install(archive: Path | None = None) -> Path:
    release = json.loads((ROOT / "release.json").read_text())
    target = ROOT / "dist"
    marker = target / "c2sim-release.json"
    if target.exists():
        if (
            marker.is_file()
            and json.loads(marker.read_text()) == release
            and (target / "index.html").is_file()
        ):
            return target
        raise ValueError(f"{target} already exists but does not match the pinned release")
    with tempfile.TemporaryDirectory(prefix="orbat-download-", dir=ROOT) as temporary:
        staging = Path(temporary)
        package = archive
        if package is None:
            package = staging / "release.zip"
            with urlopen(release["url"], timeout=120) as response, package.open("wb") as output:
                shutil.copyfileobj(response, output)
        with package.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != release["sha256"]:
            raise ValueError("ORBAT release SHA-256 mismatch; nothing was installed")
        with ZipFile(package) as bundle:
            for entry in bundle.infolist():
                path = PurePosixPath(entry.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != "dist"
                    or "\\" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise ValueError(f"Unsafe archive entry: {entry.filename}")
            bundle.extractall(staging)
        site = staging / "dist"
        if not (site / "index.html").is_file():
            raise ValueError("Upstream archive does not contain dist/index.html")
        shutil.copyfile(ROOT / "LICENSE.upstream.txt", site / "LICENSE.orbat-mapper.txt")
        (site / "c2sim-release.json").write_text(json.dumps(release, indent=2) + "\n")
        site.rename(target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Use an already downloaded official ZIP")
    args = parser.parse_args()
    print(install(args.archive))
