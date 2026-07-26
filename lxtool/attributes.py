"""Canonical attribute vocabulary and cross-format normalisation.

Every library names the same physical parameter differently.  GDTF says
``ColorAdd_R``, Open Fixture Library says ``Red``, grandMA2 says ``ColorRGB1``
and a ChamSys head just says ``Red``.  Matching a fixture across libraries is
mostly the problem of agreeing what to call things, so that lives here.

The canonical names below are deliberately close to GDTF's attribute list,
since GDTF is the only one of the four with a published, versioned vocabulary.
"""

from __future__ import annotations

import re

# Canonical attributes grouped by the encoder bank a tech would find them on.
# The group drives both display order and the "how disruptive is this change"
# weighting in matching.py.
GROUPS: dict[str, tuple[str, ...]] = {
    "intensity": ("Dimmer", "Shutter", "Strobe"),
    "position": ("Pan", "Tilt", "PanTiltSpeed"),
    "colour": (
        "ColorMacro", "Cyan", "Magenta", "Yellow", "Red", "Green", "Blue",
        "White", "Amber", "UV", "Lime", "Indigo", "ColorWheel", "ColorWheel2",
        "CTO", "CTB", "Hue", "Saturation",
    ),
    "beam": (
        "Gobo1", "Gobo1Rot", "Gobo2", "Gobo2Rot", "Prism", "PrismRot",
        "Focus", "Zoom", "Iris", "Frost", "Animation", "AnimationRot",
    ),
    "control": ("Control", "Function", "Reset", "Lamp", "Fan", "Speed", "Macro"),
}

ALL: tuple[str, ...] = tuple(a for g in GROUPS.values() for a in g)

GROUP_OF: dict[str, str] = {a: g for g, attrs in GROUPS.items() for a in attrs}

# Attributes where a mismatch is merely cosmetic vs. where it breaks the rig.
# Used to rank which channels a tech must fix first.
CRITICALITY: dict[str, int] = {
    "intensity": 5,
    "position": 4,
    "colour": 3,
    "beam": 2,
    "control": 1,
}

