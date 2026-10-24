# Procrastinot — Academic Mission Control 2.0

**Track 3: Best Use of Google Gemini** · One local web application · Python + FastAPI + Playwright + Google Gemini

Procrastinot is an academic emergency copilot. You connect your own Canvas account, select current courses, and Procrastinot gathers accessible assignments, syllabi, module pages and PDFs. Gemini then reads the *course documents*, including syllabus tables, to identify external homework (e.g., Pearson), written assignments, midterms, due-date conflicts, and AI-use policies. Clicking any task opens a rescue studio with high-yield notes and study blocks that start **from the current time**, not a manually entered number of hours.

## Start on a Mac (exact steps)

**1. Stop the old Streamlit Procrastinot first.** In its VS Code terminal, press **Control + C**. Close the old Procrastinot Chromium window. This project replaces the previous Streamlit versions; don't mix the old and new source files.

**2. Extract this ZIP, open the new `Procrastinot_Reimagined` folder in VS Code, and open Terminal → New Terminal.** The terminal must be inside that new folder. You should see `server.py`, `requirements.txt` and `setup_mac.sh` on the left.

**3. Run the following setup command exactly once:**

```bash
bash setup_mac.sh
```

This creates its own `.venv`, installs the Python packages, and installs the browser automation engine. Python 3.11+ recommended. Requires an internet connection. Setup may take a few minutes.

**4. Start Procrastinot:**

```bash
bash run_mac.sh
```

Open **http://127.0.0.1:8501** in your regular browser. Leave the terminal running. If port 8501 is occupied, you probably still have the old Streamlit server running: press Control + C in its terminal.

**5. Connect Canvas.** Click **Connect to UCSD Canvas**. The project opens a separate Chromium window with a dedicated profile for login; complete UCSD/Duo sign-in there. Keep that Chromium window open until Procrastinot lists your courses. If course discovery doesn't advance automatically, click **I'm signed in** inside Procrastinot.

**6. Select your current courses → Import selected courses.** A new Chromium window may reopen using the saved login; you should not usually need to authenticate again. Import status appears on the website. Canvas course selection works best when the school provides term metadata (e.g. Fall 2026 / FA26). You can show other courses too.

