import sys
from pathlib import Path

# Ensure project root is on sys.path so imports like `from queue.url_queue import …` work.
sys.path.insert(0, str(Path(__file__).parent.parent))
