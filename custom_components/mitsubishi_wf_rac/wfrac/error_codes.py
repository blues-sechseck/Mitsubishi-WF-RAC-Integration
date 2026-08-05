"""Klartext descriptions for the self-diagnosis ErrorCode (rac_parser.py).

Source and confidence notes: ../../../../fehlercodes-selfdiagnose.md
(transcribed from the official MHI service/user manuals). Not hardware-
verified against a real E<n> occurrence - only cross-checked between the two
manuals.
"""

from typing import Final

# High confidence: outdoor unit/refrigeration-circuit codes. Wording and
# blink-pattern position match word-for-word between the SRR service manual
# (source of these E-numbers) and the SRK user's manual (Daniel's own model
# family, SRK35ZS-WF) - the outdoor unit/refrigeration circuit is the same
# platform across indoor unit form factors.
_HIGH_CONFIDENCE: Final[dict[str, str]] = {
    "E1": "Fehler Kabel-Fernbedienung",
    "E5": "Signalübertragungsfehler Indoor/Outdoor",
    "E35": "Kühl-Hochdruckschutz",
    "E36": "Verdichter-Übertemperatur",
    "E37": "Außenwärmetauscher-Sensor-Fehler",
    "E38": "Außenlufttemperatur-Sensor-Fehler",
    "E39": "Druckgasleitungs-Sensor-Fehler",
    "E40": "Absperrventil (Gasseite) geschlossen",
    "E42": "Current Cut (Startstrom-Abschaltung)",
    "E47": "Active-Filter-Spannungsfehler",
    "E48": "Außenlüfter-Fehler",
    "E51": "Leistungstransistor-Fehler",
    "E57": "Kältekreis-Schutzabschaltung",
    "E58": "Current-Safe-Abschaltung",
    "E59": "Störung Außeneinheit",
    "E60": "Rotor-Blockade",
}

# Medium confidence: blink-pattern position matches between the two manuals,
# but the E-number itself is only confirmed for a different indoor unit
# form factor (E9: SRR series, explicitly marked "SRR series only" in the
# service manual) or not cross-checked at all (E16, no E-number in the SRK
# manual to compare against).
_MEDIUM_CONFIDENCE: Final[dict[str, str]] = {
    "E9": "Drain-Störung (Service-Manual: nur SRR-Baureihe, für SRK unbestätigt)",
    "E16": "Innenlüfter-Fehler (Nummer nicht am SRK selbst verifiziert)",
}


def describe_error_code(code: str) -> str | None:
    """Klartext description for an E<n> ErrorCode, or None if undocumented
    (every M<n> maintenance code, and any E-number outside the tables above -
    deliberately no text there rather than guessing, see
    fehlercodes-selfdiagnose.md)."""
    return _HIGH_CONFIDENCE.get(code) or _MEDIUM_CONFIDENCE.get(code)
