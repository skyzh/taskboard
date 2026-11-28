"""Stage the reviewed, public SRE-38 ARM64 qualification inputs."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import lzma
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
DEBIAN = "https://snapshot.debian.org/archive/debian/20261127T204935Z/"
RASPBERRY_PI = "https://archive.raspberrypi.com/debian/"

RUNTIME = {
    "python.tar.gz": (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20250612/"
        "cpython-3.13.5%2B20250612-aarch64-unknown-linux-gnu-install_only.tar.gz",
        "sha256", "685971ded0af96d1685941243ae1853c70c482b6f858dd86818760776d9c3cb9",
    ),
    "node.tar.xz": (
        "https://nodejs.org/dist/v24.22.3/node-v24.22.3-linux-arm64.tar.xz",
        "sha256", "6e51a2ada00f0011cc266f65d98922b403d7ad88f809ca2aa615a5fe1ce6e86c",
    ),
    "playwright-core.tgz": (
        "https://registry.npmjs.org/playwright-core/-/playwright-core-1.65.1.tgz",
        "sha512", "86a3e9cfb6e548c3f1595768784d728814b35ec2a254e77693e9230af994cb00"
        "f618f3b60ddf477dc5a7abd34dd061db3590c5819f497c116bf97e7f04ba34ab",
    ),
    "uv.tar.gz": (
        "https://github.com/astral-sh/uv/releases/download/0.13.9/uv-aarch64-unknown-linux-gnu.tar.gz",
        "sha256", "0797542018e541a9288855df555c8077eed06f7a47d206362030480e8ecc9f3d",
    ),
}
BROWSER = {
    "chromium": "bbe868b179378fd0f2ba6776420c05fe7dbe377a95eaf2953d3dde27f0f21fe6",
    "chromium-common": "43bd277aceb2c24b0982e82f0fa9baccbda9fca374228d15a9c3dbfc4635aece",
    "chromium-sandbox": "08ed97c8a0ca391c165e0ae9b1d057b853a629c0087ec504cc23de36ba016b6f",
}
BROWSER_VERSION = "1:151.0.7922.108-1~deb13u1+rpt1"
ZENOTY_SHA256 = "a9cdb91d6984227db657357d81343a40b6a0d681052301bb7796be2f02404779"


def verify(path, algorithm, expected):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, algorithm).hexdigest() != expected:
            raise ValueError(f"Digest mismatch for {path.name}")


def download(path, url, algorithm, expected):
    if not path.is_file():
        with urlopen(url, timeout=30) as response, path.open("xb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
    verify(path, algorithm, expected)


def package_index(text):
    packages = {}
    for stanza in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.splitlines()
                      if re.match(r"^[A-Za-z0-9-]+: ", line))
        if "SHA256" in fields:
            packages[fields["Package"], fields["Version"], fields["Architecture"]] = fields
    return packages


def lock():
    return json.loads((HERE / "debian-lock.json").read_text())


def stage(root):
    suite = root / "debian/dists/trixie"
    index_path = suite / "main/binary-arm64/Packages.xz"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    (root / "debs").mkdir(exist_ok=True)
    download(suite / "InRelease", DEBIAN + "dists/trixie/InRelease", "sha256",
             "bfacad4ca61899ce20d4a0d439f29339f66541561b67506364efb88e27dee870")
    download(index_path, DEBIAN + "dists/trixie/main/binary-arm64/Packages.xz", "sha256",
             "5b9a010e6730e4019e90fcb81c92ad59f061955890d691b7d111637d3f8d3dab")

    repo_key = HERE / "rpi-keyring.gpg"
    release = HERE / "rpi-InRelease"
    index = HERE / "rpi-Packages.gz"
    verify(repo_key, "sha256", "3a29901549ae65a910de13a32551e6eea7f45ba1fd70f0cb67e5c70d98488071")
    verify(release, "sha256", "e135cc85798f996fd0b92392ed218d08bd092d7619db598402630fff99380eb5")
    subprocess.run(["gpgv", "--keyring", repo_key, release], check=True)
    verify(index, "sha256", "f2f21d06a9ee902aec65af034d9a5505bc198985706375b4be27ca3143dfa3e0")
    if not re.search(r"^ f2f21d06a9ee902aec65af034d9a5505bc198985706375b4be27ca3143dfa3e0 "
                     r"+505343 main/binary-arm64/Packages\.gz$", release.read_text(), re.MULTILINE):
        raise ValueError("Raspberry Pi signed index does not bind the captured package list")
    zenoty = package_index(gzip.decompress(index.read_bytes()).decode())["zenoty", "0.3", "arm64"]
    if zenoty["SHA256"] != ZENOTY_SHA256:
        raise ValueError("Raspberry Pi signed index does not bind zenoty")

    work = [(root / name, url, algorithm, digest) for name, (url, algorithm, digest) in RUNTIME.items()]
    work.append((root / "zenoty.deb", RASPBERRY_PI + zenoty["Filename"], "sha256", ZENOTY_SHA256))
    for name, digest in BROWSER.items():
        filename = f"{name}_{BROWSER_VERSION.split(':', 1)[1]}_arm64.deb"
        work.append((root / f"{name}.deb", RASPBERRY_PI + "pool/main/c/chromium/" + filename,
                     "sha256", digest))

    debian = package_index(lzma.decompress(index_path.read_bytes()).decode())
    for package in lock():
        source = debian[package["name"], package["version"], package["architecture"]]
        if (source["SHA256"], source["Filename"], int(source["Size"])) != (
                package["sha256"], package["filename"], package["size"]):
            raise ValueError(f"Debian signed index does not bind {package['name']}")
        work.append((root / "debs" / Path(source["Filename"]).name, DEBIAN + source["Filename"],
                     "sha256", source["SHA256"]))
    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(download, *entry) for entry in work]:
            future.result()


def compare_solvers(paths):
    expected = sorted((p["name"], p["version"], p["architecture"]) for p in lock())
    expected.extend((package, BROWSER_VERSION, "arm64") for package in BROWSER)
    expected.append(("zenoty", "0.3", "arm64"))
    for path in paths:
        records = []
        for line in path.read_text().splitlines():
            if line.startswith("Inst "):
                match = re.fullmatch(r"Inst (\S+)(?: \[[^\]]+\])? \((\S+) .* \[([^\]]+)\]\)", line)
                if match is None:
                    raise ValueError(f"Unexpected solver record: {line}")
                records.append(match.groups())
        if sorted(records) != sorted(expected):
            raise ValueError(f"APT selection differs from the lock in {path.name}")
    print(f"Both APT simulations match all {len(expected)} locked packages.")


if __name__ == "__main__":
    if sys.argv[1] == "stage":
        stage(Path(sys.argv[2]))
    elif sys.argv[1] == "compare":
        compare_solvers([Path(value) for value in sys.argv[2:]])
    else:
        raise SystemExit("Use stage ROOT or compare SOLVER_LOGS.")
