"""Candidate identity primitives for the Flagger recovery pilot."""

from .identity import AttributionRefused, CandidateIdentity, ContainerImage, is_manual_rollback, resolve

__all__ = [
    "AttributionRefused", "CandidateIdentity", "ContainerImage", "is_manual_rollback", "resolve",
]
