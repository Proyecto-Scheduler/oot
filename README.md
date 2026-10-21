# oot

CLI for out-of-tree kernel development. You develop in a clone of the kernel, pinned to a tag or branch. `oot` extracts your committed changes into a small, separate git repo (the patches repo), and applies them to a fresh kernel clone later, so another group can pick up where you left off.

## Install

```sh
pip install -e .
```

Requires Python 3.10+ and `git` on `PATH`.

## Configuration

`oot` reads `./oot.yml` by default. Use `-c <file>` to point elsewhere.

```yaml
dir: ~/.oot/                # project root

kernel:
  url: https://github.com/freebsd/freebsd-src
  ref: release/14.1.0       # tag or branch your work starts from
  # depth: 1                # clone depth, default 1
  # dir: /usr/src           # default: <dir>/kernel

patches:
  url: https://github.com/you/your-patches
  ref: main
  # dir: ...                # default: <dir>/patches
```

`url` is optional. Without it, `oot` uses whatever is already in `dir`.

## Workflow

Starting a feature:

1. `oot fetch -t kernel` clones the kernel at `kernel.ref`.
2. Work in the kernel and commit as you go.
3. `oot sync -m "..."` writes your changes to the patches repo and commits there. Run it again at any time to update the patch set; `--dry-run` shows what would change.
4. Push the patches repo.

Continuing someone else's work:

1. `oot fetch` clones the kernel and the patches repo.
2. `oot install` applies the patches onto the kernel and commits them there.
3. Work and commit in the kernel, then `oot sync` as above. The patch set then holds both the installed patches and your new work.

You can also move to a newer `kernel.ref`: fetch it, then install. Install merges your patches with the upstream changes (see below).

## Commands

| Command | What it does |
|---|---|
| `oot fetch [-t kernel\|patches\|all] [-f]` | Clone each repo at its `ref`, or update an existing clone to it (reset and clean). Refuses to update a repo with local commits or uncommitted changes. `-f` discards them and re-clones. |
| `oot sync -m <message> [--dry-run]` | Make the patches repo mirror the kernel's committed changes since its base, and commit. Asks before continuing if the kernel has uncommitted changes, or if the sync would remove more than half of the patch set. `--dry-run` prints the plan and changes nothing. |
| `oot install [--dry-run] [--on-conflict abort\|skip\|force] [-ff] [-m <metadata>]` | Apply the patch set to the kernel and commit once. Without `--on-conflict` it prompts per conflict. `--dry-run` only reports conflicts. `-ff` stops at the first error. |
| `oot path kernel\|patches` | Print the resolved directory. |
| `oot git <args>` | Run `git -C <patches dir> <args>` and exit with git's exit code. |

`-v` sets log level to INFO, `-vv` to DEBUG.

`oot git` gets no shell completion. For completion, define a function instead:

```bash
oot-git() {
  git -C "$(oot path patches)" "$@"
}
```

Pass `-c <file>` to the inner `oot` if you do not use the default config path.

## How it works

The patches repo mirrors the kernel tree layout but holds only the changed files, plus `metadata.json` and `.oot/base/`. `.oot/base/` holds the base version of each modified file (its base copy), so install can merge without the kernel's history.

```json
{
  "kernel_commit": "<base commit>",
  "files": [
    {"path": "sys/kern/sched_4bsd.c",  "base_blob": "<blob at base>", "status": "modified"},
    {"path": "sys/kern/sched_petri.c", "base_blob": null,             "status": "new"},
    {"path": "sys/kern/old.c",         "base_blob": null,             "status": "deleted"}
  ]
}
```

Files you want versioned in the patches repo but not touched by `oot` (README, CI config, `.gitignore`) go in `.ootignore`, which uses gitignore syntax. Anchor root-level names with `/`, otherwise they also match nested kernel files with the same name:

```gitignore
/README.md
/.gitignore
/.github/
```

### sync

1. The base is `git merge-base HEAD <upstream>`, where `<upstream>` is the tag `kernel.ref` if one exists, otherwise `origin/<kernel.ref>`. For a tag that is the tag commit. For a branch it is the point your work forked from, even if upstream has moved on since.
2. `git diff <base> HEAD` lists the changes: added files are `new`, deleted files are `deleted`, everything else is `modified`. A rename counts as `new` plus `deleted`. Only committed changes are read; uncommitted ones are listed, and sync asks whether to continue without them.
3. The patches repo is made to match: changed files are written with their content at `HEAD`, base copies are written from the base, and patch files no longer changed are removed. `metadata.json`, `.oot/`, `.ootignore` and what `.ootignore` matches are never treated as patches. A kernel change to one of those paths, or to a path the patches repo's `.gitignore` ignores, is an error.
4. The patches repo is committed with the given message, if anything changed. It is created with `git init` if it does not exist yet.

Each sync describes the whole patch set from scratch, so the patches repo is generated output: hand edits there are overwritten. Syncing a kernel that does not have the patches installed would remove them, so sync asks first when it would remove more than half of the current patch set.

### install

Per file, by status:

- `new`: copy into the kernel tree. If a different file already exists there, ask the resolver.
- `modified`: 3-way merge of base copy, current kernel file and patches copy (`git merge-file`). If it conflicts, ask the resolver. Install needs no kernel history, so it works on a fresh shallow clone of a newer release.
- `deleted`: remove the file from the kernel tree. Missing files are ignored.

Resolver answers: `force` (overwrite; for modified text files, write conflict markers; for binary files, take the patches copy), `skip` (leave this file), `abort` (stop).

Install commits once at the end. It does not commit in dry run, or if any file was left with conflict markers: resolve them and commit yourself.

## Code layout

```
src/oot/
  cli.py            click entry point; loads config, maps flags to command calls
  config.py         load_project(): reads oot.yml into Project; fills and resolves repo dirs
  metadata.py       metadata.json models with load/save; patch_files() lists the patch set
  errors.py         OotError and subclasses: expected failures, shown without a traceback
  commands/         one module per command; no click dependency
    fetch.py
    sync.py
    install.py
    path.py
    git.py
  git/
    repo.py         Repo: thin wrapper over the git CLI via subprocess
    merge.py        merge3: 3-way merge of bytes via git merge-file
    inspect.py      filesystem helpers
tests/              pytest suite; not shipped in the package
```

Commands take a `Project` and plain arguments, so they can be called and tested without click. Interactive decisions are passed in as callables: `resolver` in install returns an `Action` (`force`, `skip`, `abort`), `confirm` in fetch and sync returns a bool. The CLI passes prompting versions; `--on-conflict` passes a fixed one.

All git work goes through `Repo.git(*args)`, which runs `git -C <path>` and returns the `CompletedProcess`. There is no git library dependency.

Commands raise `OotError` subclasses for failures the user can fix. `OotGroup` in `cli.py` turns those, `OSError` and failed git calls (`CalledProcessError`) into a one-line error; anything else is a bug and keeps its traceback.

## Tests

```sh
pip install -e '.[dev]'
pytest
```

Most tests use real files and git repos in `tmp_path`; `test_repo.py` mocks `subprocess.run`. `test_sync.py` ends with a round trip of the two-group workflow.
