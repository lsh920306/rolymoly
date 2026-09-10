"""Build a reviewable source archive without local databases or credentials."""
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def build():
    root_files = ["app.py", "streamlit_app.py", "requirements.txt", "run.bat", ".gitignore", "Dockerfile", ".dockerignore", "compose.yaml", "README.md", "DESIGN.md", "REVIEW.md", "CODE_REVIEW.md", "REHEARSAL.md", "DEPLOYMENT.md"]
    sources = [ROOT / name for name in root_files]
    sources.extend(path for path in sorted(ROOT.glob("*.md")) if path not in sources)
    sources.extend(sorted((ROOT / "docs").glob("*.md")))
    sources.append(ROOT / ".streamlit" / "secrets.toml.example")
    for folder in ("roly", "app_pages", "tests", "scripts"):
        sources.extend(sorted((ROOT / folder).glob("*.py")))
    sources.extend(sorted(path for path in (ROOT / "static").iterdir() if path.is_file()))
    config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    # Leave the local app bound to loopback. Hosting selects its own address.
    config = "\n".join(line for line in config.splitlines() if not line.strip().startswith("address =")) + "\n"
    content = {".streamlit/config.toml": config.encode(), ".python-version": b"3.11\n"}
    for path in sources:
        path = path.resolve(strict=True)
        if not path.is_relative_to(ROOT):
            raise ValueError(f"Source outside the project: {path.name}")
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix in (".sqlite3", ".db", ".log") or any(part in (".data", ".venv", "test-artifacts", "review-source") for part in path.parts):
            raise ValueError(f"Private or generated artifact: {relative}")
        content[relative] = path.read_bytes()
    manifest = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in sorted(content.items())}
    content["SOURCE-MANIFEST.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    output = ROOT / "dist" / "rolymoly-source.zip"
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(content.items()):
            archive.writestr(name, data)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive integrity check failed")
        for name, entry in manifest.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != entry["sha256"]:
                raise ValueError(f"Archive hash mismatch: {name}")
    print(f"Source archive: {output.name}; {len(content)} files; {output.stat().st_size:,} bytes")
    return output


if __name__ == "__main__":
    build()
