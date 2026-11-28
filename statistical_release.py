"""Source-controlled integration release decision, independent of campaign receipts."""
from learning_loop import digest

# Adding an identity requires independent calibration, verification and release review.
# Keep this module outside the protocol implementation hash so it can also revoke old certificates.
RELEASED_PROTOCOLS = frozenset()
BLOCKER = "F16_statistical_calibration_release_hold"


def released(protocol):
    return digest(protocol) in RELEASED_PROTOCOLS
