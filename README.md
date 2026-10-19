# SafeRoutes

A data-driven women's-safety system. District-level crime and socio-economic
data are clustered into safety tiers (K-Means + Gaussian Mixture), every
district gets a 0-100 comparative safety score, and a route planner picks the
safest of several road routes, draws a confidence corridor around it, and
raises an in-app alert if live GPS shows the user leaving that corridor for a
high-risk district.

Built with Django, scikit-learn, shapely and Leaflet.

## Pipeline

1. **Data ingestion** — `core/` loads Census 2011 and Kerala Police crime statistics from CSV (`load_real_data`).
2. **Pre-processing** — `clustering/ml/pipeline.py`: rates per 100k, literacy disparity, correlation-based feature selection. Nothing is imputed: an indicator with any missing district is dropped and the reason is recorded.
3. **Clustering** — K-Means and GMM are evaluated for k = 2..6 (elbow, silhouette, Calinski-Harabasz, Davies-Bouldin, BIC/AIC all stored); the tier scheme selects k from {3, 4}.
4. **Safety scoring** — crime rate, police presence, literacy, min-max normalised (against benchmark cities if loaded). Weights renormalise over whichever indicators exist.
5. **Routing and alerts** — `tracking/`: OSRM candidates → scored → safest chosen; 1.96σ buffer; live GPS checked against the path *segments*.

`/model/` shows the evidence behind step 3 (elbow, BIC, silhouette, correlation matrices, dropped features, run notes).

## Accounts

Registration is email + password, followed by a 6-digit code emailed to the
address. The account stays inactive until the code is entered.

- Codes are stored as keyed hashes, expire after 10 minutes, lock after 5 wrong guesses, and resends are throttled (60 s cooldown, 5 per hour).
- Login failures are rate-limited per email + IP, and errors never reveal whether an email is registered.
- Routes, live GPS trails and alerts belong to the user who created them. Another user's route returns 404; API calls without a session return 401.
- **Emergency contact (optional, off by default).** A user can name one person (Account menu → *Emergency contact*). That person is emailed the journey and last known position only if the user presses *Notify*, or — if they ticked the option — when a safety alert goes unanswered for a visible 60-second countdown that any answer cancels. The contact is emailed when added, the user must confirm they've told them, the address never reaches the browser, the server re-checks opt-in and an unanswered alert before any automatic send, and there is at most one email per route per 10 minutes. Authorities are never contacted.

## Setup (local)

Python 3.11+.

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export DJANGO_DEBUG=1                  # local development; see "Configuration" for production
python manage.py migrate
python manage.py load_real_data        # needs core/data/kerala_crime_against_women.csv (see "Data")
python manage.py train_clusters
python manage.py runserver
```

With `DJANGO_DEBUG=1` and no `EMAIL_HOST`, verification codes are printed to the
terminal running `runserver`.

Run the tests with `DJANGO_DEBUG=1 python manage.py test`.

## Data

**Bundled:** `core/data/kerala_districts.csv` — Census of India 2011 district
population and literacy for the 14 Kerala districts, with district-HQ
coordinates. Caveats:

- Literacy is literates ÷ *total* population (the source lacks the 0-6 population), so it runs a few points below the official 7+ rate. Relative order is preserved.
- Population is from 2011 while crime figures are recent, so per-100k rates are overstated for districts that have grown since. Replace with a projection if you have one.

**Not bundled — crime data.** `core/data/kerala_crime_against_women.csv` must be
created on a machine with open internet access:

```bash
python manage.py fetch_real_data --only crime
python manage.py load_real_data --years 3
```

This reads each Kerala Police unit's published "Crime against women" table
(`https://<unit>.keralapolice.gov.in/crime-statistics/crime-against-woman`, 20
units serving 14 districts). Safeguards in the parser and loader:

- A row whose cell count differs from the header is rejected rather than guessed.
- The published total is used; crime heads are summed and compared with it, and mismatches are recorded (`checksum_ok`).
- District-years where any police unit is missing are skipped (never understated), and years the source labels partial ("up to October") are excluded from averages.
- Rape / kidnapping splits are not published by every unit, so the model uses the **total** only.

Be aware that the units do not maintain their pages uniformly (some stop at
2022-2024, some skip a year, some totals don't equal the sum of their rows). The
loader will tell you which district-years it skipped; if no year is complete for
every district it stops with an error rather than inventing one. An alternative
source with consistent district coverage is the NCRB "Crime in India"
district-wise tables (data.gov.in / Mendeley); any CSV in the schema of
`kerala_crime_against_women.csv` loads unchanged.

**Optional** `core/data/benchmark_cities.csv` — reference cities. Rows must declare
`crime_metric = women_crime_per_100k`; rows measuring anything else are
rejected, because normalising against a different measure silently skews every
score. Without it, districts are scored relative to each other and the UI says so.

Indicators with no public district-level source here (police strength,
per-capita expenditure, forest cover, growth rate) are left empty, and the model
simply does not use them. Add `police_strength` to the districts CSV and it is
picked up automatically.

## Routing

`OSRM_BASE_URL` (default: the public demo server) supplies road geometry and
alternatives. If it is unreachable the planner falls back to a straight line
between district centres and the route page says so. Point it at your own OSRM
for anything beyond a demo.

Safety scores are at **district** resolution: each point along a route takes the
score of its nearest district centre. That is as fine as the data supports; it
cannot tell a safe street from an unsafe one inside a district.

## Configuration

| Variable | Purpose |
|---|---|
| `DJANGO_DEBUG` | `1` for local development only |
| `DJANGO_SECRET_KEY` | **Required** when debug is off |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts (default includes `*.up.railway.app`) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated `https://` origins |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | SMTP for OTP emails. **Without `EMAIL_HOST`, codes are not emailed** and nobody can finish registering |
| `SQLITE_PATH` | Database file; point at a persistent volume in production or it is lost on redeploy |
| `DJANGO_SSL_REDIRECT`, `DJANGO_HSTS_SECONDS` | Opt-in HTTPS hardening once the site is HTTPS-only |
| `OSRM_BASE_URL`, `OSRM_TIMEOUT_SECONDS` | Routing engine |

Deploy steps: set the variables above, then `python manage.py migrate`,
`collectstatic --noinput`, `load_real_data`, `train_clusters`, and serve with
`gunicorn saferoutes_project.wsgi`.

## Known limitations

- The login throttle uses Django's cache; with the default per-process cache and several gunicorn workers the limit applies per worker. Use a shared cache (Redis/database) for a strict limit.
- Behind a proxy, `REMOTE_ADDR` is the proxy's address, so the login throttle is effectively per-email.
- Live GPS is whatever the browser reports; the page must stay open to keep tracking.
- Escalation is by email only (no SMS or phone call) and works only while the page is open; the countdown runs in the browser. Contacting emergency services directly is not implemented, and the email tells the contact to call 112.
- MySQL was named in the original proposal; the project uses SQLite. Switch via `DATABASES` and `mysqlclient`.
