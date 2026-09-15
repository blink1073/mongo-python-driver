"""A custom hatch build hook for pymongo."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomHook(BuildHookInterface):
    """The pymongo build hook."""

    def initialize(self, version, build_data):
        """Initialize the hook."""
        if self.target_name == "sdist":
            return
        here = Path(__file__).parent.resolve()
        sys.path.insert(0, str(here))

        subprocess.run([sys.executable, "_setup.py", "build_ext", "-i"], check=True)

        # Ensure wheel is marked as binary and contains the binary files.
        build_data["infer_tag"] = True
        build_data["pure_python"] = False
        if os.environ.get("PYMONGO_BUILD_ABI3"):
            from packaging.tags import sys_tags

            # abi3 wheels support Python 3.11+ (with the GIL). Build the
            # cp311-abi3-<platform> tag from the current platform.
            tag = next(
                t
                for t in sys_tags()
                if "manylinux" not in t.platform and "musllinux" not in t.platform
            )
            build_data["tag"] = f"cp311-abi3-{tag.platform}"
        if os.name == "nt":
            patt = ".pyd"
        else:
            patt = ".so"
        for pkg in ["bson", "pymongo"]:
            dpath = here / pkg
            for fpath in dpath.glob(f"*{patt}"):
                relpath = os.path.relpath(fpath, here)
                build_data["artifacts"].append(relpath)
                build_data["force_include"][relpath] = relpath
