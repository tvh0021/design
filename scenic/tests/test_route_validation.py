import numpy as np
import pytest

from scenic.cli import main
from scenic.score import project_lonlat
from scenic.validate import stretch_ends, v5_moments

from test_search import ladder


def test_stretch_ends_are_the_farthest_nodes():
    g = ladder().g
    assert set(stretch_ends(g, np.array([4, 5, 6, 7]))) == {0, 4}
    assert set(stretch_ends(g, np.array([1, 2]))) == {1, 3}


def test_v5_hits_by_point_and_by_member_edges():
    mom = ladder().moments
    sx, sy = project_lonlat(np.array([-76.94, -76.925, -76.91]), np.array([43.03] * 3))
    se = np.array([6, 6, 6])
    cfg = {"radius_m": 300.0, "moments_need": 1, "non_moments_max": 0,
           "moment": [{"label": "at the moment", "at": [-76.925, 43.0305]},
                      {"label": "member edge end", "at": [-76.9101, 43.03]}],
           "non_moment": [{"label": "bottom road", "at": [-76.925, 43.0]}]}
    r = v5_moments(mom, cfg, (sx, sy, se))
    assert [x["hit"] for x in r["moments"]] == [True, False]
    assert r["by_member_edges"] == {"moments_hit": 2, "non_moments_hit": 0}
    assert r["non_moments_hit"] == 0 and r["pass"]


def test_route_cli_rejects_bad_coordinates():
    with pytest.raises(SystemExit):
        main(["route", "--from", "43.1,-77.6", "--to", "not-a-point"])
    with pytest.raises(SystemExit):
        main(["route", "--from", "95,-77.6", "--to", "43,-76"])


def test_v5_with_no_moment_members_reports_a_plain_fail():
    from scenic.search import Moments

    sx, sy = project_lonlat(np.array([-76.925]), np.array([43.03]))
    cfg = {"radius_m": 300.0, "moments_need": 1, "non_moments_max": 0,
           "moment": [{"label": "somewhere", "at": [-76.925, 43.0305]}], "non_moment": []}
    r = v5_moments(Moments.empty(1), cfg, (sx, sy, np.array([0])))
    assert r["moments_hit"] == 0 and not r["pass"]
    assert "member_dist_m" not in r["moments"][0]
