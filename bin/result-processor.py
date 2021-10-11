#!/usr/bin/python3

'''Utility to handle various run result processing actions'''

import argparse
import sys
import logging
import re
import lzma
import json
import time
import datetime
from pathlib import Path
from dataclasses import dataclass


@dataclass
class global_vars:
    '''Global variables'''

    args = None
    log_debug_format =  '[%(module)s %(funcName)s:%(lineno)d]\n[%(asctime)s][%(levelname) 8s] %(message)s'
    log_normal_format = '%(message)s'
    log = None
    run_dir = None


def process_options():
    '''Define the CLI argument parsing options'''

    parser = argparse.ArgumentParser(description = "Crucible run result processor")

    parser.add_argument("--crucible-run-dir",
                        dest = "crucible_run_dir",
                        help = "Where are the Crucible run results stored",
                        type = str,
                        default = "/var/lib/crucible/run")

    parser.add_argument("--log-level",
                        dest = "log_level",
                        help = "Control how much logging output should be generated",
                        default = "normal",
                        choices = [ "normal", "debug" ])

    subparsers = parser.add_subparsers(help = "sub-command help",
                                       dest = "mode",
                                       required = True)


    parser_ls = subparsers.add_parser("ls",
                                      help = "result listing help")
    parser_ls.add_argument("--type",
                           dest = "type",
                           help = "What type of result listing to display",
                           choices = [ "tags", "run-id", "short" ],
                           type = str,
                           default = "tags")

    parser_ls.add_argument("--result-dir",
                           dest = "result_dir",
                           help = "A specific result directory to operate on",
                           type = str,
                           default = None)

    parser_ls.add_argument("--filter-type",
                           dest = "filter_type",
                           help = "What type of filter to apply",
                           choices = [ "name", "tags" ],
                           type = str,
                           default = "name")

    parser_ls.add_argument("--filters",
                           dest = "filters",
                           help = "One or more filters (if any) to apply",
                           type = str,
                           action = 'append',
                           default = [])


    parser_tags = subparsers.add_parser("tags",
                                       help = "tags result help")
    parser_tags.add_argument("--action",
                             dest = "action",
                             help = "What to do with the tag(s)",
                             choices = [ "add", "remove", "ls" ],
                             type = str,
                             default = "ls")

    parser_tags.add_argument("--result-dir",
                             dest = "result_dir",
                             help = "A specific result directory to operate on",
                             type = str,
                             default = None,
                             required = True)

    parser_tags.add_argument("--tags",
                             dest = "tags",
                             help = "One or more tags to operate on; with optional value separated by ':'",
                             type = str,
                             action = 'append',
                             default = [])


    myglobal.args = parser.parse_args()

    if myglobal.args.log_level == 'debug':
        logging.basicConfig(level = logging.DEBUG, format = myglobal.log_debug_format, stream = sys.stdout)
    elif myglobal.args.log_level == 'normal':
        logging.basicConfig(level = logging.INFO, format = myglobal.log_normal_format, stream = sys.stdout)

    myglobal.log = logging.getLogger(__file__)

    myglobal.log.debug(myglobal.args)

    # allow for this to eventually be discovered by an environment
    # variable in addition to the CLI argument
    myglobal.run_dir = myglobal.args.crucible_run_dir


def load_rickshaw_run(result_directory):
    data = None

    rickshaw_run_output = result_directory / 'run' / 'rickshaw-run.json.xz'
    if rickshaw_run_output.exists():
        myglobal.log.debug("found %s" % (rickshaw_run_output))
        with lzma.open(rickshaw_run_output, 'rt') as json_file:
            data = json.load(json_file)
    else:
        rickshaw_run_output = result_directory / 'run' / 'rickshaw-run.json'
        if rickshaw_run_output.exists():
            myglobal.log.debug("found %s" % (rickshaw_run_output))
            with open(rickshaw_run_output, 'rt') as json_file:
                data = json.load(json_file)

    return data


def write_json_fp(json_fp, json_data):
    return json.dump(json_data, json_fp, indent = 4, separators = (',', ': '), sort_keys = True)


