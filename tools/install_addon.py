from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "blender_addon" / "phone_hand_controller"


def candidate_roots() -> list[Path]:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    blender = appdata / "Blender Foundation" / "Blender"
    if not blender.exists():
        return []
    roots = []
    for version_dir in sorted(blender.iterdir()):
        if not version_dir.is_dir():
            continue
        try:
            major = int(version_dir.name.split(".", 1)[0])
        except ValueError:
            continue
        if major >= 4:
            roots.append(version_dir / "scripts" / "addons")
    return roots


def install(destination_root: Path) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = (destination_root / "phone_hand_controller").resolve()
    expected_parent = destination_root.resolve()
    if destination.parent != expected_parent or destination.name != "phone_hand_controller":
        raise RuntimeError(f"refusing unexpected destination: {destination}")
    shutil.copytree(
        SOURCE,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Blender add-on into user add-on folders")
    parser.add_argument("--destination-root", type=Path, action="append", default=[])
    args = parser.parse_args()
    roots = args.destination_root or candidate_roots()
    if not roots:
        print("No Blender 4.x user add-on folder found. Use --destination-root to specify one.")
        return 1
    for root in roots:
        destination = install(root)
        print(f"installed: {destination}")
    print("Restart Blender or use Edit > Preferences > Add-ons > Refresh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
