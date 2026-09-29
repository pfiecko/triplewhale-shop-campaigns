import os
from pathlib import Path

os.environ.setdefault("CONFIG_PATH", str(Path(__file__).with_name("config.test.yaml")))
