# CGRecruit web app

The React web app recruiters and candidates use: the board, inbox, calendar, call queue, intelligence pages and settings for recruiters, and the apply, screening-chat, status and reschedule pages for candidates.

Built with Create React App and [CRACO](https://craco.js.org/), Tailwind CSS and Radix UI components. In production it is built once and served by the backend from `backend/static`, on the same address as the API.

## Run it locally

You need the backend running on `http://localhost:8000` first ([docs/SETUP.md](../docs/SETUP.md#5-a-safe-local-run)).

```
cd frontend
cp .env.example .env.development.local    # REACT_APP_BACKEND_URL=http://localhost:8000
npm ci                                    # installs exactly what package-lock.json lists
npm start
```

It opens `http://localhost:3000`. Sign in with your local owner account.

If `REACT_APP_BACKEND_URL` is empty during `npm start`, API calls go through `src/setupProxy.js` instead. Make sure that file points at your own local or test server, never at a live app.

## Build it

The deploy builds the web app for you. To build by hand:

```
cd frontend
REACT_APP_BACKEND_URL= npm run build        # empty on purpose: the app calls its own address
```

The result is in `frontend/build`; the backend serves whatever is in `backend/static`. `REACT_APP_BACKEND_URL` is baked in at build time and must be **set and empty** for a live build. If it is missing altogether the app calls `undefined/api`, and with a `localhost` value it calls your computer: either way the live app shows a blank page.

## Where things are

| Path | What |
| --- | --- |
| `src/App.js` | Routes: recruiter pages behind the login, candidate pages (`/apply`, `/retry`, `/applicant`, `/reschedule`, `/refer`) open |
| `src/pages/` | One file per page |
| `src/components/` | The board, candidate drawer, dialogs; `settings/` holds each Settings section |
| `src/lib/api.js` | The API client (cookie sign-in, `withCredentials`) |
| `public/` | `index.html`, `manifest.json`, icons, logo, service worker |
| `src/index.css`, `tailwind.config.js` | Colours and theme |

Branding (name, icons, logo, colours): [docs/BRANDING.md](../docs/BRANDING.md).