# Literal aliases seen in real library files.  Checked before the regex rules.
_ALIASES: dict[str, str] = {
    # intensity
    "dim": "Dimmer", "intens": "Dimmer", "intensity": "Dimmer",
    "master dimmer": "Dimmer", "masterdimmer": "Dimmer", "mdim": "Dimmer",
    "strobe": "Strobe", "shutter/strobe": "Shutter", "shut": "Shutter",
    # position
    "pan": "Pan", "tilt": "Tilt", "panfine": "Pan", "tiltfine": "Tilt",
    "pan/tilt speed": "PanTiltSpeed", "ptspeed": "PanTiltSpeed",
    "movement speed": "PanTiltSpeed", "speedpantilt": "PanTiltSpeed",
    # colour - subtractive
    "cyan": "Cyan", "magenta": "Magenta", "yellow": "Yellow",
    "colormixcyan": "Cyan", "colormixmagenta": "Magenta", "colormixyellow": "Yellow",
    "coloradd_c": "Cyan", "colorsub_c": "Cyan",
    "coloradd_m": "Magenta", "colorsub_m": "Magenta",
    "coloradd_y": "Yellow", "colorsub_y": "Yellow",
    # colour - additive
    "red": "Red", "green": "Green", "blue": "Blue", "white": "White",
    "amber": "Amber", "uv": "UV", "lime": "Lime", "indigo": "Indigo",
    "coloradd_r": "Red", "coloradd_g": "Green", "coloradd_b": "Blue",
    "coloradd_w": "White", "coloradd_a": "Amber", "coloradd_uv": "UV",
    "coloradd_l": "Lime", "colorrgb1": "Red", "colorrgb2": "Green",
    "colorrgb3": "Blue", "colorrgb4": "White", "colorrgb5": "Amber",
    "colorrgb6": "UV",
    # colour - wheels and temperature
    "color": "ColorWheel", "colour": "ColorWheel", "color1": "ColorWheel",
    "colour1": "ColorWheel", "colorwheel": "ColorWheel", "color2": "ColorWheel2",
    "colour2": "ColorWheel2", "colormacro": "ColorMacro", "colourmacro": "ColorMacro",
    "cto": "CTO", "ctc": "CTO", "ctb": "CTB", "colortemp": "CTO",
    "hue": "Hue", "saturation": "Saturation",
    # beam
    "gobo": "Gobo1", "gobo1": "Gobo1", "gobowheel": "Gobo1",
    "gobo1<t>pos": "Gobo1Rot", "gobo1 rot": "Gobo1Rot", "goborot": "Gobo1Rot",
    "gobo1rot": "Gobo1Rot", "gobospin": "Gobo1Rot",
    "gobo2": "Gobo2", "gobo2rot": "Gobo2Rot", "gobo2 rot": "Gobo2Rot",
    "prism": "Prism", "prism1": "Prism", "prismrot": "PrismRot",
    "prism1pos": "PrismRot", "focus": "Focus", "zoom": "Zoom",
    "iris": "Iris", "frost": "Frost", "frost1": "Frost",
    "animation": "Animation", "animationwheel": "Animation",
    # GDTF's own spelling of the above, so a file we write reads back identically
    "shutter1": "Shutter", "shutter1strobe": "Strobe", "color1": "ColorWheel",
    "color2": "ColorWheel2", "colormacro1": "ColorMacro", "gobo1pos": "Gobo1Rot",
    "gobo2pos": "Gobo2Rot", "prism1": "Prism", "prism1pos": "PrismRot",
    "focus1": "Focus", "frost1": "Frost", "iris1": "Iris",
    "animationwheel1": "Animation", "animationwheel1pos": "AnimationRot",
    "control1": "Control", "pantiltspeed": "PanTiltSpeed",
    # control
    "control": "Control", "function": "Function", "reset": "Reset",
    "lamp": "Lamp", "lampcontrol": "Lamp", "fan": "Fan", "fanspeed": "Fan",
    "speed": "Speed", "macro": "Macro", "effect": "Macro",
}

# Ordered regex fallbacks, applied to the lower-cased, punctuation-stripped name.
_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^(pan)\b", "Pan"),
    (r"^(tilt)\b", "Tilt"),
    (r"dimmer|^intens", "Dimmer"),
    (r"strob", "Strobe"),
    (r"shutter", "Shutter"),
    (r"^cyan|^c$", "Cyan"),
    (r"^magenta|^m$", "Magenta"),
    (r"^yellow|^y$", "Yellow"),
    (r"^red|^r$", "Red"),
    (r"^green|^g$", "Green"),
    (r"^blue|^b$", "Blue"),
    (r"^white|^w$", "White"),
    (r"^amber", "Amber"),
    (r"^uv|ultraviolet", "UV"),
    (r"colou?r ?wheel ?2|colou?r ?2", "ColorWheel2"),
    (r"colou?r ?macro", "ColorMacro"),
    (r"colou?r", "ColorWheel"),
    (r"gobo ?2.*(rot|spin|index)", "Gobo2Rot"),
    (r"gobo.*(rot|spin|index)", "Gobo1Rot"),
    (r"gobo ?2", "Gobo2"),
    (r"gobo", "Gobo1"),
    (r"prism.*(rot|spin|index)", "PrismRot"),
    (r"prism", "Prism"),
    (r"focus", "Focus"),
    (r"zoom", "Zoom"),
    (r"iris", "Iris"),
    (r"frost|diffus", "Frost"),
    (r"anim.*(rot|spin|index)", "AnimationRot"),
    (r"anim", "Animation"),
    (r"reset", "Reset"),
    (r"lamp", "Lamp"),
    (r"fan", "Fan"),
    (r"speed", "Speed"),
    (r"macro|effect", "Macro"),
    (r"control|function|mode", "Control"),
)

_FINE_RE = re.compile(r"\b(fine|lsb|low ?byte|16 ?bit)\b|_f$", re.I)

_PUNCT_RE = re.compile(r"[_\-./\\]+")
_WS_RE = re.compile(r"\s+")


