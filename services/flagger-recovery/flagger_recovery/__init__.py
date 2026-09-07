"""Candidate identity and deployment record primitives for the Flagger recovery pilot."""

from .identity import AttributionRefused, CandidateIdentity, ContainerImage, is_manual_rollback, resolve
from .record import ConfigMapStore, DeploymentRecord, InMemoryStore, PutResult, RecordStore, canary_label, make_key

__all__ = [
    "AttributionRefused", "CandidateIdentity", "ContainerImage", "is_manual_rollback", "resolve",
    "ConfigMapStore", "DeploymentRecord", "InMemoryStore", "PutResult", "RecordStore", "canary_label", "make_key",
]
