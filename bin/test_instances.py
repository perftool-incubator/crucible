import unittest
import json
import os
import shutil # For copying file
from unittest.mock import patch, mock_open # For mocking file operations and sys.exit
import sys

# Add the directory containing manage_instances.py to the Python path
# This assumes test_manage_instances.py is in the same directory as manage_instances.py
# or that manage_instances.py is installed/accessible in the PYTHONPATH.
# For simplicity in this environment, we'll assume they are in the same directory
# and manage_instances can be imported directly.
import manage_instances # The script we are testing

class TestManageInstances(unittest.TestCase):

    def setUp(self):
        """Set up for test methods."""
        self.test_dir = "test_json_data_dir"
        os.makedirs(self.test_dir, exist_ok=True)
        self.test_file_path = os.path.join(self.test_dir, "test_instances.json")

        # manage_instances.py still defines DEFAULT_DATA
        self.default_data_backup = manage_instances.DEFAULT_DATA.copy()

        self.initial_data = {
            "instances": [
                {"name": "test1", "host": "host1", "cdmver": "v1"},
                {"name": "test2", "host": "host2", "cdmver": "v2", "userpass": "pass2"}
            ],
            "index-to": "test1",
            "query-from": ["test1", "test2"]
        }
        # Create a fresh test file for each test
        with open(self.test_file_path, 'w') as f:
            json.dump(self.initial_data.copy(), f, indent=2) # Use a copy

    def tearDown(self):
        """Tear down after test methods."""
        if os.path.exists(self.test_file_path):
            os.remove(self.test_file_path)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir) # Remove the test directory
        manage_instances.DEFAULT_DATA = self.default_data_backup # Restore default data

    # --- Test load_json_data ---
    def test_load_json_data_existing_file(self):
        data = manage_instances.load_json_data(self.test_file_path)
        self.assertEqual(data, self.initial_data)

    def test_load_json_data_non_existent_file(self):
        non_existent_file = os.path.join(self.test_dir, "non_existent.json")
        data = manage_instances.load_json_data(non_existent_file)
        self.assertEqual(data, manage_instances.DEFAULT_DATA)


    def test_load_json_data_empty_file(self):
        empty_file = os.path.join(self.test_dir, "empty.json")
        with open(empty_file, 'w') as f:
            pass # Create an empty file
        data = manage_instances.load_json_data(empty_file)
        self.assertEqual(data, manage_instances.DEFAULT_DATA)


    def test_load_json_data_missing_keys(self):
        corrupt_data = {"instances": [{"name": "corrupt", "host": "ch", "cdmver": "cv"}]} # Missing index-to, query-from
        corrupt_file = os.path.join(self.test_dir, "corrupt_missing_keys.json")
        with open(corrupt_file, 'w') as f:
            json.dump(corrupt_data, f)

        loaded_data = manage_instances.load_json_data(corrupt_file)
        self.assertIn("instances", loaded_data)
        self.assertEqual(loaded_data["instances"], corrupt_data["instances"])
        self.assertIsNone(loaded_data["index-to"]) # Should be initialized
        self.assertEqual(loaded_data["query-from"], []) # Should be initialized

    @patch('builtins.print') # Mock print to suppress output during test
    @patch('sys.exit') # Mock sys.exit
    def test_load_json_data_invalid_json(self, mock_exit, mock_print):
        invalid_json_file = os.path.join(self.test_dir, "invalid.json")
        with open(invalid_json_file, 'w') as f:
            f.write("{not_json: ") # Invalid JSON content

        manage_instances.load_json_data(invalid_json_file)
        mock_exit.assert_called_once_with(1)


    # --- Test save_json_data ---
    def test_save_json_data(self):
        data_to_save = {"instances": [{"name": "new", "host": "new_host", "cdmver": "new_v"}], "index-to": "new", "query-from": ["new"]}
        manage_instances.save_json_data(self.test_file_path, data_to_save)
        with open(self.test_file_path, 'r') as f:
            saved_data = json.load(f)
        self.assertEqual(saved_data, data_to_save)

    # --- Test add_instance ---
    def test_add_instance_new(self):
        data = self.initial_data.copy()
        result = manage_instances.add_instance(data, "test3", "host3", "v3")
        self.assertTrue(result)
        self.assertEqual(len(data["instances"]), 3)
        self.assertEqual(data["instances"][-1]["name"], "test3")

    def test_add_instance_with_userpass(self):
        data = self.initial_data.copy()
        result = manage_instances.add_instance(data, "test_up", "host_up", "v_up", userpass="secret")
        self.assertTrue(result)
        self.assertEqual(data["instances"][-1]["userpass"], "secret")

    def test_add_instance_empty_userpass(self):
        data = self.initial_data.copy()
        result = manage_instances.add_instance(data, "test_empty_up", "host_empty_up", "v_empty_up", userpass="")
        self.assertTrue(result)
        self.assertIn("userpass", data["instances"][-1])
        self.assertEqual(data["instances"][-1]["userpass"], "")

    def test_add_instance_existing(self):
        data = self.initial_data.copy()
        result = manage_instances.add_instance(data, "test1", "host_new", "v_new")
        self.assertFalse(result)
        self.assertEqual(len(data["instances"]), 2)

    def test_add_instance_set_index_to(self):
        data = self.initial_data.copy()
        manage_instances.add_instance(data, "test_idx", "host_idx", "v_idx", set_index_to=True)
        self.assertEqual(data["index-to"], "test_idx")

    def test_add_instance_add_to_query_from(self):
        data = self.initial_data.copy()
        manage_instances.add_instance(data, "test_qf", "host_qf", "v_qf", set_query_from=True)
        self.assertIn("test_qf", data["query-from"])

    def test_add_instance_add_to_query_from_already_exists(self):
        data = self.initial_data.copy()
        manage_instances.add_instance(data, "test1_new", "h", "v", set_query_from=True)
        data["query-from"].append("test1")
        manage_instances.add_instance(data, "test_qf_again", "h_qfa", "v_qfa", set_query_from=True)
        self.assertIn("test_qf_again", data["query-from"])

    # --- Test remove_instance ---
    def test_remove_instance_existing(self):
        data = self.initial_data.copy()
        result = manage_instances.remove_instance(data, "test1")
        self.assertTrue(result)
        self.assertEqual(len(data["instances"]), 1)
        self.assertNotIn("test1", [inst["name"] for inst in data["instances"]])
        self.assertNotIn("test1", data["query-from"])
        self.assertIsNone(data["index-to"])

    def test_remove_instance_non_existing(self):
        data = self.initial_data.copy()
        result = manage_instances.remove_instance(data, "non_existent_instance")
        self.assertFalse(result)
        self.assertEqual(len(data["instances"]), 2)

    def test_remove_instance_updates_query_from(self):
        data = self.initial_data.copy()
        manage_instances.remove_instance(data, "test2")
        self.assertNotIn("test2", data["query-from"])

    def test_remove_instance_updates_index_to_and_warns(self):
        data = self.initial_data.copy()
        data["index-to"] = "test2"
        with patch('builtins.print') as mock_print:
            manage_instances.remove_instance(data, "test2")
            self.assertIsNone(data["index-to"])
            mock_print.assert_any_call("'index-to' was 'test2', now set to None. Warning: No instance is currently configured for 'index-to'.")

    # --- Test update_instance ---
    def test_update_instance_existing_host_cdmver(self):
        data = self.initial_data.copy()
        result = manage_instances.update_instance(data, "test1", new_host="new_host1", new_cdmver="v1_new")
        self.assertTrue(result)
        self.assertEqual(data["instances"][0]["host"], "new_host1")
        self.assertEqual(data["instances"][0]["cdmver"], "v1_new")

    def test_update_instance_userpass(self):
        data = self.initial_data.copy()
        manage_instances.update_instance(data, "test1", new_userpass="pass1_new")
        self.assertEqual(data["instances"][0]["userpass"], "pass1_new")
        manage_instances.update_instance(data, "test2", new_userpass="pass2_updated")
        self.assertEqual(data["instances"][1]["userpass"], "pass2_updated")

    def test_update_instance_remove_userpass(self):
        data = self.initial_data.copy()
        self.assertIn("userpass", data["instances"][1])
        manage_instances.update_instance(data, "test2", remove_userpass_flag=True)
        self.assertNotIn("userpass", data["instances"][1])

    def test_update_instance_remove_userpass_not_set(self):
        data = self.initial_data.copy()
        self.assertNotIn("userpass", data["instances"][0])
        with patch('builtins.print') as mock_print:
            result = manage_instances.update_instance(data, "test1", remove_userpass_flag=True)
            self.assertFalse(result) # Expect False as no actual data change occurred
            expected_print_msg = "Info: Instance 'test1' found, but no update parameters provided or no changes were applicable."
            mock_print.assert_any_call(expected_print_msg)
        self.assertNotIn("userpass", data["instances"][0])


    def test_update_instance_set_index_to(self):
        data = self.initial_data.copy()
        manage_instances.update_instance(data, "test2", set_index_to=True)
        self.assertEqual(data["index-to"], "test2")

    def test_update_instance_add_to_query_from(self):
        data = self.initial_data.copy()
        data["query-from"] = ["test1"]
        manage_instances.update_instance(data, "test2", add_to_query_from=True)
        self.assertIn("test2", data["query-from"])

    def test_update_instance_non_existing(self):
        data = self.initial_data.copy()
        result = manage_instances.update_instance(data, "non_existent_instance", new_host="dummy")
        self.assertFalse(result)

    def test_update_instance_no_changes_specified(self):
        data = self.initial_data.copy()
        with patch('builtins.print') as mock_print:
            result = manage_instances.update_instance(data, "test1")
            self.assertFalse(result)
            mock_print.assert_any_call("Info: Instance 'test1' found, but no update parameters provided or no changes were applicable.")

    # --- Test display_info ---
    @patch('builtins.print')
    def test_display_info(self, mock_print):
        data = self.initial_data.copy()
        manage_instances.display_info(self.test_file_path, data)
        mock_print.assert_any_call(f"Current configuration from '{self.test_file_path}':")
        args, _ = mock_print.call_args_list[1]
        self.assertEqual(json.loads(args[0]), data)


    # --- Test main function (basic argument parsing checks) ---
    @patch('manage_instances.load_json_data')
    @patch('manage_instances.add_instance')
    @patch('manage_instances.save_json_data')
    def test_main_add_action(self, mock_save, mock_add, mock_load):
        mock_load.return_value = self.initial_data.copy()
        mock_add.return_value = True
        # Updated test_args for subcommand 'add'
        test_args = ['manage_instances.py', '--cfg', self.test_file_path, 'add', '--name', 'new_inst', '--host', 'h', '--cdmver', 'cv']
        with patch.object(sys, 'argv', test_args):
            manage_instances.main()
        mock_load.assert_called_once_with(self.test_file_path)
        mock_add.assert_called_once_with(self.initial_data, 'new_inst', 'h', 'cv', None, False, False)
        mock_save.assert_called_once()

    @patch('manage_instances.load_json_data')
    @patch('manage_instances.remove_instance')
    @patch('manage_instances.save_json_data')
    def test_main_remove_action(self, mock_save, mock_remove, mock_load):
        mock_load.return_value = self.initial_data.copy()
        mock_remove.return_value = True
        # Updated test_args for subcommand 'remove'
        test_args = ['manage_instances.py', '--cfg', self.test_file_path, 'remove', '--name', 'test1']
        with patch.object(sys, 'argv', test_args):
            manage_instances.main()
        mock_remove.assert_called_once_with(self.initial_data, 'test1')
        mock_save.assert_called_once()

    @patch('manage_instances.load_json_data')
    @patch('manage_instances.update_instance')
    @patch('manage_instances.save_json_data')
    def test_main_update_action(self, mock_save, mock_update, mock_load):
        mock_load.return_value = self.initial_data.copy()
        mock_update.return_value = True
        # Updated test_args for subcommand 'update'
        test_args = ['manage_instances.py', '--cfg', self.test_file_path, 'update', '--name', 'test1', '--host', 'new_h']
        with patch.object(sys, 'argv', test_args):
            manage_instances.main()
        mock_update.assert_called_once_with(self.initial_data, 'test1', 'new_h', None, None, False, False, False, False)
        mock_save.assert_called_once()

    @patch('manage_instances.load_json_data')
    @patch('manage_instances.display_info')
    #@patch('sys.exit')
    #def test_main_info_action(self, mock_sys_exit, mock_display, mock_load):
    def test_main_info_action(self, mock_display, mock_load):
        mock_load.return_value = self.initial_data.copy()
        # Updated test_args for subcommand 'info'
        test_args = ['manage_instances.py', '--cfg', self.test_file_path, 'info']
        with patch.object(sys, 'argv', test_args):
            manage_instances.main()
        mock_display.assert_called_once_with(self.test_file_path, self.initial_data)
        #mock_sys_exit.assert_called_once_with(0)

    @patch('argparse.ArgumentParser.error')
    def test_main_add_missing_args(self, mock_argparse_error):
        mock_argparse_error.side_effect = SystemExit(2)
        # Updated test_args for subcommand 'add' with missing args
        test_args = ['manage_instances.py', '--cfg', self.test_file_path, 'add', '--name', 'only_name'] # Missing --host, --cdmver
        with patch.object(sys, 'argv', test_args):
            with self.assertRaises(SystemExit):
                 manage_instances.main()
        # Argparse error for missing required args in a subparser
        mock_argparse_error.assert_called_once_with("the following arguments are required: --host, --cdmver")


    @patch('argparse.ArgumentParser.error')
    def test_main_update_missing_action_args(self, mock_argparse_error):
        mock_argparse_error.side_effect = SystemExit(2)
        # Updated test_args for subcommand 'update' with no action fields
        test_args = ['manage_instances.py', '--cfg', self.test_file_path, 'update', '--name', 'test1']
        with patch.object(sys, 'argv', test_args):
            with self.assertRaises(SystemExit):
                manage_instances.main()
        # This specific error message comes from parser_update.error() in manage_instances.py
        mock_argparse_error.assert_called_once_with("At least one update field (--host, --cdmver, --userpass, --remove-userpass, --index, --query) must be specified.")

    @patch('argparse.ArgumentParser.error')
    def test_main_missing_cfg_arg(self, mock_argparse_error):
        mock_argparse_error.side_effect = SystemExit(2)
        test_args = ['manage_instances.py', 'add', '--name', 'n', '--host', 'h', '--cdmver', 'v'] # Missing --cfg
        with patch.object(sys, 'argv', test_args):
            with self.assertRaises(SystemExit):
                manage_instances.main()
        # Argparse error for missing top-level required argument
        mock_argparse_error.assert_called_once_with('the following arguments are required: --cfg')


if __name__ == '__main__':
    unittest.main(argv=['first-arg-is-ignored'], exit=False)

