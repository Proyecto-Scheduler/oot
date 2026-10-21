# Glossary

**Kernel repo**: the upstream kernel checkout (`kernel.dir`), cloned shallow at `kernel.ref`.

**Patches repo**: the git repo (`patches.dir`) holding only the files you changed or added, at the same relative paths as in the kernel, plus `metadata.json` and `.oot/`.

**Patch set**: the files in the patches repo that are patches: everything except `metadata.json`, `.oot/`, `.ootignore` and what `.ootignore` matches. Computed by `patch_files()`.

**Upstream**: the kernel tree at `origin/<kernel.ref>`. Sync reads upstream, never kernel `HEAD`, because install commits patches onto `HEAD`.

**Metadata**: `metadata.json` in the patches repo. Records `kernel_commit` (upstream commit at sync time) and one entry per patched file with its file status.

**File status**: `new` (not in upstream), `modified` (differs from upstream), `deleted` (removed from upstream on install).

**Base copy**: the upstream version of a modified file at the time it was first captured, stored at `.oot/base/<path>` in the patches repo. Install merges against it, so it never depends on kernel history.

**Base blob**: the git blob hash of a base copy, kept per file in metadata as provenance.

**Merge**: the 3-way combination of base copy, current kernel file and patches copy (`git merge-file`). Has conflicts when upstream and your patch changed the same lines.

**Resolver**: the decision made on a conflict: `force` (apply anyway: conflict markers for text, patches copy for binary), `skip`, or `abort`.
