"""Data-access adapters for the medical analysis platform."""

from medical_ai.repositories.identity import IdentityRepository
from medical_ai.repositories.medical_data import MedicalDataRepository

__all__ = ["IdentityRepository", "MedicalDataRepository"]
