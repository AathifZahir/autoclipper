import argparse
import sys

if sys.version_info < (3, 11):
    sys.exit("AutoClipper needs Python 3.11 or newer. Install it from python.org and run setup.bat again.")

from .app import main

p = argparse.ArgumentParser(prog="autoclipper", description="Automatic Twitch clipper")
p.add_argument("--test-mode", action="store_true", help="show moments but don't create clips")
p.add_argument("--clip-now", metavar="CHANNEL", help="make one clip of a live channel right now")
args = p.parse_args()
sys.exit(main(dry_run=args.test_mode, clip_now=args.clip_now))
