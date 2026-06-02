import unittest
import json
import os
import shutil
from unittest.mock import patch
import sys

import manage_opensearch

class TestManageOpenSearch(unittest.TestCase):

    def setUp(self):
        self.test_dir = "test_json_data_dir"
        os.makedirs(self.test_dir, exist_ok=True)
        self.test_file_path = os.path.join(self.test_dir, "test_services.json")

        self.initial_opensearch_data = {
            "instances": [
                {"name": "test1", "host": "host1", "cdmver": "v1"},
                {"name": "test2", "host": "host2", "cdmver": "v2", "userpass": "pass2"}
            ],
            "index-to": "test1",
            "query-from": ["test1", "test2"]
        }

        self.initial_services_data = {
            "cdm-server": {"port": 3000},
            "httpd": {"port": 8080},
            "image-sourcing": {
                "use": True,
                "services": {
                    "x86_64": {
                        "start": True,
                        "location": {"address": "localhost", "port": 8888, "protocol": "http"}
                    }
                }
            },
            "valkey": {"monitor": {"enabled": False}},
            "opensearch": self.initial_opensearch_data.copy()
        }

        with open(self.test_file_path, 'w') as f:
            json.dump(self.initial_services_data, f, indent=4)

    def tearDown(self):
        if os.path.exists(self.test_file_path):
            os.remove(self.test_file_path)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    # --- Test load_json_data ---
    def test_load_json_data_existing_file(self):
        data = manage_opensearch.load_json_data(self.test_file_path)
        self.assertEqual(data, self.initial_opensearch_data)

    @patch('sys.exit')
    def test_load_json_data_non_existent_file(self, mock_exit):
        mock_exit.side_effect = SystemExit(1)
        non_existent_file = os.path.join(self.test_dir, "non_existent.json")
        with self.assertRaises(SystemExit):
            manage_opensearch.load_json_data(non_existent_file)
        mock_exit.assert_called_once_with(1)

    @patch('sys.exit')
    def test_load_json_data_empty_file(self, mock_exit):
        mock_exit.side_effect = SystemExit(1)
        empty_file = os.path.join(self.test_dir, "empty.json")
        with open(empty_file, 'w') as f:
            pass
        with self.assertRaises(SystemExit):
            manage_opensearch.load_json_data(empty_file)
        mock_exit.assert_called_once_with(1)

    @patch('sys.exit')
    def test_load_json_data_missing_opensearch_key(self, mock_exit):
        mock_exit.side_effect = SystemExit(1)
        no_opensearch_file = os.path.join(self.test_dir, "no_opensearch.json")
        with open(no_opensearch_file, 'w') as f:
            json.dump({"cdm-server": {"port": 3000}}, f)
        with self.assertRaises(SystemExit):
            manage_opensearch.load_json_data(no_opensearch_file)
        mock_exit.assert_called_once_with(1)

    def test_load_json_data_missing_inner_keys(self):
        partial_file = os.path.join(self.test_dir, "partial.json")
        partial_data = {
            "cdm-server": {"port": 3000},
            "httpd": {"port": 8080},
            "image-sourcing": {"use": True, "services": {}},
            "valkey": {"monitor": {"enabled": False}},
            "opensearch": {"instances": [{"name": "x", "host": "h", "cdmver": "v"}]}
        }
        with open(partial_file, 'w') as f:
            json.dump(partial_data, f)
        loaded_data = manage_opensearch.load_json_data(partial_file)
        self.assertIn("instances", loaded_data)
        self.assertIsNone(loaded_data["index-to"])
        self.assertEqual(loaded_data["query-from"], [])

    @patch('builtins.print')
    @patch('sys.exit')
    def test_load_json_data_invalid_json(self, mock_exit, mock_print):
        mock_exit.side_effect = SystemExit(1)
        invalid_json_file = os.path.join(self.test_dir, "invalid.json")
        with open(invalid_json_file, 'w') as f:
            f.write("{not_json: ")
        with self.assertRaises(SystemExit):
            manage_opensearch.load_json_data(invalid_json_file)
        mock_exit.assert_called_once_with(1)

    # --- Test save_json_data ---
    def test_save_json_data_preserves_other_sections(self):
        new_opensearch = {"instances": [{"name": "new", "host": "new_host", "cdmver": "new_v"}], "index-to": "new", "query-from": ["new"]}
        manage_opensearch.save_json_data(self.test_file_path, new_opensearch)
        with open(self.test_file_path, 'r') as f:
            saved_data = json.load(f)
        self.assertEqual(saved_data["opensearch"], new_opensearch)
        self.assertEqual(saved_data["cdm-server"], {"port": 3000})
        self.assertEqual(saved_data["httpd"], {"port": 8080})
        self.assertIn("valkey", saved_data)
        self.assertIn("image-sourcing", saved_data)

    # --- Test add_instance ---
    def test_add_instance_new(self):
        data = self.initial_opensearch_data.copy()
        result = manage_opensearch.add_instance(data, "test3", "host3", "v3")
        self.assertTrue(result)
        self.assertEqual(len(data["instances"]), 3)
        self.assertEqual(data["instances"][-1]["name"], "test3")

    def test_add_instance_with_userpass(self):
        data = self.initial_opensearch_data.copy()
        result = manage_opensearch.add_instance(data, "test_up", "host_up", "v_up", userpass="secret")
        self.assertTrue(result)
        self.assertEqual(data["instances"][-1]["userpass"], "secret")

    def test_add_instance_empty_userpass(self):
        data = self.initial_opensearch_data.copy()
        result = manage_opensearch.add_instance(data, "test_empty_up", "host_empty_up", "v_empty_up", userpass="")
        self.assertTrue(result)
        self.assertIn("userpass", data["instances"][-1])
        self.assertEqual(data["instances"][-1]["userpass"], "")

    def test_add_instance_existing(self):
        data = self.initial_opensearch_data.copy()
        result = manage_opensearch.add_instance(data, "test1", "host_new", "v_new")
        self.assertFalse(result)
        self.assertEqual(len(data["instances"]), 2)

    def test_add_instance_set_index_to(self):
        data = self.initial_opensearch_data.copy()
        manage_opensearch.add_instance(data, "test_idx", "host_idx", "v_idx", set_index_to=True)
        self.assertEqual(data["index-to"], "test_idx")

    def test_add_instance_add_to_query_from(self):
        data = self.initial_opensearch_data.copy()
        manage_opensearch.add_instance(data, "test_qf", "host_qf", "v_qf", set_query_from=True)
        self.assertIn("test_qf", data["query-from"])

    def test_add_instance_add_to_query_from_already_exists(self):
        data = self.initial_opensearch_data.copy()
        manage_opensearch.add_instance(data, "test1_new", "h", "v", set_query_from=True)
        data["query-from"].append("test1")
        manage_opensearch.add_instance(data, "test_qf_again", "h_qfa", "v_qfa", set_query_from=True)
        self.assertIn("test_qf_again", data["query-from"])

    # --- Test remove_instance ---
    def test_remove_instance_existing(self):
        data = self.initial_opensearch_data.copy()
        result = manage_opensearch.remove_instance(data, "test1")
        self.assertTrue(result)
        self.assertEqual(len(data["instances"]), 1)
        self.assertNotIn("test1", [inst["name"] for inst in data["instances"]])
        self.assertNotIn("test1", data["query-from"])
        self.assertIsNone(data["index-to"])

    def test_remove_instance_non_existing(self):
        data = self.initial_opensearch_data.copy()
        result = manage_opensearch.remove_instance(data, "non_existent_instance")
        self.assertFalse(result)
        self.assertEqual(len(data["instances"]), 2)

    def test_remove_instance_updates_query_from(self):
        data = self.initial_opensearch_data.copy()
        manage_opensearch.remove_instance(data, "test2")
        self.assertNotIn("test2", data["query-from"])

    def test_remove_instance_updates_index_to_and_warns(self):
        data = self.initial_opensearch_data.copy()
        data["index-to"] = "test2"
        with patch('builtins.print') as mock_print:
            manage_opensearch.remove_instance(data, "test2")
            self.assertIsNone(data["index-to"])
            mock_print.assert_any_call("'index-to' was 'test2', now set to None. Warning: No instance is currently configured for 'index-to'.")

    # --- Test update_instance ---
    def test_update_instance_existing_host_cdmver(self):
        data = self.initial_opensearch_data.copy()
        result = manage_opensearch.update_instance(data, "test1", new_host="new_host1", new_cdmver="v1_new")
        self.assertTrue(result)
        self.assertEqual(data["instances"][0]["host"], "new_host1")
        self.assertEqual(data["instances"][0]["cdmver"], "v1_new")

    def test_update_instance_userpass(self):
        data = self.initial_opensearch_data.copy()
        manage_opensearch.update_instance(data, "test1", new_userpass="pass1_new")
        self.assertEqual(data["instances"][0]["userpass"], "pass1_new")
        manage_opensearch.update_instance(data, "test2", new_userpass="pass2_updated")
        self.assertEqual(data["instances"][1]["userpass"], "pass2_updated")

    def test_update_instance_remove_userpass(self):
        data = self.initial_opensearch_data.copy()
        self.assertIn("userpass", data["instances"][1])
        manage_opensearch.update_instance(data, "test2", remove_userpass_flag=True)
        self.assertNotIn("userpass", data["instances"][1])

    def test_update_instance_remove_userpass_not_set(self):
        data = self.initial_opensearch_data.copy()
        self.assertNotIn("userpass", data["instances"][0])
        with patch('builtins.print') as mock_print:
            result = manage_opensearch.update_instance(data, "test1", remove_userpass_flag=True)
            self.assertFalse(result)
            expected_print_msg = "Info: Instance 'test1' found, but no update parameters provided or no changes were applicable."
            mock_print.assert_any_call(expected_print_msg)
        self.assertNotIn("userpass", data["instances"][0])

    def test_update_instance_set_index_to(self):
        data = self.initial_opensearch_data.copy()
        manage_opensearch.update_instance(data, "test2", set_index_to=True)
        self.assertEqual(data["index-to"], "test2")

    def test_update_instance_add_to_query_from(self):
        data = self.initial_opensearch_data.copy()
        data["query-from"] = ["test1"]
        manage_opensearch.update_instance(data, "test2", add_to_query_from=True)
        self.assertIn("test2", data["query-from"])

    def test_update_instance_non_existing(self):
        data = self.initial_opensearch_data.copy()
        result = manage_opensearch.update_instance(data, "non_existent_instance", new_host="dummy")
        self.assertFalse(result)

    def test_update_instance_no_changes_specified(self):
        data = self.initial_opensearch_data.copy()
        with patch('builtins.print') as mock_print:
            result = manage_opensearch.update_instance(data, "test1")
            self.assertFalse(result)
            mock_print.assert_any_call("Info: Instance 'test1' found, but no update parameters provided or no changes were applicable.")

    # --- Test display_info ---
    @patch('builtins.print')
    def test_display_info(self, mock_print):
        data = self.initial_opensearch_data.copy()
        manage_opensearch.display_info(self.test_file_path, data)
        mock_print.assert_any_call(f"Current opensearch configuration from '{self.test_file_path}':")
        args, _ = mock_print.call_args_list[1]
        self.assertEqual(json.loads(args[0]), data)

    # --- Test main function ---
    @patch('manage_opensearch.load_json_data')
    @patch('manage_opensearch.add_instance')
    @patch('manage_opensearch.save_json_data')
    def test_main_add_action(self, mock_save, mock_add, mock_load):
        mock_load.return_value = self.initial_opensearch_data.copy()
        mock_add.return_value = True
        test_args = ['manage_opensearch.py', '--cfg', self.test_file_path, 'add', '--name', 'new_inst', '--host', 'h', '--cdmver', 'cv']
        with patch.object(sys, 'argv', test_args):
            manage_opensearch.main()
        mock_load.assert_called_once_with(self.test_file_path)
        mock_add.assert_called_once_with(self.initial_opensearch_data, 'new_inst', 'h', 'cv', None, False, False)
        mock_save.assert_called_once()

    @patch('manage_opensearch.load_json_data')
    @patch('manage_opensearch.remove_instance')
    @patch('manage_opensearch.save_json_data')
    def test_main_remove_action(self, mock_save, mock_remove, mock_load):
        mock_load.return_value = self.initial_opensearch_data.copy()
        mock_remove.return_value = True
        test_args = ['manage_opensearch.py', '--cfg', self.test_file_path, 'remove', '--name', 'test1']
        with patch.object(sys, 'argv', test_args):
            manage_opensearch.main()
        mock_remove.assert_called_once_with(self.initial_opensearch_data, 'test1')
        mock_save.assert_called_once()

    @patch('manage_opensearch.load_json_data')
    @patch('manage_opensearch.update_instance')
    @patch('manage_opensearch.save_json_data')
    def test_main_update_action(self, mock_save, mock_update, mock_load):
        mock_load.return_value = self.initial_opensearch_data.copy()
        mock_update.return_value = True
        test_args = ['manage_opensearch.py', '--cfg', self.test_file_path, 'update', '--name', 'test1', '--host', 'new_h']
        with patch.object(sys, 'argv', test_args):
            manage_opensearch.main()
        mock_update.assert_called_once_with(self.initial_opensearch_data, 'test1', 'new_h', None, None, False, False, False, False)
        mock_save.assert_called_once()

    @patch('manage_opensearch.load_json_data')
    @patch('manage_opensearch.display_info')
    def test_main_info_action(self, mock_display, mock_load):
        mock_load.return_value = self.initial_opensearch_data.copy()
        test_args = ['manage_opensearch.py', '--cfg', self.test_file_path, 'info']
        with patch.object(sys, 'argv', test_args):
            manage_opensearch.main()
        mock_display.assert_called_once_with(self.test_file_path, self.initial_opensearch_data)

    @patch('argparse.ArgumentParser.error')
    def test_main_add_missing_args(self, mock_argparse_error):
        mock_argparse_error.side_effect = SystemExit(2)
        test_args = ['manage_opensearch.py', '--cfg', self.test_file_path, 'add', '--name', 'only_name']
        with patch.object(sys, 'argv', test_args):
            with self.assertRaises(SystemExit):
                manage_opensearch.main()
        mock_argparse_error.assert_called_once_with("the following arguments are required: --host, --cdmver")

    @patch('argparse.ArgumentParser.error')
    def test_main_update_missing_action_args(self, mock_argparse_error):
        mock_argparse_error.side_effect = SystemExit(2)
        test_args = ['manage_opensearch.py', '--cfg', self.test_file_path, 'update', '--name', 'test1']
        with patch.object(sys, 'argv', test_args):
            with self.assertRaises(SystemExit):
                manage_opensearch.main()
        mock_argparse_error.assert_called_once_with("At least one update field (--host, --cdmver, --userpass, --remove-userpass, --index, --query) must be specified.")

    @patch('argparse.ArgumentParser.error')
    def test_main_missing_cfg_arg(self, mock_argparse_error):
        mock_argparse_error.side_effect = SystemExit(2)
        test_args = ['manage_opensearch.py', 'add', '--name', 'n', '--host', 'h', '--cdmver', 'v']
        with patch.object(sys, 'argv', test_args):
            with self.assertRaises(SystemExit):
                manage_opensearch.main()
        mock_argparse_error.assert_called_once_with('the following arguments are required: --cfg')


if __name__ == '__main__':
    unittest.main(argv=['first-arg-is-ignored'], exit=False)
