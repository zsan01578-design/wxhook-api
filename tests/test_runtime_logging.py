import os
import subprocess
import sys


def test_launcher_persists_and_rotates_application_errors_without_http_urls(tmp_path):
    path = tmp_path / "runtime.log"
    code = """
import logging
import logging.config
from wxhook_api.core.config import load_settings
from wxhook_api.launcher import service_log_config
config = service_log_config(load_settings())
config['handlers']['persistent']['maxBytes'] = 100
config['handlers']['persistent']['backupCount'] = 2
logging.config.dictConfig(config)
for number in range(20):
    logging.getLogger('wxhook_api.services.events').error('synthetic failure %s', number)
logging.getLogger('httpx').info('private_request_url_and_token')
logging.shutdown()
"""
    env = {**os.environ, "WXHOOK_LOG_FILE": str(path)}
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    files = sorted(tmp_path.glob("runtime.log*"))
    assert len(files) == 3
    text = "".join(file.read_text(encoding="utf-8") for file in files)
    assert "synthetic failure 19" in text and "wxhook_api.services.events" in text
    assert "private_request_url_and_token" not in text
