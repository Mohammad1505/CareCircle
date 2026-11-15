# CareCircle

A shared medication and appointment manager for elderly people, their caretakers and their families. The elder stays at the centre: they confirm doses with one tap and decide who can see and edit their plan.

> CareCircle is a reminder tool. It is not a substitute for a doctor's advice.

## The problem
Elderly people often manage several medicines and appointments, while family members cannot tell whether doses were taken. Basic reminder apps notify only the elder and do nothing when a dose is missed.

## What it does
- **Passcode access:** every person (elder, caretaker, family) has their own 6-digit passcode. No passwords or sign-up forms.
- **Medicines:** add, edit and delete schedules, see today's doses and mark them as taken with one large button.
- **Appointments:** add doctor visits and receive an email reminder within 24 hours of the visit.
- **Email reminders:** upcoming dose (10 minutes before), missed dose, and appointments.
- **Elder-first control:** the elder switches each person's "view + alerts" and "can edit" access on or off at any time, and can reset or remove anyone.
- **Escalation instead of spam:** reminder to the elder first, then the caretaker if the dose is still not taken after 30 minutes, then family after 60 minutes.
- **Natural-language entry:** type "Metformin 500mg twice daily after meals" and the schedule is created using the Gemini API (a built-in fallback parser is used if no key is set).
- **Adherence tracking:** recent dose history and a weekly summary in English or Hindi, which can be emailed to caretakers and family.
- **Accessibility:** large-text, high-contrast mode, skip link, labelled controls, visible focus and screen-reader friendly status messages.

## Architecture
Browser (Flask templates) -> Flask routes -> SQLite. A background APScheduler job runs every minute. It creates the day's dose records and moves each unconfirmed dose through reminder -> caretaker alert -> family alert. Emails are sent through SMTP, and every notification is also recorded in an in-app log, so the flow can be demonstrated without email credentials. The app runs on Google Cloud Run and uses the Gemini API.

## Tech stack
Python, Flask, SQLite, APScheduler, SMTP, Google Gemini API, Docker, Google Cloud Run

## Security
- Passcodes are stored as keyed HMAC-SHA256 hashes, never in plain text. They are shown once on creation and can be reset by the elder.
- CSRF token on every form, session cookies are HttpOnly, SameSite and Secure (on Cloud Run).
- Login rate limiting (5 failed attempts per 10 minutes per client).
- Strict Content-Security-Policy, X-Frame-Options, nosniff and referrer headers.
- Role checks on every action, input length limits, parameterised SQL queries, auto-escaped templates.
- Secrets are read from environment variables. The container runs as a non-root user.

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env        # SMTP and Gemini keys are optional
python app.py               # open http://localhost:5000
```
To see the escalation quickly, set `ESCALATE_MIN=1`, add a medicine due in a couple of minutes and press "Check reminders now".

## Tests
```bash
pip install -r requirements-dev.txt
python -m pytest -q tests
```
The suite covers passcode hashing and login, CSRF, rate limiting, role-based access, medicine and appointment validation, the natural-language parser, and the full reminder -> caretaker -> family escalation.

## Deploy to Google Cloud Run
```bash
gcloud run deploy carecircle --source . --region asia-south1 --allow-unauthenticated \
  --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --set-env-vars "SECRET_KEY=<long-random-string>,GEMINI_API_KEY=<key>,SMTP_HOST=smtp.gmail.com,SMTP_USER=<gmail>,SMTP_PASS=<app-password>"
```
One always-on instance is used on purpose: the reminder scheduler runs inside the app, and SQLite lives on that instance's disk.

## Limitations
- SQLite data lives on the running instance, so it resets when the instance is replaced. A production version would use Firestore or Cloud SQL.
- A lost elder passcode cannot be recovered in this prototype.
- No medical advice, interaction checks or dose recommendations are provided.

## Future scope
Firestore storage, SMS/WhatsApp alerts, voice input, refill reminders, multi-elder dashboards for caretakers.

## Author
[Bohri Mohammad], [RCPIT], 3rd Year
