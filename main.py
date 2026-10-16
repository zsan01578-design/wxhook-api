"""PyCharm entry point: start WeChat, the API and the message page together."""

from pathlib import Path
import sys


# Allow running this file without an editable package installation.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wxhook_api.launcher import main


if __name__ == "__main__":
    main()
