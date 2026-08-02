"""Real airconStat payloads captured live against a physical device
(SRK35ZS-WF, ModelNrRaw=1, "schlafzimmer") via wfrac_live_monitor.py on
2026-08-01, re-encoded to base64 (translate_bytes()'s input format).

`fields` for each entry was computed by wfrac_live_monitor.py's own
decode_fields() at capture time - a small, separately hand-written mirror of
rac_parser.py's bit logic (not a call into rac_parser.py itself), so matching
it is a genuine cross-check rather than the parser confirming itself.
OperationMode_idx there is pre-"+1" (see rac_parser.py's
_parse_basic_settings): find_match() returns -1 when no mode bit is set,
which is how the protocol legitimately encodes AUTO (RCV_MODE_MASKS[0] == 0,
i.e. "no bits set" *is* AUTO, not a decode failure) - so idx=-1 decodes to
OperationMode=0 (AUTO), not an error.
"""

LIVE_CAPTURES = {
    "off": (
        "AACqj6r/AAAIAAAUigAAAAAAAf////9hp4EECAcqmgAAiAAABAAAAAAAAAOAIJn/gBDP/5QQAAAOQQ==",
        {
            "Operation": False,
            "OperationMode": 1,  # idx=0 -> COOL, but Operation is off so it's moot
            "PresetTemp": 21.0,
            "AirFlow": 0,
            "ModelNrRaw": 1,
            "Vacant": False,
        },
    ),
    "on_cool": (
        "AACrj7T/AAAIAAAUigAAAAAAAf/////0NoEECQc0mwAAiAAABAAAAAAAAAOAIJv/gBDT/5QQAAD4rQ==",
        {
            "Operation": True,
            "OperationMode": 1,
            "PresetTemp": 26.0,
            "AirFlow": 0,
            "ModelNrRaw": 1,
            "Vacant": False,
        },
    ),
    "on_heat": (
        "AACzj6r/AAAIAAAUigAAAAAAAf////8sM4EEEQcqmgAAgQAABAAAAAAAAAOAIJr/gBDT/5QQAACzfA==",
        {
            "Operation": True,
            "OperationMode": 2,
            "PresetTemp": 21.0,
            "AirFlow": 0,
            "ModelNrRaw": 1,
            "Vacant": False,
        },
    ),
    "on_fan_only": (
        "AACvj6r/AAAIAAAUigAAAAAAAf////8sFYEEDQcqmgAAiAAABAAAAAAAAAOAIJr/gBDT/5QQAABZ2w==",
        {
            "Operation": True,
            "OperationMode": 3,
            "PresetTemp": 21.0,
            "AirFlow": 0,
            "ModelNrRaw": 1,
            "Vacant": False,
        },
    ),
    "on_dry": (
        "AACnj6n/AAAIAAAUigAAAAAAAf////9aqIEEBQcpnAAQiAAABAAAAAAAAAOAIJz/gBDS/5QQAABUYQ==",
        {
            "Operation": True,
            "OperationMode": 4,
            "PresetTemp": 20.5,
            "AirFlow": 0,
            "ModelNrRaw": 1,
            "Vacant": False,
        },
    ),
    "on_auto": (
        "AACjj6n/AAAAAAAUigAAAAAAAf////+k6IEEAQcpnQAAgQAABAAAAAAAAAOAIJ3/gBDS/5QQAABLtw==",
        {
            "Operation": True,
            "OperationMode": 0,
            "PresetTemp": 20.5,
            "AirFlow": 0,
            "ModelNrRaw": 1,
            "Vacant": False,
        },
    ),
    "on_cool_fanspeed4": (
        "AACrjq7/AAAIAAAQigAAAAAAAf/////vAoEECQYumgAQiAIAAAAAAAAAAAOAIJr/gBDP/5QQAABWyA==",
        {
            "Operation": True,
            "OperationMode": 1,
            "PresetTemp": 23.0,
            "AirFlow": 4,
            "ModelNrRaw": 1,
            "Vacant": False,
        },
    ),
}
