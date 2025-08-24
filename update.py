"""Utility for updating dependencies and testing fatpak builds locally."""

import argparse
import json
import logging
import re
import shutil
import subprocess
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

import tomllib

logger = logging.getLogger("update.py")


PYPI = "https://pypi.org/pypi/{package}{version}/json"
NORMCAP_REPO = "https://github.com/dynobo/normcap.git"

PROJECT_ROOT = Path(__file__).parent
PYTHON_DEPS_JSON = PROJECT_ROOT / "python3-dependencies.json"
HATCHLING_DEPS_JSON = PROJECT_ROOT / "python3-hatchling.json"
FLATPAK_YAML = PROJECT_ROOT / "com.github.dynobo.normcap.yml"
PYPROJECT_TOML = PROJECT_ROOT / "normcap" / "pyproject.toml"

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
            "branch": "BRANCH",
        }
    ],
}


def get_pypi_info(package: str, version: str | None = None):
    version = f"/{version}" if version else ""
    url = PYPI.format(package=package, version=version)

    with urllib.request.urlopen(url) as response:
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
    if all(s in filename for s in ["zxing", "manylinux", "x86"]):
        return True
    if all(s in filename for s in ["jeepney", "none-any"]):
        return True
    if all(s in filename for s in ["normcap", "none-any"]):
        return True

    return False


def get_release_info(package: str, version: str, python_version: str | None = None):
    files = get_pypi_info(package=package, version=version)["urls"]
    files = [f for f in files if is_suitable(f["filename"])]

    if len(files) != 1 and python_version:
        files = [f for f in files if f"cp{python_version.replace('.', '')}" in f["url"]]

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


def add_normcap_dependencies(deps: list[str], python_version: str):
    logger.info(f"Adding modules for {', '.join(deps)} ...")
    python_deps = json.loads(PYTHON_DEPS_JSON.read_text())

    new_modules = []
    for dep in deps:
        module = get_module(package_with_version=dep, python_version=python_version)
        new_modules.append(module)

    python_deps["modules"] = new_modules
    PYTHON_DEPS_JSON.write_text(json.dumps(python_deps, indent=4))


def add_normcap(version: str, source: str) -> None:
    logger.info("Adding module for normcap ...")
    python_deps = json.loads(PYTHON_DEPS_JSON.read_text())

    if source == "pypi":
        module = get_module(f"normcap=={version}")
        python_deps["modules"].append(module)
    else:
        # Add hatchling as build dependency
        module = json.loads(HATCHLING_DEPS_JSON.read_text())
        python_deps["modules"].append(module)

        # Add normcap from git
        module = deepcopy(MODULE_TEMPLATE_NORMCAP_GIT)
        module["sources"][0]["branch"] = source
        python_deps["modules"].append(module)

    PYTHON_DEPS_JSON.write_text(json.dumps(python_deps, indent=4))


def get_appropriate_runtime_version(deps: list[str]) -> str:
    """Get PySide version in format '<major>.<minor>'."""
    pyside = [d for d in deps if d.lower().startswith("pyside")][0]
    pyside_version = pyside.split("==")[-1]
    return ".".join(pyside_version.split(".")[:2])


def set_runtime_version(version=str):
    logger.info(f"Setting runtime version to {version} in {FLATPAK_YAML} ...")
    text = FLATPAK_YAML.read_text()
    text = re.sub(r'(runtime-version:) "\d+\.\d+"', f'\\1 "{version}"', text)
    FLATPAK_YAML.write_text(text)


def set_metadata_git_commit(ref: str):
    logger.info(
        f"Setting git commit for metadata to HEAD of '{ref}' in {FLATPAK_YAML} ..."
    )
    with urllib.request.urlopen(
        f"https://api.github.com/repos/dynobo/normcap/commits/{ref}"
    ) as response:
        json_text = response.read()
    commit = json.loads(json_text)

    text = FLATPAK_YAML.read_text()
    text = re.sub(r"commit: .*", f"commit: {commit['sha']}", text)
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
        "source", default="pypi", nargs="?", help="'pypi', '<branch>' or '<tag>'."
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
        git_ref = f"v{normcap_version}"
    else:
        logger.info(f"Cloning NormCap from '{args.source}' ...")
        run("rm -rf ./normcap")
        run(f"git clone --depth=1 --branch {args.source} {NORMCAP_REPO}")
        logger.info("Retrieving NormCap info from pyproject.toml ...")
        pyproject = tomllib.loads(PYPROJECT_TOML.read_text())
        normcap_version = pyproject["project"]["version"]
        normcap_deps = pyproject["project"]["dependencies"]
        git_ref = args.source

    runtime_version = get_appropriate_runtime_version(deps=normcap_deps)
    python_version = get_python_version(runtime_version=runtime_version)

    # Make adjustments to build configs
    add_normcap_dependencies(deps=normcap_deps, python_version=python_version)
    add_normcap(version=normcap_version, source=args.source)
    set_runtime_version(version=runtime_version)
    set_metadata_git_commit(ref=git_ref)

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
