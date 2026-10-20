"""Utility for updating dependencies and testing fatpak builds locally."""

import argparse
import json
import logging
import re
import shutil
import subprocess
import tomllib
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

logger = logging.getLogger("update.py")


PYPI = "https://pypi.org/pypi/{package}{version}/json"
NORMCAP_REPO = "https://github.com/dynobo/normcap.git"
NORMCAP_DIR = "normcap"  # Checkout location, relative to the current working directory

COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Branch, tag, sha or fully-qualified ref (e.g. refs/pull/<n>/head). Reject a leading '-' and
# anything non-ref-like, so a value can never smuggle extra argv into run()'s cmd.split().
SOURCE_RE = re.compile(r"^(?![.-])[A-Za-z0-9._/-]+$")

PROJECT_ROOT = Path(__file__).parent
PYTHON_DEPS_JSON = PROJECT_ROOT / "python3-dependencies.json"
HATCHLING_DEPS_JSON = PROJECT_ROOT / "python3-hatchling.json"
FLATPAK_YAML = PROJECT_ROOT / "com.github.dynobo.normcap.yml"
PYPROJECT_TOML = PROJECT_ROOT / NORMCAP_DIR / "pyproject.toml"

MODULE_TEMPLATE = {
    "name": "python3-PKG",
    "buildsystem": "simple",
    "build-commands": [
        'pip3 install --verbose --exists-action=i --no-index --find-links="file://${PWD}" --prefix=${FLATPAK_DEST} --no-build-isolation "PKG"'
    ],
    "sources": [],
}

MODULE_TEMPLATE_NORMCAP_GIT = {
    "name": "python3-PKG",
    "buildsystem": "simple",
    "build-commands": [
        "pip3 install --verbose --exists-action=i --no-index --prefix=${FLATPAK_DEST} --no-build-isolation ."
    ],
    "sources": [
        {
            "type": "git",
            "url": NORMCAP_REPO,
            "commit": "COMMIT",
        }
    ],
}


def get_pypi_info(package: str, version: str | None = None):
    suffix = f"/{version}" if version else ""
    url = f"https://pypi.org/pypi/{package}{suffix}/json"

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler)
    with opener.open(url) as response:
        json_text = response.read()

    return json.loads(json_text)

def is_suitable(filename: str):
    filename = filename.lower()

    if ".whl" not in filename:
        return False
    if all(s in filename for s in ["shiboken", "manylinux", "x86"]):
        return True
    if all(s in filename for s in ["pyside6", "manylinux", "x86"]):
        return True
    if all(s in filename for s in ["zxing", "abi3", "manylinux", "x86"]):
        return True
    if all(s in filename for s in ["jeepney", "none-any"]):
        return True
    return all(s in filename for s in ["normcap", "none-any"])


def get_release_info(package: str, version: str, python_version: str | None = None):
    files = get_pypi_info(package=package, version=version)["urls"]
    files = [f for f in files if is_suitable(f["filename"])]

    if len(files) != 1 and python_version:
        # If there's no build for current python version, try lower ones.
        # This is necessary for zxing-cpp which now ship free-threaded version (unsupported by current KDE runtime)
        python_version_int = int(python_version.replace('.', ''))
        for i in range(5):
            files = [f for f in files if f"cp{python_version_int - i}" in f["url"]]
            if files:
                break

    if len(files) != 1:
        raise ValueError(
            f"One wheel file should be selected, but there are {len(files)}: {files}"
        )

    return files[0]


def get_module(package_with_version: str, python_version: str | None = None) -> dict:
    package, version = package_with_version.split("==")
    release_info = get_release_info(
        package=package, version=version, python_version=python_version
    )

    sha256 = release_info["digests"]["sha256"]
    url = release_info["url"]

    module: dict[str, Any] = deepcopy(MODULE_TEMPLATE)
    module["name"] = module["name"].replace("PKG", package)
    module["build-commands"][0] = module["build-commands"][0].replace("PKG", package)
    module["sources"].append({"type": "file", "url": url, "sha256": sha256})

    if "x86_64" in url:
        module["sources"][0]["only-arches"] = ["x86_64"]

    return module


