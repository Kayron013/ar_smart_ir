#!/usr/bin/env python3
"""Validate ar_smart_ir device code JSON files.

Usage: validate_device_codes.py <file.json> [file.json ...]

Exit code 0 = all files passed (warnings allowed), 1 = one or more errors.
Writes a markdown report to $GITHUB_STEP_SUMMARY when running in Actions.
"""

import base64
import binascii
import json
import os
import re
import sys
from decimal import Decimal

CONTROLLERS = {
    "Broadlink",
    "LinkNLink",
    "Xiaomi",
    "MQTT",
    "LOOKin",
    "ESPHome",
    "UFOR11",
}
ENCODINGS = {"Base64", "Hex", "Pronto", "Raw", "Tuya"}

B64_ALPHABET_RE = re.compile(r"^[A-Za-z0-9+/]+$")


class Report:
    def __init__(self, path):
        self.path = path
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)


def platform_of(path):
    parts = os.path.normpath(path).split(os.sep)
    if "codes" in parts:
        idx = parts.index("codes")
        if idx + 1 < len(parts) - 1:
            return parts[idx + 1]
    return None


def check_base64(code, where, rep):
    """Mirror the lenient decode Broadlink uses: whitespace and sloppy
    trailing padding are tolerated, truncation and bad characters are not."""
    stripped = "".join(code.split()).rstrip("=")
    if not stripped:
        rep.warn(f"{where}: empty command code")
        return
    if not B64_ALPHABET_RE.match(stripped):
        rep.error(f"{where}: invalid Base64 code (bad characters)")
        return
    if len(stripped) % 4 == 1:
        rep.error(f"{where}: invalid Base64 code (truncated — impossible length)")
        return
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        base64.b64decode(padded)
    except (binascii.Error, ValueError):
        rep.error(f"{where}: invalid Base64 code")


def check_raw(code, where, rep):
    """Raw commands are decoded with json.loads → list of numbers
    (see Helper.raw2lirc)."""
    if isinstance(code, list):
        values = code
    else:
        try:
            values = json.loads(code)
        except json.JSONDecodeError:
            rep.error(f"{where}: Raw code must be a JSON array of pulse values")
            return
    if not isinstance(values, list) or not values:
        rep.error(f"{where}: Raw code must be a non-empty JSON array")
        return
    try:
        for value in values:
            int(round(float(value)))
    except (TypeError, ValueError):
        rep.error(f"{where}: Raw code contains invalid pulse values")


def check_code(code, encoding, where, rep):
    if code is None or (isinstance(code, str) and not code.strip()):
        rep.warn(f"{where}: empty command code")
        return
    if encoding == "Raw":
        check_raw(code, where, rep)
        return
    if not isinstance(code, str):
        rep.error(f"{where}: command code must be a string")
        return
    if encoding == "Base64":
        check_base64(code, where, rep)
    elif encoding == "Hex":
        normalized = code.strip().replace(" ", "")
        if normalized.lower().startswith("0x"):
            normalized = normalized[2:]
        try:
            binascii.unhexlify(normalized)
        except (binascii.Error, ValueError):
            rep.error(f"{where}: invalid Hex code")
    elif encoding == "Pronto":
        try:
            data = bytearray.fromhex(code.replace(" ", ""))
            if not data:
                rep.warn(f"{where}: empty Pronto code")
        except ValueError:
            rep.error(f"{where}: invalid Pronto code")
    # Tuya codes vary by device generation; presence check only.


