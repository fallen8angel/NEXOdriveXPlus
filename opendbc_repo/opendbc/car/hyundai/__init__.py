"""Hyundai package compatibility helpers.

NEXOdriveXPlus previously carried a separate legacy ``CAR.HYUNDAI_NEXO``
platform. The platform definition was removed when the vehicle list was
consolidated to ``HYUNDAI_NEXO_1ST_GEN``, but older CarState code still
references the removed attribute in a comparison.

Keep a non-platform sentinel for that removed name so importing/evaluating the
legacy comparison cannot raise AttributeError. It intentionally does *not*
alias to HYUNDAI_NEXO_1ST_GEN: aliasing would incorrectly enter the old EV
ELECT_GEAR branch instead of the current FCEV EMS20/HYDROGEN_GEAR_SHIFTER path.
"""

from .values import CAR

if not hasattr(CAR, "HYUNDAI_NEXO"):
  CAR.HYUNDAI_NEXO = "__REMOVED_LEGACY_HYUNDAI_NEXO__"
