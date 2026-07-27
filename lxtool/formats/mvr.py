"""MVR (My Virtual Rig) reader and writer - whole-patch interchange.

MVR is the companion format to GDTF and the way a *rig* moves between tools:
grandMA3, Vectorworks, Capture, depence and others read and write it.  Where
GDTF describes one fixture type, MVR describes the show - which types are in
it, how many of each, and at what addresses.

An ``.mvr`` is a zip containing ``GeneralSceneDescription.xml`` plus the
``.gdtf`` files it references::

    <GeneralSceneDescription verMajor="1" verMinor="5">
      <Scene>
        <Layers>
          <Layer name="Layer 1" uuid="...">
            <ChildList>
              <Fixture name="Spot 1" uuid="...">
                <GDTFSpec>Robe@Robin_600.gdtf</GDTFSpec>
                <GDTFMode>Mode 1</GDTFMode>
                <Addresses><Address break="0">1</Address></Addresses>
                <FixtureID>1</FixtureID>
                <Matrix>{...}</Matrix>
              </Fixture>

STATUS: written against the published MVR structure but **not yet validated
against a real export**.  The parser is namespace-agnostic and searches by
local element name, so layout variations between writers are tolerated, and
a fixture whose GDTF is missing from the archive still comes through with its
address and mode rather than being dropped.  Validate with
``lx doctor <file.mvr>`` before trusting it.

For MA3, this plus :mod:`lxtool.formats.gdtf` is the whole story: MA3 is
GDTF-native for fixture types and MVR for the patch.
"""

from __future__ import annotations

import uuid as _uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ..model import Fixture, Mode, PatchedFixture, Rig
from . import gdtf

SCENE_FILE = "GeneralSceneDescription.xml"

# MVR stores a single continuous DMX address. Which universe that lands in is
# a matter of convention; 512 channels per universe with universe 1 first is
# what every desk shows the operator.
CHANNELS_PER_UNIVERSE = 512

_IDENTITY_MATRIX = "{1.000000,0.000000,0.000000}{0.000000,1.000000,0.000000}" \
                   "{0.000000,0.000000,1.000000}{0.000000,0.000000,0.000000}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element, name: str) -> ET.Element | None:
    for child in el.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text(el: ET.Element, name: str, default: str = "") -> str:
    found = _find(el, name)
    if found is not None and found.text and found.text.strip():
        return found.text.strip()
    return default


def split_address(absolute: int) -> tuple[int, int]:
    """Split MVR's continuous address into (universe, address).

    Universes and addresses are both 1-based on the way out, matching what a
    desk displays.  Address 1 is universe 1 address 1; address 513 is
    universe 2 address 1.
    """
    if absolute < 1:
        return 1, 1
    zero = absolute - 1
    return zero // CHANNELS_PER_UNIVERSE + 1, zero % CHANNELS_PER_UNIVERSE + 1


def join_address(universe: int, address: int) -> int:
    """Inverse of :func:`split_address`."""
    return (max(universe, 1) - 1) * CHANNELS_PER_UNIVERSE + max(address, 1)


def _parse_address(fixture_el: ET.Element) -> tuple[int, int]:
    """Read the first Address element, tolerating both address conventions.

    Some writers emit a continuous address, others emit an address already
    relative to a universe carried on the ``break`` attribute.  A value over
    512 can only be continuous, so that case is unambiguous; below that we
    honour ``break`` when it is present and non-zero.
    """
    addr_el = _find(fixture_el, "Address")
    if addr_el is None or not (addr_el.text or "").strip():
        return 1, 1

    try:
        raw = int(float(addr_el.text.strip()))
    except ValueError:
        return 1, 1

    if raw > CHANNELS_PER_UNIVERSE:
        return split_address(raw)

    brk = addr_el.get("break")
    if brk and brk.strip().isdigit() and int(brk) > 0:
        return int(brk), max(raw, 1)
    return split_address(raw)


