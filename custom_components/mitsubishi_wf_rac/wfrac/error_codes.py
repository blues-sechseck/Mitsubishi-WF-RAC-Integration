"""Klartext descriptions for the self-diagnosis ErrorCode (rac_parser.py).

Source and confidence notes: ../../../../fehlercodes-selfdiagnose.md
(transcribed from the official MHI service/user manuals and, since 09.08.2026,
the SRK databooks). Not hardware-verified against a real occurrence - only
cross-checked between documents.

The unit numbers errors and protective stops out of one shared space (the
databooks' "Error code, stop code table"), which is why the same number is
described once here and reused for both spellings: E<n> is the escalated,
displayed fault, M<n> the protective stop that has not (yet) escalated - see
describe_error_code().
"""

from typing import Final

# High confidence: present with matching wording in the SRR service manual, the
# SRK user's manual and the ZS-WF/ZSX-WF/ZR-WF databooks. The databook tables
# are word-for-word identical across those three series, so these are not
# specific to one model family.
_HIGH_CONFIDENCE: Final[dict[str, str]] = {
    "1": "Fehler Kabel-Fernbedienung",
    "5": "Signalübertragungsfehler Indoor/Outdoor",
    "35": "Kühl-Hochdruckschutz",
    "36": "Verdichter-Übertemperatur (110 °C)",
    "37": "Außenwärmetauscher-Sensor-Fehler",
    "38": "Außenlufttemperatur-Sensor-Fehler",
    "39": "Druckgasleitungs-Sensor-Fehler",
    "40": "Absperrventil (Gasseite) geschlossen",
    "42": "Current Cut (Startstrom-Abschaltung)",
    "47": "Active-Filter-Spannungsfehler",
    "48": "Außenlüfter-Fehler",
    "51": "Leistungstransistor-Fehler",
    "57": "Kältekreis-Schutzabschaltung",
    "58": "Current-Safe-Abschaltung",
    # Was "Störung Außeneinheit" until the databooks named the actual causes.
    "59": "Verdichter-Verkabelung unterbrochen / Spannungseinbruch",
    "61": "Verbindungsleitungen Innen-/Außeneinheit fehlerhaft",
    "62": "Serielle Übertragungsstörung",
    "80": "Innenlüfter-Motor gestört",
    "82": "Innenwärmetauscher-Sensor-Fehler",
    "84": "Kondensationsschutz aktiv",
    "85": "Frostschutz aktiv",
    "86": "Heiz-Hochdruckschutz aktiv",
}

# Medium confidence: the number is confirmed for a different indoor unit form
# factor only (9: SRR series, explicitly marked "SRR series only" in the
# service manual), or it appears in no databook table at all (16 is the SRR
# indoor fan motor number - the SRK tables use 80 for that; 60 has no entry in
# any of the three).
_MEDIUM_CONFIDENCE: Final[dict[str, str]] = {
    "9": "Drain-Störung (Service-Manual: nur SRR-Baureihe, für SRK unbestätigt)",
    "16": "Innenlüfter-Fehler (SRR-Nummer; SRK meldet dafür 80)",
    "60": "Rotor-Blockade (in keiner Databook-Tabelle belegt)",
}


def describe_error_code(code: str) -> str | None:
    """Klartext description for an E<n> or M<n> code, or None if undocumented.

    Both spellings resolve through the same table: the unit numbers protective
    stops and displayed faults out of one space, so M35 and E35 are the same
    condition at different escalation stages (the databooks' auto-recovery
    column gives the count - e.g. cooling high pressure control after 5
    occurrences, compressor overheat after 2). M codes are prefixed so the
    distinction survives into the UI, since a protective stop that recovered on
    its own is not the same news as a fault the unit is displaying.

    Anything outside the tables gets no text rather than a guess - see
    fehlercodes-selfdiagnose.md.
    """
    if not code or code == "00":
        return None
    prefix, number = code[0], code[1:]
    if prefix not in ("E", "M") or not number.isdigit():
        return None
    # rac_parser spells M codes zero-padded ("M01") and E codes bare ("E1"),
    # so normalise before the lookup rather than keying both spellings.
    number = str(int(number))
    description = _HIGH_CONFIDENCE.get(number) or _MEDIUM_CONFIDENCE.get(number)
    if description is None:
        return None
    if prefix == "M":
        return f"Schutzabschaltung: {description}"
    return description
