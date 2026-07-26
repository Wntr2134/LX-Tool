"""MVR / whole-patch tests."""

from __future__ import annotations

import io
import zipfile

import pytest

from lxtool.formats import gdtf, mvr
from lxtool.model import Channel, Fixture, Mode, PatchedFixture, Rig


# --------------------------------------------------------------------------
# address arithmetic
# --------------------------------------------------------------------------

@pytest.mark.parametrize("absolute,universe,address", [
    (1, 1, 1),
    (512, 1, 512),
    (513, 2, 1),
    (1024, 2, 512),
    (1025, 3, 1),
])
def test_address_split_and_join(absolute, universe, address):
    assert mvr.split_address(absolute) == (universe, address)
    assert mvr.join_address(universe, address) == absolute


def test_address_split_clamps_nonsense():
    assert mvr.split_address(0) == (1, 1)
    assert mvr.split_address(-5) == (1, 1)


# --------------------------------------------------------------------------
# fixtures / rigs
# --------------------------------------------------------------------------

def _fixture(mfr="ACME", model="Mover", channels=("Dimmer", "Pan", "Tilt")):
    mode = Mode(name="Mode 1", channels=[
        Channel(offset=i, name=a, attribute=a) for i, a in enumerate(channels, 1)
    ])
    return Fixture(manufacturer=mfr, model=model, modes=[mode])


def test_footprint_and_span():
    pf = PatchedFixture(name="Spot 1", fixture=_fixture(), mode="Mode 1", address=10)
    assert pf.footprint == 3
    assert pf.last_address == 12
    assert pf.absolute_address == 10


def test_absolute_address_across_universes():
    pf = PatchedFixture(name="x", fixture=_fixture(), mode="Mode 1", universe=3, address=5)
    assert pf.absolute_address == 1029


def test_footprint_falls_back_to_first_mode():
    pf = PatchedFixture(name="x", fixture=_fixture(), mode="")
    assert pf.footprint == 3


def test_conflicts_detects_overlap():
    a = PatchedFixture(name="A", fixture=_fixture(), mode="Mode 1", address=1)   # 1-3
    b = PatchedFixture(name="B", fixture=_fixture(), mode="Mode 1", address=3)   # 3-5
    c = PatchedFixture(name="C", fixture=_fixture(), mode="Mode 1", address=10)
    rig = Rig(fixtures=[a, b, c])
    clashes = rig.conflicts()
    assert len(clashes) == 1
    assert {clashes[0][0].name, clashes[0][1].name} == {"A", "B"}


def test_conflicts_ignores_other_universes():
    a = PatchedFixture(name="A", fixture=_fixture(), mode="Mode 1", universe=1, address=1)
    b = PatchedFixture(name="B", fixture=_fixture(), mode="Mode 1", universe=2, address=1)
    assert Rig(fixtures=[a, b]).conflicts() == []


def test_rig_types_deduplicates():
    fx = _fixture()
    rig = Rig(fixtures=[
        PatchedFixture(name=f"S{i}", fixture=fx, mode="Mode 1", address=i * 10)
        for i in range(1, 4)
    ])
    assert len(rig.fixtures) == 3
    assert len(rig.types()) == 1


def test_by_universe_sorts_by_address():
    rig = Rig(fixtures=[
        PatchedFixture(name="late", fixture=_fixture(), mode="Mode 1", address=100),
        PatchedFixture(name="early", fixture=_fixture(), mode="Mode 1", address=1),
    ])
    assert [p.name for p in rig.by_universe()[1]] == ["early", "late"]


# --------------------------------------------------------------------------
# archive round-trip
# --------------------------------------------------------------------------

@pytest.fixture
def rig():
    spot = _fixture("Robe", "Robin 600", ("Dimmer", "Pan", "Tilt", "Zoom"))
    par = _fixture("Chauvet", "COLORdash", ("Dimmer", "Red", "Green", "Blue"))
    return Rig(name="show", fixtures=[
        PatchedFixture(name="Spot 1", fixture=spot, mode="Mode 1",
                       fixture_id="1", universe=1, address=1, layer="Spots"),
        PatchedFixture(name="Spot 2", fixture=spot, mode="Mode 1",
                       fixture_id="2", universe=1, address=11, layer="Spots"),
        PatchedFixture(name="Par 1", fixture=par, mode="Mode 1",
                       fixture_id="3", universe=2, address=1, layer="Wash"),
    ])


def test_write_produces_valid_archive(rig, tmp_path):
    out = mvr.write(rig, tmp_path / "show.mvr")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert mvr.SCENE_FILE in names
    # Both distinct types embedded as GDTF, named Manufacturer@Model.gdtf
    gdtfs = sorted(n for n in names if n.endswith(".gdtf"))
    assert gdtfs == ["Chauvet@COLORdash.gdtf", "Robe@Robin 600.gdtf"]


