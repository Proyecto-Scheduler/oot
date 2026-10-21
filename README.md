# oot

CLI for out-of-tree kernel development. You keep your kernel changes in a small, separate git repo (the patches repo) instead of a fork of the whole kernel. `oot` clones both repos, records what your patches change, and applies them onto a fresh kernel tree.

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
  ref: releng/15.0          # branch or tag
  # depth: 1                # clone depth, default 1
  # dir: /usr/src           # default: <dir>/kernel

patches:
  url: https://github.com/you/your-patches
  ref: main
  # dir: ...                # default: <dir>/patches
```

`url` is optional. Without it, `oot` uses whatever is already in `dir`.

## Commands

| Command | What it does |
|---|---|
| `oot fetch [-t kernel\|patches\|all] [-f]` | Clone each repo into its `dir`, or fetch and hard reset to `ref` if it is already a clone. `-f` deletes a non-empty, non-git `dir` and clones. |
| `oot sync -m <message> [--dry-run]` | Compare the patches repo against upstream (`origin/<kernel.ref>`), write `metadata.json` and base copies, commit the patches repo. |
| `oot install [--dry-run] [--on-conflict abort\|skip\|force] [-ff] [-m <metadata>]` | Apply the patches recorded in `metadata.json` to the kernel tree and commit once. Without `--on-conflict` it prompts per conflict. `--dry-run` only reports conflicts. `-ff` stops at the first error. |
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

The patches repo mirrors the kernel tree layout but contains only the files you changed or added, plus `metadata.json` and `.oot/base/`, which holds the upstream version each modified file was based on (its base copy):

```json
{
  "kernel_commit": "<upstream commit at sync time>",
  "files": [
    {"path": "sys/kern/kern_sched.c", "base_blob": "<kernel blob>", "status": "modified"},
    {"path": "sys/kern/my_sched.c",   "base_blob": null,            "status": "new"},
    {"path": "sys/kern/old.c",        "base_blob": null,            "status": "deleted"}
  ]
}
```

Files you want versioned in the patches repo but not installed into the kernel (README, CI config, `.gitignore`) go in `.ootignore`, which uses gitignore syntax. Anchor root-level names with `/`, otherwise they also match nested kernel files with the same name:

```gitignore
/README.md
/.gitignore
/.github/
```

Typical loop:

1. `oot fetch` to get both trees.
2. Edit or add files in the patches repo, at the same relative paths as in the kernel.
3. `oot sync -m "..."` to record the change set.
4. `oot install` to apply it onto the kernel tree, build, test.
5. Later, bump `kernel.ref`, `oot fetch -t kernel`, `oot install` again.

### sync

Sync compares against `origin/<kernel.ref>`, not `HEAD`, because install commits patches onto `HEAD`.

For every file in the patches repo (tracked or untracked, honouring `.gitignore`, excluding `metadata.json`, `.oot/`, `.ootignore` and anything it matches):

- not in upstream: `new`
- in upstream, identical: dropped
- in upstream, different: `modified`, with `base_blob` set to the upstream blob it was based on, and a base copy written to `.oot/base/<path>`. An existing base is kept on later syncs, so install still merges against the original.

For every file in the previous `metadata.json` that is no longer in the patches repo:

- was `new`: dropped
- was `modified` or `deleted` and still in upstream: `deleted`
- otherwise: dropped

### install

Per file, by status:

- `new`: copy into the kernel tree. If a different file already exists there, ask the resolver.
- `modified`: 3-way merge of base copy, current kernel file and patches copy (`git merge-file`). If it conflicts, ask the resolver. Install needs no kernel history, so it works on a fresh shallow clone of a newer release.
- `deleted`: remove the file from the kernel tree. Missing files are ignored.

Resolver answers: `force` (overwrite; for modified text files, write conflict markers; for binary files, take the patches copy), `skip` (leave this file), `abort` (stop).

Install commits once at the end. It does not commit in dry run, or if any file was left with conflict markers: resolve them and commit yourself.

Metadata from before base copies existed is upgraded by the next `oot sync`, which recreates base copies from the recorded blobs if the kernel repo still has them.

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

Commands take a `Project` and plain arguments, so they can be called and tested without click. Interactive decisions are passed in as callables: `resolver` in install returns an `Action` (`force`, `skip`, `abort`), `confirm` in fetch returns a bool. The CLI passes prompting versions; `--on-conflict` passes a fixed one.

All git work goes through `Repo.git(*args)`, which runs `git -C <path>` and returns the `CompletedProcess`. There is no git library dependency.

Commands raise `OotError` subclasses for failures the user can fix. `OotGroup` in `cli.py` turns those, `OSError` and failed git calls (`CalledProcessError`) into a one-line error; anything else is a bug and keeps its traceback.

## Tests

```sh
pip install -e '.[dev]'
pytest
```

`test_sync.py`, `test_fetch.py`, `test_install.py` and `test_merge.py` use real files and git repos in `tmp_path`. `test_repo.py` mocks `subprocess.run`.