def walk_codes(obj, encoding, where, rep):
    """Recursively validate every leaf command code."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk_codes(v, encoding, f"{where}/{k}", rep)
    elif isinstance(obj, list) and encoding != "Raw":
        for i, v in enumerate(obj):
            walk_codes(v, encoding, f"{where}[{i}]", rep)
    else:
        check_code(obj, encoding, where, rep)


def expected_temps(mn, mx, precision):
    temps = []
    t = mn
    while t <= mx:
        temps.append(format(float(t), "g"))
        t += precision
    return temps


def validate_common(data, rep):
    for key in ("manufacturer", "supportedController", "commandsEncoding", "commands"):
        if key not in data:
            rep.error(f"missing required key '{key}'")
    if not isinstance(data.get("commands"), dict) or not data.get("commands"):
        rep.error("'commands' must be a non-empty object")
    models = data.get("supportedModels")
    if models is not None and (
        not isinstance(models, list) or not all(isinstance(m, str) for m in models)
    ):
        rep.error("'supportedModels' must be a list of strings")
    ctrl = data.get("supportedController")
    if ctrl is not None and ctrl not in CONTROLLERS:
        rep.error(
            f"unknown supportedController '{ctrl}' (expected one of {sorted(CONTROLLERS)})"
        )
    enc = data.get("commandsEncoding")
    if enc is not None and enc not in ENCODINGS:
        rep.error(
            f"unknown commandsEncoding '{enc}' (expected one of {sorted(ENCODINGS)})"
        )


def validate_climate(data, rep):
    try:
        mn = Decimal(str(data["minTemperature"]))
        mx = Decimal(str(data["maxTemperature"]))
        precision = Decimal(str(data.get("precision", 1)))
    except (KeyError, ArithmeticError, ValueError):
        rep.error("min/maxTemperature and precision must be numbers")
        return
    if mn >= mx:
        rep.error(f"minTemperature ({mn}) must be less than maxTemperature ({mx})")
    if precision <= 0:
        rep.error(f"precision must be positive, got {precision}")
        return

    op_modes = data.get("operationModes")
    fan_modes = data.get("fanModes")
    swing_modes = data.get("swingModes")
    if not isinstance(op_modes, list) or not op_modes:
        rep.error("'operationModes' must be a non-empty list")
        return
    if not isinstance(fan_modes, list) or not fan_modes:
        rep.error("'fanModes' must be a non-empty list")
        return
    if swing_modes is not None and (not isinstance(swing_modes, list) or not swing_modes):
        rep.error("'swingModes' must be a non-empty list when present")
        return

    commands = data.get("commands")
    if not isinstance(commands, dict):
        return
    if "off" not in commands:
        rep.error("commands must include an 'off' command")

    temps = expected_temps(mn, mx, precision)

    for op in op_modes:
        op_cmds = commands.get(op)
        if not isinstance(op_cmds, dict):
            rep.warn(f"commands/{op}: missing or not an object")
            continue
        for fan in fan_modes:
            fan_cmds = op_cmds.get(fan)
            if not isinstance(fan_cmds, dict):
                rep.warn(f"commands/{op}/{fan}: missing or not an object")
                continue
            if swing_modes:
                for swing in swing_modes:
                    swing_cmds = fan_cmds.get(swing)
                    if not isinstance(swing_cmds, dict):
                        rep.warn(f"commands/{op}/{fan}/{swing}: missing or not an object")
                        continue
                    check_temps(swing_cmds, temps, f"commands/{op}/{fan}/{swing}", rep)
            else:
                check_temps(fan_cmds, temps, f"commands/{op}/{fan}", rep)


def check_temps(temp_dict, temps, where, rep):
    missing = [t for t in temps if t not in temp_dict]
    if missing:
        shown = ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        rep.warn(f"{where}: missing temperature keys: {shown}")
    unknown = [k for k in temp_dict if k not in temps]
    if unknown:
        shown = ", ".join(unknown[:8]) + ("…" if len(unknown) > 8 else "")
        rep.warn(f"{where}: temperature keys outside min/max/precision range: {shown}")


def validate_media_player(data, rep):
    commands = data.get("commands")
    if not isinstance(commands, dict):
        return
    if "off" not in commands and "on" not in commands:
        rep.warn("media_player has neither 'on' nor 'off' command")
    sources = commands.get("sources")
    if sources is not None and not isinstance(sources, dict):
        rep.error("commands/sources must be an object of source name -> code")


def validate_fan(data, rep):
    speeds = data.get("speed")
    if speeds is not None and (not isinstance(speeds, list) or not speeds):
        rep.error("'speed' must be a non-empty list when present")
    commands = data.get("commands")
    if isinstance(commands, dict) and "off" not in commands:
        rep.warn("fan has no 'off' command")


def validate_light(data, rep):
    commands = data.get("commands")
    if isinstance(commands, dict):
        if "on" not in commands or "off" not in commands:
            rep.warn("light should include 'on' and 'off' commands")


def validate_file(path):
    rep = Report(path)
    if not os.path.isfile(path):
        rep.error("file not found")
        return rep
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        rep.error(f"invalid JSON: {exc}")
        return rep
    except UnicodeDecodeError:
        rep.error("file is not valid UTF-8")
        return rep

    if not isinstance(data, dict):
        rep.error("top-level JSON must be an object")
        return rep

    validate_common(data, rep)

    platform = platform_of(path)
    if platform == "climate":
        validate_climate(data, rep)
    elif platform == "media_player":
        validate_media_player(data, rep)
    elif platform == "fan":
        validate_fan(data, rep)
    elif platform == "light":
        validate_light(data, rep)
    else:
        rep.warn(f"unrecognised platform folder '{platform}'; ran common checks only")

    enc = data.get("commandsEncoding")
    if enc in ENCODINGS and isinstance(data.get("commands"), dict):
        walk_codes(data["commands"], enc, "commands", rep)

    return rep


def main(argv):
    files = [f for f in argv if f.strip()]
    if not files:
        print("No device code files to validate.")
        return 0

    reports = [validate_file(f) for f in files]
    failed = [r for r in reports if r.errors]

    lines = ["## Device code validation", ""]
    for rep in reports:
        status = "❌" if rep.errors else ("⚠️" if rep.warnings else "✅")
        lines.append(f"### {status} `{rep.path}`")
        for e in rep.errors:
            lines.append(f"- ❌ {e}")
        for w in rep.warnings:
            lines.append(f"- ⚠️ {w}")
        if not rep.errors and not rep.warnings:
            lines.append("- All checks passed")
        lines.append("")
    lines.append(
        f"**{len(reports) - len(failed)}/{len(reports)} file(s) passed**"
    )
    report_md = "\n".join(lines)
    print(report_md)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report_md + "\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