def fetch_normcap(source: str) -> str:
    """Check out normcap at `source` (sha, ref, branch or tag); return the commit SHA."""
    if not SOURCE_RE.match(source):
        raise ValueError(f"Invalid source '{source}'")

    logger.info(f"Fetching NormCap from '{source}' ...")
    shutil.rmtree(f"./{NORMCAP_DIR}", ignore_errors=True)

    if COMMIT_SHA_RE.match(source) or source.startswith("refs/"):
        # `git clone --branch` cannot handle plain SHAs or fully-qualified refs.
        run(f"git init --quiet ./{NORMCAP_DIR}")
        run(f"git -C ./{NORMCAP_DIR} remote add origin {NORMCAP_REPO}")
        run(f"git -C ./{NORMCAP_DIR} fetch --depth=1 --quiet origin {source}")
        run(f"git -C ./{NORMCAP_DIR} checkout --quiet --detach FETCH_HEAD")
    else:
        run(
            f"git clone --depth=1 --quiet --branch {source} "
            f"{NORMCAP_REPO} ./{NORMCAP_DIR}"
        )

    return run(f"git -C ./{NORMCAP_DIR} rev-parse HEAD").strip()


def resolve_tag_commit(tag: str) -> str:
    """Resolve a tag to the commit it points to (peeled, if it's an annotated tag)."""
    if not SOURCE_RE.match(tag):
        raise ValueError(f"Invalid tag '{tag}'")

    logger.info(f"Resolving commit of tag '{tag}' ...")
    # Both patterns are needed: with a pattern, git omits the peeled '^{}' line of
    # annotated tags unless the peeled ref is requested explicitly.
    output = run(f"git ls-remote --tags {NORMCAP_REPO} {tag} {tag}^{{}}")

    commits = [
        line.split()[0]
        for line in output.splitlines()
        if line.endswith(f"{tag}^{{}}")
    ]
    if not commits:  # Lightweight tag: the ref itself points to the commit
        commits = [
            line.split()[0]
            for line in output.splitlines()
            if line.endswith(f"refs/tags/{tag}")
        ]
    if len(commits) != 1:
        raise RuntimeError(f"Could not resolve tag '{tag}' to a commit")

    return commits[0]


def add_normcap_dependencies(deps: list[str], python_version: str):
    logger.info(f"Adding modules for {', '.join(deps)} ...")
    python_deps = json.loads(PYTHON_DEPS_JSON.read_text())

    new_modules = []
    for dep in deps:
        module = get_module(package_with_version=dep, python_version=python_version)
        new_modules.append(module)

    python_deps["modules"] = new_modules
    PYTHON_DEPS_JSON.write_text(json.dumps(python_deps, indent=4))


def add_normcap(version: str, source: str, commit: str) -> None:
    logger.info("Adding module for normcap ...")
    python_deps = json.loads(PYTHON_DEPS_JSON.read_text())

    if source == "pypi":
        module = get_module(f"normcap=={version}")
        python_deps["modules"].append(module)
    else:
        # Add hatchling as build dependency
        module = json.loads(HATCHLING_DEPS_JSON.read_text())
        python_deps["modules"].append(module)

        # Add normcap from git, pinned to the exact commit
        module = deepcopy(MODULE_TEMPLATE_NORMCAP_GIT)
        module["sources"][0]["commit"] = commit
        python_deps["modules"].append(module)

    PYTHON_DEPS_JSON.write_text(json.dumps(python_deps, indent=4))


def get_appropriate_runtime_version() -> str:
    """Get the latest available KDE Platform 6.x runtime from Flathub."""
    output = run(
        "flatpak remote-ls --user --runtime --columns=ref flathub"
    )
    branches = []
    for line in output.splitlines():
        match = re.match(
            r"^runtime/org\.kde\.Platform/[^/]+/(6\.\d+)$", line.strip()
        )
        if match:
            branches.append(match.group(1))
    if not branches:
        raise RuntimeError(
            "No KDE Platform 6.x runtime found on Flathub remote."
        )
    branches.sort(key=lambda v: tuple(int(x) for x in v.split(".")))
    return branches[-1]


def set_runtime_version(version: str) -> None:
    logger.info(f"Setting runtime version to {version} in {FLATPAK_YAML} ...")
    text = FLATPAK_YAML.read_text()
    text = re.sub(r'(runtime-version:) "\d+\.\d+"', f'\\1 "{version}"', text)
    FLATPAK_YAML.write_text(text)


def set_metadata_git_commit(commit: str) -> None:
    if not COMMIT_SHA_RE.match(commit):
        raise ValueError(f"Invalid commit '{commit}'")

    logger.info(f"Setting git commit for metadata to '{commit}' in {FLATPAK_YAML} ...")
    text = FLATPAK_YAML.read_text()
    text = re.sub(r"commit: .*", f"commit: {commit}", text)
    FLATPAK_YAML.write_text(text)


