#!/usr/bin/env python3
"""
Validates device-code JSON files for the AR Smart IR codes library.

Usage:
    python validate_device_codes.py <file1> <file2> ...

Only files that live under custom_components/ar_smart_ir/codes/<platform>/
are checked; anything else is ignored (with a warning if it looks like it
was meant to be a code file but is misplaced).

Exits non-zero if any file fails validation. Writes a markdown report to
$GITHUB_STEP_SUMMARY if that env var is set, and always prints to stdout.
"""

import base64
import json
import os
import re
import sys

CODES_ROOT = "custom_components/ar_smart_ir/codes"

VALID_PLATFORMS = {"climate", "fan", "light", "media_player"}

VALID_CONTROLLERS = {
    "Broadlink",
    "LinkNLink",
    "Xiaomi",
    "MQTT",
    "LOOKin",
    "ESPHome",
    "Infrared",
    "Tuya",
    "UFOR11",
}

VALID_ENCODINGS = {"Base64", "Hex", "Pronto", "Raw", "Tuya"}

VALID_HVAC_MODES = {"off", "auto", "cool", "heat", "dry", "fan_only", "heat_cool"}

# keys every platform's JSON must have
COMMON_REQUIRED_KEYS = {
    "manufacturer": str,
    "supportedModels": list,
    "supportedController": str,
    "commandsEncoding": str,
    "commands": dict,
}

# extra keys required per platform, on top of COMMON_REQUIRED_KEYS
PLATFORM_REQUIRED_KEYS = {
    "climate": {
        "minTemperature": (int, float),
        "maxTemperature": (int, float),
        "precision": (int, float),
        "operationModes": list,
        "fanModes": list,
    },
    "fan": {
        "speed": list,
    },
    "light": {},
    "media_player": {},
}

FILENAME_RE = re.compile(r"^\d+\.json$")


class Result:
    def __init__(self, path):
        self.path = path
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    @property
    def ok(self):
        return not self.errors


def relevant_codes_file(path):
    """True if this path is (or looks like it's meant to be) a device code file."""
    norm = path.replace("\\", "/")
    if norm.endswith(".json") and f"/{CODES_ROOT}/" in f"/{norm}":
        return True
    # catches the "dropped straight in codes/" mistake, e.g. codes/1783.json
    if norm.endswith(".json") and norm.rstrip("/").endswith(f"{CODES_ROOT}"):
        return True
    if re.search(r"/ar_smart_ir/codes/[^/]+\.json$", norm):
        return True
    return False


def check_location(path, res):
    norm = path.replace("\\", "/")
    m = re.search(rf"{re.escape(CODES_ROOT)}/(.+)$", norm)
    if not m:
        res.error(f"File is not under `{CODES_ROOT}/` at all.")
        return None

    rest = m.group(1)
    parts = rest.split("/")

    if len(parts) == 1:
        res.error(
            f"File is sitting directly in `{CODES_ROOT}/` (`{rest}`). "
            f"It must be inside a platform subfolder, e.g. "
            f"`{CODES_ROOT}/climate/{rest}`."
        )
        return None

    if len(parts) > 2:
        res.error(
            f"File is nested too deep (`{rest}`). Expected "
            f"`{CODES_ROOT}/<platform>/<id>.json`."
        )
        return None

    platform, filename = parts
    if platform not in VALID_PLATFORMS:
        res.error(
            f"Unknown platform folder `{platform}`. Must be one of: "
            f"{', '.join(sorted(VALID_PLATFORMS))}."
        )
        return None

    if not FILENAME_RE.match(filename):
        res.error(
            f"Filename `{filename}` should just be a numeric device id, "
            f"e.g. `1783.json`."
        )

    return platform


def check_schema(data, platform, res):
    for key, expected_type in COMMON_REQUIRED_KEYS.items():
        if key not in data:
            res.error(f"Missing required key `{key}`.")
        elif not isinstance(data[key], expected_type):
            res.error(
                f"Key `{key}` should be {expected_type.__name__}, "
                f"got {type(data[key]).__name__}."
            )

    if isinstance(data.get("supportedModels"), list) and not data["supportedModels"]:
        res.error("`supportedModels` is empty — list at least one model.")

    controller = data.get("supportedController")
    if controller is not None and controller not in VALID_CONTROLLERS:
        res.error(
            f"`supportedController` = `{controller}` is not recognized. "
            f"Must be one of: {', '.join(sorted(VALID_CONTROLLERS))}."
        )

    encoding = data.get("commandsEncoding")
    if encoding is not None and encoding not in VALID_ENCODINGS:
        res.error(
            f"`commandsEncoding` = `{encoding}` is not recognized. "
            f"Must be one of: {', '.join(sorted(VALID_ENCODINGS))}."
        )

    if isinstance(data.get("commands"), dict) and not data["commands"]:
        res.error("`commands` is empty.")

    for key, expected_type in PLATFORM_REQUIRED_KEYS.get(platform, {}).items():
        if key not in data:
            res.error(f"Missing required key `{key}` (required for `{platform}`).")
        elif not isinstance(data[key], expected_type):
            names = (
                expected_type.__name__
                if isinstance(expected_type, type)
                else "/".join(t.__name__ for t in expected_type)
            )
            res.error(f"Key `{key}` should be {names}.")

    if platform == "climate" and isinstance(data.get("operationModes"), list):
        bad = [m for m in data["operationModes"] if m not in VALID_HVAC_MODES]
        if bad:
            res.warn(
                f"`operationModes` contains unrecognized modes: {bad}. "
                f"Expected a subset of {sorted(VALID_HVAC_MODES)}."
            )

    if platform == "climate":
        lo, hi = data.get("minTemperature"), data.get("maxTemperature")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo >= hi:
            res.error(f"`minTemperature` ({lo}) must be less than `maxTemperature` ({hi}).")


