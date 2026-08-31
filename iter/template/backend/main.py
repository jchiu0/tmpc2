from pathlib import Path

from runtime.app import create_app


ROOT = Path(__file__).resolve().parent.parent
app = create_app(ROOT / "app_spec.json", ROOT / "data" / "app.db")
