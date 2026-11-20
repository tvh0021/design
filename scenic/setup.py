from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithConfig(build_py):
    """Copy top-level config/*.toml into scenic/_config/ when building a wheel."""

    def run(self) -> None:
        super().run()
        cfg_src = Path(__file__).resolve().parent / "config"
        if cfg_src.is_dir():
            dst = Path(self.build_lib) / "scenic" / "_config"
            dst.mkdir(parents=True, exist_ok=True)
            for toml_file in sorted(cfg_src.glob("*.toml")):
                shutil.copy2(toml_file, dst / toml_file.name)


setup(cmdclass={"build_py": BuildPyWithConfig})
