# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "blackboxprotobuf==1.0.1",
#   "cryptography>=43,<51",
#   "jieba==0.42.1",
#   "flask>=3.1,<4",
# ]
# ///
"""Client-independent entry point; uv manages dependencies outside the skill."""
import os
from pathlib import Path
import runpy
import sys


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Requires Python 3.10 or newer.")
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    here = Path(__file__).resolve().parent
    if sys.argv[1:] == ["--python-path"]:
        print(sys.executable)
        return
    if sys.argv[1:2] == ["compare"]:
        target = here / "compare_groups.py"
        sys.argv = [str(target), *sys.argv[2:]]
    else:
        target = here / "runtime" / "cli.py"
        sys.argv[0] = str(target)
    try:
        import blackboxprotobuf  # noqa: F401
        import cryptography  # noqa: F401
    except ImportError:
        raise SystemExit("Missing dependencies: use uv run --locked --script scripts/qq.py, "
                         "or install scripts/runtime/requirements.txt into your Python environment.")
    sys.path.insert(0, str(here / "runtime"))
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
