"""Named locations (waypoints) read from the RMF building map.

The map is the single source of truth for place names, so the same code works
for the office demo now and for a custom factory map later. Waypoint params set
in the traffic editor tell us what each place can be used for:

    pickup_dispenser   delivery pickup point (value = dispenser name)
    dropoff_ingestor   delivery drop-off point (value = ingestor name)
    is_charger         charging station
    is_parking_spot    parking spot
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

# rmf_building_map_msgs/Param type constants
_TYPE_STRING, _TYPE_INT, _TYPE_DOUBLE, _TYPE_BOOL = 1, 2, 3, 4


@dataclass
class Location:
    name: str
    level: str
    x: float
    y: float
    pickup_handler: str | None = None
    dropoff_handler: str | None = None
    is_charger: bool = False
    is_parking_spot: bool = False

    def to_dict(self) -> dict:
        """Public view for the LLM. Handler names are an internal detail."""
        return {
            "name": self.name,
            "level": self.level,
            "x": self.x,
            "y": self.y,
            "can_pickup": self.pickup_handler is not None,
            "can_dropoff": self.dropoff_handler is not None,
            "is_charger": self.is_charger,
        }


def _param_value(param: dict):
    kind = param.get("type")
    if kind == _TYPE_STRING:
        return param.get("value_string")
    if kind == _TYPE_INT:
        return param.get("value_int")
    if kind == _TYPE_DOUBLE:
        return param.get("value_float")
    if kind == _TYPE_BOOL:
        return param.get("value_bool")
    # Unknown type: return whichever value is set.
    for key in ("value_string", "value_bool", "value_int", "value_float"):
        if param.get(key) not in (None, "", 0, False):
            return param[key]
    return None


def parse_locations(building_map: dict) -> dict[str, Location]:
    """Extract named waypoints from every nav graph on every level."""
    locations: dict[str, Location] = {}
    for level in building_map.get("levels") or []:
        level_name = level.get("name", "")
        for graph in level.get("nav_graphs") or []:
            for vertex in graph.get("vertices") or []:
                name = (vertex.get("name") or "").strip()
                if not name:
                    continue
                params = {p.get("name"): _param_value(p) for p in vertex.get("params") or []}
                loc = locations.get(name)
                if loc is None:
                    loc = Location(
                        name=name,
                        level=level_name,
                        x=round(float(vertex.get("x", 0.0)), 2),
                        y=round(float(vertex.get("y", 0.0)), 2),
                    )
                    locations[name] = loc
                # The same waypoint can appear in several fleets' graphs; merge params.
                if params.get("pickup_dispenser"):
                    loc.pickup_handler = str(params["pickup_dispenser"])
                if params.get("dropoff_ingestor"):
                    loc.dropoff_handler = str(params["dropoff_ingestor"])
                loc.is_charger = loc.is_charger or bool(params.get("is_charger"))
                loc.is_parking_spot = loc.is_parking_spot or bool(params.get("is_parking_spot"))
    return locations


class LocationIndex:
    """Case-insensitive lookup with "did you mean" suggestions."""

    def __init__(self, locations: dict[str, Location]) -> None:
        self.locations = locations
        self._by_lower = {name.lower(): loc for name, loc in locations.items()}

    def __len__(self) -> int:
        return len(self.locations)

    def resolve(self, name: str) -> Location | None:
        return self._by_lower.get(name.strip().lower())

    def suggest(self, name: str, n: int = 3) -> list[str]:
        matches = difflib.get_close_matches(name.strip().lower(), list(self._by_lower), n=n, cutoff=0.5)
        return [self._by_lower[m].name for m in matches]

    def pickups(self) -> list[str]:
        return sorted(l.name for l in self.locations.values() if l.pickup_handler)

    def dropoffs(self) -> list[str]:
        return sorted(l.name for l in self.locations.values() if l.dropoff_handler)

    def chargers(self) -> list[str]:
        return sorted(l.name for l in self.locations.values() if l.is_charger)
