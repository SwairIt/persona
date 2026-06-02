"""Entry point: `python -m app <subcommand>`."""
from app.cli import main
import sys
if __name__ == "__main__":
    sys.exit(main())
