#!/usr/bin/env python3
# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python

"""
Crucible controller image management tool.

Subcommands:
    build   - Build the controller container image
    push    - Push the built image to the registry
    manifest - Create a multi-arch manifest from pushed images

All subcommands compute a composite hash from the HEAD commits of all
repos that contribute requirements to the controller image. This hash
is used for image tagging and provenance tracking.
"""

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from invoke import run as invoke_run




def run(cmd, hide=False, warn=False):
    """Run a shell command via invoke.

    Args:
        cmd: Command string to execute
        hide: If True, capture stdout/stderr instead of printing
        warn: If True, don't raise on non-zero exit

    Returns:
        invoke Result object
    """
    print(f"Running: {cmd}")
    result = invoke_run(cmd, hide=hide, warn=warn)
    if not warn and result.exited != 0:
        sys.exit(result.exited)
    return result


def get_crucible_home():
    """Get CRUCIBLE_HOME from environment or /etc/sysconfig/crucible."""
    crucible_home = os.environ.get("CRUCIBLE_HOME")
    if crucible_home:
        return crucible_home

    sysconfig = Path("/etc/sysconfig/crucible")
    if sysconfig.exists():
        for line in sysconfig.read_text().splitlines():
            line = line.strip()
            if line.startswith("CRUCIBLE_HOME="):
                value = line.split("=", 1)[1].strip('"').strip("'")
                return value

    print("ERROR: CRUCIBLE_HOME not defined", file=sys.stderr)
    sys.exit(1)


