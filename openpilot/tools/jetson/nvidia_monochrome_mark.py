"""Tiny monochrome NVIDIA mark sourced from the user's provided reference image.

Only the black/white logo pixels are embedded. Materialized in Jetson runtime
storage for existing raylib texture loading; no network or model activity.
"""
import base64
import os
from pathlib import Path

from openpilot.tools.jetson.state import RUNTIME

LOGO_PNG_BASE64 = (
  "iVBORw0KGgoAAAANSUhEUgAAAFQAAABCCAYAAADJ5PFHAAAEY0lEQVR42u1c247rIAyMUf7/l31emiOX+jIGAqTbSJFWqyaYYXzFhI5FFzMfu11E1P+OVWASEQzqNdHsIljPjQBuG0B3ZKYE+lrs1utcKfhxHLuh202w8mPn2KvMZmalVvRtgJ6rmCn+p4HKfx7QC6BWj2zYMv6zgAJAcs1I+YwRRtHTmHuOYKQW5xnAkmUK5N/O4mzPXBql6ndmLgDrR41HvXFo6QFSmSiLG0nv3n5fT0SOYUySHs9QA0TzfRKIgM0cyeYwlh/JUIUtJphXzCmZZglaxaRUsde1sxcQu7CVGpnpAjnQtqrjyOTAkWsJQ88sK/l9BhkwP57TbKay4CyeJ/k7BVRaHQFQg70kOTkNyGqSHI2nVXmUd5lx7Ci7eqsN9QQkMeI1YSWmZAE8RU6mBkfm/VYMKpn6iOKI4Xw+2KOsKHsmwXqPlyxUMnDllLzQautqEwHqqqqmBKn6LdfgARV9qjUiswMwHdAWAa2CsQGkyT7AexMgK+/OUDU2BIoZmcXhTIgm1XwXME1Agb0V9mxjAFB3HKot1EsGLduiHRlqhjwBSzkA3XOCJjOVCODTEL/LMQ3YEk2uAoJAllJDVYpeoJjA1KksUgdQ5KJdGPrBsiAzItDheKBQQ2FENQ2zTMHQXN6owLOViyNZlBOXQjWFGlSP2dOqTcogFDkfJMOp2ZLQAI7ArCtTQOg2n6FofBgwKiyoOIC46m2lsmi+P4KhqSczaoeU8ZLJQwhmAkgrCZlbYAY8v1n2SyyQptrsOSurPhps06xV+ayj0uxWJltC5LRso7ELywBh5qp8q0111Jxb5QKK0+l0dErFPrJ/Rs3SLOEpHh62q4kNv2U5fVfniLO3c4HFB7CbaQEesVAxJcvrd8ODMif84SAtzDqs4QAutaEtIdbAIvAtTFxqQ9HMyupf2gXErWwo4oEBb/xVbc3TOpjRbOXp17RDC0Cz11eA+ztWgy38fipf596/63ftqfI7q/9Pc3ZmaPYga8S2bFppZVHZftPMuBEjkSLNEDU1CrfL1bxXNlROdxciO0ltNzG7kjMYiha3Wwo03jNnNrB+DUIKCGwtllLmY2NxWRmPWuQzNuxcGRWSsDGGWTFrjUP/tyF6/U+Jyg2N137mrOpXDbzcYqvLAMGzxn1mrNTSFg7P+65MiVGWPei8PLoH9TGnEqhhfWeBQrrvqDUMEeeTPPk4w07kjGpzLi/34V839cand2Va3gcNEBmAcIsQtS+okRYDUuT9HbB5JOh1F3PvVyKMpl3XrtashhmKvLhh9Wlk1tGjDZ66Z05Dzyjf8ZOLDrJj+gDOEJTRgx929zInWNESN5oqm3gXa+zMpLNl5eIPWkC15z4TWwaHM+rbjcWH7inN+vKCxdDOcXiELOUOm4Oyr7epAGDPrISDb2FoNiafkDn1RBHknJJmixzlLjXcw0HHi4Y4TOmYIg08G50EATFdusfz8HtKKWt6kKOT1v+thQi+iPa91ypN+QcFKfFT/hApDQAAAABJRU5ErkJggg=="
)
LOGO_FILE = RUNTIME / 'nvidia-mono-white-mark.png'


def ensure_logo():
  """Write a bounded, fixed white/transparent logo once, using atomic replacement."""
  payload = base64.b64decode(LOGO_PNG_BASE64, validate=True)
  if LOGO_FILE.is_file() and LOGO_FILE.read_bytes() == payload:
    return LOGO_FILE
  LOGO_FILE.parent.mkdir(parents=True, exist_ok=True)
  temp = LOGO_FILE.with_name(f'{LOGO_FILE.name}.{os.getpid()}.tmp')
  try:
    temp.write_bytes(payload)
    os.replace(temp, LOGO_FILE)
  finally:
    temp.unlink(missing_ok=True)
  return LOGO_FILE
