"""Request validation. Unknown keys are rejected so typos never pass silently."""

from django.conf import settings
from rest_framework import serializers

MAX_LOCATION_LENGTH = 200
COORDINATE_ALIASES = {
    "lat": "lat",
    "latitude": "lat",
    "lon": "lon",
    "lng": "lon",
    "longitude": "lon",
}


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({key: ["Unknown field."] for key in unknown})
        return super().to_internal_value(data)


class LocationField(serializers.Field):
    """A place as text (city, ZIP, ``"lat,lon"``, street address) or a ``{lat, lon}`` object."""

    default_error_messages = {
        "blank": "This location must not be empty.",
        "too_long": f"Location text may be at most {MAX_LOCATION_LENGTH} characters.",
        "invalid_type": 'Use a text string or an object like {{"lat": 41.88, "lon": -87.63}}.',
        "bad_object": "A coordinate object needs numeric 'lat' and 'lon' and nothing else.",
        "out_of_range": "Latitude must be -90..90 and longitude -180..180.",
    }

    def to_internal_value(self, data):
        if isinstance(data, str):
            text = " ".join(data.split())
            if not text:
                self.fail("blank")
            if len(text) > MAX_LOCATION_LENGTH:
                self.fail("too_long")
            return text
        if isinstance(data, dict):
            coords = {}
            for key, value in data.items():
                name = COORDINATE_ALIASES.get(key)
                if name is None or name in coords or isinstance(value, bool):
                    self.fail("bad_object")
                try:
                    coords[name] = float(value)
                except (TypeError, ValueError):
                    self.fail("bad_object")
            if set(coords) != {"lat", "lon"}:
                self.fail("bad_object")
            if not (-90 <= coords["lat"] <= 90 and -180 <= coords["lon"] <= 180):
                self.fail("out_of_range")
            return coords
        self.fail("invalid_type")

    def to_representation(self, value):
        return value


def _planner_setting(name):
    return settings.FUEL_PLANNER[name]


class OptionsSerializer(StrictSerializer):
    max_detour_miles = serializers.FloatField(required=False)
    initial_fuel_fraction = serializers.FloatField(required=False, min_value=0.0, max_value=1.0)
    geometry = serializers.ChoiceField(
        choices=["simplified", "full", "none"], required=False, default="simplified"
    )

    def validate_max_detour_miles(self, value):
        low, high = (
            _planner_setting("MIN_ALLOWED_DETOUR_MILES"),
            _planner_setting("MAX_ALLOWED_DETOUR_MILES"),
        )
        if not low <= value <= high:
            raise serializers.ValidationError(f"Must be between {low:g} and {high:g} miles.")
        return value


class FuelRouteRequestSerializer(StrictSerializer):
    start = LocationField()
    finish = LocationField()
    options = OptionsSerializer(required=False)

    def validated_options(self) -> dict:
        """Options with defaults filled in."""
        raw = self.validated_data.get("options", {})
        return {
            "max_detour_miles": raw.get(
                "max_detour_miles", _planner_setting("DEFAULT_MAX_DETOUR_MILES")
            ),
            "initial_fuel_fraction": raw.get("initial_fuel_fraction", 1.0),
            "geometry": raw.get("geometry", "simplified"),
        }
