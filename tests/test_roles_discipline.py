"""Generic titles naming a non-software discipline are not software roles (a real queue
put 'Engineer I - Aerodynamics' on top)."""
import pytest

from jobcopilot.filters import non_software_discipline, role_match

OFF = ["Engineer I - Aerodynamics", "Engineer I - Avionics", "Engineer II - Structures (MUAS)",
       "Engineer II - Mechanical Design (P05115)", "Electrical Engineer", "Embedded Firmware Engineer",
       "Process Engineer", "RF Engineer", "Propulsion Engineer I", "Civil Engineer"]
KEEP = ["Software Engineer, Avionics", "Hardware-in-the-loop Software Engineer", "SDE II", "Engineer I",
        "Data Engineer", "AI Engineer", "Software Engineer - Mainframe", "Backend Engineer (Payments)",
        "QA Engineer", "Advance Software Engineer"]


@pytest.mark.parametrize("title", OFF)
def test_non_software_disciplines_are_off(title):
    assert non_software_discipline(title)
    assert role_match(title) == ("other_engineering", "off")


@pytest.mark.parametrize("title", KEEP)
def test_software_titles_still_match(title):
    assert not non_software_discipline(title)
    fam, tier = role_match(title)
    assert tier != "off" and fam != "other_engineering"
