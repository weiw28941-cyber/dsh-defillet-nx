"""Audit the prospective source tree, not Git history or redistribution rights."""

import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".json", ".yaml", ".yml", ".md",
                 ".ps1", ".bat", ".example", ".toml", ".txt", ".html", ".ts", ".tsx"}
PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/]+(?:Users[\\/]+[^\\/\s]+|Desktop[\\/]+|Miniconda\d*[\\/]+)", re.I),
    re.compile(r"(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}|\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r'"resolved"\s*:\s*"(?:file:|git\+|https?://(?!registry\.npmjs\.org/))'),
)


def git_paths(*args):
    result = subprocess.run(["git", "ls-files", "-z", *args], cwd=ROOT,
                            check=True, stdout=subprocess.PIPE)
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def main():
    failures = []
    ignored = git_paths("-ci", "--exclude-standard")
    failures.extend("Ignored but tracked: " + path for path in ignored)
    paths = sorted(set(git_paths("--cached", "--others", "--exclude-standard")))
    scanned = 0
    for name in paths:
        path = ROOT / name
        if not path.is_file() or (path.suffix not in TEXT_SUFFIXES and path.name != ".gitignore"):
            continue
        scanned += 1
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if any(pattern.search(line) for pattern in PATTERNS):
                # Report locations only; never echo potential credentials.
                failures.append("Source audit match: {}:{}".format(name, number))

    manifest = json.loads((ROOT / "dsh-plugins/defeature-ui/release-files.json").read_text(encoding="utf-8"))
    for name in manifest["runtime"]:
        if name not in paths or not (ROOT / name).is_file():
            failures.append("Missing publication runtime input: " + name)
    skill_inputs = [".dsh/skills/" + name for name in manifest["skills"]]
    for name in skill_inputs:
        if name not in paths or not (ROOT / name).is_file():
            failures.append("Missing publication skill input: " + name)

    for failure in failures:
        print(failure)
    print("Audited {} text files; {} findings.".format(scanned, len(failures)))
    print("Heuristic source audit only. Git history, licenses and third-party rights require separate review.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
