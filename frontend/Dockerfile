# syntax=docker/dockerfile:1
FROM node:22-slim

ENV NODE_ENV=development
WORKDIR /app

# Manifests first, as their own layer: only rebuilt when dependencies change,
# not on every source edit. Same reasoning as the backend's uv layer.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173

# The Vite dev server, not a production build behind nginx. Deliberate: this
# stack is a development stack -- the backend runs uvicorn --reload for the same
# reason -- and HMR is worth more here than a minified bundle. A production
# image would be a second stage running `npm run build` and serving `dist/`.
CMD ["npm", "run", "dev"]
