#!/usr/bin/env python3

import json
import argparse
import sys
import os

write_required = False

def load_json_data(filepath):
    """
    Loads the opensearch section from services.json.
    Returns the opensearch data dict on success, exits on error.
    """
    try:
        with open(filepath, 'r') as f:
            if os.fstat(f.fileno()).st_size == 0:
                print(f"Error: File '{filepath}' is empty.")
                sys.exit(1)
            full_config = json.load(f)

            if "opensearch" not in full_config or not isinstance(full_config.get("opensearch"), dict):
                print(f"Error: 'opensearch' key not found or not an object in {filepath}.")
                sys.exit(1)

            data = full_config["opensearch"]

            if "instances" not in data or not isinstance(data.get("instances"), list):
                print(f"Warning: 'instances' key not found or not a list in {filepath}. Initializing 'instances'.")
                data["instances"] = []
            if "index-to" not in data:
                data["index-to"] = None
            if "query-from" not in data or not isinstance(data.get("query-from"), list):
                print(f"Warning: 'query-from' key not found or not a list in {filepath}. Initializing 'query-from'.")
                data["query-from"] = []
            return data
    except FileNotFoundError:
        print(f"Error: File '{filepath}' not found.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from '{filepath}'. The file might be corrupted.")
        sys.exit(1)

def save_json_data(filepath, data):
    """Saves opensearch data back into the opensearch section of services.json."""
    try:
        with open(filepath, 'r') as f:
            full_config = json.load(f)

        full_config["opensearch"] = data

        with open(filepath, 'w') as f:
            json.dump(full_config, f, indent=4)
        print(f"Data successfully saved to '{filepath}'.")
    except IOError:
        print(f"Error: Could not write to file '{filepath}'.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Could not read '{filepath}' for update.")
        sys.exit(1)

def add_instance(data, name, host, cdmver, userpass=None, set_query_from=False, set_index_to=False):
    if not isinstance(data.get("instances"), list):
        print("Critical Error: 'instances' key is missing or not a list. Cannot add instance.")
        return False

    for instance in data["instances"]:
        if instance.get("name") == name:
            print(f"Error: Instance with name '{name}' already exists in 'instances'. No changes made.")
            return False

    new_instance_dict = {
        "name": name,
        "host": host,
        "cdmver": cdmver
    }
    if userpass is not None:
        new_instance_dict["userpass"] = userpass

    data["instances"].append(new_instance_dict)
    print(f"Instance '{name}' added to 'instances' list.")

    if set_query_from:
        if name not in data["query-from"]:
            data["query-from"].append(name)
            print(f"Instance '{name}' added to 'query-from' list.")
        else:
            print(f"Info: Instance '{name}' was already present in 'query-from' list.")

    if set_index_to:
        data["index-to"] = name
        print(f"Value of 'index-to' has been set to '{name}'.")

    return True

def remove_instance(data, name):
    if not isinstance(data.get("instances"), list):
        print("Critical Error: 'instances' key is missing or not a list. Cannot remove instance.")
        return False

    initial_len = len(data["instances"])
    data["instances"] = [inst for inst in data["instances"] if inst.get("name") != name]

    if len(data["instances"]) < initial_len:
        print(f"Instance '{name}' removed from 'instances' list.")

        if "query-from" in data and isinstance(data["query-from"], list) and name in data["query-from"]:
            data["query-from"].remove(name)
            print(f"Instance '{name}' also removed from 'query-from' list.")

        if data.get("index-to") == name:
            data["index-to"] = None
            print(f"'index-to' was '{name}', now set to None. Warning: No instance is currently configured for 'index-to'.")
        return True
    else:
        print(f"Error: Instance with name '{name}' not found in 'instances' list. No changes made.")
        return False

