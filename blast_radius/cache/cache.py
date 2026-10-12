import hashlib
import os
import pickle
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..parser import parser as parser_module
from ..parser.parser import ParsedFile, PythonParser

# A change to parser.py can change what a ParsedFile holds, so its source is part of the cache key
PARSER_VERSION = hashlib.sha1(Path(parser_module.__file__).read_bytes()).hexdigest()
CACHE_FILE = "parse-cache.pickle"
FALLBACK_DIR = ".blast-radius-cache"   # used only when the repo is not a git checkout


@dataclass
class Entry:
    mtime_ns: int
    size: int
    digest: str
    parsed: ParsedFile


def cache_dir(repo: Path) -> Path:
    """Inside .git, so the cache never shows up in `git status` and needs no .gitignore line."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return Path(out) / "blast-radius"
    except (OSError, subprocess.CalledProcessError):
        return repo / FALLBACK_DIR


class ParseCache:
    """Keeps one ParsedFile per file between runs, so only changed files are parsed again.

    Check order for each file:
      1. same mtime and size as last run -> reuse without reading the file (the fast path)
      2. same content hash               -> reuse, store the new mtime (e.g. after `git checkout`)
      3. otherwise                       -> parse
    """

    def __init__(self, repo: str | Path, path: Path | None = None):
        self.repo = Path(repo)
        self.path = path or cache_dir(self.repo) / CACHE_FILE
        self.old: dict[str, Entry] = self._load()
        self.new: dict[str, Entry] = {}    # only files seen this run, so deleted files drop out on save
        self.hits = 0
        self.misses = 0
        self.dirty = False                 # True once any entry differs from what is on disk

    def _load(self) -> dict[str, Entry]:
        try:
            with open(self.path, "rb") as f:
                version, entries = pickle.load(f)
        except Exception:                  # no cache yet, or a corrupt/old-format file: start empty
            return {}
        return entries if version == PARSER_VERSION else {}

    def get(self, rel: str, parser: PythonParser) -> ParsedFile:
        full = self.repo / rel
        st = full.stat()
        old = self.old.get(rel)
        if old is not None and (old.mtime_ns, old.size) == (st.st_mtime_ns, st.st_size):
            self.hits += 1
            self.new[rel] = old
            return old.parsed

        source = full.read_bytes()
        digest = hashlib.blake2b(source, digest_size=16).hexdigest()
        self.dirty = True
        if old is not None and old.digest == digest:
            self.hits += 1
            parsed = old.parsed
        else:
            self.misses += 1
            parsed = parser.parse_file(rel, source)
        self.new[rel] = Entry(st.st_mtime_ns, st.st_size, digest, parsed)
        return parsed

    def save(self) -> None:
        # every entry reused as-is and none deleted (new's keys are a subset of old's): nothing to write
        if not self.dirty and len(self.new) == len(self.old):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump((PARSER_VERSION, self.new), f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, self.path)         # atomic: a crash mid-write never leaves a half-written cache