def run(cmd: str) -> str:
    process = subprocess.Popen(
        cmd.split(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )

    output_lines = []
    for line in process.stdout or []:
        print(line, end="")
        output_lines.append(line)

    process.wait()
    if process.returncode != 0:
        raise RuntimeError(
            f"Command {cmd} failed with return code {process.returncode}"
        )

    return "".join(output_lines)


def get_python_version(runtime_version: str) -> str:
    logger.info(f"Installing org.kde.Platform {runtime_version} ...")
    run(
        "flatpak install --user --noninteractive "
        f"flathub org.kde.Platform//{runtime_version}"
    )

    version = run(
        "flatpak run --user --sandbox --command=python3 "
        f"org.kde.Platform//{runtime_version} --version"
    )
    version = version.removeprefix("Python").strip()
    version = ".".join(version.split(".")[:2])

    logger.info(f"org.kde.Platform {runtime_version} contains Python {version}")
    return version


def init_logger() -> None:
    log_format = "%(asctime)s - %(levelname)-7s - %(name)s:%(lineno)d - %(message)s"
    logging.basicConfig(format=log_format, datefmt="%H:%M:%S")
    logger.setLevel(logging.DEBUG)


def parse_args() -> argparse.Namespace:
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument(
        "source",
        default="pypi",
        nargs="?",
        help="'pypi', '<branch>', '<tag>', '<commit-sha>' or '<ref>'.",
    )
    arg_parser.add_argument(
        "--install",
        "-i",
        action="store_true",
        help="Build & install flatpak locally for testing.",
    )
    arg_parser.add_argument(
        "--run",
        "-r",
        action="store_true",
        help="Build, install & run flatpak locally for testing.",
    )
    arg_parser.add_argument(
        "--bundle",
        "-b",
        action="store_true",
        help="Build flatpak bundle",
    )
    arg_parser.add_argument(
        "--keep",
        "-k",
        action="store_true",
        help="Keep temporary build files",
    )
    args = arg_parser.parse_args()
    return args


def main(args):
    init_logger()

    # Gather infos
    if args.source == "pypi":
        logger.info("Retrieving NormCap info from PyPI...")
        normcap = get_pypi_info(package="normcap")
        normcap_version = normcap["info"]["version"]
        normcap_deps = normcap["info"]["requires_dist"]
        normcap_deps = [d for d in normcap_deps if '"build"' not in d]
        logger.info(f"Found version {normcap_version}, deps: {', '.join(normcap_deps)}")
        commit = resolve_tag_commit(f"v{normcap_version}")
    else:
        commit = fetch_normcap(args.source)
        logger.info("Retrieving NormCap info from pyproject.toml ...")
        pyproject = tomllib.loads(PYPROJECT_TOML.read_text())
        normcap_version = pyproject["project"]["version"]
        normcap_deps = pyproject["project"]["dependencies"]

    runtime_version = get_appropriate_runtime_version()
    python_version = get_python_version(runtime_version=runtime_version)

    # Make adjustments to build configs
    add_normcap_dependencies(deps=normcap_deps, python_version=python_version)
    add_normcap(version=normcap_version, source=args.source, commit=commit)
    set_runtime_version(version=runtime_version)
    set_metadata_git_commit(commit=commit)

    # Install and run
    if args.install or args.run or args.bundle:
        logger.info("Building flatpak locally ...")
        install_opt = "--install " if args.install else ""
        repo_opt = "--repo repo" if args.bundle else ""
        run(
            "flatpak-builder --user --force-clean --install-deps-from=flathub "
            f"{install_opt} {repo_opt} "
            "build-dir com.github.dynobo.normcap.yml"
        )

    if args.bundle:
        logger.info("Building flatpak bundle ...")
        run(
            "flatpak build-bundle repo "
            "NormCap-0.0.1-x86_64.flatpak com.github.dynobo.normcap"
        )

    if args.run:
        logger.info("Starting NormCap from flatpak ...")
        run("flatpak run -v debug")

    logger.info("Done.")


if __name__ == "__main__":
    args = parse_args()

    try:
        main(args)
    finally:
        if not args.keep:
            logger.info("Performing cleanup ...")
            shutil.rmtree(PROJECT_ROOT / "build-dir", ignore_errors=True)
            shutil.rmtree(PROJECT_ROOT / "repo", ignore_errors=True)
            shutil.rmtree(PROJECT_ROOT / "normcap", ignore_errors=True)