def update_instance(data, name, new_host=None, new_cdmver=None, new_userpass=None, set_index_to=False, add_to_query_from=False, remove_query_from=False, remove_userpass_flag=False):
    instance_to_update = None
    for inst in data.get("instances", []):
        if inst.get("name") == name:
            instance_to_update = inst
            break

    if not instance_to_update:
        print(f"Error: Instance with name '{name}' not found. Cannot update.")
        return False

    updated_fields_messages = []
    changes_made = False

    if new_host is not None:
        instance_to_update["host"] = new_host
        updated_fields_messages.append(f"host set to '{new_host}'")
        changes_made = True

    if new_cdmver is not None:
        instance_to_update["cdmver"] = new_cdmver
        updated_fields_messages.append(f"cdmver set to '{new_cdmver}'")
        changes_made = True

    if remove_userpass_flag:
        if "userpass" in instance_to_update:
            del instance_to_update["userpass"]
            updated_fields_messages.append("userpass removed")
            changes_made = True
        else:
            updated_fields_messages.append("userpass was not set, no change to userpass")
    elif new_userpass is not None:
        instance_to_update["userpass"] = new_userpass
        updated_fields_messages.append(f"userpass set to '{new_userpass}'")
        changes_made = True

    if set_index_to:
        if data.get("index-to") != name:
            data["index-to"] = name
            updated_fields_messages.append(f"'index-to' set to '{name}'")
            changes_made = True
        else:
            updated_fields_messages.append(f"'index-to' was already '{name}'")

    if add_to_query_from:
        if "query-from" not in data or not isinstance(data["query-from"], list):
            data["query-from"] = []

        if name not in data["query-from"]:
            data["query-from"].append(name)
            updated_fields_messages.append(f"'{name}' added to 'query-from'")
            changes_made = True
        else:
            updated_fields_messages.append(f"'{name}' was already in 'query-from'")

    if remove_query_from:
        if "query-from" in data and name in data["query-from"]:
            data["query-from"].remove(name)
            updated_fields_messages.append(f"'{name}' removed from 'query-from'")
            if not data["query-from"]:
                updated_fields_messages.append(f"Warning: No instance is currently configured for 'query-from'")
            changes_made = True
        else:
            updated_fields_messages.append(f"'{name}' was not present in 'query-from'")

    if changes_made:
        print(f"Instance '{name}' updated: {'; '.join(updated_fields_messages)}.")
    else:
        print(f"Info: Instance '{name}' found, but no update parameters provided or no changes were applicable.")
        return False

    return True


def display_info(filepath, data):
    print(f"Current opensearch configuration from '{filepath}':")
    print(json.dumps(data, indent=2))

def find_instance(instances, target_name):
    for instance in instances:
        if "name" in instance:
            if instance["name"] == target_name:
                return instance
    return None

def list_instance_names(data):
    for instance in data["instances"]:
        if "name" in instance:
            print(instance["name"])

def instance_host_access_opt(data, name):
    instance_obj = find_instance(data["instances"], name)
    cmdline = ""
    if instance_obj != None:
        cmdline = cmdline + " --host " + instance_obj["host"]
        if "userpass" in instance_obj:
            cmdline = cmdline + " --userpass " + instance_obj["userpass"]
    print(cmdline)

def query_opt(data):
    for instance_name in data["query-from"]:
        instance_obj = find_instance(data["instances"], instance_name)
        cmdline = ""
        if instance_obj != None:
            cmdline = cmdline + " --host " + instance_obj["host"] + " --ver " + instance_obj["cdmver"]
            if "userpass" in instance_obj:
                cmdline = cmdline + " --userpass " + instance_obj["userpass"]
            print(cmdline)

def index_instance(data):
    instance_name = None
    if "index-to" in data:
        instance_name = data["index-to"]
    if instance_name != None:
        print(instance_name)

def instance_cdmver_opt(data, name):
    instance_obj = find_instance(data["instances"], name)
    if instance_obj != None:
        print("--ver " + instance_obj["cdmver"])

def gendocs_opt(data):
    if "index-to" in data:
        instance_cdmver_opt(data, data["index-to"])