def replace_rickshaw_run(result_directory, data):
    timestamp = datetime.datetime.fromtimestamp(time.time())
    timestamp = timestamp.strftime(".%Y-%m-%d_%H:%M:%S.%f")

    rickshaw_run_output = result_directory / 'run' / 'rickshaw-run.json.xz'
    if rickshaw_run_output.exists():
        myglobal.log.debug("found %s" % (rickshaw_run_output))

        # backup old file
        rickshaw_run_output_backup = rickshaw_run_output.parent / (rickshaw_run_output.name + timestamp)
        rickshaw_run_output.rename(rickshaw_run_output_backup)

        # create new file
        with lzma.open(rickshaw_run_output, 'wt') as json_file:
            write_json_fp(json_file, data)
    else:
        rickshaw_run_output = result_directory / 'run' / 'rickshaw-run.json'
        if rickshaw_run_output.exists():
            myglobal.log.debug("found %s" % (rickshaw_run_output))

            # backup old file
            rickshaw_run_output_backup = rickshaw_run_output.parent / (rickshaw_run_output.name + timestamp)
            rickshaw_run_output.rename(rickshaw_run_output_backup)

            # create new file
            with open(rickshaw_run_output, 'wt') as json_file:
                write_json_fp(json_file, data)
        else:
            return 1

    return 0


def validate_result_directory(result_directory):
    if not result_directory.exists():
        myglobal.log.error("The requested result directory does not exist [%s]" % (result_directory))
        return 1

    if not result_directory.is_dir():
        myglobal.log.error("The requested result directory is not a directory [%s]" % (result_directory))
        return 1

    return 0


def log_result_directory(result_directory):
    if not result_directory.is_symlink():
        myglobal.log.info("result: %s" % (result_directory.name))
    else:
        symlink_target = result_directory.readlink()
        myglobal.log.info("result: %s -> %s" % (result_directory.name, symlink_target.name))

    return 0


def show_tags(data):
    if 'tags' in data:
        tags = ""
        for tag in data['tags']:
            if len(tags):
                tags += ", "
            tags += "%s:" % (tag['name'])
            match = re.search(r"^.*\s.*$", tag['val'])
            if match:
                tags += "\"%s\"" % (tag['val'])
            else:
                tags += tag['val']
        myglobal.log.info("tags:   %s" % (tags))
    else:
        myglobal.log.error("tags:   Not Found")

    return 0


def check_for_tag_filter(data, tag_filter):
    match = split_tag(tag_filter)
    tag_filter_name = None
    tag_filter_value = None
    if match:
        # tag_filter has a tag and a value
        tag_filter_name = match.group(1)
        tag_filter_value = match.group(2)
    else:
        # tag_filter is only a tag
        tag_filter_name = tag_filter

    myglobal.log.debug("tag_filter %s has name=%s and value=%s" % (tag_filter, tag_filter_name, tag_filter_value))

    for tag in data['tags']:
        if tag['name'] == tag_filter_name:
            myglobal.log.debug("tag_filter '%s' matches tag name '%s'" % (tag_filter, tag['name']))

            if tag_filter_value is not None:
                if tag['val'] == tag_filter_value:
                    myglobal.log.debug("tag_filter '%s' matches tag value '%s'" % (tag_filter, tag['val']))
                    myglobal.log.debug("tag_filter found")
                    return 0
                else:
                    myglobal.log.debug("tag_filter '%s' does not match tag value '%s'" % (tag_filter, tag['val']))
            else:
                myglobal.log.debug("tag_filter '%s' does not require a tag value match" % (tag_filter))
                myglobal.log.debug("tag_filter found")
                return 0
        else:
            myglobal.log.debug("tag_filter '%s' does not match tag name '%s'" % (tag_filter, tag['name']))

    return 1


def ls_result_directory(result_directory):
    if validate_result_directory(result_directory):
        return 1

    data = load_rickshaw_run(result_directory)

    if len(myglobal.args.filters) and myglobal.args.filter_type == "tags":
        if data is not None:
            if 'tags' in data:
                found_match = False
                for tag_filter in myglobal.args.filters:
                    if check_for_tag_filter(data, tag_filter):
                        myglobal.log.debug("result directory '%s' does not match tag filter '%s'" % (result_directory, tag_filter))
                    else:
                        found_match = True

                if not found_match:
                    myglobal.log.debug("result directory does not match any tag filters")
                    return 0
            else:
                myglobal.log.debug("result directory '%s' has no tags to filter on" % (result_directory))
                return 0
        else:
            myglobal.log.debug("result directory '%s' has no rickshaw-run" % (result_directory))
            return 0

    log_result_directory(result_directory)

    if myglobal.args.type == "short":
        pass
    if myglobal.args.type == "tags":
        if data is not None:
            show_tags(data)
        else:
            myglobal.log.error("Could not find a valid rickshaw-run.json[.xz]")
    elif myglobal.args.type == "run-id":
        # check if the result directory conforms to the "new"
        # format where the session-id/run-id is embedded in
        # the directory name
        match = re.search(r"^([a-zA-Z0-9]+)--([0-9-]+)_([0-9:]+)--([a-f0-9-]+)$", result_directory.name)
        if match:
            # found the run-id in the directory name
            myglobal.log.info("run-id: %s" % (match.group(4)))
        else:
            # the result directory is in the "old" name format
            # so we need to load the run-id from the
            # rickshaw-run json
            run_id = None
            if data is not None:
                run_id = None
                if 'id' in data:
                    run_id = data['id']
                elif 'run-id' in data:
                    run_id = data['run-id']
                if run_id is not None:
                    myglobal.log.info("run-id: %s" % (run_id))
                else:
                    myglobal.log.error("run-id: Not Found")
            else:
                myglobal.log.error("Could not find a valid rickshaw-run.json[.xz]")

    myglobal.log.info("")

    return 0


