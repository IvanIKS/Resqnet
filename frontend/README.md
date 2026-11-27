# ResQNet Frontend

Vite + React stub for the ResQNet disaster-response UI.

## Setup

```bash
npm install
npm run dev     # starts dev server on http://localhost:5173
```

`/api/*` requests are proxied to the FastAPI backend at `http://localhost:8000`
(see `vite.config.js`).

## Scripts

- `npm run dev` — start the dev server
- `npm run build` — production build into `dist/`
- `npm run preview` — preview the production build
