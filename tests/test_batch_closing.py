"""Hardware-free regression tests for closing a batch from settings."""
import ast
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def load_routes(namespace):
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef)
             and node.name in ("batch_settings", "close_batch")]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(nodes, []), "app.py", "exec"), namespace)


class BatchClosingTests(unittest.TestCase):
    def test_close_then_stale_save_keeps_batch_in_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "tilt.json"
            index_path = Path(directory) / "history.json"
            log_path = Path(directory) / "batch.jsonl"
            tilt_cfg = {"Red": {"brewid": "batch-1", "beer_name": "Stout", "is_active": True}}

            def save_json(path, value):
                Path(path).write_text(json.dumps(value), encoding="utf-8")
                return True

            def append_metadata(color, record):
                with log_path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(record) + "\n")
                return True

            request = SimpleNamespace(method="POST", form={"color": "Red", "brewid": "batch-1"})
            namespace = {
                "request": request,
                "tilt_cfg": tilt_cfg,
                "valid_device_key": lambda color: color if color == "Red" else None,
                "load_batch_index": lambda color: json.loads(index_path.read_text()) if index_path.exists() else [],
                "save_batch_index": lambda color, records: save_json(index_path, records),
                "load_latest_batch_metadata": lambda brewid: {},
                "append_batch_metadata_to_batch_jsonl": append_metadata,
                "save_json": save_json,
                "TILT_CONFIG_FILE": config_path,
                "log_event": lambda *args, **kwargs: None,
                "jsonify": lambda value: value,
                "datetime": datetime,
                "json": json,
            }
            load_routes(namespace)

            result = namespace["close_batch"]()
            self.assertTrue(result["success"])
            self.assertFalse(json.loads(index_path.read_text())[0]["is_active"])
            self.assertFalse(json.loads(log_path.read_text())["is_active"])
            self.assertEqual(json.loads(config_path.read_text())["Red"]["brewid"], "")

            request.form = {"tilt_color": "Red", "expected_brewid": "batch-1",
                            "beer_name": "Stout"}
            self.assertEqual(namespace["batch_settings"]()[1], 409)
            self.assertFalse(json.loads(index_path.read_text())[0]["is_active"])
            self.assertEqual(json.loads(config_path.read_text())["Red"]["brewid"], "")

    def test_empty_stale_form_cannot_replace_new_batch(self):
        request = SimpleNamespace(method="POST", form={
            "tilt_color": "Red", "expected_brewid": "", "beer_name": "Old form",
        })
        namespace = {"request": request, "tilt_cfg": {"Red": {"brewid": "new-batch"}},
                     "valid_device_key": lambda color: color}
        load_routes(namespace)

        self.assertEqual(namespace["batch_settings"]()[1], 409)
        self.assertEqual(namespace["tilt_cfg"]["Red"]["brewid"], "new-batch")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the UI regression test")
    def test_slider_navigation_and_failure_handling(self):
        template = (ROOT / "templates" / "batch_settings.html").read_text(encoding="utf-8")
        script = template[template.index("    var batchStatePending = false;"):
                          template.index("    function pauseTracking(color)")]
        script = script.replace("{{ selected_tilt | tojson }}", '"Red"')
        script = script.replace("{{ url_for('batch_settings') | tojson }}", '"/batch_settings"')
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let source = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => source += chunk);
process.stdin.on('end', async () => {
  for (const scenario of ['close', 'cancel', 'failure', 'network', 'reopen']) {
    const button = {disabled: false};
    const slider = {checked: scenario !== 'reopen', disabled: false, dataset: {brewid: 'batch-1'}};
    let destination = '', calls = 0, message = '';
    const context = {
      document: {querySelector: () => button},
      window: {location: {replace: url => destination = url, reload: () => destination = 'reload'}},
      confirm: () => scenario !== 'cancel',
      alert: text => message = text,
      fetch: (url, options) => {
        calls++;
        assert.equal(slider.disabled, true);
        assert.equal(button.disabled, true);
        assert.equal(context.batchStatePending, true);
        assert.equal(url, scenario === 'reopen' ? '/reopen_batch' : '/close_batch');
        assert.equal(options.body, 'brewid=batch-1&color=Red');
        if (scenario === 'network') return Promise.reject(new Error('offline'));
        return Promise.resolve({json: () => Promise.resolve(
          scenario === 'failure' ? {success: false, error: 'failed'} : {success: true}
        )});
      }
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    context.toggleBatchState(slider);
    await new Promise(resolve => setImmediate(resolve));
    if (scenario === 'close') assert.equal(destination, '/batch_settings');
    if (scenario === 'reopen') assert.equal(destination, 'reload');
    if (scenario === 'cancel' || scenario === 'failure' || scenario === 'network') {
      assert.equal(slider.checked, false);
      assert.equal(slider.disabled, false);
      assert.equal(button.disabled, false);
      assert.equal(context.batchStatePending, false);
      assert.equal(destination, '');
    }
    if (scenario === 'cancel') assert.equal(calls, 0);
    if (scenario === 'failure') assert.match(message, /failed/);
    if (scenario === 'network') assert.match(message, /offline/);
  }
});
"""
        result = subprocess.run(["node", "-e", harness], input=script, text=True,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
