# Geodesic Map Context

Interactive browser-based visualization of shortest-distance geodesic paths between global cities on an orthographically projected Earth.

## Language

**Geodesic Path**:
The shortest curved path between two points on the surface of a sphere, lying along a great circle.
_Avoid_: Straight line, flight route, trajectory

**Great Circle**:
The intersection of the sphere with a plane passing through the center of the sphere.
_Avoid_: Circumference, orbit

**Spherical Midpoint**:
The point on the sphere surface equidistant from both points along the minor great-circle arc, calculated via normalized 3D Cartesian unit vectors.
_Avoid_: Average coordinates, center point

**Orthographic Center**:
The latitude and longitude coordinate set as the center pole $(lat_0, lon_0)$ of the orthographic projection so that the visible hemisphere directly faces the midpoint.
_Avoid_: Map center, camera position

**Antipodal Pair**:
Two points that are diametrically opposite on the globe ($180^\circ$ apart), for which infinitely many great circle paths exist and the midpoint is geometrically degenerate. By domain convention, the polar path passing through the North Pole is selected with an explicit degeneracy indicator.
_Avoid_: Opposite cities, inverse locations

**City Record**:
A compact geographical tuple representing a global municipality: `[name, country, lat, lng]`, filtered from the top 10,000 cities by population.
_Avoid_: Location, waypoint, pin

**Prevailing Wind Offset**:
A directional flight duration adjustment reflecting atmospheric jet streams, applying a calculated percentage penalty when routing against prevailing westerly winds.
_Avoid_: Wind speed, weather forecast