def load_controller_config(crucible_home):
    """Load controller.json configuration.

    Returns a dict with keys: userenv, userenv_file, userenv_label,
    repo, architectures.
    """
    config_path = Path(crucible_home) / "workshop" / "controller.json"
    if not config_path.exists():
        print(f"ERROR: {config_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    # Derive userenv file path and label
    config["userenv_file"] = f"{config['userenv']}.json"

    userenv_path = Path(crucible_home) / "workshop" / config["userenv_file"]
    if userenv_path.exists():
        with open(userenv_path) as f:
            userenv_json = json.load(f)
        config["userenv_label"] = userenv_json.get("userenv", {}).get("name", config["userenv"])
    else:
        config["userenv_label"] = config["userenv"]

    # Derive repos list and requirement file paths from subprojects
    config["repos"] = []
    config["requirements"] = []
    for sp in config["subprojects"]:
        config["repos"].append((sp["name"], sp["path"]))
        req_file = sp.get("requirements", f"{sp['path']}/workshop.json")
        config["requirements"].append(req_file)

    return config


def compute_hashes(crucible_home, repos):
    """Compute commit hashes from all contributing repos.

    Args:
        crucible_home: Path to crucible installation
        repos: List of (name, relative_path) tuples

    Returns:
        (repo_hashes, composite_hash) where repo_hashes is an ordered
        list of (name, hash) tuples and composite_hash is a SHA-256
        of the sorted repo:hash pairs.
    """
    repo_hashes = []

    # Ensure git trusts all subproject directories (needed when running
    # as root inside a container with repos owned by a different user)
    run("git config --global --add safe.directory '*'", hide=True, warn=True)

    for name, rel_path in repos:
        repo_path = Path(crucible_home) / rel_path
        if not repo_path.is_dir():
            print(f"ERROR: Could not find repo '{name}' at '{repo_path}'", file=sys.stderr)
            sys.exit(1)

        result = run(f"git -C {repo_path} rev-parse HEAD", hide=True, warn=True)
        if result.exited != 0:
            print(f"ERROR: Could not get commit hash for '{name}' at '{repo_path}'", file=sys.stderr)
            if result.stderr:
                print(f"  git error: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)

        commit_hash = result.stdout.strip()
        repo_hashes.append((name, commit_hash))

    # Composite hash from sorted repo:hash pairs
    hash_input = "\n".join(f"{name}:{h}" for name, h in sorted(repo_hashes))
    composite_hash = hashlib.sha256(hash_input.encode()).hexdigest()

    return repo_hashes, composite_hash


def print_hashes(repo_hashes, composite_hash):
    """Print repo hashes for logging."""
    print("Controller image provenance:")
    for name, h in repo_hashes:
        print(f"  {name}: {h}")
    print(f"  composite: {composite_hash}")


def generate_provenance(repo_hashes, composite_hash, config_dump=None):
    """Generate provenance data structure."""
    provenance = {
        "build-date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "composite-hash": composite_hash,
        "repos": {name: h for name, h in repo_hashes},
    }
    if config_dump:
        provenance["config"] = config_dump
    return provenance


def cmd_build(args):
    """Build the controller container image."""
    crucible_home = get_crucible_home()
    os.chdir(crucible_home)

    conf = load_controller_config(crucible_home)
    repo_hashes, composite_hash = compute_hashes(crucible_home, conf["repos"])
    print_hashes(repo_hashes, composite_hash)

    os.environ["TOOLBOX_HOME"] = str(Path(crucible_home) / "subprojects" / "core" / "toolbox")

    workshop_args = [
        "--userenv", f"./workshop/{conf['userenv_file']}",
        "--label", "crucible-controller",
    ]
    for req_file in conf["requirements"]:
        workshop_args.extend(["--requirements", req_file])

    workshop_py = "./subprojects/core/workshop/workshop.py"

    # Capture resolved config via --dump-config
    print("Capturing resolved configuration...")
    dump_result = run(
        f"{workshop_py} {' '.join(workshop_args)} --dump-config true",
        hide=True, warn=True
    )
    config_dump = None
    if dump_result.exited == 0 and dump_result.stdout:
        # Extract JSON from output (skip log lines before the JSON)
        lines = dump_result.stdout.splitlines()
        json_start = None
        for i, line in enumerate(lines):
            if line.strip().startswith("{"):
                json_start = i
                break
        if json_start is not None:
            try:
                config_dump = json.loads("\n".join(lines[json_start:]))
            except json.JSONDecodeError:
                print("WARNING: Could not parse resolved configuration")
    else:
        print("WARNING: Could not capture resolved configuration")

    # Generate provenance
    provenance = generate_provenance(repo_hashes, composite_hash, config_dump)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write provenance file
        provenance_file = Path(tmpdir) / "build-provenance.json"
        with open(provenance_file, "w") as f:
            json.dump(provenance, f, indent=2)

        print("Provenance file:")
        print(json.dumps(provenance, indent=2))

        # Generate requirements file to copy provenance into the image
        provenance_req = {
            "workshop": {"schema": {"version": "2020.03.02"}},
            "userenvs": [{
                "name": "crucible-controller",
                "requirements": ["build-provenance"]
            }],
            "requirements": [{
                "name": "build-provenance",
                "type": "files",
                "files_info": {
                    "files": [{
                        "src": str(provenance_file),
                        "dst": "/etc/crucible/build-provenance.json"
                    }]
                }
            }]
        }
        provenance_req_file = Path(tmpdir) / "provenance-requirements.json"
        with open(provenance_req_file, "w") as f:
            json.dump(provenance_req, f, indent=2)

        # Generate config file with provenance annotations
        annotations = [f"crucible.provenance.composite-hash={composite_hash}"]
        for name, h in repo_hashes:
            annotations.append(f"crucible.provenance.{name}={h}")

        provenance_config = {
            "workshop": {"schema": {"version": "2020.04.30"}},
            "config": {"annotations": annotations}
        }
        provenance_config_file = Path(tmpdir) / "provenance-config.json"
        with open(provenance_config_file, "w") as f:
            json.dump(provenance_config, f, indent=2)

        # Build the image
        extra_args = " ".join(args.extra_args) if args.extra_args else ""
        build_cmd = (
            f"{workshop_py} {' '.join(workshop_args)}"
            f" --requirements {provenance_req_file}"
            f" --config {provenance_config_file}"
        )
        if extra_args:
            build_cmd += f" {extra_args}"

        run(build_cmd)


def cmd_push(args):
    """Push the built controller image to the registry."""
    crucible_home = get_crucible_home()
    os.chdir(crucible_home)

    conf = load_controller_config(crucible_home)
    repo_hashes, composite_hash = compute_hashes(crucible_home, conf["repos"])
    print_hashes(repo_hashes, composite_hash)

    the_date = datetime.now().strftime("%Y-%m-%d")
    the_arch = platform.machine()
    tag = f"{the_date}_{composite_hash}_{the_arch}"

    if not the_date or not the_arch or not composite_hash:
        print(f"ERROR: Could not determine proper tag [{tag}]", file=sys.stderr)
        sys.exit(1)

    controller_repo = conf["repo"]
    source = f"localhost/workshop/{conf['userenv_label']}_crucible-controller"
    destination = f"{controller_repo}:{tag}"

    run(f"buildah push {source} {destination}")


def cmd_manifest(args):
    """Create a multi-arch manifest from pushed images."""
    crucible_home = get_crucible_home()
    os.chdir(crucible_home)

    conf = load_controller_config(crucible_home)
    repo_hashes, composite_hash = compute_hashes(crucible_home, conf["repos"])
    print_hashes(repo_hashes, composite_hash)

    controller_repo = conf["repo"]
    architectures = conf["architectures"]

    # Search registry for images matching composite hash
    result = run(
        f"podman search --list-tags --no-trunc --limit=250 {controller_repo}",
        hide=True
    )

    images = []
    for line in result.stdout.splitlines():
        if composite_hash in line:
            parts = line.split()
            if len(parts) >= 2:
                images.append(f"{parts[0]}:{parts[1]}")

    print("Found images:")
    for image in images:
        print(f"  {image}")

    # Verify all architectures are present
    missing = [arch for arch in architectures if not any(arch in img for img in images)]
    if missing:
        print(f"ERROR: Missing images for architecture(s): {' '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    manifest_tag = args.tag
    local_manifest = f"localhost/controller-manifest:{manifest_tag}"

    # Clean up existing manifest if present
    result = run(f"podman manifest exists {local_manifest}", hide=True, warn=True)
    if result.exited == 0:
        run(f"podman manifest rm {local_manifest}")

    run(f"podman manifest create {local_manifest}")
    for image in images:
        run(f"podman manifest add {local_manifest} docker://{image}")
    run(f"podman manifest push {local_manifest} {controller_repo}:{manifest_tag}")


def main():
    parser = argparse.ArgumentParser(
        description="Crucible controller image management tool"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # build subcommand
    subparsers.add_parser("build", help="Build the controller image")

    # push subcommand
    subparsers.add_parser("push", help="Push the built image to the registry")

    # manifest subcommand
    manifest_parser = subparsers.add_parser(
        "manifest", help="Create a multi-arch manifest"
    )
    manifest_parser.add_argument(
        "tag", help="Tag for the manifest"
    )

    # Use parse_known_args so unrecognized args (e.g., --config for
    # workshop.py) pass through to the build subcommand
    args, remaining = parser.parse_known_args()
    args.extra_args = remaining

    if args.command == "build":
        cmd_build(args)
    elif args.command == "push":
        cmd_push(args)
    elif args.command == "manifest":
        cmd_manifest(args)


if __name__ == "__main__":
    main()