**7. Configure Gemini.** Open **Settings → Google Gemini**, get a key from [Google AI Studio](https://aistudio.google.com/apikey), paste it into the key box, and click Save. If you did this *before* importing courses, the syllabus scan starts automatically after sync; otherwise click **Scan syllabi** once. Gemini will send relevant course material to Google's API for analysis. You can also set `GEMINI_API_KEY` in a local `.env` file if preferred.

**8. Use Today, Exam Radar, Study Studio, and Sources.** The source-backed dashboard should now include syllabus-only dates in addition to Canvas assignments. Choose an assignment or exam and press **Generate rescue pack**. The student can choose morning, evening or balanced study time. There is no hour slider.

To stop the app, press **Control + C** in the new terminal. No need to reinstall packages between runs.

## What this actually does

| Feature | Implementation |
| --- | --- |
| Canvas browser sign-in | Playwright persistent local Chromium profile; *no Canvas API token* |
| Course discovery | Current-term classification, with date/FA26 filtering and fallback |
| Automatic document collection | Permitted Canvas API requests through session cookies; syllabus HTML, module pages, PDFs, assignments, submission state, exam calendar events |
| Hidden deadline discovery | **Gemini 2.5 Flash** analyzes course documents, including select PDFs natively to interpret layouts/tables; structured JSON extraction |
| External Pearson and written HW | Extract due dates and evidence *from syllabus*, not direct Pearson login/API |
| Deadline ledger | Source-backed merge, clear verification status, duplicate reconciliation, conflicts flagged, explicit ambiguity warnings |
| Exam Radar | Dedicated exam list from Canvas and syllabus, each linking to study studio |
| AI study studio | Gemini high-yield topic analysis, quick notes, active-recall questions, Cut List, practical schedule |
| AI policy | `unknown` unless a directly matching quote exists in an imported course document; no assumption that AI is allowed |
| Completion tracking | Canvas submission state when available, otherwise optional student checkmark; never treats no Canvas record as definitive unfinished |
| Public course website | Optional `https://cse29.site/` import using bounded same-domain crawl that respects robots.txt |
| Demo mode | Clearly labeled fictional illustration, never mixed with live Canvas data |

## Architecture

```text
Local web browser (localhost:8501)
        |
        v
FastAPI server (server.py) ---------------------> static/index.html + CSS + vanilla JS
        |
        +--> canvas_worker.py (Playwright-managed Chromium)
        |        |--> student's UCSD login via institution SSO/Duo
        |        |--> accessible Canvas assignments + submission metadata
        |        |--> syllabus body, modules, module pages, accessible PDFs
        |
        +--> course_website.py (bounded, public-only imports)
        |
        +--> intelligence.py (Google Gemini)
        |        |--> syllabus table/exam/Pearson/AI policy extraction
        |        |--> assignment/exam rescue packs, note & schedule generation
        |
        +--> engine.py (deterministic source reconciliation + scheduling)
        |
        +--> .procrastinot_local/ (local JSON + PDF cache + isolated browser profile)
```

All Canvas network access is constrained by the student's own authorized browser session. Course documents sent to Gemini go to Google's API under the user's Gemini account and applicable terms. The local app is not a hosted multi-user service. Don't publish `.procrastinot_local`, browser profiles, downloaded course materials, the `.env` file, or API keys.

## Why some deadlines were missing in the earlier version

Canvas's Assignments API often does **not** list due dates hosted in Pearson or written homework calendars described in a syllabus PDF. The previous implementation treated that partial list as complete and incorrectly displayed "0 urgent." This version deliberately maintains a **coverage warning** until syllabus materials have actually been analyzed. If a syllabus says "every Monday," Procrastinot can show the next weekday occurrence but flags it for verification, because a recurrence does not establish an exact due time or holiday exception.

Evidence strategy:
1. Keep source URLs, filenames, and short quotes for syllabus-derived dates.
2. Treat Canvas due timestamps as authoritative *for that Canvas assignment*, not for all class obligations.
3. Preserve externally sourced tasks separately unless title and date genuinely match.
4. Flag contradictions, missing years, and missing times for review.
5. Never treat Gemini as proof of submission or permission to use AI in graded work.

## Important limitations (be transparent with judges)

- **Institution-dependent Canvas access:** The signed-in browser method has worked in your previous course/PDF tests, but this version cannot be tested against your private UCSD session from outside your Mac. Canvas can deny some resources or change its pages.
- **No direct Pearson/Gradescope integration:** These portals are not automatically authenticated. Procrastinot reads their deadlines only when the syllabus or Canvas provides them, or when the student supplies the date.
- **Syllabus extraction is imperfect:** Gemini may miss a table row, misunderstand a relative date, or fail to provide a quote that exactly matches extracted text. Procrastinot marks ambiguous items; verify the original document. It cannot guarantee every date has been found.
- **No automatic always-on background sync:** Sync is triggered inside the running local app. When the app is closed, it doesn't keep working on a student's computer.
- **Study windows are suggestions, not known free time:** Because there is no connected personal calendar, the app doesn't know jobs, classes or sleep schedules. The user chooses a broad study preference and adjusts their own plan.
- **No live Gemini tests included:** Automated tests mock Gemini's structured response; the actual API requires a valid key and network access on your Mac. This project uses the official `google-genai` SDK and a configured Gemini model.
- **App is local-only:** Don't deploy this exact Playwright profile flow to Streamlit Cloud or a publicly shared host; production multi-user rollout needs approved institution OAuth or a supported integration.

## Testing

From the project folder (after installing dependencies):

```bash
.venv/bin/python -m pytest -q
node tests/ui_smoke.cjs  # optional if Node.js installed
```

The Python suite tests source reconciliation, date/time ambiguity, weekly recurrence, course-term filtering, anti-CSRF origin rules, AI-policy evidence verification and server health. `tests/ui_smoke.cjs` verifies the static page view generation with fake data. These are not substitutes for testing a real Canvas import and Gemini response on your Mac.

## Official references

- [Canvas REST API](https://canvas.instructure.com/doc/api/)
- [Canvas Courses API](https://canvas.instructure.com/doc/api/courses.html)
- [Playwright persistent browser contexts](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [Google Gemini document understanding](https://ai.google.dev/gemini-api/docs/document-processing)
- [Google Gen AI Python SDK](https://googleapis.github.io/python-genai/)

## Suggested hackathon demonstration

1. Show the welcome screen: **one Procrastinot app** rather than a separate Chrome extension.
2. Click Connect Canvas; use the authorized browser session; select a real course.
3. In **Sources**, show the imported syllabus and other PDFs with original URLs.
4. In **Today / Exam Radar**, point to a Pearson, written homework or midterm date extracted from the syllabus, with a source quote. Contrast it with an actual Canvas assignment.
5. Open **Study Studio** and let Gemini produce course-specific notes, active recall questions, a realistic timed plan, and an academically responsible Cut List.
6. Explain why Gemini is the central reasoning engine, not just a decorative chatbot.

**Track:** Best Use of Google Gemini (Track 3). Track eligibility/winning is determined by the event organizers, not by this README.
