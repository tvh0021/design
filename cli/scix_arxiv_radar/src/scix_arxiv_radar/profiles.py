"""Named literature profiles. Source grammars stay local to their adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    description: str
    arxiv_query: str
    ads_query: str


PROFILES: dict[str, Profile] = {
    "grmhd": Profile(
        name="grmhd",
        description="Resistive GRMHD, IMEX, constrained transport, and higher-order finite-volume methods.",
        arxiv_query=(
            '(all:"resistive GRMHD" OR all:"general relativistic magnetohydrodynamics" '
            'OR all:GRMHD OR all:"relativistic MHD" '
            'OR (all:IMEX AND (all:MHD OR all:magnetohydrodynamics)) '
            'OR (all:"constrained transport" AND (all:MHD OR all:magnetohydrodynamics)) '
            'OR (all:"higher order finite volume" AND (all:MHD OR all:magnetohydrodynamics)))'
        ),
        ads_query=(
            '(abs:"resistive GRMHD" OR abs:"general relativistic magnetohydrodynamics" '
            'OR abs:GRMHD OR abs:"relativistic MHD" '
            'OR (abs:IMEX AND (abs:MHD OR abs:magnetohydrodynamics)) '
            'OR (abs:"constrained transport" AND (abs:MHD OR abs:magnetohydrodynamics)) '
            'OR (abs:"higher order finite volume" AND (abs:MHD OR abs:magnetohydrodynamics)))'
        ),
    ),
    "m87": Profile(
        name="m87",
        description="M87, AGN feedback, turbulence, and XRISM.",
        arxiv_query='(all:"M87" OR all:"AGN feedback" OR all:turbulence OR all:XRISM)',
        ads_query='(abs:"M87" OR abs:"AGN feedback" OR abs:turbulence OR abs:XRISM)',
    ),
}


def require_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError as error:
        choices = ", ".join(sorted(PROFILES))
        raise ValueError(f"Unknown profile {name!r}. Choose one of: {choices}.") from error