def run_results_ls_mode():
    if myglobal.args.result_dir is not None:
        return ls_result_directory(Path(myglobal.args.result_dir))
    else:
        run_dir = Path(myglobal.run_dir)
        if run_dir.exists() and run_dir.is_dir():
            dir_list = []
            if len(myglobal.args.filters) and myglobal.args.filter_type == "name":
                for filter in myglobal.args.filters:
                    filter_dir_list = run_dir.glob(filter)
                    dir_list.extend(filter_dir_list)

                # remove any duplicate entries by creating a set
                dir_list = set(dir_list)

                # convert from a set back to a list
                dir_list = list(dir_list)
            else:
                dir_list.extend(run_dir.iterdir())

            for result in dir_list:
                if result.name == "latest":
                    continue

                ls_result_directory(result)
        else:
            myglobal.log.error("Invalid Crucible run results directory '%s'!" % (myglobal.run_dir))
            return 1

    return 0


def split_tag(tag):
    return re.search(r"^([a-zA-Z0-9-_\s]+):([a-zA-Z0-9-_:\s\\/]+)$", tag)


def add_tags(data):
    if len(myglobal.args.tags) == 0:
        myglobal.log.error("You must specify at least one tag to add using --tags")
        return 1

    if not 'tags' in data:
        data['tags'] = []

    for tag in myglobal.args.tags:
        match = split_tag(tag)
        if match:
            found_match = False
            for tag in data['tags']:
                if match.group(1) == tag['name']:
                    found_match = True
                    tag['val'] = match.group(2)

            if not found_match:
                data['tags'].append({ 'name': match.group(1), 'val': match.group(2) })
        else:
            myglobal.log.error("Encountered incomplete/illegal tag")
            return 1

    show_tags(data)

    return 0


def remove_tags(data):
    if not 'tags' in data:
        myglobal.log.warning("There are no tags to remove")
        return 1

    if len(myglobal.args.tags) == 0:
        myglobal.log.error("You must specify at least one tag to remove using --tags")
        return 1

    found_matches = False
    for removal_tag in myglobal.args.tags:
        match = re.search(r":", removal_tag)
        if match:
            myglobal.log.error("Specifying a value with a tag to remove does not make sense")
            return 1

        for existing_tag in data['tags']:
            if existing_tag['name'] == removal_tag:
                data['tags'].remove(existing_tag)
                found_matches = True

    if not found_matches:
        myglobal.log.error("There were no matching tags removed")
        return 1

    show_tags(data)

    return 0


def run_results_tag_mode():
    run_dir = Path(myglobal.args.result_dir)

    if validate_result_directory(run_dir):
        return 1

    log_result_directory(run_dir)

    data = load_rickshaw_run(run_dir)

    if data is not None:
        if myglobal.args.action == "ls":
            show_tags(data)
        elif myglobal.args.action == "add":
            if add_tags(data):
                return 1

            replace_rickshaw_run(run_dir, data)
        elif myglobal.args.action == "remove":
            if remove_tags(data):
                return 1

            replace_rickshaw_run(run_dir, data)
    else:
        myglobal.log.error("Could not find a valid rickshaw-run.json[.xz]")
        return 1

    return 0


def main():
    '''Primary base function'''

    process_options()

    if myglobal.args.mode == "ls":
        return run_results_ls_mode()
    elif myglobal.args.mode == "tags":
        return run_results_tag_mode()

    return 0


if __name__ == "__main__":
    myglobal = global_vars()
    sys.exit(main())
