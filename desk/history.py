"""Archive this run's facts registry and memos under reports/history/YYYY-MM-DD/.

    python -m desk.history

The folder is named after the *data* date (the registry's newest fetch), not
the wall clock, so re-running the archive for the same data is idempotent and
a snapshot run never invents a new date. Only facts.json and the memos are
kept: they are what a reader needs to audit a past claim, and together they
are small (about 0.5 MB before git compression). Charts and CSVs can be
regenerated from the committed data snapshot of that commit.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from desk.config import REPORTS_DIR
from desk.facts import load_registry
from desk.verify import verify_text

HISTORY_DIR = REPORTS_DIR / "history"


def archive(reports: Path = REPORTS_DIR, history: Path = HISTORY_DIR) -> Path:
    reg = load_registry(reports / "facts.json")
    day = (reg.meta.get("data_as_of") or "")[:10]
    if len(day) != 10:
        raise SystemExit("history: facts.json has no data_as_of date")
    memos = sorted((reports / "memos").glob("*.md"))
    bad = [m.name for m in memos if not verify_text(m.read_text(encoding="utf-8"), reg).ok]
    if bad:  # never archive something the verifier rejects
        raise SystemExit(f"history: refusing to archive unverified memos: {bad}")
    dest = history / day
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "memos").mkdir(parents=True)
    shutil.copy2(reports / "facts.json", dest / "facts.json")
    for m in memos:
        shutil.copy2(m, dest / "memos" / m.name)
    return dest


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    dest = archive()
    print(f"history: archived facts.json + {len(list((dest / 'memos').glob('*.md')))} memos -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
