# Fuel Route Planner (Django)

API that returns a driving route between two US locations plus cost-optimal fuel stops
(500-mile range, 10 mpg). Work in progress: this README is completed in the final stage.

## Quick start (Windows, PowerShell)

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env      # then edit DJANGO_SECRET_KEY
python manage.py check
pytest
```

## Dataset

The assessment CSV is **not** included in this repository (redistribution permission is not
established). Place your copy at `data/fuel-prices-for-be-assessment.csv`
(or set `FUEL_CSV_PATH` in `.env`).