def _clean(name: str) -> str:
    """Lower-case, strip punctuation and GDTF's geometry prefix."""
    s = name.strip().lower()
    # GDTF DMX channel names are "Geometry_Attribute"; keep the last segment
    # only when the prefix looks like a geometry rather than part of the name.
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


# Alias keys are written above in their natural form ("coloradd_r",
# "shutter/strobe"), but lookups happen on cleaned names where punctuation has
# already become whitespace. Clean the keys once so the two sides agree.
_ALIASES_CLEAN: dict[str, str] = {_clean(k): v for k, v in _ALIASES.items()}


def is_fine(name: str) -> bool:
    """True when a channel name marks it as the fine/LSB half of a 16-bit pair."""
    return bool(_FINE_RE.search(name))


def normalise(name: str, *, default: str = "Unknown") -> str:
    """Map a raw channel name from any library onto a canonical attribute.

    Returns ``default`` when nothing matches, rather than guessing - an honest
    "Unknown" is far more useful in a diff than a confident wrong answer.
    """
    if not name:
        return default

    raw = name.strip()
    cleaned = _clean(raw)

    # Drop a fine-marker so "Pan Fine" and "Pan" normalise alike.
    without_fine = _WS_RE.sub(" ", _FINE_RE.sub(" ", cleaned)).strip()

    for candidate in (cleaned, without_fine):
        if candidate in _ALIASES_CLEAN:
            return _ALIASES_CLEAN[candidate]
        # GDTF style: try the segment after a geometry prefix
        parts = candidate.split(" ")
        if len(parts) > 1 and parts[-1] in _ALIASES_CLEAN:
            return _ALIASES_CLEAN[parts[-1]]

    for pattern, attr in _PATTERNS:
        if re.search(pattern, without_fine or cleaned):
            return attr

    return default


def group_of(attribute: str) -> str:
    return GROUP_OF.get(attribute, "control")


# Colour systems.  Whether a fixture mixes subtractively (CMY), additively
# (RGB+) or picks from a wheel is one of the strongest matching signals there
# is: a CMY profile and an RGBW profile with the same channel count are not
# interchangeable, however similar their names look.
_SUBTRACTIVE = frozenset({"Cyan", "Magenta", "Yellow"})
_ADDITIVE = frozenset({"Red", "Green", "Blue", "White", "Amber", "UV", "Lime", "Indigo"})
_WHEEL = frozenset({"ColorWheel", "ColorWheel2", "ColorMacro"})


def colour_system(attrs: set[str] | frozenset[str]) -> str:
    """Classify a mode's colour system from its attribute set.

    Returns one of ``cmy``, ``rgb``, ``hybrid``, ``wheel`` or ``none``.
    ``hybrid`` covers fixtures carrying both subtractive and additive mixing,
    which do exist and must not be collapsed into either camp.
    """
    sub = bool(attrs & _SUBTRACTIVE)
    add = bool(attrs & _ADDITIVE)
    if sub and add:
        return "hybrid"
    if sub:
        return "cmy"
    if add:
        return "rgb"
    if attrs & _WHEEL:
        return "wheel"
    return "none"


def colour_detail(attrs: set[str] | frozenset[str]) -> str:
    """Human label for the exact emitter set, e.g. ``RGBW`` or ``CMY``."""
    system = colour_system(attrs)
    if system in ("rgb", "hybrid"):
        order = ("Red", "Green", "Blue", "White", "Amber", "UV", "Lime", "Indigo")
        letters = {"Red": "R", "Green": "G", "Blue": "B", "White": "W",
                   "Amber": "A", "UV": "UV", "Lime": "L", "Indigo": "I"}
        tag = "".join(letters[a] for a in order if a in attrs)
        return f"CMY+{tag}" if system == "hybrid" else tag
    if system == "cmy":
        return "CMY"
    if system == "wheel":
        return "colour wheel"
    return "no colour"


def criticality(attribute: str) -> int:
    """How badly a mismatch on this attribute hurts, 1 (cosmetic) to 5 (fatal)."""
    return CRITICALITY.get(group_of(attribute), 1)