def test_roundtrip_preserves_patch(rig, tmp_path):
    out = mvr.write(rig, tmp_path / "show.mvr")
    back = mvr.read(out)

    assert len(back.fixtures) == 3
    assert len(back.types()) == 2

    by_name = {p.name: p for p in back.fixtures}
    assert by_name["Spot 1"].universe == 1
    assert by_name["Spot 1"].address == 1
    assert by_name["Spot 2"].address == 11
    # Universe 2 survives the continuous-address round trip.
    assert by_name["Par 1"].universe == 2
    assert by_name["Par 1"].address == 1
    assert by_name["Par 1"].layer == "Wash"
    assert by_name["Spot 1"].fixture_id == "1"


def test_roundtrip_preserves_fixture_types(rig, tmp_path):
    back = mvr.read(mvr.write(rig, tmp_path / "show.mvr"))
    spot = next(p for p in back.fixtures if p.name == "Spot 1")
    assert spot.fixture.manufacturer == "Robe"
    assert spot.fixture.model == "Robin 600"
    assert spot.mode == "Mode 1"
    assert spot.fixture.modes[0].attribute_set() == {"Dimmer", "Pan", "Tilt", "Zoom"}


def test_layers_are_preserved(rig, tmp_path):
    back = mvr.read(mvr.write(rig, tmp_path / "show.mvr"))
    assert {p.layer for p in back.fixtures} == {"Spots", "Wash"}


# --------------------------------------------------------------------------
# tolerance of real-world variation
# --------------------------------------------------------------------------

def _mvr_from_xml(path, xml: str, gdtfs: dict[str, bytes] | None = None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(mvr.SCENE_FILE, xml)
        for name, blob in (gdtfs or {}).items():
            zf.writestr(name, blob)
    return path


SCENE = """<?xml version="1.0" encoding="UTF-8"?>
<GeneralSceneDescription verMajor="1" verMinor="5">
  <Scene><Layers>
    <Layer name="Main" uuid="L1"><ChildList>
      <Fixture name="Spot 1" uuid="F1">
        <GDTFSpec>Robe@Robin.gdtf</GDTFSpec>
        <GDTFMode>Mode 1</GDTFMode>
        <Addresses><Address break="0">513</Address></Addresses>
        <FixtureID>7</FixtureID>
      </Fixture>
      <Fixture name="Spot 2" uuid="F2">
        <GDTFSpec>Robe@Robin.gdtf</GDTFSpec>
        <GDTFMode>Mode 1</GDTFMode>
        <Addresses><Address break="2">17</Address></Addresses>
        <FixtureID>8</FixtureID>
      </Fixture>
    </ChildList></Layer>
  </Layers></Scene>
</GeneralSceneDescription>
"""


def test_missing_gdtf_still_yields_the_patch(tmp_path):
    """A type referenced but not embedded must not lose the fixture."""
    path = _mvr_from_xml(tmp_path / "a.mvr", SCENE)
    rig = mvr.read(path)
    assert len(rig.fixtures) == 2
    spot = rig.fixtures[0]
    assert spot.name == "Spot 1"
    assert spot.fixture.modes == []          # type unknown
    assert spot.universe == 2 and spot.address == 1   # address still recovered


def test_both_address_conventions(tmp_path):
    """Continuous addresses and break-relative addresses both resolve."""
    rig = mvr.read(_mvr_from_xml(tmp_path / "b.mvr", SCENE))
    by_name = {p.name: p for p in rig.fixtures}
    # 513 continuous -> universe 2 address 1
    assert (by_name["Spot 1"].universe, by_name["Spot 1"].address) == (2, 1)
    # break=2, address 17 -> universe 2 address 17
    assert (by_name["Spot 2"].universe, by_name["Spot 2"].address) == (2, 17)


def test_namespaced_document_is_tolerated(tmp_path):
    xml = SCENE.replace(
        "<GeneralSceneDescription ",
        '<GeneralSceneDescription xmlns="http://schemas.mvrdevelopment.org/mvr" ',
    )
    rig = mvr.read(_mvr_from_xml(tmp_path / "c.mvr", xml))
    assert len(rig.fixtures) == 2


def test_embedded_gdtf_is_parsed(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as gz:
        gz.writestr("description.xml", gdtf.build_description(_fixture("Robe", "Robin")))

    path = _mvr_from_xml(tmp_path / "d.mvr", SCENE, {"Robe@Robin.gdtf": buf.getvalue()})
    rig = mvr.read(path)
    assert rig.fixtures[0].fixture.manufacturer == "Robe"
    assert rig.fixtures[0].fixture.modes[0].attribute_set() == {"Dimmer", "Pan", "Tilt"}


def test_corrupt_embedded_gdtf_degrades_gracefully(tmp_path):
    path = _mvr_from_xml(tmp_path / "e.mvr", SCENE, {"Robe@Robin.gdtf": b"not a zip"})
    rig = mvr.read(path)
    assert len(rig.fixtures) == 2
    assert rig.fixtures[0].fixture.modes == []


def test_not_an_mvr(tmp_path):
    path = tmp_path / "f.mvr"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("something-else.txt", "hello")
    with pytest.raises(ValueError, match="GeneralSceneDescription"):
        mvr.read(path)