def main():
    global write_required
    parser = argparse.ArgumentParser(
        description="Manage OpenSearch instances in the Crucible services configuration file.",
        prog="manage_opensearch.py"
    )
    parser.add_argument(
        '--cfg',
        type=str,
        required=True,
        help='Path to the services.json configuration file (required).'
    )

    subparsers = parser.add_subparsers(dest='action', required=True, title='actions',
                                       description='Valid actions:', help='Action to perform')

    parser_add = subparsers.add_parser('add', help='Add a new OpenSearch instance.')
    parser_add.add_argument('--name', type=str, required=True, help='Name of the instance.')
    parser_add.add_argument('--host', type=str, required=True, help='Host of the instance.')
    parser_add.add_argument('--cdmver', type=str, required=True, help='CDM version of the instance.')
    parser_add.add_argument('--userpass', type=str, nargs='?', const="", default=None, help='Optional user/password credentials.')
    parser_add.add_argument('--query', action='store_true', help='Also adds the instance name to "query-from".')
    parser_add.add_argument('--index', action='store_true', help='Also sets "index-to" to this instance name.')

    parser_remove = subparsers.add_parser('remove', help='Remove an OpenSearch instance by name.')
    parser_remove.add_argument('--name', type=str, required=True, help='Name of the instance to remove.')

    parser_update = subparsers.add_parser('update', help='Update an existing OpenSearch instance.')
    parser_update.add_argument('--name', type=str, required=True, help='Name of the instance to update.')
    parser_update.add_argument('--host', type=str, help='New host for the instance.')
    parser_update.add_argument('--cdmver', type=str, help='New CDM version for the instance.')
    parser_update.add_argument('--userpass', type=str, nargs='?', const="", default=None, help='New user/password credentials. Use --remove-userpass to clear.')
    parser_update.add_argument('--remove-userpass', action='store_true', help='Removes the userpass field.')
    parser_update.add_argument('--query', action='store_true', help='Adds/ensures the instance name is in "query-from".')
    parser_update.add_argument('--no-query', action='store_true', help='Removes the instance name from "query-from".')
    parser_update.add_argument('--index', action='store_true', help='Sets "index-to" to this instance name.')

    subparsers.add_parser('info', help='Display the current OpenSearch configuration.')

    subparsers.add_parser('query-opt', help='Display the cmdline options to pass to query utils.')

    subparsers.add_parser('index-instance', help='Display the default instance to index to.')

    subparsers.add_parser('gendocs-opt', help='Display the cmdline options to pass to doc generation utils.')

    parser_gethostaccess = subparsers.add_parser('instance-host-access-opt', help='Display the host access options for a specific instance.')
    parser_gethostaccess.add_argument('--name', type=str, required=True, help='Name of the instance.')

    parser_getcdmver = subparsers.add_parser('instance-cdmver-opt', help='Display the CDM version for a specific instance.')
    parser_getcdmver.add_argument('--name', type=str, required=True, help='Name of the instance.')

    subparsers.add_parser('list-instance-names', help='Display a list of all instance names.')

    args = parser.parse_args()

    config_filepath = args.cfg
    data = load_json_data(config_filepath)

    if args.action == 'add':
        if add_instance(data, args.name, args.host, args.cdmver, args.userpass, args.query, args.index):
            write_required = True
    elif args.action == 'remove':
        if remove_instance(data, args.name):
            write_required = True
    elif args.action == 'update':
        update_action_specified = any([
            args.host is not None,
            args.cdmver is not None,
            args.userpass is not None,
            args.remove_userpass,
            args.index,
            args.query,
            args.no_query
        ])
        if not update_action_specified:
            parser_update.error("At least one update field (--host, --cdmver, --userpass, --remove-userpass, --index, --query) must be specified.")

        effective_userpass = args.userpass if not args.remove_userpass else None
        if update_instance(data, args.name, args.host, args.cdmver, effective_userpass, args.index, args.query, args.no_query, args.remove_userpass):
            write_required = True
    elif args.action == 'info':
        display_info(config_filepath, data)
    elif args.action == 'query-opt':
        query_opt(data)
    elif args.action == 'index-instance':
        index_instance(data)
    elif args.action == 'gendocs-opt':
        gendocs_opt(data)
    elif args.action == 'instance-cdmver-opt':
        instance_cdmver_opt(data, args.name)
    elif args.action == 'instance-host-access-opt':
        instance_host_access_opt(data, args.name)
    elif args.action == 'list-instance-names':
        list_instance_names(data)

    if write_required:
        print("writing config file")
        save_json_data(config_filepath, data)

if __name__ == "__main__":
    main()
