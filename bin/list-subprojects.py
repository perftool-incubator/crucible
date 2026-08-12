#!/usr/bin/env python3

import argparse
import json
import os
import sys

from jsonschema import validate, ValidationError

CRUCIBLE_HOME = os.environ.get("CRUCIBLE_HOME")

KIND_CONFIG = {
    "tool": {
        "subprojects_dir": "subprojects/tools",
        "rickshaw_field": "tool",
        "metadata_filename": "tool-metadata.json",
        "subgroup_field": "subtools",
        "envelope_key": "tools",
    },
    "benchmark": {
        "subprojects_dir": "subprojects/benchmarks",
        "rickshaw_field": "benchmark",
        "metadata_filename": "benchmark-metadata.json",
        "subgroup_field": "sub-benchmarks",
        "envelope_key": "benchmarks",
    },
}


def process_options():
    parser = argparse.ArgumentParser(description="List installed tools or benchmarks and their metadata")

    parser.add_argument('--kind',
                         choices=['tool', 'benchmark'],
                         required=True,
                         help='Which kind of subproject to list')
    parser.add_argument('--name',
                         help='Limit output to a single subproject')
    parser.add_argument('--format',
                         choices=['table', 'json'],
                         default='table',
                         help='Output format (default: table)')

    return parser.parse_args()


def load_json(filepath):
    with open(filepath, 'r') as f:
        return json.load(f)


def build_entry(subproject_dir, config, schema):
    """Aggregate one subproject's rickshaw.json/{tool,benchmark}-metadata.json/multiplex.json
    into a single normalized entry. Falls back to name-only if the metadata file is
    missing or fails schema validation -- a malformed file for one subproject should
    not prevent the rest of the listing from working.
    """
    rickshaw_json_path = os.path.join(subproject_dir, "rickshaw.json")
    if not os.path.isfile(rickshaw_json_path):
        return None

    try:
        rickshaw_data = load_json(rickshaw_json_path)
        name = rickshaw_data[config["rickshaw_field"]]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"WARNING: could not read '{config['rickshaw_field']}' from {rickshaw_json_path}: {e}", file=sys.stderr)
        return None

    subgroup_field = config["subgroup_field"]
    entry = {
        "name": name,
        "has_metadata": False,
        "description": None,
        subgroup_field: [],
        "cdm_indexed": None,
        "cdm_sources": [],
        "output_files": [],
        "params": None,
    }

    metadata_path = os.path.join(subproject_dir, config["metadata_filename"])
    if os.path.isfile(metadata_path):
        try:
            metadata = load_json(metadata_path)
            validate(instance=metadata, schema=schema)
            entry["has_metadata"] = True
            entry["description"] = metadata.get("description")
            entry[subgroup_field] = metadata.get(subgroup_field, [])
            entry["cdm_indexed"] = metadata.get("cdm_indexed")
            entry["cdm_sources"] = metadata.get("cdm_sources", [])
            entry["output_files"] = metadata.get("output_files", [])
        except json.JSONDecodeError as e:
            print(f"WARNING: {metadata_path} is not valid JSON, falling back to name-only for '{name}': {e}", file=sys.stderr)
        except ValidationError as e:
            print(f"WARNING: {metadata_path} failed schema validation, falling back to name-only for '{name}': {e.message}", file=sys.stderr)

    multiplex_path = os.path.join(subproject_dir, "multiplex.json")
    if os.path.isfile(multiplex_path):
        try:
            multiplex = load_json(multiplex_path)
            entry["params"] = {
                "presets": multiplex.get("presets", {}),
                "validations": multiplex.get("validations", {}),
            }
        except json.JSONDecodeError as e:
            print(f"WARNING: could not parse {multiplex_path}: {e}", file=sys.stderr)

    return entry


def collect_entries(config, name_filter):
    subprojects_root = os.path.join(CRUCIBLE_HOME, config["subprojects_dir"])
    schema_path = os.path.join(CRUCIBLE_HOME, "schema", config["metadata_filename"])
    schema = load_json(schema_path)

    entries = []
    if not os.path.isdir(subprojects_root):
        return entries

    for dir_name in sorted(os.listdir(subprojects_root)):
        subproject_dir = os.path.join(subprojects_root, dir_name)
        if not os.path.isdir(subproject_dir):
            continue

        entry = build_entry(subproject_dir, config, schema)
        if entry is None:
            continue
        if name_filter and entry["name"] != name_filter:
            continue
        entries.append(entry)

    return entries


MAX_DESCRIPTION_WIDTH = 70


def truncate(text, max_width):
    if len(text) <= max_width:
        return text
    return text[:max_width - 3].rstrip() + "..."


def print_table(entries, config):
    subgroup_field = config["subgroup_field"]
    subgroup_label = subgroup_field.replace("-", " ").title()
    subgroup_singular = subgroup_field.rstrip("s")
    headers = ["Name", "Description", subgroup_label, "CDM Indexed"]

    rows = []
    for entry in entries:
        subgroup = entry[subgroup_field]
        subgroup_display = str(len(subgroup)) if subgroup else "-"

        if not entry["has_metadata"]:
            cdm_display = "-"
        elif subgroup:
            cdm_display = f"per-{subgroup_singular}"
        else:
            cdm_display = "yes" if entry["cdm_indexed"] else "no"

        description = truncate(entry["description"], MAX_DESCRIPTION_WIDTH) if entry["description"] else "-"
        rows.append([entry["name"], description, subgroup_display, cdm_display])

    all_rows = [headers] + rows
    widths = [max(len(str(row[i])) for row in all_rows) for i in range(len(headers))]

    def fmt_row(row):
        return "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))

    print(fmt_row(headers))
    print(fmt_row(["-" * w for w in widths]))
    for row in rows:
        print(fmt_row(row))


def main():
    args = process_options()
    config = KIND_CONFIG[args.kind]
    entries = collect_entries(config, args.name)

    if args.format == "json":
        print(json.dumps({config["envelope_key"]: entries}, indent=4))
    else:
        print_table(entries, config)


if __name__ == "__main__":
    main()
