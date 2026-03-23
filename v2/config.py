import os

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
JUDGE_MODEL = "gemini-3.1-flash-lite-preview"

# Paths (relative to v2/)
DATA_DIR = os.path.join(os.path.dirname(__file__), "..")
V2_DIR = os.path.dirname(__file__)
SEARCHES_DIR = os.path.join(V2_DIR, "searches")
GLOBAL_RULES_PATH = os.path.join(V2_DIR, "global_rules.json")
