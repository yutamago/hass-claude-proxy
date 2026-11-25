#!/usr/bin/env python3
"""Copy Home Assistant's core `anthropic` integration into this repository.

Usage: scripts/sync_upstream.py <home-assistant-tag> [--core-dir DIR]

The copy is mechanical: it renames the domain to `claude_proxy`, rewrites import
paths, and resolves `[%key:...%]` translation references (custom integrations
cannot use them). Run it on the `upstream` branch, commit, then merge `upstream`
into `main`. All proxy-specific changes live on `main` only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
UPSTREAM_DOMAIN = "anthropic"
DOMAIN = "claude_proxy"
SRC_COMPONENT = "homeassistant/components/anthropic"
SRC_TESTS = "tests/components/anthropic"
DST_COMPONENT = REPO / "custom_components" / DOMAIN
DST_TESTS = REPO / "tests"
SKIP_FILES = {"quality_scale.yaml", "strings.json"}
KEY_REF = re.compile(r"\[%key:([^%]+)%\]")
CORE_REPO = "https://github.com/home-assistant/core.git"


def git(core: Path, *args: str) -> str:
    """Run git in the upstream clone and return stdout."""
    return subprocess.run(
        ["git", "-C", str(core), *args], check=True, capture_output=True, text=True
    ).stdout


def ensure_clone(core: Path, tag: str) -> None:
    """Make sure a blobless clone exists and has the tag."""
    if not (core / ".git").exists():
        core.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--filter=blob:none",
                "--no-checkout",
                CORE_REPO,
                str(core),
            ],
            check=True,
        )
    git(core, "fetch", "-q", "--filter=blob:none", "origin", "tag", tag)


def list_files(core: Path, tag: str, prefix: str) -> list[str]:
    """List files under a path at the tag."""
    out = git(core, "ls-tree", "-r", "--name-only", tag, prefix + "/")
    return [line for line in out.splitlines() if line]


def show(core: Path, tag: str, path: str) -> str:
    """Return a file's text at the tag."""
    return git(core, "show", f"{tag}:{path}")


def rewrite_python(text: str) -> str:
    """Point imports at the custom component."""
    text = text.replace(
        f"homeassistant.components.{UPSTREAM_DOMAIN}", f"custom_components.{DOMAIN}"
    )
    return text.replace(f'DOMAIN = "{UPSTREAM_DOMAIN}"', f'DOMAIN = "{DOMAIN}"')


def rewrite_tests(text: str) -> str:
    """Rewrite domain literals that tests and snapshots compare against."""
    text = rewrite_python(text)
    text = text.replace(f'platform="{UPSTREAM_DOMAIN}"', f'platform="{DOMAIN}"')
    return text.replace(f"'platform': '{UPSTREAM_DOMAIN}'", f"'platform': '{DOMAIN}'")


def resolve_translations(core: Path, tag: str, strings: dict) -> dict:
    """Inline every [%key:...%] reference, the way core's build script does."""
    cache: dict[str, dict] = {}

    def source(parts: list[str]) -> tuple[dict, list[str]]:
        if parts[0] == "common":
            key = "homeassistant/strings.json"
            path = parts
        elif parts[0] == "component":
            key = f"homeassistant/components/{parts[1]}/strings.json"
            path = parts[2:]
        else:
            raise ValueError(f"Unsupported reference {':'.join(parts)}")
        if parts[0] == "component" and parts[1] == UPSTREAM_DOMAIN:
            return strings, path
        if key not in cache:
            cache[key] = json.loads(show(core, tag, key))
        return cache[key], path

    def lookup(ref: str, depth: int = 0) -> str:
        if depth > 10:
            raise ValueError(f"Reference loop at {ref}")
        tree, path = source(ref.split("::"))
        node = tree
        for part in path:
            node = node[part]
        return KEY_REF.sub(lambda m: lookup(m[1], depth + 1), node)

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, str):
            return KEY_REF.sub(lambda m: lookup(m[1]), node)
        return node

    return walk(strings)


def main() -> int:
    """Run the sync."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="Home Assistant release tag, e.g. 2026.11.3")
    parser.add_argument(
        "--core-dir",
        type=Path,
        default=Path.home() / ".cache" / "claude_proxy" / "core",
        help="Blobless clone of home-assistant/core (created if missing)",
    )
    args = parser.parse_args()
    core: Path = args.core_dir
    tag: str = args.tag
    ensure_clone(core, tag)

    for dst in (DST_COMPONENT, DST_TESTS):
        if dst.exists():
            for path in sorted(dst.rglob("*"), reverse=True):
                if "__pycache__" in path.parts:
                    continue
                if path.is_file():
                    path.unlink()
                elif not any(path.iterdir()):
                    path.rmdir()

    for src in list_files(core, tag, SRC_COMPONENT):
        rel = src.removeprefix(SRC_COMPONENT + "/")
        if rel in SKIP_FILES:
            continue
        text = show(core, tag, src)
        if rel.endswith(".py"):
            text = rewrite_python(text)
        elif rel == "manifest.json":
            manifest = json.loads(text)
            manifest["domain"] = DOMAIN
            text = json.dumps(manifest, indent=2) + "\n"
        out = DST_COMPONENT / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)

    strings = json.loads(show(core, tag, f"{SRC_COMPONENT}/strings.json"))
    translations = DST_COMPONENT / "translations" / "en.json"
    translations.parent.mkdir(parents=True, exist_ok=True)
    translations.write_text(
        json.dumps(
            resolve_translations(core, tag, strings), indent=2, ensure_ascii=False
        )
        + "\n"
    )

    for src in list_files(core, tag, SRC_TESTS):
        rel = src.removeprefix(SRC_TESTS + "/")
        text = show(core, tag, src)
        if rel.endswith((".py", ".ambr")):
            text = rewrite_tests(text)
        out = DST_TESTS / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)

    (REPO / "UPSTREAM").write_text(
        f"home-assistant/core {tag} {git(core, 'rev-list', '-n1', tag).strip()}\n"
    )
    print(f"Synced {SRC_COMPONENT} at {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
