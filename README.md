# Yale talks calendars

Calendar feeds for talks at Yale's **Wu Tsai Institute** and **Department of Psychology**, so they show up in Apple Calendar, Google Calendar or Outlook and stay up to date.

**Subscribe here: https://himalaya-gir.github.io/yale-talks-calendars/**

| Calendar | Where it comes from |
| --- | --- |
| Wu Tsai Institute — [`wti.ics`](https://himalaya-gir.github.io/yale-talks-calendars/wti.ics) | Built here every 6 hours from [wti.yale.edu/events](https://wti.yale.edu/events), since the Institute publishes no calendar feed |
| Psychology — Department, Cognitive, Neuroscience, Social/Personality, Developmental, Clinical | The department's own public Google Calendars, as embedded on [psychology.yale.edu/events](https://psychology.yale.edu/events) |

Unofficial; not affiliated with Yale.

## How it works

[`scripts/build.py`](scripts/build.py) (Python standard library only) runs in GitHub Actions every 6 hours ([`update.yml`](.github/workflows/update.yml)):

- **Wu Tsai Institute**: reads the upcoming and recent past events, writes `public/wti.ics`, and keeps every event it has seen in [`data/wti-events.json`](data/wti-events.json). Past talks stay in the calendar; a future talk that disappears from the site is treated as cancelled and removed.
- **Psychology**: records which Google Calendars the events page embeds in [`data/psych-calendars.json`](data/psych-calendars.json). If one is added, removed, or goes quiet for a year, the workflow opens an issue so you can subscribe or unsubscribe.
- Builds `public/index.html` (the subscribe page) and deploys `public/` to GitHub Pages.

The repository only gets a commit when event data changes, plus a monthly heartbeat: GitHub pauses scheduled workflows in public repositories after 60 days without activity.

## When something breaks

- **GitHub emails you that a run failed**: most likely the WTI site layout changed. The last good feed stays online. The run log says which pages could not be read; adjust `parse_wti_event` or `listing_links` in `build.py`.
- **An issue "Psychology calendars changed" appears**: the department added or retired a calendar; subscribe from the page above.
- **Run it now**: Actions → *Update calendars* → *Run workflow*. Locally: `python3 scripts/build.py` (writes `public/`).

## Adding another source

Write an `update_…()` function next to `update_wti()` that returns events in the same shape (`url`, `summary`, `start`, `end`, `location`, `description`, `cancelled`), a feed writer like `write_wti_ics()`, and a card in `write_index()`. If the source already has a public iCal or Google Calendar feed, subscribe to it directly instead.
