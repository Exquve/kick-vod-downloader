"""PyInstaller giriş noktası."""
import multiprocessing
import sys

from kickindirici.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