def read(path: Path | str) -> Rig:
    """Read an ``.mvr`` archive into a :class:`Rig`."""
    path = Path(path)
    try:
        zf_ctx = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{path.name} is not an MVR archive: {exc}") from exc

    with zf_ctx as zf:
        names = [n for n in zf.namelist() if n.endswith(SCENE_FILE)]
        if not names:
            raise ValueError(f"{path.name} has no {SCENE_FILE} - not an MVR archive")
        try:
            root = ET.fromstring(zf.read(names[0]))
        except ET.ParseError as exc:
            raise ValueError(f"{SCENE_FILE} is not valid XML: {exc}") from exc

        # Cache the embedded fixture types; several fixtures share each one.
        types: dict[str, Fixture] = {}

        def load_type(spec: str) -> Fixture:
            if spec in types:
                return types[spec]
            match = next(
                (n for n in zf.namelist() if Path(n).name.lower() == Path(spec).name.lower()),
                None,
            )
            if match is None:
                # Referenced but not embedded. Keep the fixture rather than
                # dropping it - the address and mode are still useful.
                fx = Fixture(manufacturer="", model=Path(spec).stem, source="mvr")
            else:
                try:
                    fx = gdtf.parse_description(_description_of(zf, match))
                    fx.source = "mvr"
                except (ValueError, zipfile.BadZipFile, ET.ParseError):
                    fx = Fixture(manufacturer="", model=Path(spec).stem, source="mvr")
            fx.source_id = spec
            types[spec] = fx
            return fx

        rig = Rig(name=path.stem, source="mvr")

        for layer_el in [e for e in root.iter() if _local(e.tag) == "Layer"]:
            layer_name = layer_el.get("name") or _text(layer_el, "Name") or ""
            for fx_el in [e for e in layer_el.iter() if _local(e.tag) == "Fixture"]:
                spec = _text(fx_el, "GDTFSpec")
                if not spec:
                    continue
                universe, address = _parse_address(fx_el)
                rig.fixtures.append(PatchedFixture(
                    name=fx_el.get("name") or _text(fx_el, "Name") or spec,
                    fixture=load_type(spec),
                    mode=_text(fx_el, "GDTFMode"),
                    fixture_id=_text(fx_el, "FixtureID"),
                    universe=universe,
                    address=address,
                    layer=layer_name,
                    uuid=fx_el.get("uuid") or "",
                ))

    return rig


def _description_of(zf: zipfile.ZipFile, name: str) -> bytes:
    """Extract description.xml from a .gdtf entry nested inside the MVR zip."""
    import io

    with zf.open(name) as fh:
        inner = io.BytesIO(fh.read())
    with zipfile.ZipFile(inner) as gz:
        desc = [n for n in gz.namelist() if n.lower().endswith("description.xml")]
        if not desc:
            raise ValueError(f"{name} contains no description.xml")
        return gz.read(desc[0])


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _gdtf_filename(fixture: Fixture) -> str:
    """MVR names embedded types ``Manufacturer@Model.gdtf``."""
    def clean(s: str) -> str:
        return "".join(c if c.isalnum() or c in "-_ " else "_" for c in s).strip() or "Unknown"

    return f"{clean(fixture.manufacturer)}@{clean(fixture.model)}.gdtf"


def build_scene(rig: Rig, specs: dict[str, str]) -> str:
    """Render the GeneralSceneDescription.xml for ``rig``."""
    root = ET.Element("GeneralSceneDescription", {"verMajor": "1", "verMinor": "5"})
    ET.SubElement(root, "UserData")
    scene = ET.SubElement(root, "Scene")
    ET.SubElement(scene, "AUXData")
    layers_el = ET.SubElement(scene, "Layers")

    # Preserve layer grouping from the source rig.
    by_layer: dict[str, list[PatchedFixture]] = {}
    for pf in rig.fixtures:
        by_layer.setdefault(pf.layer or "Layer 1", []).append(pf)

    for layer_name, members in by_layer.items():
        layer_el = ET.SubElement(layers_el, "Layer", {
            "name": layer_name,
            "uuid": str(_uuid.uuid4()).upper(),
        })
        ET.SubElement(layer_el, "Matrix").text = _IDENTITY_MATRIX
        children = ET.SubElement(layer_el, "ChildList")

        for pf in members:
            fx_el = ET.SubElement(children, "Fixture", {
                "name": pf.name,
                "uuid": (pf.uuid or str(_uuid.uuid4())).upper(),
            })
            ET.SubElement(fx_el, "Matrix").text = _IDENTITY_MATRIX
            ET.SubElement(fx_el, "GDTFSpec").text = specs[pf.fixture.key.lower()]
            ET.SubElement(fx_el, "GDTFMode").text = pf.mode or (
                pf.fixture.modes[0].name if pf.fixture.modes else ""
            )
            addresses = ET.SubElement(fx_el, "Addresses")
            addr = ET.SubElement(addresses, "Address", {"break": "0"})
            addr.text = str(pf.absolute_address)
            ET.SubElement(fx_el, "FixtureID").text = pf.fixture_id or ""
            ET.SubElement(fx_el, "UnitNumber").text = "0"
            ET.SubElement(fx_el, "CastShadow").text = "true"

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def write(rig: Rig, path: Path | str) -> Path:
    """Write ``rig`` as an ``.mvr`` archive with its fixture types embedded."""
    path = Path(path)

    specs: dict[str, str] = {}
    for fx in rig.types():
        specs[fx.key.lower()] = _gdtf_filename(fx)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(SCENE_FILE, build_scene(rig, specs))
        for fx in rig.types():
            name = specs[fx.key.lower()]
            if not fx.modes:
                # Nothing to describe; skip rather than embed an empty type.
                continue
            import io

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as gz:
                gz.writestr("description.xml", gdtf.build_description(fx))
            zf.writestr(name, buf.getvalue())

    return path
