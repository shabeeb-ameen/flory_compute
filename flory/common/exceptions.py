"""Exceptions that package :mod:`flory` might raise.
"""

class VolumeFractionError(ValueError):
    """Error indicating that the volume fraction is smaller than 0."""


class ComponentNumberError(ValueError):
    """Error indicating mismatch of number of components."""


class FeatureNumberError(ValueError):
    """Error indicating mismatch of number of features."""

