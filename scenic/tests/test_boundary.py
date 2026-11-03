import numpy as np
import pyarrow as pa
import pytest
from pyproj import Transformer
from shapely.geometry import box

from scenic.boundary import edges_in_state, filter_moments, load_boundary, points_in_state


def coordinates():
    tr = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
    return tr.transform([500, 0, -199, -201, 500], [500, 500, 500, 500, 1201])


def test_boundary_tolerance_and_filter():
    polygon = box(0, 0, 1000, 1000)
    lon, lat = coordinates()
    assert points_in_state(lon, lat, polygon).tolist() == [True, True, True, False, False]
    table = pa.table({"id": [0, 2, 4, 6, 8], "lon": lon, "lat": lat,
                      "type": ["water"] * 5, "label": ["inside", "line", "bridge", "outside", "north"],
                      "members": [[0], [1], [2], [3], [4]]}).replace_schema_metadata({b"fp": b"test"})
    kept, info = filter_moments(table, {"by_type": {"water": 5}}, log=lambda _: None, polygon=polygon)
    assert kept["label"].to_pylist() == ["inside", "line", "bridge"]
    assert kept["id"].to_pylist() == [0, 1, 2]
    assert kept["members"].to_pylist() == [[0], [1], [2]]
    assert kept.schema.metadata == table.schema.metadata
    assert info["state_boundary"]["removed_labels"] == ["outside", "north"]
    assert info["moments"] == 3
    assert info["by_type"] == {"water": 3}


def test_missing_boundary_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError, match="python -m scenic.boundary"):
        load_boundary(tmp_path / "absent.geojson")


def test_negative_tolerance():
    with pytest.raises(ValueError):
        points_in_state([], [], box(0, 0, 1, 1), tolerance_m=-1)


def test_member_edge_inside_keeps_bridge_cluster():
    """A bridge whose representative point lands on the far shore stays when a
    member edge touches a node inside the state; a road wholly outside goes."""
    polygon = box(0, 0, 1000, 1000)
    tr = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
    lon, lat = tr.transform([-900, -900], [500, 500])  # both points 900 m outside
    # edge 0: from inside (500,500) to outside (-900,500); edge 1: wholly outside
    u, v = [0, 2], [1, 3]
    node_x, node_y = [500, -900, -900, -1500], [500, 500, 500, 500]
    table = pa.table({"id": [0, 1], "lon": lon, "lat": lat, "type": ["water"] * 2,
                      "label": ["bridge", "island road"], "members": [[0], [1]], "edge_idx": [0, 1]})
    edge_inside = edges_in_state(u, v, node_x, node_y, polygon)
    assert edge_inside.tolist() == [True, False]
    kept, info = filter_moments(table, {"by_type": {"water": 2}}, log=lambda _: None,
                                polygon=polygon, edge_inside=edge_inside)
    assert kept["label"].to_pylist() == ["bridge"]
    assert info["state_boundary"]["removed_labels"] == ["island road"]
