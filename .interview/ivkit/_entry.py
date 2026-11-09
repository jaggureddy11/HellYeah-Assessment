#!/usr/bin/env python3
"""Real entry point. The launchers call this; nobody needs to type it."""
import os, sys, pathlib

pkg_parent = pathlib.Path(__file__).resolve().parents[1]      # .interview
sys.path.insert(0, str(pkg_parent))
os.environ.setdefault("INTERVIEW_REPO", str(pkg_parent.parent))

from ivkit.cli import main
sys.exit(main(sys.argv[1:]))
