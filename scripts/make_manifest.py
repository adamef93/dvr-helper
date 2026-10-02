#!/usr/bin/env python3
"""Generate the Dispatcharr plugin-repo manifests for a release zip.

    python3 scripts/make_manifest.py dist/dvr_helper-<version>.zip [commit_sha]

Writes manifest.json (the URL you add in Dispatcharr -> Plugins -> Repos) and
metadata/dvr-helper/manifest.json (the plugin detail view). The checksums are
taken from the zip you pass, so pass the exact file attached to the GitHub
release. Prior versions already listed in metadata/dvr-helper/manifest.json are
kept.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

OWNER, REPO, SLUG = "adamef93", "dvr-helper", "dvr-helper"
REPO_URL = f"https://github.com/{OWNER}/{REPO}"
RAW = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/main"
REGISTRY_NAME = "DVR Helper (adamef93)"  # must not read as an official Dispatcharr repo

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
zip_path = sys.argv[1]
commit = sys.argv[2] if len(sys.argv) > 2 else ""
plugin = json.load(open(os.path.join(root, "dvr_helper", "plugin.json")))
version = plugin["version"]
data = open(zip_path, "rb").read()
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
rel_url = f"v{version}/{os.path.basename(zip_path)}"  # relative to root_url

entry = {
    "version": version,
    "prerelease": any(not p.isdigit() for p in version.split(".")),
    "commit_sha": commit,
    "commit_sha_short": commit[:7],
    "build_timestamp": now,
    "last_updated": now,
    "checksum_md5": hashlib.md5(data).hexdigest(),
    "checksum_sha256": hashlib.sha256(data).hexdigest(),
    "min_dispatcharr_version": plugin.get("min_dispatcharr_version", ""),
    "source_url": f"{REPO_URL}/releases/download/{rel_url}",
    "url": rel_url,
    "size": len(data),
}

detail_path = os.path.join(root, "metadata", SLUG, "manifest.json")
versions = []
if os.path.exists(detail_path):
    old = json.load(open(detail_path)).get("manifest", {})
    versions = [v for v in old.get("versions", []) if v.get("version") != version]
versions.insert(0, entry)

detail = {
    "generated_at": now,
    "manifest": {
        "slug": SLUG,
        "name": plugin["name"],
        "description": plugin["description"],
        "author": plugin["author"],
        "maintainers": [plugin["author"]],
        "license": plugin.get("license", ""),
        "source_type": "external",
        "source_url": f"{REPO_URL}/releases/download/v{{version}}/dvr_helper-{{version}}.zip",
        "repo_url": REPO_URL,
        "registry_url": REPO_URL,
        "registry_name": REGISTRY_NAME,
        "last_updated": now,
        "latest": {**entry, "latest_url": rel_url},
        "versions": versions,
    },
}
os.makedirs(os.path.dirname(detail_path), exist_ok=True)
json.dump(detail, open(detail_path, "w"), indent=2)
open(detail_path, "a").write("\n")

repo_manifest = {
    "registry_url": REPO_URL,
    "registry_name": REGISTRY_NAME,
    "root_url": f"{REPO_URL}/releases/download",
    "plugins": [{
        "slug": SLUG,
        "name": plugin["name"],
        "description": plugin["description"],
        "manifest_url": f"{RAW}/metadata/{SLUG}/manifest.json",
        "author": plugin["author"],
        "license": plugin.get("license", ""),
        "last_updated": now,
        "latest_version": version,
        "latest_md5": entry["checksum_md5"],
        "latest_sha256": entry["checksum_sha256"],
        "latest_url": rel_url,
        "latest_size": len(data),
        "min_dispatcharr_version": entry["min_dispatcharr_version"],
    }],
}
json.dump(repo_manifest, open(os.path.join(root, "manifest.json"), "w"), indent=2)
open(os.path.join(root, "manifest.json"), "a").write("\n")
print("wrote manifest.json and metadata/%s/manifest.json for %s" % (SLUG, version))
