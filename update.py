import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import date
from pathlib import Path

PYPI = "https://pypi.org/pypi/{package}/json"

PROJECT_ROOT = Path(__file__).parent
PYTHON_DEPS_JSON = PROJECT_ROOT / "python3-dependencies.json"
MANIFEST_XML = PROJECT_ROOT / "com.github.dynobo.normcap.appdata.xml"
FLATPAK_YAML = PROJECT_ROOT / "com.github.dynobo.normcap.yml"

MODULE_TEMPLATE = {
    "name": "python3-PACKAGE",
    "buildsystem": "simple",
    "build-commands": [
        'pip3 install --verbose --exists-action=i --no-index --find-links="file://${PWD}" --prefix=${FLATPAK_DEST} --no-build-isolation "PACKAGE"'
    ],
    "sources": [],
}


def get_pypi_info(package: str):
    with urllib.request.urlopen(PYPI.format(package=package)) as response:
        json_text = response.read()
    return json.loads(json_text)


def is_suitable(filename: str):
    if ".whl" not in filename:
        return False
    if "manylinux" in filename and "x86" in filename:
        return True
    if "none-any" in filename:
        return True
    return False


def get_release_info(package: str, version: str):
    package_info = get_pypi_info(package=package)
    files = package_info["releases"][version]
    files = [f for f in files if is_suitable(f["filename"])]
    if len(files) != 1:
        raise ValueError(
            f"One wheel file should be selected, but there are {len(files)}: {files}"
        )
    return files[0]


def add_release_to_manifest(version: str):
    tree = ET.parse(MANIFEST_XML)
    root = tree.getroot()

    for releases in root.findall("releases"):
        new_release = ET.Element(
            "release", {"version": version, "date": date.today().strftime("%Y-%m-%d")}
        )
        releases.insert(0, new_release)

    ET.indent(tree, "  ")
    tree.write(MANIFEST_XML, encoding="utf-8", xml_declaration=True)


def update_python_deps(deps: list[str]):
    python_deps = json.loads(PYTHON_DEPS_JSON.read_text())

    new_modules = []
    for dep in deps:
        package_name, package_version = dep.split("==")
        release_info = get_release_info(package=package_name, version=package_version)
        sha256 = release_info["digests"]["sha256"]
        url = release_info["url"]

        module = deepcopy(MODULE_TEMPLATE)
        module["name"] = module["name"].replace("PACKAGE", package_name)
        module["build-commands"][0] = module["build-commands"][0].replace(
            "PACKAGE", package_name
        )
        module["sources"].append({"type": "file", "url": url, "sha256": sha256})
        new_modules.append(module)

    python_deps["modules"] = new_modules
    PYTHON_DEPS_JSON.write_text(json.dumps(python_deps, indent=4))


def update_runtime(version: str):
    text = FLATPAK_YAML.read_text()
    text = re.sub(
        r"(runtime-version:) \d+\.\d+$",
        f"\\1 {version}",
        text,
        flags=re.RegexFlag.MULTILINE,
    )
    FLATPAK_YAML.write_text(text)


def main():
    normcap = get_pypi_info(package="normcap")
    normcap_version = normcap["info"]["version"]
    normcap_deps = normcap["info"]["requires_dist"]

    add_release_to_manifest(version=normcap_version)

    deps = [d for d in normcap_deps if "extra == " not in d]
    deps = deps[::-1]  # Sort shiboken before pyside
    deps.append(f"normcap=={normcap_version}")

    update_python_deps(deps=deps)

    pyside_dep = [d for d in deps if d.lower().startswith("pyside")][0]
    pyside_version = pyside_dep.split("==")[-1].split(".")
    runtime_version = f"{pyside_version[0]}.{pyside_version[1]}"
    update_runtime(version=runtime_version)


if __name__ == "__main__":
    main()
