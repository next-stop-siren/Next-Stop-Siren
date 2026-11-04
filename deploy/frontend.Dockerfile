# Build with the repository root as context; .dockerignore is an allowlist.
FROM node:24.21.0-bookworm-slim AS build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN test "$(node --version)" = v24.21.0 && test "$(npm --version)" = 11.19.0 && npm ci
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

FROM nginx:1.28-alpine AS runtime
RUN rm -f /etc/nginx/conf.d/default.conf
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY deploy/nginx/acme.conf /etc/nginx/acme.conf
COPY --from=build /frontend/dist /usr/share/nginx/html
EXPOSE 80 443
STOPSIGNAL SIGQUIT
CMD ["nginx", "-g", "daemon off;"]
