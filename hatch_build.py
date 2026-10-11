import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

GENERATED = Path("src") / "blackice_mcp" / "_build.py"


class CommitHashHook(BuildHookInterface):
    def initialize(self, version, build_data):
        build_data["force_include"][str(GENERATED)] = str(GENERATED)

        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "--short=8", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                cwd=self.root,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            commit = "unknown"

        (Path(self.root) / GENERATED).write_text(f'COMMIT = "{commit}"\n')
