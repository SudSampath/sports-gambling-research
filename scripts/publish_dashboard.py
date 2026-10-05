"""Publish reviewed static artifacts to this public repository's gh-pages branch.

Run only for an authorized publication. Never starts campaign processes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import uuid

from validate_public_dashboard import validate

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "SudSampath/sports-gambling-research"
APP_FILES = ["index.html", "styles.css", "app.js", "format.js", "icon.svg"]


def run(*args, cwd=ROOT, input=None):
    return subprocess.run(args, cwd=cwd, input=input, text=True, check=True, capture_output=True).stdout.strip()


def prepare(expected_commit: str) -> tuple[str, list[str]]:
    commit = run("git", "rev-parse", "HEAD")
    if expected_commit != commit:
        raise ValueError("Publication requires the exact reviewed HEAD commit")
    if run("git", "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Commit reviewed changes before publishing")
    validate(ROOT / "web/data")
    build = json.loads((ROOT / "web/dist/build.json").read_text())
    if build["source_commit"] != commit:
        raise ValueError("Rebuild from the reviewed commit before publishing")
    manifest = json.loads((ROOT / "web/data/index.json").read_text())
    source_files = APP_FILES + ["data/index.json", "data/recovery-proof.json"] + [f"data/{name}" for name in manifest["campaigns"]]
    for name in source_files:
        run("git", "ls-files", "--error-unmatch", "--", f"web/{name}")
        source, built = ROOT / "web" / name, ROOT / "web/dist" / name
        if source.is_symlink() or built.is_symlink() or source.read_bytes() != built.read_bytes():
            raise ValueError("Build differs from reviewed source or contains a symlink")
    files = source_files + ["build.json", ".nojekyll"]
    actual = {str(p.relative_to(ROOT / "web/dist")) for p in (ROOT / "web/dist").rglob("*") if p.is_file()}
    if actual != set(files) or (ROOT / "web/dist/.nojekyll").read_bytes():
        raise ValueError("Unexpected publication artifacts")
    return commit, files


def publish(commit: str, files: list[str]) -> str:
    repo = json.loads(run("gh", "repo", "view", REPOSITORY, "--json", "visibility,viewerPermission"))
    if repo["visibility"] != "PUBLIC" or repo["viewerPermission"] != "ADMIN":
        raise ValueError("Public repository admin access is required for Pages setup")
    if json.loads(run("gh", "api", f"repos/{REPOSITORY}/commits/{commit}"))["sha"] != commit:
        raise ValueError("Push the reviewed source commit before publishing source links")
    try:
        pages = json.loads(run("gh", "api", f"repos/{REPOSITORY}/pages"))
    except subprocess.CalledProcessError as exc:
        if '404' not in exc.stderr:
            raise
        pages = None
    if pages and (pages.get("build_type") == "workflow" or pages.get("source") != {"branch": "gh-pages", "path": "/"}):
        raise ValueError("An existing Pages configuration uses a different publishing source")
    worktree = ROOT / ".runs" / f"pages-publish-{uuid.uuid4().hex}"
    temporary_branch = f"pages-stage-{uuid.uuid4().hex}"
    remote = run("git", "remote", "get-url", "origin")
    if remote not in (f"https://github.com/{REPOSITORY}.git", f"https://github.com/{REPOSITORY}", f"git@github.com:{REPOSITORY}.git"):
        raise ValueError("Origin is not the intended public repository")
    existing = run("git", "ls-remote", "--heads", "origin", "gh-pages")
    if existing:
        run("git", "fetch", "origin", "gh-pages")
    worktree.parent.mkdir(exist_ok=True)
    run("git", "worktree", "add", "--detach", str(worktree), "FETCH_HEAD" if existing else commit)
    try:
        if existing:
            old_files = run("git", "ls-files", cwd=worktree).splitlines()
            allowed_old = set(APP_FILES + ["build.json", ".nojekyll", "data/index.json", "data/recovery-proof.json"])
            for name in old_files:
                if name not in allowed_old and not (name.startswith("data/") and name.endswith(".json") and "/" not in name[5:]):
                    raise ValueError("gh-pages contains files outside the dashboard; preserve them")
            if old_files:
                run("git", "rm", "--", *old_files, cwd=worktree)
        else:
            run("git", "switch", "--orphan", temporary_branch, cwd=worktree)
        for name in files:
            destination = worktree / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "web/dist" / name, destination)
        run("git", "add", "--", *files, cwd=worktree)
        if run("git", "diff", "--cached", "--name-only", cwd=worktree):
            run("git", "commit", "-m", f"Publish Research Desk from {commit[:12]} (SUD-200)", cwd=worktree)
            run("git", "push", "origin", "HEAD:refs/heads/gh-pages", cwd=worktree)
        if pages is None:
            pages = json.loads(run("gh", "api", "--method", "POST", f"repos/{REPOSITORY}/pages", "--input", "-",
                                   input=json.dumps({"build_type": "legacy", "source": {"branch": "gh-pages", "path": "/"}})))
        return pages["html_url"]
    finally:
        # This worktree was created solely for generated public artifacts.
        run("git", "worktree", "remove", "--force", str(worktree))
        if not existing and subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{temporary_branch}"], cwd=ROOT).returncode == 0:
            run("git", "branch", "-D", temporary_branch)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", required=True, help="Exact reviewed HEAD SHA")
    parser.add_argument("--publish", action="store_true", help="Push static artifacts and configure Pages if absent")
    args = parser.parse_args()
    commit, files = prepare(args.source_commit)
    if args.publish:
        print(f"Publication submitted: {publish(commit, files)}")
    else:
        print(f"Validated {len(files)} publication files from {commit}. No remote changes.")
