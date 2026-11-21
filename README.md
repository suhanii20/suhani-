# RISD Workday class-seat watcher 🔔

Checks full RISD classes in Workday every few minutes and **pushes a notification
to your phone the moment a seat opens**, so you can jump in and register.

How it works: a real Chrome window (run by Playwright) keeps you logged in to
Workday, opens each class section page you saved, reads "Section Status" /
"Enrolled/Capacity", and sends an alert through the free **ntfy** app.

> It runs on **your computer** because Workday needs your RISD login + Duo.
> Leave the computer awake while it's watching.

---

## 1. Set up phone notifications (2 min)

1. Install **ntfy** on your phone ([iPhone](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Tap **+** and subscribe to a topic with a long random name, e.g.
   `suhani-risd-classes-8f3k29xq` (anyone who knows the name can see the alerts, so make it unguessable).

## 2. Install (once)

Needs Python 3.11+. In Terminal, in this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp config.example.toml config.toml
```

Open `config.toml` and set `ntfy_topic` to the same name you used in the app. Then:

```bash
python watcher.py test-notify      # your phone should buzz
```

## 3. Log in to Workday (once, and again whenever it expires)

```bash
python watcher.py login
```

A browser opens → sign in with your RISD account + Duo (choose "trust this browser" if
offered) → when you see Workday home, press **Enter** in Terminal. If the address in
the browser isn't `wd5.myworkday.com/risd/...`, put the right one in `home_url`.

## 4. Add the classes you want

In that browser (or your normal one), open Workday → find the course → open the
**section** you want (e.g. Section 02) → copy the URL from the address bar into
`config.toml`:

```toml
[[course]]
name = "ILLUS 2000 Section 02"
url = "https://wd5.myworkday.com/risd/d/inst/....htmld"
```

Add one `[[course]]` block per section.

## 5. Run it

```bash
python watcher.py check    # one pass, to make sure each class reads correctly
python watcher.py watch    # keep checking every 10 min (Ctrl+C to stop)
```

Example output:

```
ILLUS 2000 Section 02: status=Closed, 15/15 enrolled -> full
```

When a class flips from full to open you get an **urgent** push: tap it to go
straight to the section page. You get one alert per opening (not one every 10 min).
If your login expires you get a single "please log in again" push.

**Mac tip:** stop the laptop sleeping while it watches:
`caffeinate -i python watcher.py watch`

## Troubleshooting

- **"UNKNOWN" / "no seat info found"**: RISD's page may label seats differently.
  Run `python watcher.py dump "<section url>"`, look at `debug/dump.txt` and
  `debug/dump.png`, and add a custom `seats_pattern` or
  `enrolled_capacity_pattern` to that course (examples in `config.example.toml`).
- **Want to watch it work?** Set `headless = false` in `config.toml`.
- **Be reasonable with the interval**: 5–15 minutes is plenty; hammering
  Workday could get your session flagged.

Files kept private by `.gitignore`: `config.toml`, `state.json`, `browser-profile/`
(your login cookies, never share this), `debug/`.
