#!/usr/bin/env python3
"""Builds the release zip: the plugin and its docs, without the tests."""

import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLUG = "stream-demote"
FOLDER = "stream_demote"
INCLUDE = ("plugin.json", "plugin.py", "README.md", "LICENSE", "CHANGELOG.md")
TREES = ("stream_demote", "examples")


def main():
    with open(os.path.join(ROOT, "plugin.json"), encoding="utf-8") as handle:
        version = json.load(handle)["version"]
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, f"{SLUG}-{version}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in INCLUDE:
            archive.write(os.path.join(ROOT, name), f"{FOLDER}/{name}")
        for tree in TREES:
            for base, folders, files in os.walk(os.path.join(ROOT, tree)):
                folders[:] = [item for item in folders if item != "__pycache__"]
                for name in files:
                    path = os.path.join(base, name)
                    archive.write(path, f"{FOLDER}/{os.path.relpath(path, ROOT)}")
    print(target)


if __name__ == "__main__":
    main()
