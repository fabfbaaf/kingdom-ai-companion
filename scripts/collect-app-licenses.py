"""Preserve installed Python dependency licenses for a portable app build."""

from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
import sys
from pathlib import Path


def main() -> None:
    project = Path(__file__).resolve().parent.parent
    output = project / "third_party" / "python"
    output.mkdir(parents=True, exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise SystemExit("Python runtime LICENSE.txt is missing; preserve it before packaging.")
    runtime = output / f"CPython-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    runtime.mkdir(exist_ok=True)
    shutil.copyfile(python_license, runtime / "LICENSE.txt")
    records = [{"name": "CPython", "version": sys.version.split()[0], "files": [str((runtime / "LICENSE.txt").relative_to(output))]}]
    for distribution in sorted(importlib.metadata.distributions(), key=lambda item: item.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        if name == "kingdom-ai-companion":
            continue
        component = re.sub(r"[^A-Za-z0-9._-]", "_", f"{name}-{distribution.version}")
        copied = []
        for source_entry in distribution.files or []:
            if not re.search(r"(^|[/\\])(licenses?([^/\\]*)|copying([^/\\]*)|notice([^/\\]*))($|[/\\])", str(source_entry), re.IGNORECASE):
                continue
            source = Path(distribution.locate_file(source_entry))
            if not source.is_file():
                continue
            if source.suffix.lower() in {".py", ".pyc", ".pyo", ".dll", ".exe", ".pdb"}:
                continue
            # Original path components are represented in a filename, never executed.
            filename = re.sub(r"[^A-Za-z0-9._-]", "_", str(source_entry))
            target = output / component / filename
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(str(target.relative_to(output)))
        records.append({"name": name, "version": distribution.version,
                        "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
                        "project_urls": distribution.metadata.get_all("Project-URL") or [], "files": copied})
    (output / "LICENSES.json").write_text(json.dumps({"scope": "Dependencies present in the app build environment, including build and test tools; not all are bundled in the app", "components": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Preserved licenses for {len(records)} Python runtime and build dependencies.")


if __name__ == "__main__":
    main()
