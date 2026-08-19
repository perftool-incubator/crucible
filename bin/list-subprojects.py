#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import sys
import textwrap

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


def build_entry(subproject_dir, config, schema, multiplex_schema=None):
    """Aggregate one subproject's rickshaw.json/{tool,benchmark}-metadata.json/multiplex.json
    into a single normalized entry. Falls back to name-only if the metadata file is
    missing or fails schema validation -- a malformed file for one subproject should
    not prevent the rest of the listing from working.
    """
    rickshaw_json_path = os.path.join(subproject_dir, "rickshaw.json")
    if not os.path.isfile(rickshaw_json_path):
        print(f"WARNING: {rickshaw_json_path} not found, skipping '{os.path.basename(subproject_dir)}'", file=sys.stderr)
        return None

    try:
        rickshaw_data = load_json(rickshaw_json_path)
        if not isinstance(rickshaw_data, dict):
            raise TypeError(f"expected a JSON object, got {type(rickshaw_data).__name__}")
        name = rickshaw_data[config["rickshaw_field"]]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
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
            if not isinstance(multiplex, dict):
                raise TypeError(f"expected a JSON object, got {type(multiplex).__name__}")
            if multiplex_schema is not None:
                validate(instance=multiplex, schema=multiplex_schema)
            entry["params"] = {
                "presets": multiplex.get("presets", {}),
                "validations": multiplex.get("validations", {}),
            }
        except (json.JSONDecodeError, TypeError) as e:
            print(f"WARNING: could not parse {multiplex_path}: {e}", file=sys.stderr)
        except ValidationError as e:
            print(f"WARNING: {multiplex_path} failed schema validation, ignoring params for '{name}': {e.message}", file=sys.stderr)

    entry["metrics_count"] = count_metrics(entry, subgroup_field)
    entry["params_count"] = count_params(entry)

    return entry


def count_metrics(entry, subgroup_field):
    """Total distinct CDM metric types this subproject reports, summed across
    cdm_indexed subtools/sub-benchmarks (or the top-level cdm_sources if it
    has no subgroup). None if there's no metadata file to derive this from.
    """
    if not entry["has_metadata"]:
        return None

    subgroup = entry.get(subgroup_field, [])
    if subgroup:
        return sum(
            len(source.get("types", []))
            for item in subgroup if item.get("cdm_indexed")
            for source in item.get("cdm_sources", [])
        )
    if entry.get("cdm_indexed"):
        return sum(len(source.get("types", [])) for source in entry.get("cdm_sources", []))
    return 0


def count_params(entry):
    """Total distinct arg names covered by multiplex.json's presets and
    validations -- the full recognized/validated parameter surface, not just
    the ones with a default. None if there's no multiplex.json file at all.
    """
    params = entry.get("params")
    if params is None:
        return None

    args = set()
    for group in params.get("presets", {}).values():
        for param in group:
            if "arg" in param:
                args.add(param["arg"])
    for validation in params.get("validations", {}).values():
        args.update(validation.get("args", []))
    return len(args)


def collect_entries(config, name_filter):
    subprojects_root = os.path.join(CRUCIBLE_HOME, config["subprojects_dir"])
    schema_path = os.path.join(CRUCIBLE_HOME, "schema", config["metadata_filename"])
    schema = load_json(schema_path)

    multiplex_schema_path = os.path.join(CRUCIBLE_HOME, "subprojects", "core", "multiplex", "JSON", "req-schema.json")
    multiplex_schema = load_json(multiplex_schema_path) if os.path.isfile(multiplex_schema_path) else None

    entries = []
    if not os.path.isdir(subprojects_root):
        return entries

    for dir_name in sorted(os.listdir(subprojects_root)):
        subproject_dir = os.path.join(subprojects_root, dir_name)
        if not os.path.isdir(subproject_dir):
            continue

        entry = build_entry(subproject_dir, config, schema, multiplex_schema)
        if entry is None:
            continue
        if name_filter and entry["name"] != name_filter:
            continue
        entries.append(entry)

    return entries


MIN_DESCRIPTION_WIDTH = 20
COLUMN_SEPARATOR = "  "


def print_table(entries, config, terminal_width=None):
    if terminal_width is None:
        terminal_width = shutil.get_terminal_size(fallback=(100, 24)).columns

    subgroup_field = config["subgroup_field"]
    subgroup_label = subgroup_field.replace("-", " ").title()
    headers = ["Name", "Description", subgroup_label, "Metrics", "Params"]

    fixed_cells = []
    for entry in entries:
        subgroup = entry.get(subgroup_field, [])
        subgroup_display = str(len(subgroup)) if subgroup else "-"

        metrics = count_metrics(entry, subgroup_field)
        metrics_display = str(metrics) if metrics is not None else "-"

        params = count_params(entry)
        params_display = str(params) if params is not None else "-"

        fixed_cells.append([entry["name"], subgroup_display, metrics_display, params_display])

    # Name/subgroup/metrics/params widths are fixed by content; whatever's
    # left of the terminal width goes to Description, which wraps instead of
    # truncating -- unlike the other columns, its content is prose and can
    # run arbitrarily long.
    def column_width(header, cell_index):
        return max([len(header)] + [len(c[cell_index]) for c in fixed_cells])

    name_width = column_width(headers[0], 0)
    subgroup_width = column_width(headers[2], 1)
    metrics_width = column_width(headers[3], 2)
    params_width = column_width(headers[4], 3)

    reserved = name_width + subgroup_width + metrics_width + params_width + len(COLUMN_SEPARATOR) * 4
    description_wrap_width = max(MIN_DESCRIPTION_WIDTH, terminal_width - reserved)

    rows = []
    for entry, cells in zip(entries, fixed_cells):
        name, subgroup_display, metrics_display, params_display = cells
        if entry["description"]:
            single_line_description = " ".join(entry["description"].split())
            desc_lines = textwrap.wrap(single_line_description, width=description_wrap_width) or ["-"]
        else:
            desc_lines = ["-"]

        for i, desc_line in enumerate(desc_lines):
            if i == 0:
                rows.append([name, desc_line, subgroup_display, metrics_display, params_display])
            else:
                rows.append(["", desc_line, "", "", ""])

    description_width = max([len(headers[1])] + [len(row[1]) for row in rows])
    widths = [name_width, description_width, subgroup_width, metrics_width, params_width]

    def fmt_row(row):
        return COLUMN_SEPARATOR.join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))

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
