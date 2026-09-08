# Pilot scratch notes

Scratch marker for a validation run. This file is deliberately absent from
`kustomization.yaml`'s `resources:` list, so kustomize never renders it and
Flux never applies anything from it. It exists only to move the pilot branch
under an open correction proposal without touching the release itself, and it
is removed again by the release that follows.
