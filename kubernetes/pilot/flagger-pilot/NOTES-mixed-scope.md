# Second path in a failing release

This file exists so one rejected release changes two paths under
`kubernetes/pilot/flagger-pilot/` in a single commit. `kustomization.yaml` lists
its resources explicitly and does not list this file, so nothing here reaches the
cluster: the only thing it changes is the shape of the commit the recovery
receiver has to reason about.

The correction the receiver proposes restores `helmrelease.yaml` alone, because
that is the one file in its allowed-target list. The first acceptance run showed
the writer evaluating that correction as an allow, which would have left this
file's change standing behind a restored `helmrelease.yaml`. The writer now
compares the promoted revision to the failed one and refuses when the failed
release touched more than the correction's target; this release is the case that
refusal exists for.

Delete it with the release that clears the fault.
