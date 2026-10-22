"""Database models."""

from django.db import models


class Station(models.Model):
    """A fuel stop with its best (lowest) listed price and approximate location.

    Coordinates are *not* in the source data: they are derived from the station's
    ``City, State`` via the offline gazetteer, so they are the centroid of the town
    (``location_precision == "city"``), typically within a few miles of the station.
    """

    PRECISION_CITY = "city"
    PRECISION_EXACT = "exact"
    PRECISION_CHOICES = [
        (PRECISION_CITY, "City centroid (approximate)"),
        (PRECISION_EXACT, "Exact"),
    ]

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    # 8 decimal places: the supplied prices carry up to 8 (e.g. 3.00733333), and costs are
    # computed from the price exactly as supplied.
    price = models.DecimalField(max_digits=12, decimal_places=8, help_text="USD per gallon")
    latitude = models.FloatField()
    longitude = models.FloatField()
    location_precision = models.CharField(
        max_length=10, choices=PRECISION_CHOICES, default=PRECISION_CITY
    )
    location_source = models.CharField(max_length=20, help_text="Gazetteer table that matched")
    source_rows = models.PositiveSmallIntegerField(default=1, help_text="CSV rows merged")

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
