# Synthetic demo data

`synthetic_stations.csv` is a **synthetic** dataset used for the public demo deployment. Station
names ("DEMO FUEL STOP #n") are fictional, prices are invented by a deterministic formula (see
`scripts/generate_demo_stations.py`), and the stations sit in public-domain place centroids along
a few example routes. **The prices are not real and are unrelated to any real fuel data.**

Load it with `python manage.py load_demo_data`. The private assessment CSV is a different file,
is not in this repository, and is never read by that command.
