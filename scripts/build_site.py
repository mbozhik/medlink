"""Build the GitHub Pages demo from the same application used by uvicorn."""
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "api" / "main.py"
DEST = ROOT / "site"


def build():
    spec = spec_from_file_location("medlink_site_source", SOURCE)
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    schema = module.app.openapi()
    # Paths already include /api/v1. This virtual origin never receives network
    # requests: Swagger's userFetch delegates API calls to the browser worker.
    schema["servers"] = [{"url": "https://medlink.local", "description": "Python в браузере · временные данные"}]
    (DEST / "api").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE, DEST / "api" / "main.py")
    (DEST / "openapi.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
    (DEST / ".nojekyll").touch()
    print(f"Built {len(schema['paths'])} paths; API source copied byte-for-byte to {DEST}")


if __name__ == "__main__":
    build()
