#!/usr/bin/env python3
"""Generate THIRD_PARTY_NOTICES.md from the dependencies actually installed.

Gate 3 of the release checklist requires a third-party license check. The
honest way to produce one is to read the metadata of the packages that are
really resolved into a release environment, not to restate ``requirements.txt``
by hand.

Python packages are read from the running interpreter's installed
distributions, so this must run inside an environment built from
``requirements.txt``. Web packages are read from ``apps/web/node_modules``,
which ``npm ci`` populates from the committed lockfile.

The script refuses to write a partial file: if a direct requirement is missing
from the environment, it fails instead of quietly omitting that package's
license. A notices file that silently drops a dependency is worse than none.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from importlib import metadata
from pathlib import Path
import re
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPOSITORY_ROOT / "requirements.txt"
NODE_MODULES = REPOSITORY_ROOT / "apps" / "web" / "node_modules"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "THIRD_PARTY_NOTICES.md"

#: ``package~=1.2``, ``package[extra]>=1.2``, ``package``
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9._-]+)")

#: Development-only requirements. They are installed in a contributor's
#: environment but are not redistributed in the release artifact, so they do
#: not belong in its notices.
DEVELOPMENT_ONLY = frozenset(
    {"pytest", "pytest-asyncio", "pytest-cov", "ruff", "mypy"}
)

UNKNOWN_LICENSE = "UNKNOWN"


@dataclass(frozen=True, order=True)
class Package:
    name: str
    version: str
    license_name: str
    homepage: str


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def direct_requirements() -> list[str]:
    names = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        match = _REQUIREMENT_NAME.match(stripped)
        if match:
            names.append(match.group(1))
    return names


def _license_from_metadata(meta: metadata.PackageMetadata) -> str:
    # Newer packaging metadata puts an SPDX expression in License-Expression;
    # older ones put either an SPDX id or the whole license text in License,
    # and many put only a Trove classifier.
    expression = meta.get("License-Expression")
    if expression:
        return str(expression).strip()

    classifiers = [
        value
        for key, value in meta.items()
        if key == "Classifier" and str(value).startswith("License ::")
    ]
    if classifiers:
        # "License :: OSI Approved :: Apache Software License" -> the last part
        return ", ".join(sorted({str(c).split("::")[-1].strip() for c in classifiers}))

    declared = meta.get("License")
    if declared:
        text = str(declared).strip()
        # Some projects embed the entire license text in this field.
        if "\n" not in text and len(text) <= 64:
            return text
        return "see package metadata"

    return UNKNOWN_LICENSE


def _homepage_from_metadata(meta: metadata.PackageMetadata) -> str:
    home = meta.get("Home-page")
    if home:
        return str(home).strip()
    for key, value in meta.items():
        if key == "Project-URL":
            label, _, url = str(value).partition(",")
            if label.strip().lower() in {"homepage", "source", "repository"}:
                return url.strip()
    return ""


def python_packages() -> list[Package]:
    installed = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        installed[_normalize(name)] = dist

    missing = [
        requirement
        for requirement in direct_requirements()
        if _normalize(requirement) not in installed
    ]
    if missing:
        raise SystemExit(
            "Refusing to write a partial notices file. These direct requirements are "
            f"not installed in {sys.executable}: {', '.join(sorted(missing))}. "
            "Run this inside an environment built from requirements.txt."
        )

    packages = []
    for normalized, dist in installed.items():
        if normalized in {_normalize(n) for n in DEVELOPMENT_ONLY}:
            continue
        meta = dist.metadata
        packages.append(
            Package(
                name=str(meta["Name"]),
                version=dist.version or "",
                license_name=_license_from_metadata(meta),
                homepage=_homepage_from_metadata(meta),
            )
        )
    return sorted(packages)


def _node_license(manifest: dict) -> str:
    declared = manifest.get("license")
    if isinstance(declared, str):
        return declared
    if isinstance(declared, dict) and declared.get("type"):
        return str(declared["type"])
    licenses = manifest.get("licenses")
    if isinstance(licenses, list):
        types = [item.get("type") for item in licenses if isinstance(item, dict)]
        if types:
            return ", ".join(str(t) for t in types if t)
    return UNKNOWN_LICENSE


def node_packages() -> list[Package]:
    if not NODE_MODULES.is_dir():
        raise SystemExit(
            f"{NODE_MODULES} does not exist. Run `npm ci --prefix apps/web` first."
        )

    packages = []
    for manifest_path in NODE_MODULES.glob("**/package.json"):
        # Skip nested fixtures and a package's own node_modules duplicates are
        # kept: a hoisted tree can legitimately hold two versions of one name.
        relative = manifest_path.relative_to(NODE_MODULES)
        if relative.parts[-2:-1] == ("dist",) or "test" in relative.parts:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = manifest.get("name")
        version = manifest.get("version")
        if not name or not version:
            continue
        packages.append(
            Package(
                name=str(name),
                version=str(version),
                license_name=_node_license(manifest),
                homepage=str(manifest.get("homepage") or ""),
            )
        )
    # One row per name+version; a hoisted tree repeats the same manifest.
    return sorted(set(packages))


def render(python: list[Package], node: list[Package]) -> str:
    def table(packages: list[Package]) -> str:
        rows = ["| Package | Version | License |", "|---|---|---|"]
        for package in packages:
            link = (
                f"[{package.name}]({package.homepage})"
                if package.homepage.startswith(("http://", "https://"))
                else package.name
            )
            rows.append(f"| {link} | {package.version} | {package.license_name} |")
        return "\n".join(rows)

    unknown = [p for p in (*python, *node) if p.license_name == UNKNOWN_LICENSE]

    sections = [
        "# Third-party notices",
        "",
        "Creative AI Studio itself is distributed under the MIT License (see `LICENSE`).",
        "It depends on the third-party packages listed below, each under its own license.",
        "",
        "This file is generated by `scripts/collect_third_party_notices.py` from the",
        "packages actually installed, not from a hand-maintained list. Regenerate it with",
        "`make third-party-notices` whenever dependencies change, and before cutting a",
        "release.",
        "",
        "The release artifact ships no Python packages and no `node_modules`: it carries",
        "`requirements.txt` and a prebuilt `apps/web/dist`. The Python packages below are",
        "installed by the operator at install time; the web packages below are build-time",
        "dependencies, some of which are compiled into `apps/web/dist`.",
        "",
        f"## Python ({len(python)} packages)",
        "",
        table(python),
        "",
        f"## Web ({len(node)} packages)",
        "",
        table(node),
        "",
    ]

    if unknown:
        sections.extend(
            [
                "## Packages with no license declared in their metadata",
                "",
                "These packages declare no license field or classifier. Check them",
                "individually before redistributing anything that embeds them.",
                "",
                *(f"- {p.name} {p.version}" for p in sorted(unknown)),
                "",
            ]
        )

    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"where to write the notices (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the generated content differs from the file on disk",
    )
    args = parser.parse_args()

    content = render(python_packages(), node_packages())

    if args.check:
        if not args.output.exists():
            print(f"{args.output} does not exist; run `make third-party-notices`.")
            return 1
        if args.output.read_text(encoding="utf-8") != content:
            print(
                f"{args.output} is out of date; run `make third-party-notices` and commit."
            )
            return 1
        print(f"{args.output} is up to date.")
        return 0

    args.output.write_text(content, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
