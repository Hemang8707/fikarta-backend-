"""Fail if tracked files look like they contain real personal data (pattern from creditsage-lab's repo PII scanner).

Checks: PAN numbers, Aadhaar-like 12-digit numbers, Indian mobile numbers, and bureau report files outside the
synthetic fixtures folder. Synthetic placeholders used in tests (ABCDE1234F, 9876543210) are allowed.
Usage: python scripts/scan_pii.py   (exit code 1 when something is found)
"""
import pathlib
import re
import subprocess
import sys

ALLOWED_VALUES = {"ABCDE1234F", "9876543210", "9811223344", "9822334455"}  # synthetic test placeholders
PATTERNS = {
    "PAN": re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"),
    "Aadhaar-like": re.compile(r"\b[2-9][0-9]{3}\s?[0-9]{4}\s?[0-9]{4}\b"),
    "Indian mobile": re.compile(r"(?<![\d.])(?:\+91[\s-]?)?[6-9][0-9]{9}(?![\d.])"),
}
SKIP_SUFFIXES = {".db", ".sqlite", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".pdf", ".lock", ".svg", ".woff", ".woff2", ".ttf"}
SKIP_PARTS = {"vendor", "node_modules", "export", "scraper", "pipeline"}


def tracked_files(root: pathlib.Path):
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root, capture_output=True, text=True, check=True).stdout
        return [root / p for p in out.splitlines()]
    except Exception:
        return [p for p in root.rglob("*") if p.is_file()]


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    findings = []
    for f in tracked_files(root):
        rel = f.relative_to(root).as_posix()
        if f.suffix.lower() in SKIP_SUFFIXES or set(f.parts) & SKIP_PARTS or not f.is_file():
            continue
        if re.search(r"(crif|cibil|experian|equifax).*\.(json|xml|pdf)$", rel, re.I) and "fixtures/crif_synthetic/" not in rel:
            findings.append((rel, 0, "bureau report file outside synthetic fixtures"))
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, pat in PATTERNS.items():
                for m in pat.finditer(line):
                    v = re.sub(r"[\s+-]", "", m.group(0))[-10:] if name == "Indian mobile" else m.group(0)
                    if v in ALLOWED_VALUES:
                        continue
                    findings.append((rel, n, f"possible {name}"))
    for rel, n, why in findings:
        print(f"{rel}:{n}: {why}")
    print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
