# ASI/ — Amsterdam Scientific Instruments software (NOT in version control)

The files that normally live in this folder are **licensed, proprietary
software provided by Amsterdam Scientific Instruments (ASI)**. They are **not
open source** and **must not be committed to or distributed through this public
repository**.

For that reason the binaries themselves are intentionally excluded from git
(see the repository `.gitignore`). This folder and its structure are kept only
so the application can find the binaries at their expected paths once you place
them here locally.

> **All ASI binaries were also removed from the entire git history**, across
> every branch, using `git-filter-repo`. They are no longer present in any past
> commit and are therefore not downloadable from this repository's history.

## Expected contents

Obtain these directly from Amsterdam Scientific Instruments and place them here:

| Path                     | Description                                            |
| ------------------------ | ------------------------------------------------------ |
| `ASI/live-cli`           | ASI live-cli acquisition binary                        |
| `ASI/serval-4.1.1.jar`   | Serval server (version 4.1.1)                          |
| `ASI/tpx3dump`           | `tpx3dump` binary (~144 MB)                            |
| `ASI/tpx3dump.zip`       | Zipped distribution of `tpx3dump`                      |
| `ASI/long-daq/live-cli`  | live-cli build used for long DAQ runs                  |
| `ASI/ui-updates/live-cli`| live-cli build used by the UI update workflow          |
| `ASI/logs/`              | Runtime logs written by Serval (generated locally)     |

Exact filenames/versions may change as ASI ships updates; keep this list in
sync with what you actually run.

## Licensing

These binaries are covered by their own respective ASI licenses, not by the
open-source license that governs the rest of this repository. Do not
redistribute them publicly.
