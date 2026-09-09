# Second path in a failing release

This file exists so one rejected release changes two paths under
`kubernetes/pilot/flagger-pilot/` in a single commit. `kustomization.yaml` lists
its resources explicitly and does not list this file, so nothing here reaches the
cluster: the only thing it changes is the shape of the commit the recovery
receiver has to reason about.

The correction the receiver proposes restores `helmrelease.yaml` alone, because
that is the one file in its allowed-target list. What happens to this file when
that correction is applied is the question the run was set up to answer.

Delete it with the release that clears the fault.