def walk_leaves(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk_leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_leaves(v)
    elif isinstance(obj, str):
        yield obj


def check_commands_payload(data, res):
    encoding = data.get("commandsEncoding")
    commands = data.get("commands")
    if not isinstance(commands, dict):
        return

    leaves = list(walk_leaves(commands))
    if not leaves:
        res.error("`commands` has no leaf code strings at all.")
        return

    if encoding == "Base64":
        bad = 0
        for code in leaves:
            try:
                # validate=True catches non-alphabet characters/bad padding
                base64.b64decode(code, validate=True)
            except Exception:
                bad += 1
        if bad:
            res.error(f"{bad}/{len(leaves)} command codes are not valid Base64.")
    elif encoding == "Hex":
        bad = [c for c in leaves if not re.fullmatch(r"[0-9A-Fa-f]+", c)]
        if bad:
            res.error(f"{len(bad)}/{len(leaves)} command codes are not valid hex.")
    # Pronto / Raw / Tuya: format varies too much to strictly validate here;
    # just confirm they're non-empty strings.
    empty = [c for c in leaves if not c.strip()]
    if empty:
        res.error(f"{len(empty)} command code(s) are empty strings.")


def check_duplicates(platform, filename, data, res):
    """Warn if manufacturer+model already exists elsewhere in this platform."""
    folder = os.path.join(CODES_ROOT, platform)
    if not os.path.isdir(folder):
        return
    manufacturer = (data.get("manufacturer") or "").strip().lower()
    models = {m.strip().lower() for m in data.get("supportedModels", []) if isinstance(m, str)}
    if not manufacturer or not models:
        return

    for fname in os.listdir(folder):
        if fname == filename or not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, fname), encoding="utf-8") as f:
                other = json.load(f)
        except Exception:
            continue
        other_manufacturer = (other.get("manufacturer") or "").strip().lower()
        other_models = {
            m.strip().lower() for m in other.get("supportedModels", []) if isinstance(m, str)
        }
        if other_manufacturer == manufacturer and models & other_models:
            res.warn(
                f"Possible duplicate: `{fname}` already covers "
                f"{data.get('manufacturer')} model(s) {models & other_models}."
            )


def validate_file(path):
    res = Result(path)

    if not os.path.isfile(path):
        # file was deleted in this PR - nothing to validate
        return None

    platform = check_location(path, res)

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        res.error(f"Invalid JSON: {e}")
        return res
    except Exception as e:
        res.error(f"Could not read file: {e}")
        return res

    if not isinstance(data, dict):
        res.error("Top level of the JSON must be an object.")
        return res

    if platform:
        check_schema(data, platform, res)
        check_commands_payload(data, res)
        check_duplicates(platform, os.path.basename(path), data, res)

    return res


def main():
    paths = sys.argv[1:]
    candidate_paths = [p for p in paths if relevant_codes_file(p)]

    if not candidate_paths:
        print("No device-code JSON files changed in this PR — nothing to validate.")
        return 0

    results = []
    for path in candidate_paths:
        res = validate_file(path)
        if res is not None:
            results.append(res)

    lines = ["# AR Smart IR — device code validation\n"]
    any_errors = False

    for res in results:
        if res.ok and not res.warnings:
            lines.append(f"✅ `{res.path}` — looks good\n")
            continue
        if res.errors:
            any_errors = True
            lines.append(f"❌ `{res.path}`")
            for e in res.errors:
                lines.append(f"  - **Error:** {e}")
        else:
            lines.append(f"⚠️ `{res.path}`")
        for w in res.warnings:
            lines.append(f"  - Warning: {w}")
        lines.append("")

    report = "\n".join(lines)
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(report)

    return 1 if any_errors else 0


if __name__ == "__main__":
    sys.exit(main())
