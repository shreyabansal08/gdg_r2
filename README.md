# URL Shortener

A small Flask app that shortens urls, redirects visitors, and tracks clicks. Data is stored in SQLite.

## Setup

```bash
git clone https://github.com/shreyabansal08/gdg_r2
cd <folder>

python -m venv venv
source venv/bin/activate        # windows cmd: venv\Scripts\activate
                                # git bash:     source venv/Scripts/activate

pip install -r requirements.txt
```

## Run

```bash
export API_KEY="pickyourkey"     # windows cmd: set API_KEY=...
python app.py
```

The server starts at http://127.0.0.1:5000 and creates `shortener.db` on first run.
If `API_KEY` is not set, a random key is printed in the terminal at startup.

Optional settings: `DB_PATH` (database file) and `BASE_URL` (domain used in returned short urls).

## API

| method | path | notes |
|--------|------|-------|
| POST | /shorten | body: `{"url": "...", "alias": "optional", "ttl_seconds": 3600}` |
| GET | /<code> | redirects to the original url (410 if expired) |
| GET | /stats/<code> | clicks, created_at, original url |
| DELETE | /<code> | needs `X-API-Key` header |

## Example

```bash
curl -X POST http://127.0.0.1:5000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'

curl -L http://127.0.0.1:5000/<code>
curl http://127.0.0.1:5000/stats/<code>
curl -X DELETE http://127.0.0.1:5000/<code> -H "X-API-Key: pick-a-long-secret"
```
